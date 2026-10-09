"""A small client for the Unraid GraphQL API (standard library only, read-only queries).

Each part of what the collector wants is its own query (a "section"), so one the server cannot answer
(no VMs, an older API) costs that section and nothing else. Queries are written as nested dicts of the
fields wanted. If the server replies `Cannot query field "x"`, the client drops that field and asks
again (up to a few times) and reports which fields it had to drop, so a difference between Unraid API
versions degrades to less data and a clear note in `brief collector test unraid`, never a broken run.

Only queries are ever sent. The key goes in the `x-api-key` header and is never logged or returned.
"""

import json
import re
from collections.abc import Callable
from copy import deepcopy

from brief.collectors.httpjson import ApiError, request_json, tls_context

MAX_RETRIES = 4
_UNKNOWN_FIELD = re.compile(r'Cannot query field "(\w+)"')

Selection = dict  # field name -> nested Selection, or None for a leaf


def render(selection: Selection) -> str:
    parts = []
    for name, sub in selection.items():
        parts.append(f"{name} {{ {render(sub)} }}" if isinstance(sub, dict) else name)
    return " ".join(parts)


def document(selection: Selection) -> str:
    return "{ " + render(selection) + " }"


def prune(selection: Selection, field: str) -> bool:
    """Remove the first nested field called `field` (never a top-level one). True if something was removed."""

    def walk(node: Selection, top: bool) -> bool:
        for name in list(node):
            if name == field and not top:
                del node[name]
                return True
            if isinstance(node[name], dict) and walk(node[name], False):
                if not node[name]:
                    del node[name]
                return True
        return False

    return walk(selection, True)


class UnraidApi:
    def __init__(self, url: str, key: str, timeout: float = 10.0, verify_tls: bool = True):
        self.url, self._key, self.timeout = url, key, timeout
        self._context = tls_context(url, verify_tls)  # an Unraid server usually has a self-signed certificate

    def raw(self, query: str) -> dict:
        data = request_json(
            self.url,
            {"content-type": "application/json", "x-api-key": self._key},
            self.timeout,
            self._context,
            body=json.dumps({"query": query}).encode(),
            error_json=True,
        )
        if not isinstance(data, dict):
            raise ApiError("bad_response", "the reply was not a GraphQL response")
        return data

    def section(self, selection: Selection) -> tuple[dict, list[str]]:
        """(data, dropped): run one section, dropping fields the server does not know. Raises ApiError."""
        wanted = deepcopy(selection)
        dropped: list[str] = []
        for _ in range(MAX_RETRIES + 1):
            reply = self.raw(document(wanted))
            errors = reply.get("errors") or []
            if errors:
                message = str(
                    errors[0].get("message", "unknown error") if isinstance(errors[0], dict) else errors[0]
                )
                unknown = _UNKNOWN_FIELD.search(message)
                if unknown and prune(wanted, unknown.group(1)):
                    dropped.append(unknown.group(1))
                    continue
                if reply.get("data") is None:
                    raise ApiError("graphql", message[:200])
            data = reply.get("data")
            if not isinstance(data, dict):
                raise ApiError("bad_response", "the reply had no data")
            return data, dropped
        raise ApiError("graphql", "too many unknown fields in the query")


ApiFactory = Callable[..., UnraidApi]
