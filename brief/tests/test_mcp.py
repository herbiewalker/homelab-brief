"""Task 26: the read-only MCP server: tokens, scopes, privacy, audit, HTTP and stdio, and the AI page's tokens."""

import io
import json
import re
from datetime import UTC, datetime, timedelta

import pytest
from test_admin import lab  # noqa: F401  (the fixture: an HTTPS app with a signed-in admin)
from test_cli_tools import box  # noqa: F401  (the fixture: a state dir, config dir, and store)
from test_interfaces import SYSTEMS_YAML

from brief import cli, mcpserver, mcptokens
from brief.store import iso

H = {"content-type": "application/json"}


def cfg_dir(env):
    return env.settings.state_dir.parent / "cfg" / "brief"


def turn_on(env, on=True):
    (cfg_dir(env) / "systems.yaml").write_text(SYSTEMS_YAML, encoding="utf-8")
    text = f"schema: 1\nenabled: {str(on).lower()}\nfeatures:\n  mcp_server: {str(on).lower()}\n"
    (cfg_dir(env) / "ai.yaml").write_text(text)


def token(env, scopes=("status", "findings", "history", "systems", "map"), locality="lan", name="client"):
    return mcptokens.create(env.store, name, list(scopes), locality)[1]


def rpc(client, secret, method, params=None, msg_id=1, **kw):
    body = {"jsonrpc": "2.0", "method": method}
    if msg_id is not None:
        body["id"] = msg_id
    if params is not None:
        body["params"] = params
    headers = {**H, "authorization": f"Bearer {secret}"} if secret else H
    return client.post("/mcp", content=json.dumps(body), headers={**headers, **kw.get("headers", {})})


def call(client, secret, tool, args=None):
    r = rpc(client, secret, "tools/call", {"name": tool, "arguments": args or {}})
    assert r.status_code == 200, r.text
    return r.json()["result"]


def audit(env, like="mcp.%"):
    with env.store.read() as db:
        return [
            dict(r)
            for r in db.execute("SELECT user, action, detail FROM audit_log WHERE action LIKE ?", (like,))
        ]


# ---------------------------------------------------------------- tokens


def test_a_token_is_shown_once_stored_hashed_and_works_until_revoked(lab):  # noqa: F811
    token_id, secret = mcptokens.create(lab.store, "ha", ["status"], "lan", by="admin:x")
    with lab.store.read() as db:
        row = dict(db.execute("SELECT * FROM mcp_token").fetchone())
    assert secret not in json.dumps(row) and row["hash"] != secret and len(row["hash"]) == 64
    assert mcptokens.verify(lab.store, secret).name == "ha"
    assert (
        mcptokens.verify(lab.store, secret + "x") is None and mcptokens.verify(lab.store, "hbm_nope") is None
    )
    assert mcptokens.verify(lab.store, "") is None
    assert mcptokens.listing(lab.store)[0]["last_used"]
    mcptokens.revoke(lab.store, token_id)
    assert mcptokens.verify(lab.store, secret) is None
    assert mcptokens.listing(lab.store) == [] and len(mcptokens.listing(lab.store, include_revoked=True)) == 1


@pytest.mark.parametrize(
    "name,scopes,locality,why",
    [
        ("", ["status"], "lan", "name"),
        ("bad/name", ["status"], "lan", "name"),
        ("a", [], "lan", "at least one"),
        ("a", ["nope"], "lan", "unknown scope"),
        ("a", ["status"], "mars", "locality"),
        ("a", ["logs"], "cloud", "cloud client cannot"),
        ("a", ["devices"], "cloud", "cloud client cannot"),
        ("a", ["dns"], "cloud", "cloud client cannot"),
    ],
)
def test_bad_tokens_are_refused(lab, name, scopes, locality, why):  # noqa: F811
    with pytest.raises(mcptokens.TokenError, match=why):
        mcptokens.create(lab.store, name, scopes, locality)


