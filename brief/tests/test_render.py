"""The static views: brief.html, board.html and status.png, from the golden day and from a failed run."""

import io

import golden_day as g
import pytest
from PIL import Image

from brief import outputs, render
from brief.store import Store


@pytest.fixture
def day(tmp_path):
    store, digest, out = g.build_daily(tmp_path)
    yield store, digest, out
    store.close()


@pytest.fixture
def failed(tmp_path):
    store = Store(tmp_path / "empty" / "baseline.db")
    out = tmp_path / "out-failed"
    try:
        digest = outputs.run_daily(store, g.SYSTEMS, g.RUN_AT, out, state_dir=tmp_path)
    finally:
        store.close()
    return digest, out


def test_a_daily_run_writes_the_three_views(day):
    _, _, out = day
    for path in (
        out / "latest" / "brief.html",
        out / "latest" / "board.html",
        out / "latest" / "status.png",
        out / "2026-09-20" / "brief.html",
        out / "2026-09-20" / "status.png",
    ):
        assert path.exists() and path.stat().st_size > 0, path
    assert (out / "latest" / "brief.html").read_bytes() == (out / "2026-09-20" / "brief.html").read_bytes()
    assert not list(out.rglob("*.tmp"))


def test_the_pages_have_no_script_and_no_outside_requests(day):
    _, _, out = day
    for name in ("brief.html", "board.html"):
        html = (out / "latest" / name).read_text(encoding="utf-8").lower()
        for banned in ("<script", "http://", "https://", "<link", "<iframe", "<img", "onclick", "@import"):
            assert banned not in html, (name, banned)


def test_the_brief_shows_the_headline_systems_and_stories(day):
    _, digest, out = day
    html = (out / "latest" / "brief.html").read_text(encoding="utf-8")
    assert digest.headline in html
    assert "Homelab brief for 2026-09-20" in html
    assert "Camera" in html and "198.51.100.3 has been silent for 2 days" in html
    assert "tile red" in html and "Run ok." in html


def test_the_board_lists_only_the_configured_number_of_stories(day):
    _, digest, _ = day
    views = render.load_views()
    views["board"]["stories"] = 2
    html = render.board_html(digest, "2026-09-20", views)
    assert html.count('class="story') == 2
    assert 'http-equiv="refresh" content="300"' in html
    views["board"]["refresh_s"] = 0
    assert "http-equiv" not in render.board_html(digest, "2026-09-20", views)


def test_text_from_the_logs_is_escaped_in_the_pages(day):
    _, digest, _ = day
    digest.stories[0] = digest.stories[0].__class__(
        **{**digest.stories[0].__dict__, "title": "<script>alert(1)</script>"}
    )
    for html in (render.brief_html(digest, "d"), render.board_html(digest, "d")):
        assert "<script>" not in html and "&lt;script&gt;" in html


def test_the_png_is_the_configured_size(day):
    _, digest, _ = day
    views = render.load_views()
    views["png"].update(width=800, height=480)
    image = Image.open(io.BytesIO(render.status_png(digest, views)))
    assert image.format == "PNG" and image.size == (800, 480)


def test_the_png_shows_each_state_in_its_own_colour(day):
    _, digest, _ = day
    image = Image.open(io.BytesIO(render.status_png(digest))).convert("RGB")
    colours = {rgb for _, rgb in image.getcolors(maxcolors=100000)}
    assert render.COLORS["red"] in colours and render.COLORS["yellow"] in colours


def test_many_systems_and_a_tiny_screen_still_draw(day):
    _, digest, _ = day
    digest.systems = [
        digest.systems[0].__class__(f"s{i}", f"System number {i}", "green", "x") for i in range(40)
    ]
    views = render.load_views()
    views["png"].update(width=320, height=240)
    assert Image.open(io.BytesIO(render.status_png(digest, views))).size == (320, 240)


def test_a_failed_run_looks_failed_in_every_view(failed):
    digest, out = failed
    assert not digest.run_ok and render.overall(digest) == "failed"
    brief = (out / "latest" / "brief.html").read_text(encoding="utf-8")
    board = (out / "latest" / "board.html").read_text(encoding="utf-8")
    for html in (brief, board):
        assert "No data today" in html and "headline failed" in html
        assert "Quiet" not in html.replace("Quiet:", "") or "tile green" not in html
        assert "Nothing needs a look" not in html and "tile green" not in html
    assert "RUN FAILED" in brief
    image = Image.open(io.BytesIO((out / "latest" / "status.png").read_bytes())).convert("RGB")
    colours = {rgb for _, rgb in image.getcolors(maxcolors=100000)}
    assert render.COLORS["red"] in colours and render.COLORS["green"] not in colours


def test_a_view_that_cannot_render_does_not_stop_the_others(day, monkeypatch, tmp_path):
    _, digest, _ = day
    monkeypatch.setattr(render, "status_png", lambda *a, **k: 1 / 0)
    out = tmp_path / "partial"
    render.write_views(digest, out, "2026-09-20")
    assert (out / "latest" / "brief.html").exists() and (out / "latest" / "board.html").exists()
    assert not (out / "latest" / "status.png").exists()


def test_views_yaml_is_bounded_and_a_bad_file_keeps_the_defaults(tmp_path):
    folder = tmp_path / "cfg" / "brief"
    folder.mkdir(parents=True)
    env = {"BRIEF_CONFIG_DIR": str(tmp_path / "cfg"), "BRIEF_DEFAULTS_DIR": str(tmp_path / "none")}
    (folder / "views.yaml").write_text("schema: 1\npng: {width: 99999, height: 1}\nboard: {refresh_s: -4}\n")
    views = render.load_views(env)
    assert (
        views["png"]["width"] == 3840 and views["png"]["height"] == 240 and views["board"]["refresh_s"] == 0
    )
    (folder / "views.yaml").write_text("schema: 9\n")
    assert render.load_views(env) == render.load_views({"BRIEF_DEFAULTS_DIR": str(tmp_path / "none")})


def test_a_missing_font_falls_back_to_the_built_in_one(day):
    _, digest, _ = day
    views = render.load_views()
    views["png"]["font"] = "/nonexistent/font.ttf"
    assert render.status_png(digest, views)[:4] == b"\x89PNG"


def test_the_views_are_served_read_only_over_http(day, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from brief import health, web
    from brief.settings import Settings

    monkeypatch.setattr(health, "receiver_ok", lambda port: True)
    _, _, out = day
    settings = Settings(state_dir=tmp_path / "state", out_dir=out, announce_setup=False)
    (tmp_path / "state").mkdir()
    client = TestClient(web.create_app("http", settings), base_url="http://testserver")
    page = client.get("/brief.html")
    assert page.status_code == 200 and page.headers["content-type"].startswith("text/html")
    assert (
        page.headers["cache-control"] == "no-cache"
        and "script-src" not in page.headers["content-security-policy"]
    )
    assert "style-src 'unsafe-inline'" in page.headers["content-security-policy"]
    assert client.get("/board.html").status_code == 200
    png = client.get("/status.png")
    assert png.headers["content-type"] == "image/png" and png.content[:4] == b"\x89PNG"
    assert client.post("/brief.html").status_code == 405
    assert client.get("/..%2fstate.json").status_code == 404
    assert (
        client.get("/status.json")
        .headers["content-security-policy"]
        .startswith("default-src 'none'; style-src 'self'")
    )
    (out / "latest" / "status.png").unlink()
    assert client.get("/status.png").status_code == 404
