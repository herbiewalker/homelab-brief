"""The settings framework: every config setting as a form control, written back to the file (Task 33).

A page describes a group of settings as a `Section` (a file, where in it the settings live, and a list of `Field`
and `Rows`). This module does the rest, the same way for every file:

  load      the current values from the file (and the shipped default, for a hint and a reset), with placeholders
            such as `<server-1-ip>` shown as empty fields
  parse     a posted form into typed values, with one plain-language error per field
  apply     the values written back as text: a scalar is changed in place (comments and order stay), a missing
            setting is added, a list of rows is rewritten (and anything a row holds that the form has no column
            for, such as an OID list, is kept)

It never writes a file itself. The caller saves the text through the config editor, so the same digest check,
validation, history, undo, and audit apply as for the text editor. A secret is never a field: a `var` field names
the environment variable that holds it.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass, replace

import yaml

from brief import yamlscalar
from brief.config import is_placeholder

VAR_NAME = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
MAX_ROWS = 100


@dataclass(frozen=True)
class Field:
    key: str
    label: str
    kind: str  # text | number | switch | choice | var | list (text items) | multi (tick any of `choices`)
    help: str = ""
    choices: tuple[str, ...] = ()
    unit: str = ""
    placeholder: str = ""
    minimum: float | None = None
    maximum: float | None = None
    integer: bool = True
    required: bool = (
        False  # must be filled in (a row column), or before the thing can be switched on (a field)
    )
    pattern: str = ""  # a regular expression the text must match, with the message in `pattern_help`
    pattern_help: str = ""
    slug: bool = False  # any name is accepted and saved as a lowercase id (letters, digits, hyphens)
    clearable: bool = False  # an empty box means "empty" (written as ""), not "leave the file alone"
    initial: object = ""  # what a newly added row starts with (a switch defaults to off unless this is True)
    lines: int = (
        0  # kind "list": 0 = one line separated by commas or spaces, n = a box of n lines, one item each
    )
    group: str = ""  # a heading shown above this field (and the ones after it, until the next group)
    item_type: str = ""  # kind "list": "int" or "float" turns each item into a number
    utc_time: bool = False  # a time of day in UTC: the page shows it in the viewer's own time zone too
    check: object = (
        None  # an optional function(text) -> a problem message or None, for rules a pattern cannot say
    )

    def relaxed(self) -> "Field":
        """The same control without the 'needed before it can be switched on' rule."""
        return replace(self, required=False)


@dataclass(frozen=True)
class Rows:
    key: str
    label: str
    columns: tuple[Field, ...]
    noun: str = "row"
    help: str = ""
    required: bool = False  # at least one row is needed before the thing can be switched on
    test_label: str = ""  # a "test" button on each row (posts the row to <card>/rowtest)


@dataclass(frozen=True)
class Section:
    id: str
    title: str
    file: str
    root: tuple[str, ...]
    items: tuple[Field | Rows, ...]
    blurb: str = ""
    icon: str = "sliders"
    switch: str = "enabled"  # the on/off key at the root, or "" for none
    switch_label: str = "Switched on"
    grid_title: str = ""
    test_label: str = ""  # a "test" button on the card when set
    grid: tuple[
        str, ...
    ] = ()  # column names: fields keyed "<row>.<column>" are shown as a table, one row each
    preview: bool = False  # a "what would this have changed last week" button (replays the last week)

    @property
    def scalar_fields(self) -> list[Field]:
        return [i for i in self.items if isinstance(i, Field)]

    @property
    def row_lists(self) -> list[Rows]:
        return [i for i in self.items if isinstance(i, Rows)]


class _Missing:
    def __repr__(self) -> str:
        return "MISSING"


MISSING = _Missing()

SPLIT_ITEMS = re.compile(r"[,\s]+")
SPLIT_LINES = re.compile(r"[,\r\n]+")
TICKED = ("on", "true", "1")


def _raw(form: Mapping, name: str, kind: str):
    """A posted value: a list for a group of ticks, otherwise the last value sent under that name."""
    if kind == "multi":
        getter = getattr(form, "getlist", None)
        got = getter(name) if getter else form.get(name, [])
        return [got] if isinstance(got, str) else list(got or [])
    return form.get(name)


def split_items(text: str, lines: int = 0) -> list[str]:
    """Typed items as a list: one per line in a box, otherwise separated by commas or spaces. No duplicates."""
    parts = (SPLIT_LINES if lines else SPLIT_ITEMS).split(text)
    seen, out = set(), []
    for part in (p.strip() for p in parts):
        if part and part not in seen:
            seen.add(part)
            out.append(part)
    return out


# ---------------------------------------------------------------- reading


def _node(data, root: tuple[str, ...]) -> Mapping:
    for key in root:
        data = data.get(key) if isinstance(data, Mapping) else None
    return data if isinstance(data, Mapping) else {}


PLACEHOLDER = re.compile(r"<[^<>\n]{1,60}>")


def _has_placeholder(row: Mapping) -> bool:
    for value in row.values():
        items = value if isinstance(value, list) else [value]
        if any(isinstance(v, str) and PLACEHOLDER.search(v) for v in items):
            return True
    return False


def _shown(value):
    """A value as a form shows it: anything holding a <placeholder>, such as <server-1-ip>, is an empty field."""
    if isinstance(value, str) and (is_placeholder(value) or PLACEHOLDER.search(value)):
        return ""
    return "" if value is None else value


def _dig(mapping, key: str, fallback, default):
    """The value at a dotted key ("kinds.absence.rank") in `mapping`, else the same in `fallback`, else `default`."""
    for source in (mapping, fallback):
        node = source
        for part in key.split("."):
            node = node.get(part, MISSING) if isinstance(node, Mapping) else MISSING
        if node is not MISSING:
            return node
    return default


def _shown_value(f: Field, value):
    """A setting as its control holds it: a list of ticked choices, a box of lines, or a one-line list."""
    if f.kind == "hours":  # {start: "22:00", end: "06:30"} as "22:00-06:30"
        if isinstance(value, Mapping) and value.get("start") and value.get("end"):
            return f"{value['start']}-{value['end']}"
        return ""
    if f.kind == "paragraph":
        return " ".join(str(value).split()) if isinstance(value, str) else ""
    if f.kind == "multi":
        return [str(v) for v in value] if isinstance(value, list) else []
    if f.kind == "list":
        items = [str(v) for v in value] if isinstance(value, list) else []
        return ("\n" if f.lines else ", ").join(items)
    return _shown(value)


def load(section: Section, text: str, default_text: str = "") -> dict:
    """{"values": {key: value}, "defaults": {key: value}, "rows": {key: [row dicts]}, "enabled": bool}."""
    base = _node(yaml.safe_load(text) or {}, section.root)
    shipped = _node(yaml.safe_load(default_text) or {}, section.root) if default_text else {}
    values, defaults, rows = {}, {}, {}
    for f in section.scalar_fields:
        values[f.key] = _shown_value(f, _dig(base, f.key, shipped, ""))
        defaults[f.key] = _shown_value(f, _dig(shipped, f.key, None, ""))
    for r in section.row_lists:
        listed = [row for row in (base.get(r.key) or []) if isinstance(row, Mapping)]
        cols = {c.key: c for c in r.columns}
        # a row that still holds a <placeholder> is the example the file ships with, not something to show
        rows[r.key] = [
            {
                **{k: _shown_value(cols.get(k), v) if cols.get(k) else _shown(v) for k, v in row.items()},
                "_orig": i,
            }
            for i, row in enumerate(listed)
            if not _has_placeholder(row)
        ]
    enabled = bool(base.get(section.switch)) if section.switch else True
    return {"values": values, "defaults": defaults, "rows": rows, "enabled": enabled}


# ---------------------------------------------------------------- parsing a posted form


def _one(f: Field, raw, errors: dict, where: str):
    """One control's posted value as a typed value, or MISSING when it is empty or wrong (recording the error)."""
    if f.kind == "switch":
        return raw in ("on", "true", "1", True)
    if f.kind == "multi":
        chosen = [str(x) for x in (raw or [])]
        bad = [x for x in chosen if x not in f.choices]
        if bad:
            errors[where] = "Choose from: " + ", ".join(f.choices) + "."
            return MISSING
        return [c for c in f.choices if c in chosen]
    text = ("" if raw is None else str(raw)).strip()
    if not text:
        if f.kind == "list":
            return []
        if f.clearable:
            return ""
        if f.required:
            errors[where] = "This is needed before it can be switched on."
        return MISSING
    if f.kind == "list":
        items = split_items(text, f.lines)
        for item in items:
            if "<" in item or ">" in item or "#" in item or "\x00" in item:
                errors[where] = "Replace the placeholder with a real value."
                return MISSING
            if f.pattern and not re.match(f.pattern, item):
                errors[where] = f"{item[:40]}: " + (f.pattern_help or "that does not look right.")
                return MISSING
        if f.item_type in ("int", "float"):
            try:
                return [int(i) if f.item_type == "int" else float(i) for i in items]
            except ValueError:
                errors[where] = "Use numbers separated by commas."
                return MISSING
        return items
    if f.kind == "paragraph":  # advice text holds {placeholders}; line breaks fold into spaces
        return " ".join(text.split())
    if f.kind == "hours":
        m = re.match(r"^\s*([01]\d|2[0-3]):([0-5]\d)\s*-\s*([01]\d|2[0-3]):([0-5]\d)\s*$", text)
        if not m:
            errors[where] = "Use two times in UTC like 22:00-06:30."
            return MISSING
        return {"start": f"{m[1]}:{m[2]}", "end": f"{m[3]}:{m[4]}"}
    if "\n" in text or "\r" in text or "\x00" in text:
        errors[where] = "Use one line of text."
        return MISSING
    if f.kind == "number":
        try:
            number = int(text) if f.integer else float(text)
        except ValueError:
            errors[where] = "Use a whole number." if f.integer else "Use a number."
            return MISSING
        if (f.minimum is not None and number < f.minimum) or (f.maximum is not None and number > f.maximum):
            low = "" if f.minimum is None else f"at least {f.minimum:g}"
            high = "" if f.maximum is None else f"at most {f.maximum:g}"
            errors[where] = "Use a number " + (f"{low} and {high}" if low and high else low or high) + "."
            return MISSING
        return number
    if f.kind == "choice":
        if text not in f.choices:
            errors[where] = "Choose one of: " + ", ".join(f.choices) + "."
            return MISSING
        return text
    if f.kind == "var":
        if not VAR_NAME.match(text):
            errors[where] = (
                "This looks like a key, not a name. Type the name of the container variable that holds it, "
                "for example UNRAID_KEY (capital letters, digits, underscores). Never type the key here."
                if len(text) > 40 or text.isalnum() and text == text.lower() and len(text) > 20
                else "A variable name is capital letters, digits, and underscores, for example UNRAID_KEY."
            )
            return MISSING
        return text
    if "<" in text or ">" in text:
        errors[where] = "Replace the placeholder with a real value."
        return MISSING
    if f.slug:
        text = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:31].strip("-")
    if f.pattern and not re.match(f.pattern, text):
        errors[where] = f.pattern_help or "That does not look right."
        return MISSING
    if f.check and (problem := f.check(text)):
        errors[where] = problem
        return MISSING
    return text