def test_names_are_unique_while_active_and_reusable_after_revoking(lab):  # noqa: F811
    token(lab, name="one")
    with pytest.raises(mcptokens.TokenError, match="already an active"):
        token(lab, name="one")
    mcptokens.revoke(lab.store, "one")
    token(lab, name="one")


def test_last_used_is_not_written_on_every_call(lab):  # noqa: F811
    secret = token(lab)
    now = datetime.now(UTC)
    mcptokens.verify(lab.store, secret, now)
    first = mcptokens.listing(lab.store)[0]["last_used"]
    mcptokens.verify(lab.store, secret, now + timedelta(seconds=10))
    assert mcptokens.listing(lab.store)[0]["last_used"] == first
    mcptokens.verify(lab.store, secret, now + timedelta(minutes=5))
    assert mcptokens.listing(lab.store)[0]["last_used"] != first


# ---------------------------------------------------------------- the HTTP endpoint


def test_the_endpoint_is_closed_until_the_switches_are_on(lab):  # noqa: F811
    secret = token(lab)
    assert rpc(lab.https(), secret, "ping").status_code == 404
    turn_on(lab)
    assert rpc(lab.https(), secret, "ping").status_code == 200
    turn_on(lab, False)
    assert rpc(lab.https(), secret, "ping").status_code == 404


def test_no_token_or_a_wrong_one_is_refused_and_repeated_failures_are_throttled(lab):  # noqa: F811
    turn_on(lab)
    client = lab.https()
    assert rpc(client, None, "ping").status_code == 401
    r = rpc(client, "hbm_aaaa_bbbb", "ping")
    assert r.status_code == 401 and "Bearer" in r.headers["www-authenticate"]
    for _ in range(12):
        rpc(client, "hbm_aaaa_bbbb", "ping")
    assert rpc(client, "hbm_aaaa_bbbb", "ping").status_code == 429
    assert rpc(client, token(lab), "ping").status_code == 429  # even a good token waits out the block


def test_only_post_is_accepted_and_the_origin_must_match(lab):  # noqa: F811
    turn_on(lab)
    client = lab.https()
    assert client.get("/mcp").status_code == 405 and client.get("/mcp").headers["allow"] == "POST"
    secret = token(lab)
    assert rpc(client, secret, "ping", headers={"origin": "https://evil.example"}).status_code == 403
    assert rpc(client, secret, "ping", headers={"origin": "https://testserver"}).status_code == 200


def test_the_endpoint_does_not_exist_on_the_plain_http_app(lab):  # noqa: F811
    turn_on(lab)
    assert lab.http().post("/mcp", content="{}", headers=H).status_code in (404, 405)


def test_protocol_basics(lab):  # noqa: F811
    turn_on(lab)
    client, secret = lab.https(), token(lab)
    init = rpc(client, secret, "initialize", {"protocolVersion": "2025-03-26"}).json()["result"]
    assert init["protocolVersion"] == "2025-03-26" and init["serverInfo"]["name"] == "homelab-brief"
    assert (
        rpc(client, secret, "initialize", {"protocolVersion": "1999-01-01"}).json()["result"][
            "protocolVersion"
        ]
        == mcpserver.PROTOCOLS[0]
    )
    assert rpc(client, secret, "notifications/initialized", msg_id=None).status_code == 202
    assert rpc(client, secret, "no/such").json()["error"]["code"] == mcpserver.METHOD_NOT_FOUND
    bad = client.post("/mcp", content="{nope", headers={**H, "authorization": f"Bearer {secret}"})
    assert bad.json()["error"]["code"] == mcpserver.PARSE_ERROR
    batch = client.post("/mcp", content="[]", headers={**H, "authorization": f"Bearer {secret}"})
    assert batch.json()["error"]["code"] == mcpserver.INVALID_REQUEST
    big = client.post("/mcp", content=" " * (300 * 1024), headers={**H, "authorization": f"Bearer {secret}"})
    assert big.status_code == 413


# ---------------------------------------------------------------- scopes


