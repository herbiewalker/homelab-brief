#!/usr/bin/env python3
"""A tiny stand-in for an Unraid GraphQL API, for scripts/e2e_collector.py (invented data, queries only)."""

import json
import re
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

KEY = "e2e-test-key"
DISK = {"device": "/dev/sd{}", "name": "Test HDD 1TB", "size": 1000000000000, "smartStatus": "OK"}
DATA = {
    "info": {"info": {"versions": {"core": {"unraid": "7.3.2", "api": "4.37.5", "kernel": "6.18.0"}}}},
    "array": {
        "array": {
            "state": "STARTED",
            "parities": [
                {"name": "parity", "device": "sda", "status": "DISK_OK", "temp": 31, "numErrors": 0}
            ],
            "disks": [{"name": "disk1", "device": "sdb", "status": "DISK_OK", "temp": 33, "numErrors": 0}],
            "caches": [],
        }
    },
    "disks": {
        "disks": [
            {**DISK, "id": "x:S1", "device": "/dev/sda", "serialNum": "S1", "temperature": 31},
            {**DISK, "id": "x:S2", "device": "/dev/sdb", "serialNum": "S2", "temperature": 33},
        ]
    },
    "parityHistory": {
        "parityHistory": [
            {
                "date": "2026-10-01T08:30:00.000Z",
                "duration": 16300,
                "speed": "61000000",
                "status": "COMPLETED",
                "errors": 0,
            }
        ]
    },
    "docker": {
        "docker": {
            "containers": [{"id": "c1", "names": ["/web"], "image": "example/web:1", "state": "RUNNING"}]
        }
    },
    "notifications": {"notifications": {"list": []}},
    "plugins": {"plugins": [{"name": "example.plugin", "version": "1.0"}]},
}


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("content-length", 0))))
        query = body.get("query", "")
        if self.headers.get("x-api-key") != KEY:
            return self.reply(401, {"errors": [{"message": "Unauthorized"}]})
        if not query.lstrip().startswith("{") or "mutation" in query:
            return self.reply(400, {"errors": [{"message": "only queries are accepted here"}]})
        root = re.match(r"\{\s*(\w+)", query).group(1)
        if root == "vms":  # a server without VMs answers with an error, like the real API
            message = "Failed to retrieve VM domains: VMs are not available"
            return self.reply(200, {"data": None, "errors": [{"message": message}]})
        if root == "disks" and "smartStatus" in query and "--no-smart" in sys.argv:
            return self.reply(
                400, {"errors": [{"message": 'Cannot query field "smartStatus" on type "Disk".'}]}
            )
        self.reply(200, {"data": DATA[root]})

    def reply(self, status, payload):
        raw = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, fmt, *args):
        print(f"{self.command} {self.path} key_ok={self.headers.get('x-api-key') == KEY}", flush=True)


HTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
