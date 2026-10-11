"""Change one scalar in a YAML file's text and leave everything else (comments, order, spacing) alone.

The config files are hand-written, commented, and use both block style and one-line `{ a: 1, b: 2 }` mappings, so
they are edited as text, line by line, never re-dumped. `set_path(text, ["thresholds", "deviation", "min_ratio"], 4)`
finds the key by indentation (or inside the one-line mapping that holds it) and replaces only its value. The caller
checks the result with the file's own validator; this module never guesses beyond the shape it was given.
"""

import json
import re

SAFE_WORD = re.compile(r"^[a-z_][a-z0-9_]{0,30}$")
PLAIN_TEXT = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]{0,60}$")
RESERVED = {"true", "false", "null", "yes", "no", "on", "off", "y", "n"}  # YAML would read these as non-text


class EditError(ValueError):
    pass


def render(value) -> str:
    """A scalar as YAML text: a number, true or false, or a plain lowercase word (such as a route)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(round(value, 6))
    if isinstance(value, str) and SAFE_WORD.match(value):
        return value
    raise EditError("a value must be a number, true or false, or a plain lowercase word")


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def set_path(text: str, path: list[str], value) -> str:
    """The text with the scalar at `path` replaced. Raises EditError if the path is not there as a scalar."""
    new = render(value)
    lines = text.split("\n")
    at, base = -1, -1  # the line of the last matched key, and its indentation
    for depth, key in enumerate(path):
        rx = re.compile(rf"^(?P<ind> *){re.escape(key)}:(?P<rest>.*)$")
        found = -1
        for i in range(at + 1, len(lines)):
            line = lines[i]
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            if _indent(line) <= base and at >= 0:
                break  # left the block of the parent key
            m = rx.match(line)
            if m and (at < 0 and _indent(line) == 0 or at >= 0 and _indent(line) > base):
                found = i
                break
        if found < 0:
            raise EditError(f"{'.'.join(path)} is not in the file")
        at, base = found, _indent(lines[found])
        rest = rx.match(lines[found]).group("rest")
        if depth < len(path) - 1 and rest.strip().startswith("{"):
            return _inline(lines, found, path[depth + 1 :], new)
    line = lines[at]
    m = re.match(rf"^(?P<head> *{re.escape(path[-1])}:[ \t]*)(?P<val>[^#\n]*?)(?P<tail>[ \t]*(#.*)?)$", line)
    if not m or not m.group("val").strip() or m.group("val").strip().startswith(("{", "[", ">", "|")):
        raise EditError(f"{'.'.join(path)} is not a single value")
    lines[at] = m.group("head") + new + m.group("tail")
    return "\n".join(lines)


def _inline(lines: list[str], i: int, rest: list[str], new: str, add: bool = False) -> str:
    """The value sits inside a one-line mapping on line i, possibly several levels deep: { a: { b: 1 } }."""
    line = lines[i]
    start = line.index("{")
    segment = line[start:]
    for key in rest[:-1]:  # step into nested one-line mappings
        m = re.search(rf"\b{re.escape(key)}:\s*\{{", segment)
        if not m:
            raise EditError(f"{key} is not in that line")
        start += m.end() - 1
        segment = line[start:]
    key = rest[-1]
    m = re.search(rf"(?P<head>[{{,]\s*{re.escape(key)}:\s*)(?P<val>\[[^\]]*\]|[^,}}\s]+)", segment)
    if not m and add:  # the key is not in the one-line mapping yet: add it before the closing brace
        close = segment.find("}")
        if close < 0 or "{" in segment[1:close]:
            raise EditError(f"{'.'.join(rest)} is not in that line")
        a = start + close
        lines[i] = line[:a].rstrip() + f", {key}: {new} " + line[a:]
        return "\n".join(lines)
    if not m:
        raise EditError(f"{'.'.join(rest)} is not in that line")
    a, b = start + m.start("val"), start + m.end("val")
    lines[i] = line[:a] + new + line[b:]
    return "\n".join(lines)


# ---------------------------------------------------------------- forms: text values, new keys, and lists of rows


def render_text(value) -> str:
    """Any scalar as YAML for a form field: numbers, true/false, a plain word as itself, other text in double
    quotes (JSON string rules are valid YAML double-quoted). A mapping or list is written as a one-line flow."""
    if isinstance(value, bool | int | float):
        return render(value)
    if isinstance(value, str):
        if PLAIN_TEXT.match(value) and value.lower() not in RESERVED:
            return value
        return json.dumps(value, ensure_ascii=False)
    if value is None:
        return "null"
    return json.dumps(value, ensure_ascii=False)


def render_row(row: dict) -> str:
    """One list row as a one-line flow mapping: { id: one, url: "https://x/graphql", verify_tls: false }."""
    return "{ " + ", ".join(f"{k}: {render_text(v)}" for k, v in row.items()) + " }"


def _find(lines: list[str], path: list[str]) -> tuple[int, int]:
    """(line index, indentation) of the key at `path`, found by indentation. Raises EditError if it is absent."""
    at, base = -1, -1
    for key in path:
        rx = re.compile(rf"^(?P<ind> *){re.escape(key)}:(?P<rest>.*)$")
        found = -1
        for i in range(at + 1, len(lines)):
            line = lines[i]
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            if at >= 0 and _indent(line) <= base:
                break
            m = rx.match(line)
            if m and ((at < 0 and _indent(line) == 0) or (at >= 0 and _indent(line) > base)):
                found = i
                break
        if found < 0:
            raise EditError(f"{'.'.join(path)} is not in the file")
        at, base = found, _indent(lines[found])
    return at, base


def _block_end(lines: list[str], at: int, base: int) -> int:
    """The index after the last line that belongs to the key at `at` (its deeper-indented lines, comments inside)."""
    end = at + 1
    for i in range(at + 1, len(lines)):
        line = lines[i]
        if not line.strip():
            continue
        if _indent(line) <= base:
            break
        end = i + 1
    return end


def insert_key(text: str, parent: list[str], key: str, value) -> str:
    """Add `key: value` as a child of the mapping at `parent` (for a setting that the file only mentions in a
    comment, or not at all). It goes after the parent's last real line, indented like its other children."""
    lines = text.split("\n")
    at, base = _find(lines, parent)
    rest = re.match(rf"^ *{re.escape(parent[-1])}:(.*)$", lines[at]).group(1).strip()
    if rest and not rest.startswith("#"):
        raise EditError(f"{'.'.join(parent)} is not a block of settings")
    end = _block_end(lines, at, base)
    child = next(
        (_indent(ln) for ln in lines[at + 1 : end] if ln.strip() and not ln.lstrip().startswith("#")),
        base + 2,
    )
    last = at
    for i in range(at + 1, end):
        if lines[i].strip() and not lines[i].lstrip().startswith("#"):
            last = i
    # skip over the continuation lines of the last child (a list under it, say)
    while last + 1 < end and lines[last + 1].strip() and _indent(lines[last + 1]) > child:
        last += 1
    lines.insert(last + 1, " " * child + f"{key}: {render_text(value)}")
    return "\n".join(lines)


