"""A small read-only client for the UniFi Network Integration API (standard library only, GET only).

The API is served by the Network application under `/proxy/network/integration/v1` on a UniFi OS console
(`/integration/v1` on a self-hosted application; `api_path` in collectors.yaml). The key goes in the
`X-API-KEY` header. Lists are paged with `offset` and `limit` and answer
`{offset, limit, count, totalCount, data: [...]}`; this client follows the pages.
"""

from urllib.parse import urlencode

from brief.collectors.httpjson import ApiError, request_json, tls_context

PAGE = 200
MAX_PAGES = 50  # 10,000 items: far more than any home network, and a stop against a server that never ends


class UnifiApi:
    def __init__(
        self,
        base_url: str,
        key: str,
        api_path: str = "/proxy/network/integration",
        timeout: float = 10.0,
        verify_tls: bool = True,
    ):
        self.base = base_url.rstrip("/") + "/" + api_path.strip("/")
        self._key, self.timeout = key, timeout
        self._context = tls_context(base_url, verify_tls)

    def get(self, path: str, **params) -> dict:
        url = f"{self.base}{path}" + (f"?{urlencode(params)}" if params else "")
        data = request_json(url, {"x-api-key": self._key}, self.timeout, self._context)
        if not isinstance(data, dict):
            raise ApiError("bad_response", "the reply was not an object")
        return data

    def paged(self, path: str) -> list[dict]:
        """Every item of a list endpoint."""
        items: list[dict] = []
        offset = 0
        for _ in range(MAX_PAGES):
            page = self.get(path, offset=offset, limit=PAGE)
            batch = page.get("data")
            if not isinstance(batch, list):
                raise ApiError("bad_response", "the reply had no list of items")
            items += [x for x in batch if isinstance(x, dict)]
            offset += len(batch)
            total = page.get("totalCount")
            if not batch or (isinstance(total, int) and offset >= total):
                break
        return items
