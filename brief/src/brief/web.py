import time

from fastapi import FastAPI

from brief import __version__

_STARTED = time.monotonic()

app = FastAPI(title="homelab-brief", docs_url=None, redoc_url=None, openapi_url=None)


@app.get("/healthz")
def healthz() -> dict:
    return {
        "status": "ok",
        "version": __version__,
        "uptime_s": int(time.monotonic() - _STARTED),
    }