def test_tools_listed_are_only_those_the_scope_allows(lab):  # noqa: F811
    turn_on(lab)
    client = lab.https()
    names = lambda s: {t["name"] for t in rpc(client, s, "tools/list").json()["result"]["tools"]}  # noqa: E731
    assert names(token(lab, name="default")) == {
        "get_status", "list_findings", "explain_finding", "get_history", "list_systems", "get_map",
        "get_finding", "get_signature", "get_inventory", "run_replay",
    }  # fmt: skip
    assert "search_logs" in names(token(lab, ("status", "logs"), name="logs"))
    assert names(token(lab, ("systems",), name="small")) == {"list_systems", "get_inventory"}
    tool = rpc(client, token(lab, ("status",), name="one"), "tools/list").json()["result"]["tools"][0]
    assert tool["annotations"]["readOnlyHint"] is True and tool["inputSchema"]["type"] == "object"


def test_a_tool_outside_the_scope_is_denied_and_audited(lab):  # noqa: F811
    turn_on(lab)
    secret = token(lab)
    result = call(lab.https(), secret, "search_logs", {"text": "x"})
    assert result["isError"] and "scope" in result["content"][0]["text"]
    assert audit(lab) == [{"user": "mcp:client", "action": "mcp.call", "detail": "search_logs denied"}]


def test_default_scope_never_reaches_device_findings_status_or_digest(lab):  # noqa: F811
    status = {
        "run_ok": True,
        "stories": [
            {"id": "a", "kind": "deviation", "title": "gateway busy"},
            {"id": "b", "kind": "new_device", "title": "a new device joined"},
            {"id": "c", "kind": "odd_hours", "title": "late activity"},
        ],
        "developing": [{"id": "d", "kind": "traffic_spike", "title": "spike"}],
        "systems": [],
    }
    from brief.systems import Systems

    def ctx(*scopes):
        client = mcptokens.Client("t", "t", frozenset(scopes))
        return mcpserver.Context(lab.store, lab.settings, client, lambda: (status, Systems(), []))

    plain = json.loads(
        mcpserver.call_tool(ctx("findings", "status"), "list_findings", {})["content"][0]["text"]
    )
    assert [s["id"] for s in plain["stories"]] == ["a"] and plain["developing"] == []
    full = json.loads(
        mcpserver.call_tool(ctx("findings", "devices"), "list_findings", {})["content"][0]["text"]
    )
    assert [s["id"] for s in full["stories"]] == ["a", "b", "c"]
    got = json.loads(mcpserver.call_tool(ctx("status"), "get_status", {})["content"][0]["text"])
    assert [s["id"] for s in got["stories"]] == ["a"]
    media, text = mcpserver.read_resource(ctx("findings"), mcpserver.DIGEST_URI)
    assert text == mcpserver.WITHHELD
    assert mcpserver.read_resource(ctx("findings", "devices"), mcpserver.DIGEST_URI)[1] != mcpserver.WITHHELD


def test_explaining_a_device_finding_needs_the_devices_scope_and_hides_that_it_exists(lab, monkeypatch):  # noqa: F811
    from brief.detectors import Finding

    found = Finding(id="dev-1", kind="new_device", title="new thing", system=None, host=None, detail={})
    monkeypatch.setattr("brief.aitasks.find_finding", lambda *a, **k: found)
    from brief.systems import Systems

    def run(*scopes):
        client = mcptokens.Client("t", "t", frozenset(scopes))
        ctx = mcpserver.Context(lab.store, lab.settings, client, lambda: ({}, Systems(), []))
        return mcpserver.call_tool(ctx, "explain_finding", {"finding_id": "dev-1"})

    denied = run("findings")
    assert denied["isError"] and denied["content"][0]["text"] == "there is no such finding"
    ok = run("findings", "devices")
    assert not ok["isError"] and "new thing" in ok["content"][0]["text"]


