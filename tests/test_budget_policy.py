import pytest

from roj.core.budget import BudgetExceeded
from roj.core.policy import Policy, PolicyConfigError, PolicyDenied, Verdict


def test_reserve_commit_release(project):
    b = project.budget
    r = b.reserve("team:a", 1000, 0.01)
    assert b.usage("") == (1000, 0.01)
    b.commit(r, 400, 0.004)
    assert b.usage("team:a") == (400, 0.004)
    r2 = b.reserve("team:a", 100, 0.001)
    b.release(r2)
    assert b.usage("") == (400, 0.004)


def test_project_token_limit(project):
    project.budget.reserve("", 999_000, 0.0)
    with pytest.raises(BudgetExceeded) as e:
        project.budget.reserve("team:x", 2_000, 0.0)
    assert e.value.scope == "" and e.value.kind == "tokens"


def test_project_usd_limit(project):
    project.budget.reserve("", 10, 4.99)
    with pytest.raises(BudgetExceeded) as e:
        project.budget.reserve("", 10, 0.02)
    assert e.value.kind == "usd"


def test_hierarchical_limits(project):
    b = project.budget
    b.allocate("team:a", 1000, 1.0, set_by="architect")
    b.allocate("team:a/agent:w", 600, None, set_by="architect")
    b.reserve("team:a/agent:w", 500, 0.0)
    with pytest.raises(BudgetExceeded) as e:
        b.reserve("team:a/agent:w", 200, 0.0)  # agent limit 600
    assert e.value.scope == "team:a/agent:w"
    b.reserve("team:a/agent:other", 400, 0.0)
    with pytest.raises(BudgetExceeded) as e:
        b.reserve("team:a/agent:other", 200, 0.0)  # team limit 1000
    assert e.value.scope == "team:a"


def test_overallocation_warns(project):
    project.budget.allocate("team:a", 700_000, None)
    warnings = project.budget.allocate("team:b", 700_000, None)
    assert warnings and "project" in warnings[0]


def test_invalid_scope(project):
    with pytest.raises(ValueError):
        project.budget.reserve("teams/a b", 1, 0)


def test_policy_defaults(project):
    pol = project.policy
    assert pol.evaluate("t0.read").verdict == Verdict.ALLOW
    assert pol.evaluate("t1.write").verdict == Verdict.ALLOW
    for a in ("t2.delete", "t3.send_message", "t4.payment"):
        assert pol.evaluate(a).verdict == Verdict.ASK


def test_approval_flow(project):
    pol = project.policy
    v, aid = pol.check("t3.send_message", {"to": "x"}, task_id="T")
    assert v == Verdict.ASK and aid
    v2, aid2 = pol.check("t3.send_message", {"to": "x"}, task_id="T")
    assert v2 == Verdict.ASK and aid2 == aid  # same request, same approval
    pol.decide(aid, True, note="ok")
    assert pol.check("t3.send_message", {"to": "x"}, task_id="T")[0] == Verdict.ALLOW
    v3, aid3 = pol.check("t3.send_message", {"to": "y"}, task_id="T")
    pol.decide(aid3, False, note="no")
    with pytest.raises(PolicyDenied):
        pol.check("t3.send_message", {"to": "y"}, task_id="T")


def test_unanswered_approval_means_no(project):
    pol = Policy({}, project.store, project.events, "Europe/Warsaw", approval_ttl_s=-1)
    v, aid = pol.check("t3.post", {"p": 1})
    with pytest.raises(PolicyDenied):
        pol.check("t3.post", {"p": 1})


def test_t4_cannot_be_plain_auto(project):
    with pytest.raises(PolicyConfigError):
        Policy({"t4.payment": "auto"}, project.store, project.events, "Europe/Warsaw")
    with pytest.raises(PolicyConfigError):
        Policy({"t4.payment": {"mode": "auto_with_limits", "per_action": 5}}, project.store, project.events, "Europe/Warsaw")


def test_auto_with_limits(project):
    pol = Policy({"t4.payment": {"mode": "auto_with_limits", "per_action": 10, "per_day": 15, "allow": ["shop.example"]}},
                 project.store, project.events, "Europe/Warsaw")
    assert pol.evaluate("t4.payment", amount=5, target="evil.example").verdict == Verdict.ASK
    assert pol.evaluate("t4.payment", amount=11, target="shop.example").verdict == Verdict.ASK
    assert pol.check("t4.payment", {"o": 1}, amount=8, target="shop.example")[0] == Verdict.ALLOW
    # 8 already spent today: 8 more exceeds per_day 15
    assert pol.evaluate("t4.payment", amount=8, target="shop.example").verdict == Verdict.ASK
    assert pol.evaluate("t4.payment", amount=7, target="shop.example").verdict == Verdict.ALLOW


def test_flag_flip_makes_t3_auto(project):
    pol = Policy({"t3.*": "auto"}, project.store, project.events, "Europe/Warsaw")
    assert pol.evaluate("t3.send_message").verdict == Verdict.ALLOW
    assert pol.evaluate("t4.payment").verdict == Verdict.ASK
