"""What may reach a cloud model, and what a model may not invent. Used by the AI provider layer (ai.py).

Two independent checks sit on every cloud call, so a mistake in one input builder cannot leak:

  1. each task builds its cloud input from system-level facts only (never host names, addresses, log text,
     device names, or client data), and a task whose subject is a single device (a new device, a spike)
     has no cloud input at all;
  2. whatever was built is scanned just before sending (`privacy_leak`): any IP address, any MAC address, and
     any name the product knows privately (a sender, a device label or owner from devices.yaml, a client
     hostname) blocks the call. The scan reports a short label of what it found, never the value.

On the way back, `known_names` lists everything the output must not mention unless the input did (hosts,
labels, MAC addresses, finding ids), so a model that makes up or mixes up a name is caught.
"""

import re
from collections.abc import Iterable, Mapping

from brief.devices import DeviceBook
from brief.store import Store

# Findings about one household device. They exist only for the LAN: no cloud input is ever built from them.
DEVICE_KINDS = ("new_device", "traffic_spike", "odd_hours")
IPV4 = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.]*\d)")
MAC = re.compile(r"\b[0-9a-f]{2}(?::[0-9a-f]{2}){5}\b", re.IGNORECASE)
MIN_TERM = 3


def private_terms(store: Store, env: Mapping[str, str] | None = None) -> frozenset[str]:
    """Lowercase names the product knows privately: senders, device labels, owners, MACs, client hostnames."""
    terms: set[str] = set()
    book = DeviceBook.load(env)
    for device in book.devices.values():
        terms.update(t.lower() for t in (device.label, device.owner, device.mac) if t)
    try:
        with store.read() as db:
            terms.update(str(r["host"]).lower() for r in db.execute("SELECT host FROM host"))
            terms.update(
                str(r["hostname"]).lower()
                for r in db.execute("SELECT hostname FROM client WHERE hostname IS NOT NULL")
            )
    except Exception:  # an unreadable table must not turn the check off: the regexes below still run
        pass
    return frozenset(t for t in terms if len(t) >= MIN_TERM)


def privacy_leak(text: str, terms: Iterable[str] = ()) -> str | None:
    """A short label of the first private thing in `text` ("an IP address", "a MAC address", "a device or host
    name"), or None. Never returns the value itself."""
    if IPV4.search(text):
        return "an IP address"
    if MAC.search(text):
        return "a MAC address"
    low = text.lower()
    for term in terms:
        if re.search(rf"(?<![\w-]){re.escape(term)}(?![\w-])", low):
            return "a device or host name"
    return None


def known_names(
    store: Store, env: Mapping[str, str] | None = None, finding_ids: Iterable[str] = ()
) -> set[str]:
    """Names a reply may mention only if the input did: senders, device labels, MACs, and finding ids."""
    names = {n for n in private_terms(store, env)}
    names.update(i.lower() for i in finding_ids if len(i) >= MIN_TERM)
    return names
