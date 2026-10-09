"""Change one scalar in a YAML file's text and leave everything else (comments, order, spacing) alone.

The config files are hand-written, commented, and use both block style and one-line `{ a: 1, b: 2 }` mappings, so
they are edited as text, line by line, never re-dumped. `set_path(text, ["thresholds", "deviation", "min_ratio"], 4)`
finds the key by indentation (or inside the one-line mapping that holds it) and replaces only its value. The caller
checks the result with the file's own validator; this module never guesses beyond the shape it was given.
"""

import re

SAFE_WORD = re.compile(r"^[a-z_][a-z0-9_]{0,30}$")


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


def _inline(lines: list[str], i: int, rest: list[str], new: str) -> str:
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
    m = re.search(rf"(?P<head>[{{,]\s*{re.escape(key)}:\s*)(?P<val>[^,}}\s]+)", segment)
    if not m:
        raise EditError(f"{'.'.join(rest)} is not in that line")
    a, b = start + m.start("val"), start + m.end("val")
    lines[i] = line[:a] + new + line[b:]
    return "\n".join(lines)
