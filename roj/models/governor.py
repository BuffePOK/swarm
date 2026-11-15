"""Client-side flow control so agents never flood the shared gateway.

AIMD on concurrency: +1/limit per fast success (about +1 per full window),
halve on 429/5xx or when latency exceeds the target. Same idea as TCP congestion
control: probe gently, back off hard.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator

from ..config import GovernorSettings


class Governor:
    def __init__(self, settings: GovernorSettings):
        self.settings = settings
        self.limit = float(max(settings.min, min(settings.initial, settings.max)))
        self.in_flight = 0
        self._conds: dict[int, asyncio.Condition] = {}
        self.overloads = 0

    def _cond(self) -> asyncio.Condition:
        loop_id = id(asyncio.get_running_loop())
        if loop_id not in self._conds:
            self._conds[loop_id] = asyncio.Condition()
        return self._conds[loop_id]

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        cond = self._cond()
        async with cond:
            await cond.wait_for(lambda: self.in_flight < int(self.limit))
            self.in_flight += 1
        try:
            yield
        finally:
            async with cond:
                self.in_flight -= 1
                cond.notify_all()

    def on_success(self, latency_s: float) -> None:
        if latency_s > self.settings.latency_target_s:
            self._decrease()
            return
        self.limit = min(float(self.settings.max), self.limit + 1.0 / max(self.limit, 1.0))

    def on_overload(self) -> None:
        self.overloads += 1
        self._decrease()

    def _decrease(self) -> None:
        self.limit = max(float(self.settings.min), self.limit / 2.0)

    def snapshot(self) -> dict[str, float]:
        return {"limit": round(self.limit, 2), "in_flight": self.in_flight, "overloads": self.overloads}
