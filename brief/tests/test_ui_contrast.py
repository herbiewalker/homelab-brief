"""Colour contrast of the interface tokens (Task 37): every text colour on every background it is used on, in the light
theme and in both ways of getting the dark one, against WCAG 2.1 AA (4.5:1 for text, 3:1 for large text and parts of
controls). The tokens are read from the stylesheet itself, so a change that breaks contrast fails here.
"""

import re
from pathlib import Path

import pytest

CSS = (Path(__file__).resolve().parents[1] / "src" / "brief" / "static" / "ui.css").read_text(
    encoding="utf-8"
)


def tokens(block: str) -> dict[str, str]:
    return dict(re.findall(r"--([a-z0-9-]+):\s*(#[0-9a-fA-F]{6})\b", block))


def themes() -> dict[str, dict[str, str]]:
    light = tokens(CSS.split(":root {", 1)[1].split("}", 1)[0])
    media = CSS.split("@media (prefers-color-scheme: dark) {", 1)[1].split("}", 1)[0]
    dark_system = {**light, **tokens(media)}
    explicit = CSS.split(':root[data-theme="dark"] {', 1)[1].split("}", 1)[0]
    dark_chosen = {**light, **tokens(explicit)}
    return {"light": light, "dark (system)": dark_system, "dark (chosen)": dark_chosen}


def luminance(hex_colour: str) -> float:
    def channel(v: int) -> float:
        c = v / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (int(hex_colour[i : i + 2], 16) for i in (1, 3, 5))
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)


def contrast(a: str, b: str) -> float:
    la, lb = sorted((luminance(a), luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


# (text token, background token, minimum): text used on each surface
TEXT_ON = (
    ("fg", "bg", 4.5), ("fg", "surface", 4.5), ("fg", "surface-2", 4.5),
    ("muted", "bg", 4.5), ("muted", "surface", 4.5), ("muted", "surface-2", 4.5),
    ("subtle", "bg", 4.5), ("subtle", "surface", 4.5), ("subtle", "surface-2", 4.5),
    ("accent", "bg", 4.5), ("accent", "surface", 4.5), ("accent", "surface-2", 4.5), ("accent-hover", "surface", 4.5),
    ("accent", "accent-soft", 4.5),
    ("accent-fg", "accent", 4.5),
    ("ok", "surface", 4.5), ("ok", "ok-soft", 4.5), ("ok", "bg", 4.5),
    ("warn", "surface", 4.5), ("warn", "warn-soft", 4.5), ("warn", "bg", 4.5),
    ("bad", "surface", 4.5), ("bad", "bad-soft", 4.5), ("bad", "bg", 4.5),
    ("info", "surface", 4.5), ("info", "info-soft", 4.5),
    ("fg", "code-bg", 4.5), ("muted", "code-bg", 4.5),
)  # fmt: skip
PARTS = (  # borders and switches that must be visible against the page (WCAG 1.4.11)
    ("control", "surface", 3.0),
    ("control", "bg", 3.0),
    ("accent", "surface", 3.0),
)


@pytest.mark.parametrize("theme", list(themes()))
@pytest.mark.parametrize("fg, bg, minimum", TEXT_ON)
def test_text_colours_are_readable_on_their_backgrounds(theme, fg, bg, minimum):
    palette = themes()[theme]
    ratio = contrast(palette[fg], palette[bg])
    assert ratio >= minimum, (
        f"{theme}: {fg} {palette[fg]} on {bg} {palette[bg]} is {ratio:.2f}:1, needs {minimum}"
    )


@pytest.mark.parametrize("theme", list(themes()))
@pytest.mark.parametrize("fg, bg, minimum", PARTS)
def test_control_edges_and_accents_are_visible(theme, fg, bg, minimum):
    palette = themes()[theme]
    assert contrast(palette[fg], palette[bg]) >= minimum, f"{theme}: {fg} on {bg}"


@pytest.mark.parametrize("theme", list(themes()))
def test_a_danger_button_and_a_status_badge_have_readable_text(theme):
    """The danger button writes white text on the bad colour; a badge writes the status colour on its soft tint."""
    palette = themes()[theme]
    assert contrast(palette["danger-fg"], palette["danger"]) >= 4.5, f"{theme}: danger button text"


def test_the_three_themes_define_the_same_tokens():
    found = themes()
    names = set(found["light"])
    assert names <= set(found["dark (chosen)"]) and set(found["dark (chosen)"]) == set(found["dark (system)"])
