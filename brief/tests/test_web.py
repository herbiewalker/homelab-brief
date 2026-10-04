from fastapi.testclient import TestClient

from brief import __version__
from brief.web import app

client = TestClient(app)


def test_healthz_reports_ok_and_version():
    resp = client.get("/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["version"] == __version__
    assert body["uptime_s"] >= 0


def test_api_docs_are_not_exposed():
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404