def set_text(text: str, path: list[str], value) -> str:
    """Change or add one setting of any scalar type (set_path refuses free text on purpose, for the agents)."""
    try:
        return _set_any(text, path, value)
    except EditError as exc:
        if str(exc).endswith("is not in the file") and len(path) > 1:
            return insert_key(text, path[:-1], path[-1], value)
        if str(exc).endswith("is not in the file"):  # a top-level setting the file does not mention yet
            return text.rstrip("\n") + f"\n{path[0]}: {render_text(value)}\n"
        raise


def _set_any(text: str, path: list[str], value) -> str:
    lines = text.split("\n")
    inline = _parent_is_inline(lines, path, add=True) if len(path) > 1 else None
    if inline is not None:  # the value sits in a one-line { a: 1 } mapping: change just that value
        return inline(render_text(value))
    at, base = _find(lines, path)
    line = lines[at]
    m = re.match(rf"^(?P<head> *{re.escape(path[-1])}:[ \t]*)(?P<val>[^#\n]*?)(?P<tail>[ \t]*(#.*)?)$", line)
    if (
        not m
        or m.group("val").strip().startswith(("[", ">", "|"))
        or (not m.group("val").strip() and _block_end(lines, at, base) > at + 1)
    ):
        raise EditError(f"{'.'.join(path)} is not a single value")
    lines[at] = m.group("head") + render_text(value) + m.group("tail")
    return "\n".join(lines)


def _parent_is_inline(lines: list[str], path: list[str], add: bool = False):
    """When the key lives inside a one-line `{ a: 1 }` mapping, a function that replaces just that value."""
    for depth in range(len(path) - 1):
        try:
            at, _ = _find(lines, path[: depth + 1])
        except EditError:
            return None
        rest = re.match(rf"^ *{re.escape(path[depth])}:(.*)$", lines[at]).group(1).strip()
        if rest.startswith("{"):

            def go(new, at=at, rest=path[depth + 1 :], add=add):
                return _inline(lines, at, rest, new, add)

            return go
    return None