def _parse_rows(r: Rows, form: Mapping, errors: dict) -> list[dict]:
    try:
        count = max(0, min(int(form.get(f"r.{r.key}.count", 0)), MAX_ROWS))
    except ValueError:
        count = 0
    out = []
    for i in range(count):
        if form.get(f"r.{r.key}.{i}.gone"):
            continue
        posted = {c.key: form.get(f"r.{r.key}.{i}.{c.key}") for c in r.columns}
        if not any(
            str(posted[c.key] or "").strip() not in ("", str(c.initial or ""))
            for c in r.columns
            if c.kind != "switch"
        ):
            continue  # an empty row (an untouched "add") is simply dropped
        row: dict = {}
        for c in r.columns:
            where = f"r.{r.key}.{i}.{c.key}"
            if c.kind != "switch" and not str(posted[c.key] or "").strip():
                if c.required:
                    errors[where] = "Needed."
                continue
            value = _one(c.relaxed(), posted[c.key], errors, where)
            if value is not MISSING:
                row[c.key] = value
        orig = form.get(f"r.{r.key}.{i}.orig")
        row["_orig"] = int(orig) if str(orig or "").isdigit() else None
        out.append(row)
    return out


def parse(section: Section, form: Mapping) -> tuple[dict, dict, bool]:
    """(values, errors, switched on) from a posted form. A scalar that has no value is MISSING and is left alone
    in the file. A `required` scalar is checked only when the thing is being switched on."""
    errors: dict[str, str] = {}
    on = (
        bool(_one(Field(section.switch, "", "switch"), form.get(f"f.{section.switch}"), {}, ""))
        if section.switch
        else True
    )
    values: dict = {}
    for f in section.scalar_fields:
        values[f.key] = _one(f if on else f.relaxed(), _raw(form, f"f.{f.key}", f.kind), errors, f"f.{f.key}")
    for r in section.row_lists:
        values[r.key] = _parse_rows(r, form, errors)
        if on and r.required and not values[r.key]:
            errors[f"r.{r.key}"] = f"Add at least one {r.noun} before switching this on."
    return values, errors, on


