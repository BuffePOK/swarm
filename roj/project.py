"""Installation and project wiring.

Layout of a roj home (ROJ_HOME, default ~/.roj):
  roj.toml                 installation settings
  roj.db                   shared registry (models) + its own event log
  projects/<name>/
    project.toml           overrides: budget, timezone, language, permissions, leader
    state.db               events, tasks, steps, budgets, approvals, messages, cache
    cas/                   content-addressed blobs (prompts, responses, results)

Projects are isolated: nothing in one project's state is visible to another.
"""

from __future__ import annotations

from pathlib import Path

import httpx

from .config import (ROJ_TOML_TEMPLATE, EffectiveSettings, ProjectSettings, Settings, project_toml)
from .core.budget import BudgetLedger
from .core.bus import Bus
from .core.cas import CAS
from .core.eventlog import EventLog
from .core.kernel import Kernel
from .core.policy import Policy
from .core.store import GLOBAL_SCHEMA, PROJECT_SCHEMA, Store
from .hands.http import Fetcher
from .models.caller import ExactCache, LLMCaller
from .models.client import GatewayClient
from .models.governor import Governor
from .models.registry import ModelRegistry


class Installation:
    def __init__(self, home: Path, transport: httpx.AsyncBaseTransport | None = None):
        self.home = Path(home)
        if not (self.home / "roj.toml").exists():
            raise FileNotFoundError(f"no roj home at {self.home}; run `roj init` first")
        self.settings = Settings.load(self.home)
        self.store = Store(self.home / "roj.db", GLOBAL_SCHEMA)
        self.events = EventLog(self.store)
        self.client = GatewayClient(self.settings.gateway, transport=transport)
        from .models.quirks import QuirkStore

        self.client.quirks = QuirkStore(self.store, self.events)
        self.registry = ModelRegistry(self.store, self.events, self.client)
        self.governor = Governor(self.settings.governor)  # shared by all projects in this process
        self._transport = transport

    @staticmethod
    def init(home: Path) -> bool:
        """Create a home. Returns False if it already existed (nothing overwritten)."""
        home = Path(home)
        (home / "projects").mkdir(parents=True, exist_ok=True)
        cfg = home / "roj.toml"
        if cfg.exists():
            return False
        cfg.write_text(ROJ_TOML_TEMPLATE, encoding="utf-8")
        Store(home / "roj.db", GLOBAL_SCHEMA).close()
        return True

    def project_names(self) -> list[str]:
        root = self.home / "projects"
        return sorted(p.name for p in root.iterdir() if (p / "project.toml").exists()) if root.exists() else []

    def create_project(self, name: str, *, tokens: int | None = None, usd: float | None = None,
                       timezone: str | None = None) -> "Project":
        ProjectSettings(name=name)  # validates the name
        pdir = self.home / "projects" / name
        if (pdir / "project.toml").exists():
            raise FileExistsError(f"project {name!r} already exists")
        pdir.mkdir(parents=True, exist_ok=True)
        (pdir / "project.toml").write_text(project_toml(name, tokens, usd, timezone), encoding="utf-8")
        project = self.project(name)
        project.events.append("project.created", {"name": name}, actor="owner")
        return project

    def project(self, name: str) -> "Project":
        return Project(self, name)

    def close(self) -> None:
        self.store.close()


class Project:
    def __init__(self, inst: Installation, name: str):
        self.inst = inst
        self.name = name
        self.dir = inst.home / "projects" / name
        if not (self.dir / "project.toml").exists():
            raise FileNotFoundError(f"project {name!r} not found in {inst.home}")
        self.settings = EffectiveSettings.merge(inst.settings, ProjectSettings.load(self.dir))
        self.store = Store(self.dir / "state.db", PROJECT_SCHEMA)
        self.events = EventLog(self.store)
        self.cas = CAS(self.dir / "cas")
        self.budget = BudgetLedger(self.store, self.events, self.settings.budget, self.settings.timezone)
        self.policy = Policy(self.settings.permissions, self.store, self.events, self.settings.timezone)
        self.bus = Bus(self.store, self.events)
        self.cache = ExactCache(self.store)
        self.caller = LLMCaller(
            project=name, client=inst.client, registry=inst.registry, cache=self.cache, budget=self.budget,
            events=self.events, cas=self.cas, pricing=self.settings.pricing, retry=self.settings.retry,
            governor=inst.governor, default_max_tokens=self.settings.budget.default_max_tokens,
        )
        self.fetcher = Fetcher(self.store, self.cas, self.events, self.settings.web)
        self.kernel = Kernel(self)

    def profile_for(self, model: str):
        """Prompt profile of the model's family from the active Playbook (default layout if none)."""
        from .lab.playbook import load_active

        return load_active(self.inst.home).profile_for(model)

    def write_task_log(self, task_id: str):
        from .obs import runlog

        return runlog.write(self, task_id)

    def close(self) -> None:
        self.store.close()
