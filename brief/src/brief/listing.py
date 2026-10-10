"""Helpers for the admin list pages (Task 35): filters, sorting, and paging kept in the address, so a view can
be bookmarked or saved, and so a hostile value can never reach a query as anything but a checked choice or
number.
"""

from urllib.parse import urlencode

PER_PAGE = 25
MAX_Q = 120


def choice(value: str, allowed, default: str) -> str:
    """The value if it is one of `allowed`, else the default (never trust a sort or filter name)."""
    return value if value in allowed else default


def number(value, default: int = 1, low: int = 1, high: int = 100000) -> int:
    try:
        return max(low, min(int(value), high))
    except (TypeError, ValueError):
        return default


class Listing:
    """The state of one list page: its path, the current parameters, and links that change one of them."""

    def __init__(self, path: str, params: dict, defaults: dict | None = None):
        self.path = path
        self.params = {k: v for k, v in params.items() if v not in ("", None)}
        self.defaults = defaults or {}

    def get(self, key: str):
        return self.params.get(key, self.defaults.get(key, ""))

    def url(self, **override) -> str:
        merged = {**self.params, **override}
        keep = {
            k: v for k, v in merged.items() if v not in ("", None) and str(v) != str(self.defaults.get(k, ""))
        }
        return self.path + ("?" + urlencode(keep) if keep else "")

    # sorting: the same column again flips the direction, a new column starts at its natural direction
    @property
    def sort(self) -> str:
        return str(self.get("sort"))

    @property
    def direction(self) -> str:
        return str(self.get("dir") or "desc")

    def sort_url(self, key: str, natural: str = "desc") -> str:
        flip = "asc" if self.direction == "desc" else "desc"
        return self.url(sort=key, dir=flip if self.sort == key else natural, page=1)

    def aria_sort(self, key: str) -> str:
        if self.sort != key:
            return "none"
        return "ascending" if self.direction == "asc" else "descending"

    @property
    def filtered(self) -> bool:
        return any(k not in ("sort", "dir", "page") for k in self.params)

    @property
    def query(self) -> str:
        return urlencode(self.params)


def paginate(rows: list, page: int, per: int = PER_PAGE) -> dict:
    """{"rows": this page, "page", "pages", "total", "first", "last"} (page is clamped into range)."""
    total = len(rows)
    pages = max(1, -(-total // per))
    page = max(1, min(page, pages))
    start = (page - 1) * per
    return {
        "rows": rows[start : start + per],
        "page": page,
        "pages": pages,
        "total": total,
        "first": start + 1 if total else 0,
        "last": min(start + per, total),
    }