def test_findings_systems_map_and_history_over_http(lab):  # noqa: F811
    turn_on(lab)
    (cfg_dir(lab) / "map.yaml").write_text("schema: 1\nenabled: true\n")
    client, secret = lab.https(), token(lab)
    found = json.loads(call(client, secret, "list_findings", {"limit": 2})["content"][0]["text"])
    assert found["run_ok"] and len(found["stories"]) == 2
    systems = json.loads(call(client, secret, "list_systems")["content"][0]["text"])
    assert {s["id"] for s in systems["systems"]} == {"gw", "nas", "cam"} and "hosts" not in systems[
        "systems"
    ][0]
    assert "nodes" in json.loads(call(client, secret, "get_map")["content"][0]["text"])
    sig = seed_signature(lab)
    hist = json.loads(
        call(client, secret, "get_history", {"kind": "signature", "id": sig, "days": 31})["content"][0][
            "text"
        ]
    )
    assert "template" not in hist and hist["id"] == sig  # a template is generalised log text: logs scope only
    with lab.store.write() as tx:
        tx.db.execute(
            "INSERT INTO metric (ts, metric_id, value) VALUES (?, 'disk.temp', 31.5)",
            (iso(datetime.now(UTC)),),
        )
    metric = json.loads(
        call(client, secret, "get_history", {"kind": "metric", "id": "disk.temp"})["content"][0]["text"]
    )
    assert metric["daily"][0]["avg"] == 31.5


@pytest.mark.parametrize(
    "tool,args",
    [
        ("list_findings", {"limit": 0}),
        ("list_findings", {"limit": "5"}),
        ("list_findings", {"nope": 1}),
        ("get_history", {"kind": "other", "id": 1}),
        ("get_history", {"kind": "signature", "id": -5}),
        ("get_history", {"kind": "signature", "id": 10**9}),
        ("get_history", {"kind": "metric", "id": "../etc"}),
        ("get_history", {"kind": "metric", "id": "nothing.here"}),
        ("explain_finding", {}),
        ("explain_finding", {"finding_id": "no-such"}),
    ],
)
def test_bad_arguments_are_a_tool_error_not_a_crash(lab, tool, args):  # noqa: F811
    turn_on(lab)
    result = call(lab.https(), token(lab), tool, args)
    assert result["isError"] is True and "Traceback" not in result["content"][0]["text"]


def test_an_unknown_tool_is_a_protocol_error(lab):  # noqa: F811
    turn_on(lab)
    r = rpc(lab.https(), token(lab), "tools/call", {"name": "delete_everything", "arguments": {}})
    assert r.json()["error"]["code"] == mcpserver.INVALID_PARAMS


def test_no_tool_changes_anything():
    assert all(t.listing()["annotations"]["readOnlyHint"] for t in mcpserver.TOOLS.values())
    assert not any(re.search(r"set|delete|write|ack|snooze|ignore|restart", n) for n in mcpserver.TOOLS)


# ---------------------------------------------------------------- raw logs


def test_search_logs_needs_its_scope_and_is_bounded(lab):  # noqa: F811
    turn_on(lab)
    day = datetime.now(UTC).date().isoformat()
    folder = lab.settings.archive_dir / day
    folder.mkdir(parents=True)
    (folder / "198.51.100.9.log").write_text("\n".join(f"line {i} needle" for i in range(300)) + "\n")
    client = lab.https()
    secret = token(lab, ("logs",), name="logs")
    got = json.loads(
        call(client, secret, "search_logs", {"text": "needle", "limit": 5})["content"][0]["text"]
    )
    assert len(got["lines"]) == 5 and got["lines"][0]["host"] == "198.51.100.9"
    assert call(client, secret, "search_logs", {"text": "needle", "days": 9})["isError"]
    assert call(client, secret, "search_logs", {"text": ""})["isError"]
    assert call(client, secret, "search_logs", {"text": "x", "host": "../../etc"})["isError"]
    sig = seed_signature(lab)
    assert "template" in json.loads(
        call(client, token(lab, ("logs", "history"), name="l2"), "get_history",
             {"kind": "signature", "id": sig})["content"][0]["text"]
    )  # fmt: skip


# ---------------------------------------------------------------- cloud clients