# ---------------------------------------------------------------- writing back


def _same(current, value) -> bool:
    if isinstance(current, bool) or isinstance(value, bool):
        return current is value
    if isinstance(current, int | float) and isinstance(value, int | float):
        return float(current) == float(value)
    return current == value


def apply(section: Section, text: str, values: Mapping, enabled: bool | None = None) -> str:
    """The file text with the form's values written in. Only what changed is touched."""
    base = _node(yaml.safe_load(text) or {}, section.root)
    path = list(section.root)
    if section.switch and enabled is not None and bool(base.get(section.switch)) != enabled:
        text = yamlscalar.set_text(text, [*path, section.switch], enabled)
    for f in section.scalar_fields:
        value = values.get(f.key, MISSING)
        current = _dig(base, f.key, None, MISSING)
        if f.kind == "switch":
            value = bool(value) if value is not MISSING else False
            if current is MISSING and value is False:
                continue
        elif f.kind in ("list", "multi"):
            if value is MISSING or (current is MISSING and not value):
                continue
            if current is not MISSING and list(current or []) == list(value):
                continue
            text = yamlscalar.set_flow_list(text, [*path, *f.key.split(".")], list(value))
            continue
        elif value is MISSING:
            continue  # nothing typed: leave the file's own setting (or its absence) alone
        if (current is not MISSING and _same(current, value)) or (current is MISSING and value == ""):
            continue
        if f.kind == "paragraph":
            text = yamlscalar.set_folded(text, [*path, *f.key.split(".")], str(value))
        else:
            text = yamlscalar.set_text(text, [*path, *f.key.split(".")], value)
    for r in section.row_lists:
        original = [dict(x) for x in (base.get(r.key) or []) if isinstance(x, Mapping)]
        rows = []
        for row in values.get(r.key, []):
            orig = row.get("_orig")
            merged = dict(original[orig]) if orig is not None and 0 <= orig < len(original) else {}
            for c in r.columns:  # a column left empty is removed, not written as ""
                if c.key not in row and c.kind != "switch":
                    merged.pop(c.key, None)
            merged.update({k: v for k, v in row.items() if k != "_orig"})
            rows.append(merged)
        if rows != original:
            text = yamlscalar.replace_list(text, [*path, r.key], rows)
    return text


