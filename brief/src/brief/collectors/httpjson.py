"""JSON over HTTP(S) for collectors (standard library only), with errors that are safe to show.

Every collector that talks to an API uses this, so the error handling is the same everywhere: a rejected
key is `auth`, anything that stops a connection is `unreachable`, a reply that is not what was expected is
`bad_response`. The message never contains the key or the address, only what is wrong.
"""

import json
import ssl
import urllib.error
import urllib.request

MAX_RESPONSE = 8_000_000


class ApiError(Exception):
    """kind: auth (key rejected), unreachable (network, TLS, timeout), graphql (the server refused the query),
    bad_response (not JSON, too large). `message` never contains the key."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind
        self.message = message


def tls_context(url: str, verify: bool = True) -> ssl.SSLContext | None:
    if not url.lower().startswith("https"):
        return None
    context = ssl.create_default_context()
    if not verify:  # a console on a LAN address usually has a self-signed certificate
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    return context


def request_json(
    url: str,
    headers: dict[str, str],
    timeout: float,
    context: ssl.SSLContext | None,
    body: bytes | None = None,
    error_json: bool = False,
):
    """GET (or POST with `body`) and return the decoded JSON. With `error_json`, an HTTP error whose body is a
    JSON object with `errors` is returned like a reply (GraphQL servers send those with a 4xx status)."""
    request = urllib.request.Request(
        url,
        data=body,
        method="POST" if body is not None else "GET",
        headers={"accept": "application/json", **headers},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=context) as reply:
            payload = reply.read(MAX_RESPONSE + 1)
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise ApiError("auth", "the API key was rejected") from None
        if error_json:
            try:
                detail = json.loads(exc.read(MAX_RESPONSE) or b"{}")
            except ValueError:
                detail = {}
            if isinstance(detail, dict) and detail.get("errors"):
                return detail
        raise ApiError("unreachable", f"the server answered HTTP {exc.code}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, TimeoutError):
            raise ApiError(
                "unreachable",
                f"no answer within {timeout:g} s (the server is slow, or the network drops the connection; "
                "try a longer request time limit)",
            ) from None
        raise ApiError("unreachable", f"could not connect ({type(reason).__name__})") from None
    if len(payload) > MAX_RESPONSE:
        raise ApiError("bad_response", "the reply was larger than expected")
    try:
        return json.loads(payload)
    except ValueError:
        raise ApiError("bad_response", "the reply was not JSON") from None
