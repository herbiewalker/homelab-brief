"""The static views: brief.html, board.html (tablets), and status.png (the picture board).

Plain files, no JavaScript, no external requests, and no headless browser: HTML from Jinja2 templates
with inline CSS, and the PNG drawn with Pillow. They show exactly what the digest says. A failed run
looks failed (a red "No data today" board), never calm. Sizes and the refresh rate come from
brief/views.yaml.
"""

import io
import os
from collections.abc import Mapping
from pathlib import Path

import jinja2
from PIL import Image, ImageDraw, ImageFont

from brief.config import ConfigError, config_file, load_mapping
from brief.digest import Digest, SystemState, generated_at
from brief.jsonlog import log
from brief.render_md import _pipeline

HERE = Path(__file__).parent
SYSTEM_MONO = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"  # from the fonts-dejavu-core package
DEFAULTS = {
    "png": {"width": 1024, "height": 600, "font": ""},
    "board": {"refresh_s": 300, "stories": 3},
}
COLORS = {
    "green": (46, 125, 50),
    "yellow": (249, 168, 37),
    "red": (198, 40, 40),
    "unknown": (97, 97, 97),
}
INK = {"green": (255, 255, 255), "yellow": (17, 17, 17), "red": (255, 255, 255), "unknown": (255, 255, 255)}
BACKGROUND, PANEL, TEXT, MUTED = (17, 17, 17), (27, 27, 27), (230, 230, 230), (154, 154, 154)
_env = jinja2.Environment(loader=jinja2.FileSystemLoader(HERE / "templates"), autoescape=True)


def load_views(env: Mapping[str, str] | None = None) -> dict:
    """views.yaml over the defaults. A bad file keeps the defaults (and says so), never stops a run."""
    views = {k: dict(v) for k, v in DEFAULTS.items()}
    try:
        data = load_mapping(config_file("views.yaml", env))
    except (ConfigError, OSError) as exc:
        log("config_error", file="views.yaml", error=str(exc), kept="defaults")
        return views
    for section, values in views.items():
        given = data.get(section)
        if isinstance(given, dict):
            for key, default in values.items():
                if isinstance(given.get(key), type(default)) and not isinstance(given.get(key), bool):
                    values[key] = given[key]
    views["png"]["width"] = min(max(int(views["png"]["width"]), 320), 3840)
    views["png"]["height"] = min(max(int(views["png"]["height"]), 240), 2160)
    views["board"]["refresh_s"] = max(int(views["board"]["refresh_s"]), 0)
    views["board"]["stories"] = min(max(int(views["board"]["stories"]), 0), 10)
    return views


def overall(d: Digest) -> str:
    """One word for the whole lab: failed, red, yellow, green, or unknown."""
    if not d.run_ok:
        return "failed"
    states = {s.state for s in d.systems}
    for state in ("red", "yellow"):
        if state in states:
            return state
    return "unknown" if states == {"unknown"} else "green"


def _context(d: Digest, day: str, prose=None) -> dict:
    stories = [
        {
            "title": s.title,
            "system": s.system,
            "route": s.route,
            "rank": s.rank,
            "grouped": s.grouped,
            "top": list(s.top),
            "more": max(len(s.members) - len(s.top), 0) if s.grouped else 0,
        }
        for s in d.stories
    ]
    return {
        "day": day,
        "generated": generated_at(d),
        "run_ok": d.run_ok,
        "overall": overall(d),
        "headline": d.headline,
        "problems": d.problems,
        "systems": d.systems,
        "stories": stories,
        "developing": d.developing,
        "quiet": d.quiet,
        "pipeline": [line.removeprefix("- ") for line in _pipeline(d)] if d.pipeline else [],
        "prose": prose,
        "refresh": 0,
    }


def brief_html(d: Digest, day: str, prose=None) -> str:
    return _env.get_template("view_brief.html").render(**_context(d, day, prose))


def board_html(d: Digest, day: str, views: dict | None = None) -> str:
    views = views or load_views()
    ctx = _context(d, day)
    ctx["refresh"] = views["board"]["refresh_s"]
    ctx["stories"] = ctx["stories"][: views["board"]["stories"]]
    return _env.get_template("view_board.html").render(**ctx)


# ---------------------------------------------------------------- the picture board


def _font(size: int, path: str = "") -> ImageFont.ImageFont:
    """The font from views.yaml, else the monospace one the image ships, else Pillow's built-in."""
    if path:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            log("config_error", file="views.yaml", error=f"font not readable: {path}", kept="default font")
    try:
        return ImageFont.truetype(SYSTEM_MONO, size)
    except OSError:
        return ImageFont.load_default(size)


