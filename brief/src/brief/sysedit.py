"""The systems.yaml form editor: turn the file into form rows and the rows back into a file.

The rows are checked by the same rules as the file (Systems.from_mapping through the config editor), so a
form cannot write anything the pipeline would refuse. Saving rewrites the file from the form, so comments
written by hand are not kept (the previous text stays in the config history); keys the form does not know
about are carried over for each system, so nothing else is lost.
"""

import re
from collections.abc import Mapping

import yaml

from brief.config import ConfigError

KNOWN = ("id", "label", "kind", "hosts", "source_tz", "expected")
EXTRA_BLANK_ROWS = 3
HEADER = (
    "# systems.yaml, written by the admin GUI's systems editor. A system is one dashboard tile and one\n"
    "# status sensor. Comments you add by hand are kept until the next save from the form (the previous\n"
    "# text is always in the config history).\n"
)
SPLIT = re.compile(r"[,\s]+")


def split_hosts(text: str) -> list[str]:
    """Hosts typed as one list: separated by commas, spaces, or new lines; no duplicates, order kept."""
    seen, out = set(), []
    for host in SPLIT.split(text.strip()):
        if host and host.lower() not in seen:
            seen.add(host.lower())
            out.append(host)
    return out


def rows_from_text(text: str) -> tuple[list[dict], str]:
    """(rows, default_system) from the file's text; an unreadable file gives no rows."""
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError:
        return [], "other"
    if not isinstance(data, dict):
        return [], "other"
    rows = []
    for item in data.get("systems") or []:
        if not isinstance(item, dict):
            continue
        hosts = item.get("hosts") or []
        rows.append(
            {
                "id": str(item.get("id") or ""),
                "label": str(item.get("label") or ""),
                "kind": str(item.get("kind") or ""),
                "hosts": ", ".join(str(h) for h in hosts) if isinstance(hosts, list) else str(hosts),
                "source_tz": str(item.get("source_tz") or "UTC"),
                "expected": bool(item.get("expected", True)),
                "extra": {k: v for k, v in item.items() if k not in KNOWN},
            }
        )
    return rows, str(data.get("default_system") or "other")


def blank_row(host: str = "") -> dict:
    return {
        "id": "",
        "label": "",
        "kind": "",
        "hosts": host,
        "source_tz": "UTC",
        "expected": True,
        "extra": {},
    }


def rows_from_form(form: Mapping, existing: list[dict]) -> list[dict]:
    """The rows as submitted. Row i keeps the unknown keys of the system that had that id before."""
    count = int(form.get("count", "0") or 0)
    by_id = {r["id"]: r for r in existing}
    rows = []
    for i in range(min(max(count, 0), 200)):
        sid = str(form.get(f"id-{i}", "")).strip()
        row = {
            "id": sid,
            "label": str(form.get(f"label-{i}", "")).strip(),
            "kind": str(form.get(f"kind-{i}", "")).strip(),
            "hosts": str(form.get(f"hosts-{i}", "")).strip(),
            "source_tz": str(form.get(f"source_tz-{i}", "")).strip() or "UTC",
            "expected": form.get(f"expected-{i}") == "on",
            "remove": form.get(f"remove-{i}") == "on",
            "extra": by_id.get(sid, {}).get("extra", {}),
        }
        if not any(row[k] for k in ("id", "label", "kind", "hosts")):
            continue  # an untouched blank row
        rows.append(row)
    return rows


def build_text(rows: list[dict], default_system: str) -> str:
    """The systems.yaml text for these rows (rows marked `remove` are left out)."""
    systems = []
    for row in rows:
        if row.get("remove"):
            continue
        if not row["id"]:
            raise ConfigError("Every system needs an id (lowercase letters, digits, - or _).")
        item: dict = {"id": row["id"], "label": row["label"] or row["id"]}
        if row["kind"]:
            item["kind"] = row["kind"]
        item["hosts"] = split_hosts(row["hosts"])
        item["source_tz"] = row["source_tz"] or "UTC"
        item["expected"] = bool(row["expected"])
        item.update(row.get("extra") or {})
        systems.append(item)
    body = {"schema": 1, "default_system": default_system or "other", "systems": systems}
    return HEADER + yaml.safe_dump(body, sort_keys=False, allow_unicode=True, default_flow_style=False)
