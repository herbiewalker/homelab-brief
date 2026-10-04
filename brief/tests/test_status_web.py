"""GET /status.json on the read-only HTTP app (Task 7)."""

from test_web import env  # noqa: F401  (the fixture)

from brief import health


def test_status_json_is_served_without_login_and_never_cached(env):  # noqa: F811
    r = env.http().get("/status.json")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-cache"
    doc = r.json()
    assert doc["schema"] == 1 and doc["run_ok"] is True and "headline" in doc
    assert set(doc["pipeline"]) >= {"events_24h", "new_signatures_24h", "hosts_silent"}


def test_status_json_says_run_ok_false_when_the_receiver_is_down(env, monkeypatch):  # noqa: F811
    monkeypatch.setattr(health, "receiver_ok", lambda port: False)
    doc = env.http().get("/status.json").json()
    assert doc["run_ok"] is False and doc["headline"].startswith("No data")


def test_status_json_is_also_on_the_https_app(env):  # noqa: F811
    assert env.https().get("/status.json").status_code == 200