def test_a_cloud_client_gets_nothing_that_names_an_address_or_a_private_name(lab):  # noqa: F811
    turn_on(lab)
    client = lab.https()
    secret = token(lab, ("status", "findings", "systems"), "cloud", name="cloudy")
    status = call(client, secret, "get_status")
    findings = call(client, secret, "list_findings")
    for result in (status, findings):  # the golden day's findings name senders by address
        assert result["isError"] and result["content"][0]["text"].startswith("Withheld from a cloud client")
        assert "198.51.100" not in result["content"][0]["text"]
    assert ("withheld" in d["detail"] for d in audit(lab))
    write_digest(lab)
    read = rpc(client, secret, "resources/read", {"uri": mcpserver.DIGEST_URI}).json()
    assert read["error"]["message"].startswith("withheld")


# ---------------------------------------------------------------- audit, resources


def test_every_call_is_audited_without_arguments_or_results(lab):  # noqa: F811
    turn_on(lab)
    client, secret = lab.https(), token(lab, name="audited")
    call(client, secret, "list_findings", {"limit": 3})
    call(client, secret, "get_history", {"kind": "metric", "id": "secret.metric"})
    rpc(client, secret, "resources/read", {"uri": mcpserver.SYSTEMS_URI})
    rpc(client, secret, "tools/list")  # listing is not a data access
    rows = audit(lab)
    assert [r["detail"] for r in rows] == [
        "list_findings ok",
        "get_history error",
        f"{mcpserver.SYSTEMS_URI} ok",
    ]
    assert {r["user"] for r in rows} == {"mcp:audited"}
    assert "secret.metric" not in json.dumps(rows)


def test_resources_list_and_read(lab):  # noqa: F811
    turn_on(lab)
    client = lab.https()
    secret = token(lab, ("systems",), name="r1")
    uris = [r["uri"] for r in rpc(client, secret, "resources/list").json()["result"]["resources"]]
    assert uris == [mcpserver.SYSTEMS_URI]
    body = rpc(client, secret, "resources/read", {"uri": mcpserver.SYSTEMS_URI}).json()["result"]["contents"][
        0
    ]
    assert json.loads(body["text"])["systems"]
    assert (
        "scope"
        in rpc(client, secret, "resources/read", {"uri": mcpserver.DIGEST_URI}).json()["error"]["message"]
    )
    assert (
        rpc(client, secret, "resources/read", {"uri": "file:///etc/passwd"}).json()["error"]["code"] == -32602
    )
    write_digest(lab)
    full = token(lab, ("findings", "devices", "systems"), name="r2")
    digest = rpc(client, full, "resources/read", {"uri": mcpserver.DIGEST_URI}).json()["result"]["contents"][
        0
    ]
    assert digest["text"].startswith("# Homelab brief")


def test_the_weekly_resource_is_trimmed_to_system_tables_without_the_devices_scope(lab):  # noqa: F811
    turn_on(lab)
    weekly = lab.settings.out_dir / "weekly"
    weekly.mkdir(parents=True)
    (weekly / "2026-W40.md").write_text(
        "# Week\n\n## Volume by system\n| a | 1 |\n\n## New devices\n- a phone\n"
    )
    client = lab.https()
    plain = rpc(client, token(lab, ("findings",), name="w1"), "resources/read", {"uri": mcpserver.WEEKLY_URI})
    text = plain.json()["result"]["contents"][0]["text"]
    assert "Volume by system" in text and "phone" not in text
    full = rpc(
        client,
        token(lab, ("findings", "devices"), name="w2"),
        "resources/read",
        {"uri": mcpserver.WEEKLY_URI},
    )
    assert "phone" in full.json()["result"]["contents"][0]["text"]


# ---------------------------------------------------------------- the AI page