def replace_list(text: str, path: list[str], rows: list[dict], block: bool = False) -> str:
    """Rewrite the list at `path` from rows (dicts), one flow-style row per line. Comments above the key are kept;
    an empty list becomes `key: []`. The key may be absent (it is added to its parent)."""
    lines = text.split("\n")
    try:
        at, base = _find(lines, path)
    except EditError:
        if not rows:
            return text
        if len(path) == 1:  # a top-level list the file does not mention yet
            text = text.rstrip("\n") + f"\n{path[0]}: []\n"
        else:
            text = insert_key(text, path[:-1], path[-1], [])
        lines = text.split("\n")
        at, base = _find(lines, path)
    end = (
        _block_end(lines, at, base) if not re.match(rf"^ *{re.escape(path[-1])}:\s*\[", lines[at]) else at + 1
    )
    # trailing comment-only lines (an example block) stay: they were not part of the list, only indented under it
    while end > at + 1 and lines[end - 1].lstrip().startswith("#"):
        end -= 1
    head = " " * base + f"{path[-1]}:"
    if not rows:
        new = [head + " []"]
    elif block:  # one key per line, easier to read and to edit by hand
        new = [head]
        for r in rows:
            for i, (k, v) in enumerate(r.items()):
                new.append(" " * (base + 2) + ("- " if i == 0 else "  ") + f"{k}: {render_text(v)}")
    else:
        new = [head, *[" " * (base + 2) + f"- {render_row(r)}" for r in rows]]
    lines[at:end] = new
    return "\n".join(lines)


def set_flow_list(text: str, path: list[str], items: list) -> str:
    """Write a list of scalars at `path` as a one-line flow list (`key: [a, "b/24"]`), keeping the trailing comment.
    A block list under the key is replaced; an absent key is added to its parent."""
    new = "[" + ", ".join(render_text(i) for i in items) + "]"
    lines = text.split("\n")
    inline = _parent_is_inline(lines, path, add=True) if len(path) > 1 else None
    if inline is not None:  # the list sits in a one-line { a: [..] } mapping
        return inline(new)
    try:
        at, base = _find(lines, path)
    except EditError:
        return (
            insert_key(text, path[:-1], path[-1], items) if len(path) > 1 else text + f"{path[-1]}: {new}\n"
        )
    m = re.match(
        rf"^(?P<head> *{re.escape(path[-1])}:[ \t]*)(?P<val>[^#\n]*?)(?P<tail>[ \t]*(#.*)?)$", lines[at]
    )
    if not m:
        raise EditError(f"{'.'.join(path)} is not a list")
    val = m.group("val").strip()
    if val and not val.startswith("["):
        raise EditError(f"{'.'.join(path)} is not a list")
    end = at + 1
    if val.startswith("[") and "]" not in val:  # a flow list that runs onto the next lines
        for j in range(at + 1, len(lines)):
            end = j + 1
            if "]" in lines[j]:
                break
    if not val:  # a block list (or an empty value): the dash lines under the key are replaced
        end = _block_end(lines, at, base)
        while end > at + 1 and lines[end - 1].lstrip().startswith("#"):
            end -= 1
    tail = m.group("tail")
    if end > at + 1 and not tail.strip():  # a comment after the closing bracket on a later line is kept
        after = re.search(r"\][ \t]*(#.*)$", lines[end - 1])
        if after:
            tail = "   " + after.group(1)
    lines[at:end] = [m.group("head").rstrip() + " " + new + tail]
    return "\n".join(lines)


def set_folded(text: str, path: list[str], value: str, width: int = 100) -> str:
    """Write a paragraph of text at `path` as a folded block (`key: >` and indented lines), keeping what is above
    and below. Line breaks in `value` collapse to spaces, as a folded block reads them back."""
    import textwrap

    lines = text.split("\n")
    at, base = _find(lines, path)
    m = re.match(
        rf"^(?P<head> *{re.escape(path[-1])}:)[ \t]*(?P<style>[>|][-+]?)?[ \t]*(?P<rest>[^#\n]*?)[ \t]*(?P<tail>#.*)?$",
        lines[at],
    )
    if not m:
        raise EditError(f"{'.'.join(path)} is not text")
    end = _block_end(lines, at, base)
    while end > at + 1 and lines[end - 1].lstrip().startswith("#"):
        end -= 1
    body = textwrap.wrap(" ".join(value.split()), width=max(30, width - base - 2)) or [""]
    pad = " " * (base + 2)
    head = m.group("head") + " >" + (f" {m.group('tail')}" if m.group("tail") else "")
    lines[at:end] = [head, *[pad + line for line in body]]
    return "\n".join(lines)
