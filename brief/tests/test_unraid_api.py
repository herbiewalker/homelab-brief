"""Task 19: the GraphQL client, against a real local HTTP server."""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from brief.collectors.unraid_api import ApiError, UnraidApi, document, prune, render

SECTION = {"disks": {"id": None, "name": None, "smartStatus": None}}


class Server:
    """Replies from a script: each item is (status, body) and is used once, then the last one repeats."""

    def __init__(self, script):
        outer = self
        self.script, self.seen = list(script), []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("content-length", 0))
                outer.seen.append(
                    (self.headers.get("x-api-key"), json.loads(self.rfile.read(length))["query"])
                )
                status, body = outer.script.pop(0) if len(outer.script) > 1 else outer.script[0]
                payload = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self):
                outer.seen.append((self.headers.get("x-api-key"), self.path))
                status, body = outer.script.pop(0) if len(outer.script) > 1 else outer.script[0]
                payload = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        self.httpd = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_port}/graphql"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def serve():
    servers = []

    def start(*script):
        servers.append(Server(script))
        return servers[-1]

    yield start
    for s in servers:
        s.close()


def test_a_good_reply_comes_back_and_the_key_is_sent_as_a_header_only(serve):
    s = serve((200, {"data": {"disks": [{"id": "1"}]}}))
    data, dropped = UnraidApi(s.url, "secret-key").section(SECTION)
    assert data == {"disks": [{"id": "1"}]} and dropped == []
    key, query = s.seen[0]
    assert key == "secret-key" and query == "{ disks { id name smartStatus } }" and "secret-key" not in query


def test_a_rejected_key_is_an_auth_error_without_the_key_in_it(serve):
    s = serve((401, {"errors": [{"message": "Unauthorized"}]}))
    with pytest.raises(ApiError) as exc:
        UnraidApi(s.url, "secret-key").section(SECTION)
    assert exc.value.kind == "auth" and "secret-key" not in exc.value.message


def test_a_closed_port_is_unreachable():
    with pytest.raises(ApiError) as exc:
        UnraidApi("http://127.0.0.1:9/graphql", "k", timeout=2).section(SECTION)
    assert exc.value.kind == "unreachable" and exc.value.message.startswith("could not connect")


def test_a_field_the_server_does_not_know_is_dropped_and_reported(serve):
    unknown = {"errors": [{"message": 'Cannot query field "smartStatus" on type "Disk".'}]}
    s = serve((400, unknown), (200, {"data": {"disks": []}}))
    data, dropped = UnraidApi(s.url, "k").section(SECTION)
    assert dropped == ["smartStatus"] and data == {"disks": []}
    assert [q for _, q in s.seen] == ["{ disks { id name smartStatus } }", "{ disks { id name } }"]


def test_a_query_the_server_refuses_is_a_graphql_error(serve):
    s = serve(
        (200, {"data": None, "errors": [{"message": "Failed to retrieve VM domains: VMs are not available"}]})
    )
    with pytest.raises(ApiError) as exc:
        UnraidApi(s.url, "k").section({"vms": {"domains": {"id": None}}})
    assert exc.value.kind == "graphql" and "VMs are not available" in exc.value.message


def test_endless_unknown_fields_stop(serve):
    s = serve((200, {"data": None, "errors": [{"message": 'Cannot query field "a" on type "X".'}]}))
    with pytest.raises(ApiError):
        UnraidApi(s.url, "k").section({"x": {"a": None, "b": None}})
    assert len(s.seen) <= 3  # a, then b is not named, so it stops after dropping what it can


@pytest.mark.parametrize("body", [b"not json", b"[1, 2]", b'{"nothing": true}'])
def test_a_reply_that_is_not_graphql_is_a_bad_response(serve, body):
    s = serve((200, body))
    with pytest.raises(ApiError) as exc:
        UnraidApi(s.url, "k").section(SECTION)
    assert exc.value.kind == "bad_response"


def test_render_and_prune():
    selection = {"array": {"state": None, "disks": {"name": None, "temp": None}}}
    assert document(selection) == "{ array { state disks { name temp } } }"
    assert prune(selection, "temp") and render(selection) == "array { state disks { name } }"
    assert prune(selection, "name") and render(selection) == "array { state }"  # an emptied branch disappears
    assert not prune(selection, "array")  # a top-level field is never dropped
    assert not prune(selection, "missing")