def _fit(draw: ImageDraw.ImageDraw, text: str, font, width: int) -> str:
    """The text cut with an ellipsis so it is never wider than `width` pixels."""
    if draw.textlength(text, font=font) <= width:
        return text
    while text and draw.textlength(text + "...", font=font) > width:
        text = text[:-1]
    return text.rstrip() + "..."


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, width: int, lines: int) -> list[str]:
    out: list[str] = []
    line = ""
    for word in text.split():
        trial = f"{line} {word}".strip()
        if draw.textlength(trial, font=font) <= width or not line:
            line = trial
        else:
            out.append(line)
            line = word
    out.append(line)
    if len(out) > lines:
        out = [*out[: lines - 1], _fit(draw, " ".join(out[lines - 1 :]) + "  ", font, width)]
    return [_fit(draw, x, font, width) for x in out]


def status_png(d: Digest, views: dict | None = None) -> bytes:
    """The status board as a PNG: headline bar, one tile per system, the top stories, and the time."""
    views = views or load_views()
    w, h = views["png"]["width"], views["png"]["height"]
    path = views["png"]["font"]
    unit = max(h // 30, 12)
    big, mid, small = _font(unit * 2, path), _font(int(unit * 1.2), path), _font(unit, path)
    img = Image.new("RGB", (w, h), BACKGROUND)
    draw = ImageDraw.Draw(img)
    pad = unit
    state = overall(d)
    bar = COLORS["red" if state == "failed" else state]
    draw.rectangle((0, 0, w, int(unit * 6)), fill=bar)
    ink = INK["red" if state == "failed" else state]
    head = "NO DATA TODAY" if state == "failed" else d.headline
    y = pad
    for line in _wrap(draw, head, big if state == "failed" else mid, w - 2 * pad, 3):
        draw.text((pad, y), line, fill=ink, font=big if state == "failed" else mid)
        y += int(unit * 1.9) if state == "failed" else int(unit * 1.5)
    y = int(unit * 6) + pad
    systems = list(d.systems)
    if systems:
        cols = min(len(systems), max(w // (unit * 14), 1))
        room = int(h * 0.45)
        min_tile = int(unit * 2.2)
        capacity = cols * max((room - 6) // (min_tile + 6), 1)
        if len(systems) > capacity:  # too many for the screen: worst first, and say how many are left
            systems.sort(key=lambda s: ("red", "yellow", "unknown", "green").index(s.state))
            hidden = len(systems) - (capacity - 1)
            systems = [*systems[: capacity - 1], SystemState("more", f"+{hidden} more", "unknown", "")]
        rows = -(-len(systems) // cols)
        tile_h = min(int(unit * 4), room // rows - 6)
        tile_w = (w - 2 * pad - (cols - 1) * 8) // cols
        for i, s in enumerate(systems):
            cx, cy = pad + (i % cols) * (tile_w + 8), y + (i // cols) * (tile_h + 6)
            draw.rectangle((cx, cy, cx + tile_w, cy + tile_h), fill=COLORS[s.state])
            draw.text((cx + 8, cy + 4), _fit(draw, s.name, mid, tile_w - 16), fill=INK[s.state], font=mid)
            if tile_h >= unit * 3 and s.id != "more":
                draw.text(
                    (cx + 8, cy + 4 + int(unit * 1.5)),
                    _fit(draw, s.state.upper(), small, tile_w - 16),
                    fill=INK[s.state],
                    font=small,
                )
        y += rows * (tile_h + 6) + pad
    for i, s in enumerate(d.stories[: views["board"]["stories"]], 1):
        if y + unit * 1.6 > h - unit * 2:
            break
        mark = (198, 40, 40) if s.route == "push_now" else MUTED
        draw.rectangle((pad, y + 2, pad + 6, y + unit), fill=mark)
        draw.text(
            (pad + 16, y), _fit(draw, f"{i}. {s.title}", small, w - 2 * pad - 16), fill=TEXT, font=small
        )
        y += int(unit * 1.6)
    if d.run_ok and not d.stories:
        draw.text((pad, y), "Nothing needs a look.", fill=TEXT, font=small)
    stamp = f"Updated {generated_at(d)} UTC"
    draw.text((pad, h - int(unit * 1.8)), stamp, fill=MUTED, font=small)
    out = io.BytesIO()
    img.save(out, "PNG", optimize=True)
    return out.getvalue()


# ---------------------------------------------------------------- writing


def _atomic_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def write_views(
    d: Digest, out: Path, day: str, prose=None, env: Mapping[str, str] | None = None, only: tuple = ()
) -> None:
    """Write brief.html, board.html and status.png into latest/ and the dated folder. A view that
    fails to render is logged and skipped, so one broken view never stops the others or the run."""
    views = load_views(env)
    made = {
        "brief.html": lambda: brief_html(d, day, prose).encode("utf-8"),
        "board.html": lambda: board_html(d, day, views).encode("utf-8"),
        "status.png": lambda: status_png(d, views),
    }
    for name, make in made.items():
        if only and name not in only:
            continue
        try:
            data = make()
        except Exception as exc:
            log("render_failed", view=name, error=f"{type(exc).__name__}: {exc}")
            continue
        _atomic_bytes(out / "latest" / name, data)
        if name != "board.html":
            _atomic_bytes(out / day / name, data)
