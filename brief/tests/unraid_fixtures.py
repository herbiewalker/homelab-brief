"""Synthetic Unraid API responses and smartctl reports for the Task 19 tests (no real names or serials).

The shapes follow what the Unraid GraphQL API returns for each section the collector asks for; the
values are invented. `FakeApi` stands in for brief.collectors.unraid_api.UnraidApi.
"""

import json

from brief.collectors import unraid
from brief.collectors.unraid_api import ApiError

DAY = 86400


def parity_history(latest_speed_bps: int, latest_date: str, earlier: int = 61_000_000):
    """The latest check plus five earlier monthly ones at a normal speed."""
    rows = [
        {
            "date": latest_date,
            "duration": 150_000,
            "speed": str(latest_speed_bps),
            "status": "COMPLETED",
            "errors": 0,
        }
    ]
    for month in range(9, 4, -1):
        rows.append(
            {"date": f"2026-0{month}-01T08:30:00.000Z", "duration": 16_300, "speed": str(earlier),
             "status": "COMPLETED", "errors": 0}
        )  # fmt: skip
    return rows


def healthy_server(serial_prefix="A", parity=None, notices=None, temps=None):
    temps = temps or {}
    disks = [
        ("sda", "Test HDD 1TB", f"{serial_prefix}0001"),
        ("sdb", "Test HDD 1TB", f"{serial_prefix}0002"),
        ("sdc", "Test HDD 1TB", f"{serial_prefix}0003"),
        ("sdd", "Test SSD 500GB", f"{serial_prefix}0004"),
        ("sde", "Flash Drive FIT", f"{serial_prefix}0005"),
    ]
    return {
        "info": {"info": {"versions": {"core": {"unraid": "7.3.2", "api": "4.37.5", "kernel": "6.18.38"}}}},
        "array": {
            "array": {
                "state": "STARTED",
                "parities": [
                    {"name": "parity", "device": "sda", "status": "DISK_OK", "temp": 33, "numErrors": 0}
                ],
                "disks": [
                    {"name": "disk1", "device": "sdb", "status": "DISK_OK", "temp": 34, "numErrors": 2},
                    {"name": "disk2", "device": "sdc", "status": "DISK_OK", "temp": None, "numErrors": 0},
                ],
                "caches": [
                    {"name": "cache", "device": "sdd", "status": "DISK_OK", "temp": 30, "numErrors": 0}
                ],
            }
        },
        "disks": {
            "disks": [
                {
                    "id": f"srv:{serial}",
                    "device": f"/dev/{dev}",
                    "name": model,
                    "serialNum": serial,
                    "size": 1_000_204_886_016,
                    "temperature": temps.get(dev),
                    "smartStatus": "OK",
                }
                for dev, model, serial in disks
            ]
        },
        "parity": {
            "parityHistory": parity
            if parity is not None
            else parity_history(60_000_000, "2026-10-01T08:30:00.000Z")
        },
        "docker": {
            "docker": {
                "containers": [
                    {"id": "c1", "names": ["/web"], "image": "example/web:1", "state": "RUNNING"},
                    {"id": "c2", "names": ["/db"], "image": "example/db:2", "state": "RUNNING"},
                    {"id": "c3", "names": ["/old"], "image": "example/old:3", "state": "EXITED"},
                ]
            }
        },
        "vms": ApiError("graphql", "Failed to retrieve VM domains: VMs are not available"),
        "notices": {
            "notifications": {
                "list": notices
                if notices is not None
                else [
                    {
                        "id": "n1",
                        "subject": "Docker auto update",
                        "description": "x updated",
                        "importance": "INFO",
                        "timestamp": "2026-10-09T05:00:00.000Z",
                    },
                    {
                        "id": "n2",
                        "subject": "Array has 1 disk with read errors",
                        "description": "disk1 has errors",
                        "importance": "WARNING",
                        "timestamp": "2026-10-09T06:00:00.000Z",
                    },
                ]
            }
        },
        "plugins": {"plugins": [{"name": "compose.manager", "version": "2026.10.01"}]},
    }


class FakeApi:
    """Answers `section(selection)` from a dict of section name -> data (or an ApiError to raise)."""

    instances: list = []

    def __init__(self, sections: dict | ApiError, dropped: dict | None = None):
        self.sections, self.dropped, self.asked = sections, dropped or {}, []
        FakeApi.instances.append(self)

    def section(self, selection):
        name = next(n for n, s in unraid.SECTIONS.items() if next(iter(s)) == next(iter(selection)))
        self.asked.append(name)
        if isinstance(self.sections, ApiError):
            raise self.sections
        reply = self.sections[name]
        if isinstance(reply, ApiError):
            raise reply
        return reply, self.dropped.get(name, [])


def factory(servers: dict):
    """An API_FACTORY: url -> FakeApi (or a raised ApiError / a bad key)."""

    def make(url, key, timeout=10, verify_tls=True):
        if key == "rejected":
            return FakeApi(ApiError("auth", "the API key was rejected"))
        return FakeApi(servers[url])

    return make


def smartctl_report(
    serial, model="Test HDD 1TB", passed=True, temp=34, pending=0, realloc=0, crc=0, nvme=False
):
    if nvme:
        return json.dumps(
            {
                "model_name": model,
                "serial_number": serial,
                "device": {"protocol": "NVMe"},
                "smart_status": {"passed": passed},
                "nvme_smart_health_information_log": {
                    "critical_warning": 0, "temperature": temp, "percentage_used": 3, "media_errors": pending,
                },
            }
        )  # fmt: skip
    table = [
        {"id": 5, "name": "Reallocated_Sector_Ct", "value": 100, "worst": 100, "raw": {"value": realloc}},
        {"id": 9, "name": "Power_On_Hours", "value": 90, "worst": 90, "raw": {"value": 9000}},
        {"id": 197, "name": "Current_Pending_Sector", "value": 100, "worst": 100, "raw": {"value": pending}},
        {"id": 199, "name": "UDMA_CRC_Error_Count", "value": 200, "worst": 200, "raw": {"value": crc}},
    ]
    return json.dumps(
        {
            "model_name": model,
            "serial_number": serial,
            "rotation_rate": 7200,
            "device": {"protocol": "ATA"},
            "smart_status": {"passed": passed},
            "temperature": {"current": temp},
            "ata_smart_attributes": {"table": table},
        }
    )
