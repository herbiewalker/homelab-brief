"""The operations-console pieces (Task 38): trend lines, the overall-state banner, and the pages that use them."""

import pytest
from test_admin import lab  # noqa: F401  (the fixture)

from brief import publicstatus, sparkline


def status(**kw):
    base = {"run_ok": True, "systems": [], "pipeline": {}, "collectors": {}, "headline": "All quiet"}
    base.update(kw)
    return base


# ---------------------------------------------------------------- sparkline


def test_a_series_becomes_a_line_and_an_area():
    line, area = sparkline.points([1, 3, 2, 5])
    assert len(line.split()) == 4 and area.startswith("0,32.0") and area.endswith("120.0,32.0")


def test_a_flat_series_is_a_flat_line_in_the_middle():
    line, _ = sparkline.points([4, 4, 4])
    assert {p.split(",")[1] for p in line.split()} == {"16.0"}


@pytest.mark.parametrize("values", [[], [3], [None, None]])
def test_too_few_points_draw_nothing(values):
    assert sparkline.points(values) is None


def test_the_highest_value_is_at_the_top():
    line, _ = sparkline.points([0, 10])
    first, last = (float(p.split(",")[1]) for p in line.split())
    assert last < first  # SVG y grows downward


def test_a_meter_is_clamped_and_safe():
    assert sparkline.meter_pct(50, 200) == 25
    assert sparkline.meter_pct(500, 200) == 100
    assert sparkline.meter_pct(None, 200) == 0 and sparkline.meter_pct(5, 0) == 0


# ---------------------------------------------------------------- the overall state


def test_all_green_and_a_good_run_is_operational():
    s = status(systems=[{"id": "a", "state": "green"}])
    assert publicstatus.overall(s)["kind"] == "ok"


def test_a_failed_run_is_never_operational():
    s = status(run_ok=False, systems=[{"id": "a", "state": "green"}])
    b = publicstatus.overall(s)
    assert b["kind"] == "bad" and "No data" in b["title"]


def test_a_red_system_is_down_and_outranks_yellow():
    s = status(systems=[{"id": "a", "state": "yellow"}, {"id": "b", "state": "red"}])
    b = publicstatus.overall(s)
    assert b["kind"] == "bad" and b["title"] == "1 system down"


def test_silent_hosts_or_failing_collectors_make_it_a_warning():
    assert publicstatus.overall(status(pipeline={"hosts_silent": 2}))["kind"] == "warn"
    failing = {"unraid": {"ok": False}}
    assert publicstatus.overall(status(collectors=failing))["kind"] == "warn"


# ---------------------------------------------------------------- the pages


def test_the_overview_is_a_console(lab):  # noqa: F811
    html = lab.client.get("/admin").text
    for part in ('class="banner', 'class="kpis"', 'class="components"', "Incident timeline", 'class="live"'):
        assert part in html, part
    assert "30 days ago" in html  # the status bars are there even with no history yet


def test_the_public_status_page_has_the_banner_and_the_bars_and_no_script(lab):  # noqa: F811
    html = lab.http().get("/").text
    assert 'class="banner' in html and 'class="bar"' in html
    assert "<script" not in html.lower() and "/admin" not in html


def test_dark_is_the_default_theme_and_the_script_agrees():
    from pathlib import Path

    static = Path(sparkline.__file__).parent / "static"
    assert 'var t = "dark"' in (static / "theme.js").read_text(encoding="utf-8")
    css = (static / "ui.css").read_text(encoding="utf-8")
    assert css.split(":root {", 1)[1].split("}", 1)[0].count("color-scheme: dark") == 1


def test_the_container_serves_its_own_icon_and_the_pages_link_a_favicon(lab):  # noqa: F811
    icon = lab.http().get("/static/icon.png")
    assert icon.status_code == 200 and icon.content.startswith(b"\x89PNG") and len(icon.content) > 5000
    assert 'rel="icon"' in lab.http().get("/").text