def blank_row(r: Rows) -> dict:
    return {
        c.key: (c.initial if c.initial not in ("", None) else (False if c.kind == "switch" else ""))
        for c in r.columns
    }


# ---------------------------------------------------------------- re-rendering a form that is not saved yet


def _count(r: Rows, form: Mapping) -> int:
    try:
        return max(0, min(int(form.get(f"r.{r.key}.count", 0)), MAX_ROWS))
    except ValueError:
        return 0


def posted(section: Section, form: Mapping) -> dict:
    """What a form currently holds, unvalidated and as typed, for showing it again (a row was added or removed,
    or a value was refused). Empty rows are kept here: they are what the person just added."""
    ticked = ("on", "true", "1")
    values = {}
    for f in section.scalar_fields:
        raw = _raw(form, f"f.{f.key}", f.kind)
        if f.kind == "multi":
            values[f.key] = list(raw or [])
        else:
            values[f.key] = ("on" if raw in ticked else "") if f.kind == "switch" else str(raw or "")
    rows: dict[str, list[dict]] = {}
    for r in section.row_lists:
        listed = []
        for i in range(_count(r, form)):
            if form.get(f"r.{r.key}.{i}.gone"):
                continue
            row: dict = {}
            for c in r.columns:
                raw = form.get(f"r.{r.key}.{i}.{c.key}")
                row[c.key] = (raw in ticked) if c.kind == "switch" else str(raw or "")
            orig = form.get(f"r.{r.key}.{i}.orig")
            row["_orig"] = int(orig) if str(orig or "").isdigit() else None
            listed.append(row)
        rows[r.key] = listed
    on = bool(section.switch) and form.get(f"f.{section.switch}") in ticked
    return {"values": values, "rows": rows, "enabled": on if section.switch else True}


def row_action(section: Section, state: dict, action: str) -> None:
    """Apply "add:servers" or "remove:servers:2" to the rows of a posted state."""
    kind, _, rest = action.partition(":")
    key, _, index = rest.partition(":")
    rows = state["rows"].get(key)
    spec = next((r for r in section.row_lists if r.key == key), None)
    if rows is None or spec is None:
        return
    if kind == "add" and len(rows) < MAX_ROWS:
        rows.append({**blank_row(spec), "_orig": None})
    elif kind == "remove" and index.isdigit() and int(index) < len(rows):
        rows.pop(int(index))
