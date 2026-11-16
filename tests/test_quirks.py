import asyncio

from roj.models.quirks import apply, diagnose, merge


def run(c):
    return asyncio.run(c)


def test_diagnose_real_provider_errors():
    assert diagnose('{"error":{"message":"`temperature` is deprecated for this model."}}') == {"drop": ["temperature"]}
    assert diagnose('{"error":{"message":"top_p must be 1 when using greedy sampling."}}') == {"set": {"top_p": 1}}
    assert diagnose("Unsupported parameter: 'max_tokens' is not supported with this model. Use 'max_completion_tokens' instead.") == \
        {"rename": {"max_tokens": "max_completion_tokens"}}
    assert diagnose("Unsupported value: 'temperature' does not support 0 with this model. Only the default (1) value is supported.") == \
        {"drop": ["temperature"]}
    assert diagnose("`temperature` and `top_p` cannot both be specified for this model.") == {"drop": ["top_p"]}
    assert diagnose("messages is not supported") is None  # never touches content, tools or schema
    assert diagnose("invalid request") is None


def test_apply_and_merge():
    q = merge({"drop": ["temperature"]}, {"set": {"top_p": 1}})
    assert apply({"temperature": 0, "max_tokens": 5}, q) == {"max_tokens": 5, "top_p": 1}
    assert apply({"max_tokens": 5}, {"rename": {"max_tokens": "max_completion_tokens"}}) == {"max_completion_tokens": 5}


def test_client_learns_and_remembers(inst, gateway):
    state, _ = gateway
    msgs = [{"role": "user", "content": "hi"}]
    r = run(inst.client.chat("fake-notemp", msgs, {"temperature": 0, "max_tokens": 5}))
    assert r.text and inst.client.quirks.get("fake-notemp") == {"drop": ["temperature"]}
    before = state.requests["fake-notemp"]
    run(inst.client.chat("fake-notemp", msgs, {"temperature": 0, "max_tokens": 5}))
    assert state.requests["fake-notemp"] == before + 1  # remembered: no failed attempt the second time
    run(inst.client.chat("fake-greedy", msgs, {"temperature": 0, "max_tokens": 5}))
    assert inst.client.quirks.get("fake-greedy") == {"set": {"top_p": 1}}
    assert set(inst.client.quirks.all()) == {"fake-notemp", "fake-greedy"}
    assert inst.store.one("SELECT COUNT(*) n FROM events WHERE type = 'llm.quirk_learned'")["n"] == 2
    assert inst.client.quirks.forget("fake-greedy") == 1 and set(inst.client.quirks.all()) == {"fake-notemp"}


def test_doctor_flags_placeholder_telegram_chat(home, monkeypatch):
    from roj.obs.doctor import run_doctor
    from roj.project import Installation

    text = (home / "roj.toml").read_text()
    (home / "roj.toml").write_text(text.replace('telegram_chat_id = "42"', 'telegram_chat_id = "123456789"'))
    inst = Installation(home)
    try:
        checks = run(run_doctor(inst, live=True))
        assert any(c.status == "FAIL" and "example value" in c.detail for c in checks)
        assert any(c.status == "FAIL" and "not reachable" in c.detail for c in checks)
    finally:
        inst.close()
