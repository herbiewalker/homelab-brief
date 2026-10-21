"""The simple / full side menu, the Settings page, and the glossary tips."""

import re

from test_admin import lab  # noqa: F401  (the fixture)

from brief import admin, glossary


def links(html: str, block: str) -> list[str]:
    inner = html.split(f'<div class="{block}">', 1)[1]
    inner = inner.split(
        '<div class="nav-simple">' if block == "nav-full" else '<div class="sidebar-foot">', 1
    )[0]
    return re.findall(r'<a class="nav-link" href="([^"]+)"', inner)


def test_the_simple_menu_is_six_pages_and_the_full_menu_is_still_there(lab):  # noqa: F811
    html = lab.client.get("/admin").text
    assert len(links(html, "nav-simple")) == 6
    full = links(html, "nav-full")
    assert "/admin/collectors" in full and "/admin/audit" in full
    assert "/admin/settings" not in full  # the hub is only reached from the simple menu
    assert 'data-action="nav-level"' in html


def test_every_page_of_the_full_menu_is_reachable_from_the_settings_page(lab):  # noqa: F811
    html = lab.client.get("/admin/settings").text
    reachable = set(re.findall(r'class="hub-link" href="([^"]+)"', html))
    for key, href, _label in admin.NAV:
        if key not in admin.SIMPLE_KEYS and key != "help":
            assert href in reachable, key
    assert 'aria-current="page"' in html.split('<div class="nav-simple">', 1)[1]


def test_a_page_outside_the_simple_menu_marks_settings_as_current(lab):  # noqa: F811
    html = lab.client.get("/admin/audit").text
    simple = html.split('<div class="nav-simple">', 1)[1].split('<div class="sidebar-foot">', 1)[0]
    assert re.search(r'href="/admin/settings" aria-current="page"', simple)


def test_every_page_has_a_line_saying_what_it_is_for():
    for key, _href, _label in admin.NAV:
        if key not in admin.SIMPLE_KEYS:
            assert glossary.PAGES.get(key), key


def test_the_overview_tiles_carry_their_tips_and_the_help_page_lists_the_words(lab):  # noqa: F811
    html = lab.client.get("/admin").text
    assert glossary.kpi_tip("Pipeline") in html.replace("&#39;", "'")
    assert 'class="tip"' in html and 'aria-label="What is system?"' in html
    page = lab.client.get("/admin/help").text
    for term, _text in glossary.help_terms():
        assert term in page
