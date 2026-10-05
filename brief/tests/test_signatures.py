from pathlib import Path

from brief.signatures import Signatures

BRIEF = Path(__file__).resolve().parents[1]


def open_sigs(tmp_path, **kw):
    return Signatures(tmp_path / "drain3.bin", BRIEF / "drain3.ini", **kw)


def test_numbers_ips_and_macs_collapse_into_one_signature(tmp_path):
    s = open_sigs(tmp_path)
    a = s.classify("dhcp", "lease of 198.51.100.7 obtained, lease time 86400")
    b = s.classify("dhcp", "lease of 203.0.113.9 obtained, lease time 3600")
    c = s.classify("net", "link 00:00:5e:00:53:01 is up")
    d = s.classify("net", "link 00:00:5e:00:53:99 is up")
    assert a.cluster_id == b.cluster_id and c.cluster_id == d.cluster_id and a.cluster_id != c.cluster_id
    assert "<IP>" in a.template and "<MAC>" in c.template


def test_the_app_prefix_is_not_part_of_the_stored_template(tmp_path):
    t = open_sigs(tmp_path).classify("sshd", "Server listening on port 22").template
    assert not t.startswith("sshd:") and t.startswith("Server listening")


def test_the_same_text_from_two_apps_is_two_clusters(tmp_path):
    s = open_sigs(tmp_path)
    assert s.classify("one", "it broke").cluster_id != s.classify("two", "it broke").cluster_id


def test_an_untagged_message_is_still_mined(tmp_path):
    assert open_sigs(tmp_path).classify("", "firewall DROP from 198.51.100.7").template.startswith("firewall")


def test_state_survives_a_restart_so_ids_stay_the_same(tmp_path):
    miner = open_sigs(tmp_path)
    first = miner.classify("app", "something happened to 7 things")
    miner.save()
    again = open_sigs(tmp_path)
    assert again.state_existed and not again.recovered
    assert again.classify("app", "something happened to 9 things").cluster_id == first.cluster_id


def test_the_exact_match_cache_skips_the_miner_and_is_bounded(tmp_path):
    s = open_sigs(tmp_path, cache_size=3)
    first = s.classify("a", "the same line")
    assert s.classify("a", "the same line") is first
    for i in range(10):
        s.classify("a", f"distinct line {i} alpha beta {i * 'x'}")
    assert len(s._cache) <= 3


def test_a_corrupt_state_file_is_set_aside(tmp_path):
    (tmp_path / "drain3.bin").write_bytes(b"garbage")
    s = open_sigs(tmp_path)
    assert s.recovered and not s.state_existed
    assert list(tmp_path.glob("drain3.bin.corrupt-*"))
    assert s.classify("a", "works again").cluster_id >= 1


def test_very_long_lines_are_trimmed_before_mining(tmp_path):
    s = open_sigs(tmp_path)
    assert s.classify("a", "word " * 5000).template
    assert s.cluster_count == 1


def test_the_state_is_written_only_when_save_is_called_and_only_if_something_changed(tmp_path, monkeypatch):
    s = open_sigs(tmp_path)
    writes = []
    real = type(s.miner).__mro__[1].save_state  # drain3's own save_state
    monkeypatch.setattr(
        type(s.miner).__mro__[1],
        "save_state",
        lambda self, reason: (writes.append(reason), real(self, reason)),
    )
    for i in range(50):
        s.classify("app", f"unique shape number {i} " + "word " * (i % 7) + f"tail{i}")
    assert writes == [] and not (tmp_path / "drain3.bin").exists()  # nothing written per message
    assert s.save() is True and writes == ["end of cycle"] and (tmp_path / "drain3.bin").exists()
    assert s.save() is False and len(writes) == 1  # nothing changed since


def test_a_periodic_snapshot_request_does_not_force_a_save(tmp_path):
    s = open_sigs(tmp_path)
    s.miner.save_state("periodic")
    assert s.save() is False


def test_a_saved_state_reloads_with_the_same_clusters(tmp_path):
    s = open_sigs(tmp_path)
    ids = {s.classify("app", f"event kind{i} happened on host{i}").cluster_id for i in range(5)}
    s.save()
    again = open_sigs(tmp_path)
    assert again.cluster_count == s.cluster_count
    assert again.classify("app", "event kind3 happened on host3").cluster_id in ids
