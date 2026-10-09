#!/usr/bin/env python3
"""A tiny OpenAI-compatible server for scripts/e2e_ai.py (invented answers; checks the bearer key)."""

import json
from http.server import BaseHTTPRequestHandler, HTTPServer

KEY = "e2e-ai-key"
MODELS = ["e2e-model", "e2e-other"]


class Handler(BaseHTTPRequestHandler):
    def authorised(self):
        if self.headers.get("authorization") == f"Bearer {KEY}":
            return True
        self.reply(401, {"error": "bad key"})
        return False

    def do_GET(self):
        if not self.authorised():
            return
        if self.path.rstrip("/") == "/v1/models":
            return self.reply(200, {"object": "list", "data": [{"id": m, "object": "model"} for m in MODELS]})
        self.reply(404, {"error": "not found"})

    def do_POST(self):
        if not self.authorised():
            return
        body = json.loads(self.rfile.read(int(self.headers.get("content-length", 0))))
        if self.path.rstrip("/") != "/v1/chat/completions":
            return self.reply(404, {"error": "not found"})
        message = {"role": "assistant", "content": "ok"}
        if body.get("tools"):
            message["tool_calls"] = [
                {"id": "c1", "type": "function", "function": {"name": "get_time", "arguments": "{}"}}
            ]
        self.reply(
            200, {"choices": [{"message": message}], "usage": {"prompt_tokens": 12, "completion_tokens": 2}}
        )

    def reply(self, status, payload):
        raw = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, fmt, *args):
        print(
            f"{self.command} {self.path} auth_ok={self.headers.get('authorization') == f'Bearer {KEY}'}",
            flush=True,
        )


HTTPServer(("0.0.0.0", 8083), Handler).serve_forever()
