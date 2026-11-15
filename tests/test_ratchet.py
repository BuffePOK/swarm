import pytest

from roj.verify.ratchet import Ratchet


def test_baseline_then_only_up(project):
    r = Ratchet(project, margin=0.02, streak_to_raise=2)
    g = r.evaluate("s", "f1", 0.70)
    assert g.passed and g.threshold == pytest.approx(0.68)
    assert not r.evaluate("s", "f1", 0.60).passed  # regression fails, threshold unchanged
    assert r.threshold("s", "f1") == pytest.approx(0.68)
    r.evaluate("s", "f1", 0.80)
    g = r.evaluate("s", "f1", 0.82)  # two in a row above threshold + margin
    assert g.raised_to == pytest.approx(0.80) and r.threshold("s", "f1") == pytest.approx(0.80)
    r.evaluate("s", "f1", 0.50)
    assert r.threshold("s", "f1") == pytest.approx(0.80)  # never down by itself


def test_lowering_needs_owner_note(project):
    r = Ratchet(project)
    r.evaluate("s", "m", 0.9)
    with pytest.raises(ValueError):
        r.lower("s", "m", 0.5, note=" ")
    with pytest.raises(ValueError):
        r.lower("s", "m", 0.95, note="raise?")
    r.lower("s", "m", 0.5, note="new harder eval set")
    assert r.threshold("s", "m") == 0.5
    assert project.events.query(type="gate.lowered")[-1].payload["note"] == "new harder eval set"


def test_cases_append_only_with_hidden_share(project):
    r = Ratchet(project, hidden_share=0.2)
    cases = [{"text": f"case {i}"} for i in range(200)]
    assert r.add_cases("suite", cases, source="bootstrap") == 200
    assert r.add_cases("suite", cases, source="again") == 0  # idempotent, never overwritten
    hidden = sum(c["hidden"] for c in r.cases("suite"))
    assert 20 <= hidden <= 60
    assert all(not c["hidden"] for c in r.cases("suite", visible_only=True))