def test_the_ai_page_makes_and_revokes_tokens(lab):  # noqa: F811
    page = lab.client.get("/admin/ai")
    assert "MCP clients" in page.text
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
    made = lab.client.post(
        "/admin/ai/mcp/create",
        data={
            "csrf_token": csrf,
            "name": "home-assistant",
            "locality": "lan",
            "scope": ["status", "findings"],
        },
    )
    assert made.status_code == 200 and "cannot be shown again" in made.text
    secret = re.search(r"hbm_[0-9a-f]{8}_[\w-]+", made.text).group(0)
    assert mcptokens.verify(lab.store, secret).scopes == {"status", "findings"}
    listing = lab.client.get("/admin/ai").text
    assert "home-assistant" in listing and secret not in listing
    assert audit(lab, "mcp.token.create")[0]["detail"].startswith("home-assistant")
    bad = lab.client.post(
        "/admin/ai/mcp/create", data={"csrf_token": csrf, "name": "x", "locality": "cloud", "scope": ["logs"]}
    )
    assert bad.status_code == 422 and "cloud client cannot" in bad.text
    token_id = mcptokens.listing(lab.store)[0]["token_id"]
    done = lab.client.post("/admin/ai/mcp/revoke", data={"csrf_token": csrf, "token_id": token_id})
    assert done.status_code == 303 and mcptokens.verify(lab.store, secret) is None
    assert audit(lab, "mcp.token.revoke")[0]["detail"] == "home-assistant"
    assert lab.https().post("/admin/ai/mcp/create", data={"name": "x"}).status_code in (303, 401, 403)


# ---------------------------------------------------------------- the command line


def run(*argv):
    return cli.main(list(argv))


def test_cli_makes_lists_and_revokes_tokens(box, capsys):  # noqa: F811
    _, _, state, store = box
    assert run("mcp", "token-create", "claude", "--scope", "status", "--scope", "history") == 0
    out = capsys.readouterr().out
    secret = re.search(r"hbm_[0-9a-f]{8}_[\w-]+", out).group(0)
    assert mcptokens.verify(store, secret).scopes == {"status", "history"}
    assert run("mcp", "token-create", "claude") == 1 and "already" in capsys.readouterr().err
    assert run("mcp", "token-create", "c2", "--cloud", "--scope", "logs") == 1
    capsys.readouterr()
    assert run("mcp", "token-list") == 0 and "claude" in capsys.readouterr().out
    assert run("mcp", "token-revoke", "claude") == 0 and mcptokens.verify(store, secret) is None
    assert run("mcp", "token-revoke", "claude") == 1
    assert [a["user"] for a in audit_of(store, "mcp.token.%")] == ["cli:tester"] * 2
    assert "hbm_" not in json.dumps(audit_of(store, "mcp.%"))


def audit_of(store, like):
    with store.read() as db:
        return [
            dict(r)
            for r in db.execute("SELECT user, action, detail FROM audit_log WHERE action LIKE ?", (like,))
        ]


def test_stdio_serves_one_json_message_per_line_and_honours_the_switch(box, capsys, monkeypatch):  # noqa: F811
    _, user, state, store = box
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    assert run("mcp") == 1 and "off" in capsys.readouterr().err
    (user / "ai.yaml").write_text("schema: 1\nenabled: true\nfeatures:\n  mcp_server: true\n")
    lines = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "search_logs", "arguments": {"text": "x"}},
        },
    ]
    monkeypatch.setattr("sys.stdin", io.StringIO("\n".join(json.dumps(m) for m in lines) + "\n\n"))
    assert run("mcp") == 0
    replies = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [r["id"] for r in replies] == [1, 2, 3]
    assert "search_logs" not in {t["name"] for t in replies[1]["result"]["tools"]}
    assert replies[2]["result"]["isError"]
    assert run("mcp", "--scope", "bogus") in (1, 2)


def seed_signature(env):
    """One signature with a little history in the live store (the daily-run fixture has none)."""
    now = datetime.now(UTC)
    with env.store.write() as tx:
        sid = tx.signature_for(0, 1, "app", "a generalised template", now)
        tx.add_hourly([(f"{now:%Y-%m-%d}", now.hour, "198.51.100.1", sid, "info", 25)])
    return sid


def write_digest(env):
    (env.settings.out_dir / "latest" / "digest.md").write_text(
        "# Homelab brief for 2026-10-09\n\n1. 198.51.100.3 has been silent for 2 days\n", encoding="utf-8"
    )
