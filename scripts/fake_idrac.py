#!/usr/bin/env python3
"""A tiny stand-in for an iDRAC's Redfish API, for scripts/e2e_devices.py (invented data, GET only)."""

import base64
import json
from http.server import BaseHTTPRequestHandler, HTTPServer

AUTH = "Basic " + base64.b64encode(b"monitor:idrac-e2e-pw").decode()
PAGES = {
    "/redfish/v1/Chassis/System.Embedded.1/Power": {
        "PowerControl": [{"PowerConsumedWatts": 112}],
        "PowerSupplies": [{"Status": {"Health": "OK"}}, {"Status": {"Health": "OK"}}],
    },
    "/redfish/v1/Chassis/System.Embedded.1/Thermal": {
        "Temperatures": [
            {"Name": "System Board Inlet Temp", "ReadingCelsius": 21, "Status": {"Health": "OK"}},
            {"Name": "CPU1 Temp", "ReadingCelsius": 44, "Status": {"Health": "OK"}},
        ],
        "Fans": [{"Name": "Fan 1", "Reading": 4000, "ReadingUnits": "RPM", "Status": {"Health": "OK"}}],
    },
    "/redfish/v1/Managers/iDRAC.Embedded.1/LogServices/Sel/Entries": {"Members@odata.count": 9},
}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.headers.get("authorization") != AUTH:
            return self.reply(401, {"error": "unauthorized"})
        page = PAGES.get(self.path.split("?")[0])
        self.reply(200, page) if page is not None else self.reply(404, {"error": "not found"})

    def reply(self, status, payload):
        raw = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, fmt, *args):
        print(f"{self.command} {self.path} auth_ok={self.headers.get('authorization') == AUTH}", flush=True)


HTTPServer(("0.0.0.0", 8082), Handler).serve_forever()
