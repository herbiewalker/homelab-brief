"""The settings framework beyond collectors: lists, tick groups, clearable text, sections with no on/off switch."""

import yaml

from brief import settingsui, yamlscalar
from brief.settingsui import Field, Section

TEXT = """# header comment
schema: 1
daily_run_utc: "06:00"   # the daily run
weekly_day: monday
routes: [push_now]       # which routes
allowed_senders: []
ports:
  syslog: 514
  http: 80
advertised_address: ""
"""

SECTION = Section(
    "t",
    "Test",
    "x.yaml",
    (),
    (
        Field("daily_run_utc", "Daily", "text", pattern=r"^([01]\d|2[0-3]):[0-5]\d$", pattern_help="HH:MM"),
        Field("weekly_day", "Day", "choice", choices=("monday", "tuesday")),
        Field("routes", "Routes", "multi", choices=("push_now", "daily_headline")),
        Field("allowed_senders", "Allowed", "list", lines=3, pattern=r"^[0-9a-f.:/]+$", pattern_help="no"),
        Field("advertised_address", "Address", "text", clearable=True),
    ),
    switch="",
)
PORTS = Section(
    "p",
    "Ports",
    "x.yaml",
    ("ports",),
    (Field("syslog", "Syslog", "number", minimum=1, maximum=65535),),
    switch="",
)


def values_of(form, section=SECTION):
    values, errors, _ = settingsui.parse(section, form)
    assert not errors, errors
    return values


def test_a_list_and_a_tick_group_are_read_and_shown():
    state = settingsui.load(SECTION, TEXT)
    assert state["values"]["routes"] == ["push_now"] and state["values"]["allowed_senders"] == ""
    assert state["enabled"] is True  # no switch: always on


def test_a_flow_list_is_rewritten_with_its_comment_kept():
    out = yamlscalar.set_flow_list(TEXT, ["routes"], ["push_now", "daily_headline"])
    assert "routes: [push_now, daily_headline]       # which routes" in out
    assert yaml.safe_load(out)["routes"] == ["push_now", "daily_headline"]


def test_a_block_list_becomes_a_flow_list():
    text = "a:\n  items:\n    - one\n    - two\n  other: 1\n"
    out = yamlscalar.set_flow_list(text, ["a", "items"], ["x", "198.51.100.0/24"])
    assert yaml.safe_load(out) == {"a": {"items": ["x", "198.51.100.0/24"], "other": 1}}


def test_saving_changes_only_what_changed_and_keeps_comments():
    form = {
        "f.daily_run_utc": "07:15",
        "f.weekly_day": "monday",
        "f.routes": ["push_now", "daily_headline"],
        "f.allowed_senders": "198.51.100.0/24\n203.0.113.0/24",
        "f.advertised_address": "",
    }
    out = settingsui.apply(SECTION, TEXT, values_of(form))
    data = yaml.safe_load(out)
    assert data["daily_run_utc"] == "07:15" and data["routes"] == ["push_now", "daily_headline"]
    assert (
        data["allowed_senders"] == ["198.51.100.0/24", "203.0.113.0/24"] and data["advertised_address"] == ""
    )
    assert "# header comment" in out and "# the daily run" in out and "# which routes" in out


def test_clearing_a_clearable_box_writes_an_empty_string():
    text = TEXT.replace('advertised_address: ""', "advertised_address: host.example")
    form = {
        "f.daily_run_utc": "06:00",
        "f.weekly_day": "monday",
        "f.routes": ["push_now"],
        "f.advertised_address": "",
    }
    assert yaml.safe_load(settingsui.apply(SECTION, text, values_of(form)))["advertised_address"] == ""


def test_an_unticked_group_is_an_empty_list_and_a_bad_item_is_named():
    form = {"f.daily_run_utc": "06:00", "f.weekly_day": "monday", "f.allowed_senders": "not valid!"}
    values, errors, _ = settingsui.parse(SECTION, form)
    assert "not valid!" in errors["f.allowed_senders"]
    form["f.allowed_senders"] = ""
    assert values_of(form)["routes"] == []


def test_a_time_that_is_not_a_time_is_refused():
    values, errors, _ = settingsui.parse(SECTION, {"f.daily_run_utc": "25:99", "f.weekly_day": "monday"})
    assert errors["f.daily_run_utc"] == "HH:MM"


def test_a_nested_section_edits_in_place():
    out = settingsui.apply(PORTS, TEXT, values_of({"f.syslog": "5514"}, PORTS))
    assert yaml.safe_load(out)["ports"] == {"syslog": 5514, "http": 80}


def test_an_example_row_with_placeholders_in_a_list_is_not_shown():
    from brief import settingspages

    text = "schema: 1\nsystems:\n  - { id: gw, label: Gateway, hosts: [<udm-hostname>], source_tz: UTC, expected: true }\n"
    assert settingsui.load(settingspages.SYSTEMS, text)["rows"]["systems"] == []
    real = text.replace("<udm-hostname>", "198.51.100.1")
    assert settingsui.load(settingspages.SYSTEMS, real)["rows"]["systems"][0]["hosts"] == "198.51.100.1"


def test_a_list_inside_a_one_line_mapping_and_a_list_over_two_lines_are_rewritten():
    text = "tasks:\n  a: { chain: [local, template], prompt: p.md }\nagents:\n  x:\n    tools: [one, two,\n            three]   # keep\n    other: 1\n"
    out = yamlscalar.set_flow_list(text, ["tasks", "a", "chain"], ["cloud", "template"])
    out = yamlscalar.set_flow_list(out, ["agents", "x", "tools"], ["one", "four"])
    data = yaml.safe_load(out)
    assert data["tasks"]["a"] == {"chain": ["cloud", "template"], "prompt": "p.md"}
    assert data["agents"]["x"] == {"tools": ["one", "four"], "other": 1} and "# keep" in out


def test_rows_can_be_written_one_key_per_line():
    out = yamlscalar.replace_list(
        "# top\nproviders:\n  - { id: old }\nafter: 1\n",
        ["providers"],
        [{"id": "a", "model": "m x"}, {"id": "b"}],
        block=True,
    )
    assert (
        yaml.safe_load(out) == {"providers": [{"id": "a", "model": "m x"}, {"id": "b"}], "after": 1}
        and "# top" in out
    )
    assert "    model: " in out  # a block, not a flow
