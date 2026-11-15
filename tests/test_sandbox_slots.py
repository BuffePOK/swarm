import asyncio

import pytest
from pydantic import BaseModel

from roj.config import ModelBinding
from roj.contracts import TaskSpec
from roj.core.kernel import StepContext
from roj.prompting.compiler import RoleSpec
from roj.skills.sandbox import SandboxError, check_code, run_adapter
from roj.skills.slots import Slot, SlotFailed, extract_json, run_slot
from roj.testing.fake_brains import V1
from roj.testing.fake_web import energy_page

GOOD = V1


@pytest.mark.parametrize("code,needle", [
    ("import os\ndef parse(raw, base_url):\n    return os.listdir('.')\n", "import os"),
    ("def parse(raw, base_url):\n    return open('x').read()\n", "open"),
    ("def parse(raw, base_url):\n    return ().__class__.__bases__\n", "dunder"),
    ("import socket\ndef parse(raw, base_url):\n    return []\n", "socket"),
    ("from subprocess import run\ndef parse(raw, base_url):\n    return []\n", "subprocess"),
    ("def parse(raw):\n    return []\n", "exactly"),
    ("eval('1')\ndef parse(raw, base_url):\n    return []\n", "top level"),
])
def test_static_check_blocks_dangerous_code(code, needle):
    problems = check_code(code)
    assert problems and any(needle in p for p in problems)


def test_good_adapter_runs_isolated():
    items = run_adapter(GOOD, energy_page(False).decode(), "http://energy.example/energy/")
    assert len(items) == 5 and items[0]["link"].startswith("http://energy.example/energy/story/")


def test_adapter_timeout_and_bad_output():
    loop = "def parse(raw, base_url):\n    while True:\n        pass\n"
    with pytest.raises(SandboxError, match="timed out|raised|exit"):
        run_adapter(loop, "", "", timeout_s=2)
    bad = "def parse(raw, base_url):\n    return [{'title': '', 'link': 'x'}]\n"
    with pytest.raises(SandboxError, match="non-empty"):
        run_adapter(bad, "", "")


def test_extract_json():
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('Here you go: {"a": [1, 2]} thanks') == {"a": [1, 2]}
    with pytest.raises(ValueError):
        extract_json("no json here")


class Answer(BaseModel):
    answer: str


def _ctx(project):
    return StepContext(project, "task-x", "trace-x", "s", 0, 1, {}, {})


def test_slot_repairs_invalid_json(project, gateway):
    slot = Slot("tester", RoleSpec("tester", "You answer questions."), Answer, max_tokens=100)
    task = TaskSpec(goal="Answer the question about life", deliverable="Answer JSON", acceptance=["answer is exactly 42"],
                    output_format="JSON")
    res = asyncio.run(run_slot(_ctx(project), slot, task, binding=ModelBinding(model="fake-sloppy"), scope="role:tester"))
    assert res.value.answer == "42" and res.attempts == 2
    assert [e.payload["attempt"] for e in project.events.query(type="slot.invalid")] == [1]


def test_slot_gives_up_after_bounded_attempts(project, gateway):
    slot = Slot("tester", RoleSpec("tester", "You answer questions."), Answer, max_tokens=100, repair_attempts=0)
    task = TaskSpec(goal="Answer the question about life", deliverable="Answer JSON", acceptance=["answer is exactly 42"],
                    output_format="JSON")
    with pytest.raises(SlotFailed):
        asyncio.run(run_slot(_ctx(project), slot, task, binding=ModelBinding(model="fake-sloppy"), scope="role:tester"))
