import json

import pytest

from roj.core.cas import CAS, CorruptBlob


def test_chain_verifies(project):
    for i in range(5):
        project.events.append("x", {"i": i})
    r = project.events.verify()
    assert r.ok and r.count >= 5


def test_edited_event_is_detected(project):
    for i in range(5):
        project.events.append("x", {"i": i})
    seq = project.events.query(type="x")[2].seq
    project.store.run("UPDATE events SET payload = ? WHERE seq = ?", ('{"i":99}', seq))
    r = project.events.verify()
    assert not r.ok and r.first_bad_seq == seq and r.reason == "content changed"


def test_deleted_event_is_detected(project):
    for i in range(5):
        project.events.append("x", {"i": i})
    seq = project.events.query(type="x")[1].seq
    project.store.run("DELETE FROM events WHERE seq = ?", (seq,))
    r = project.events.verify()
    assert not r.ok and r.first_bad_seq == seq


def test_secrets_are_masked_but_token_counts_kept(project):
    project.events.append("x", {"api_key": "abc", "headers": {"Authorization": "Bearer xyzxyzxyzxyzxyzxyzxyz"},
                                "note": "key sk-ABCDEFGHIJKLMNOPQRSTUV leaked", "prompt_tokens": 12})
    row = project.store.one("SELECT payload FROM events WHERE type = 'x'")
    stored = json.loads(row["payload"])
    assert stored["api_key"] == "***"
    assert stored["headers"]["Authorization"] == "***"
    assert "sk-ABC" not in stored["note"]
    assert stored["prompt_tokens"] == 12


def test_cas_roundtrip_and_dedup(tmp_path):
    cas = CAS(tmp_path / "cas")
    h1 = cas.put_json({"b": 2, "a": 1})
    h2 = cas.put_json({"a": 1, "b": 2})
    assert h1 == h2  # canonical JSON
    assert cas.get_json(h1) == {"a": 1, "b": 2}


def test_cas_detects_corruption(tmp_path):
    cas = CAS(tmp_path / "cas")
    h = cas.put_text("hello")
    path = cas._path(h)
    path.write_text("tampered")
    with pytest.raises(CorruptBlob):
        cas.get_text(h)
