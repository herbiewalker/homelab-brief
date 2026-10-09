#!/usr/bin/env python3
"""A tiny stand-in for the UniFi Network Integration API, for scripts/e2e_collector.py (invented data)."""

import json
import re
from http.server import BaseHTTPRequestHandler, HTTPServer

KEY = "e2e-unifi-key"
PREFIX = "/proxy/network/integration"


def device(n, model, name, firmware, updatable=False, uplink=None):
    return {
        "id": f"dev-{n}",
        "macAddress": f"00:00:5E:00:53:{n:02X}",
        "ipAddress": "198.51.100.40",
        "name": name,
        "model": model,
        "state": "ONLINE",
        "firmwareVersion": firmware,
        "firmwareUpdatable": updatable,
        "features": [],
        "uplink": {"deviceId": f"dev-{uplink}"} if uplink else {},
    }


DEVICES = [
    device(1, "UCG-Max", "Gateway", "5.0.1"),
    device(2, "USW-Pro-Max-24-PoE", "Switch", "7.5.15", updatable=True, uplink=1),
    device(3, "U7-Pro-Max", "Access point", "8.7.11", uplink=2),
]


def page(items):
    return {"offset": 0, "limit": 200, "count": len(items), "totalCount": len(items), "data": items}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.headers.get("x-api-key") != KEY:
            return self.reply(401, {"message": "Unauthorized"})
        path = self.path.split("?")[0].removeprefix(PREFIX)
        if path == "/v1/info":
            return self.reply(200, {"applicationVersion": "9.4.1"})
        if path == "/v1/sites":
            return self.reply(
                200, page([{"id": "site-1", "name": "Default", "internalReference": "default"}])
            )
        if path == "/v1/sites/site-1/clients":
            return self.reply(
                200,
                page(
                    [
                        {
                            "id": "cl-1",
                            "macAddress": "00:00:5E:00:53:41",
                            "name": "laptop-host",
                            "type": "WIRELESS",
                        },
                        {
                            "id": "cl-2",
                            "macAddress": "00:00:5E:00:53:42",
                            "name": "console-host",
                            "type": "WIRELESS",
                        },
                    ]
                ),
            )
        if path == "/v1/sites/site-1/devices":
            return self.reply(200, page([{k: v for k, v in d.items() if k != "uplink"} for d in DEVICES]))
        match = re.fullmatch(r"/v1/sites/site-1/devices/(dev-\d+)", path)
        if match:
            row = next((d for d in DEVICES if d["id"] == match.group(1)), None)
            return self.reply(200, row) if row else self.reply(404, {"message": "not found"})
        self.reply(404, {"message": "not found"})

    def reply(self, status, payload):
        raw = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, fmt, *args):
        print(
            f"{self.command} {self.path.split('?')[0]} key_ok={self.headers.get('x-api-key') == KEY}",
            flush=True,
        )


HTTPServer(("0.0.0.0", 8081), Handler).serve_forever()
