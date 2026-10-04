"""Docker HEALTHCHECK: exit 0 if the web server answers /healthz with status ok.

/healthz is 503 when the database, the worker, or the syslog receiver is unhealthy, so a crashed
Vector or worker shows as unhealthy instead of the container looking fine.
"""

import json
import sys
import urllib.error
import urllib.request

from brief.settings import Settings


def check(port: int | str) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=3) as resp:
            return resp.status == 200 and json.load(resp).get("status") == "ok"
    except (urllib.error.URLError, OSError, ValueError):
        return False


if __name__ == "__main__":
    sys.exit(0 if check(Settings.from_env().http_port) else 1)
