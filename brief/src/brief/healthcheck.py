"""Docker HEALTHCHECK: exit 0 if the web server answers /healthz with status ok."""

import json
import os
import sys
import urllib.request


def check(port: str) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=3) as resp:
            return resp.status == 200 and json.load(resp).get("status") == "ok"
    except Exception:
        return False


if __name__ == "__main__":
    sys.exit(0 if check(os.environ.get("HTTP_PORT", "80")) else 1)
