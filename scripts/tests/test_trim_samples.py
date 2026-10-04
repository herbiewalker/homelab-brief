import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import trim_samples as t  # noqa: E402

LINES = [
    "Sep 26 13:00:01 host-01 emhttpd: read SMART /dev/disk1",
    "Sep 26 13:05:02 host-01 emhttpd: read SMART /dev/disk2",
    "Sep 26 13:10:03 host-01 emhttpd: read SMART /dev/disk3",
    "Sep 26 13:11:00 host-01 mover: started",
    "Sep 26 13:12:00 host-01 docker: updating radarr now",
    "Sep 26 13:13:00 host-01 mcad: authkey: [REDACTED]",
    "Sep 26 13:14:00 host-01 kernel: link 00:00:5e:00:53:01 up",
]
EXCLUDE = [re.compile(r"(?<![A-Za-z0-9])radarr(?![A-Za-z0-9])", re.IGNORECASE)]


def run(**kw):
    return t.trim(LINES, EXCLUDE, [], set(), **kw)


def test_repeats_of_one_message_shape_are_capped():
    kept = run(per_shape=2)
    assert [ln for ln in kept if "read SMART" in ln] == LINES[:2]


def test_excluded_words_and_redactions_are_dropped():
    kept = "\n".join(run())
    assert "radarr" not in kept and "[REDACTED]" not in kept


def test_other_shapes_survive_in_order():
    kept = run()
    assert kept == [LINES[0], LINES[1], LINES[3], LINES[6]]


def test_the_line_cap_applies():
    assert len(run(max_lines=2)) == 2


def test_guard_findings_drop_a_line():
    private = t.guard._compile({"mover"})
    assert t.trim(LINES, EXCLUDE, private, set()) == [LINES[0], LINES[1], LINES[6]]
