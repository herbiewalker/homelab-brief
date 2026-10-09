"""Read `smartctl --json -a` output, the fallback when a server's API does not expose SMART attributes.

The Unraid API reports a disk's overall SMART status and temperature, but not the attribute table
(reallocated, pending, CRC errors). A User Script on each server (deploy/unraid-smart-export.sh) writes
one smartctl JSON file per disk into a share; this module turns such a file into a `SmartReading`.

Attribute names are kept as `<id>:<name>` ("197:Current_Pending_Sector"), the form playbook.yaml rules
use. NVMe drives have no attribute table; their health log is turned into `nvme:<field>` entries.
"""

import json
from dataclasses import dataclass, field

# Attributes worth keeping a history of (everything else in the table is noise for a home array).
WATCHED = {
    5: "Reallocated_Sector_Ct",
    10: "Spin_Retry_Count",
    187: "Reported_Uncorrect",
    196: "Reallocated_Event_Count",
    197: "Current_Pending_Sector",
    198: "Offline_Uncorrectable",
    199: "UDMA_CRC_Error_Count",
}
NVME_WATCHED = ("media_errors", "critical_warning", "percentage_used")
MAX_FILE_BYTES = 2_000_000


class SmartError(ValueError):
    pass


@dataclass
class SmartReading:
    serial: str
    model: str
    passed: bool | None  # the drive's own overall verdict; None when it does not say
    temperature: int | None
    rotational: bool | None
    attrs: dict[str, dict] = field(
        default_factory=dict
    )  # "197:Current_Pending_Sector" -> {value, worst, raw}


def parse(text: str) -> SmartReading:
    if len(text) > MAX_FILE_BYTES:
        raise SmartError("the file is too large to be smartctl output")
    try:
        data = json.loads(text)
    except ValueError:
        raise SmartError("not valid JSON") from None
    if not isinstance(data, dict):
        raise SmartError("not a smartctl report")
    serial = str(data.get("serial_number") or "").strip()
    if not serial:
        raise SmartError("no serial number in the report")
    status = (data.get("smart_status") or {}).get("passed")
    temperature = (data.get("temperature") or {}).get("current")
    reading = SmartReading(
        serial=serial,
        model=str(data.get("model_name") or data.get("model_family") or "unknown").strip(),
        passed=status if isinstance(status, bool) else None,
        temperature=int(temperature) if isinstance(temperature, (int, float)) else None,
        rotational=_rotational(data),
    )
    for row in (data.get("ata_smart_attributes") or {}).get("table") or []:
        attr_id = row.get("id")
        if attr_id in WATCHED:
            raw = (row.get("raw") or {}).get("value")
            if isinstance(raw, (int, float)):
                reading.attrs[f"{attr_id}:{WATCHED[attr_id]}"] = {
                    "value": row.get("value"),
                    "worst": row.get("worst"),
                    "raw": int(raw),
                }
    log = data.get("nvme_smart_health_information_log") or {}
    for name in NVME_WATCHED:
        if isinstance(log.get(name), (int, float)):
            reading.attrs[f"nvme:{name}"] = {"value": None, "worst": None, "raw": int(log[name])}
    if reading.temperature is None and isinstance(log.get("temperature"), (int, float)):
        reading.temperature = int(log["temperature"])
    return reading


def _rotational(data: dict) -> bool | None:
    if (data.get("device") or {}).get("protocol") == "NVMe":
        return False
    rate = data.get("rotation_rate")
    if isinstance(rate, (int, float)):
        return rate > 0
    return None
