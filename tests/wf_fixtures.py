"""Workflows used by kernel tests (importable from a subprocess via PYTHONPATH)."""

from __future__ import annotations

import os
import time
from pathlib import Path

from roj.core.kernel import Step, StepContext, Workflow


def _mark(name: str) -> None:
    d = Path(os.environ["ROJ_TEST_DIR"])
    with open(d / "executions.log", "a", encoding="utf-8") as f:
        f.write(name + "\n")


async def one(ctx: StepContext) -> dict:
    _mark("one")
    return {"value": 1}


async def two(ctx: StepContext) -> dict:
    _mark("two")
    if os.environ.get("ROJ_TEST_SLOW") == "1":
        (Path(os.environ["ROJ_TEST_DIR"]) / "in_two.marker").write_text(str(os.getpid()))
        time.sleep(60)  # the test kills the process here
    return {"value": ctx.results["one"]["value"] + 1}


async def three(ctx: StepContext) -> dict:
    _mark("three")
    return {"value": ctx.results["two"]["value"] + 1}


SLOW = Workflow(name="slow", steps=(Step("one", one), Step("two", two), Step("three", three)))


async def crash_once(ctx: StepContext) -> dict:
    _mark("crash_once")
    if ctx.attempt == 1:
        raise KeyboardInterrupt("simulated hard stop")
    return {"value": ctx.results["one"]["value"] + 10}


async def after_crash(ctx: StepContext) -> dict:
    _mark("after_crash")
    return {"value": ctx.results["crash_once"]["value"] + 1}


CRASH = Workflow(name="crash", steps=(Step("one", one), Step("crash_once", crash_once), Step("after_crash", after_crash)))


async def buggy(ctx: StepContext) -> dict:
    raise ValueError("boom")


BUGGY = Workflow(name="buggy", steps=(Step("one", one), Step("buggy", buggy)))
