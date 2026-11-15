"""Settings: installation-wide `roj.toml` plus per-project `project.toml` overrides.

No model names or machine paths are hard-coded in the library: they live here,
in the owner's configuration.
"""

from __future__ import annotations

import fnmatch
import os
import tomllib
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


class GatewaySettings(BaseModel):
    base_url: str = "http://localhost:8045/v1"
    api_key_env: str = "ROJ_API_KEY"
    timeout_s: float = 120.0
    connect_timeout_s: float = 10.0

    def api_key(self) -> str:
        return os.environ.get(self.api_key_env, "")


class Price(BaseModel):
    """USD per 1M tokens."""

    input: float
    output: float
    cached_input: float | None = None  # None: assume no discount (pessimistic)

    def cost(self, prompt_tokens: int, cached_tokens: int, completion_tokens: int) -> float:
        cached = min(cached_tokens, prompt_tokens)
        cached_price = self.input if self.cached_input is None else self.cached_input
        return (
            (prompt_tokens - cached) * self.input
            + cached * cached_price
            + completion_tokens * self.output
        ) / 1_000_000


class PricingSettings(BaseModel):
    strict: bool = False  # True: refuse models without a price
    default: Price = Price(input=15.0, output=75.0)  # deliberately expensive
    models: dict[str, Price] = Field(default_factory=dict)  # exact id or glob

    def price_for(self, model: str) -> tuple[Price, bool]:
        """Return (price, known). Exact match wins, then the most specific glob."""
        if model in self.models:
            return self.models[model], True
        globs = [p for p in self.models if any(c in p for c in "*?[")]
        for pattern in sorted(globs, key=len, reverse=True):
            if fnmatch.fnmatchcase(model, pattern):
                return self.models[pattern], True
        return self.default, False


class GovernorSettings(BaseModel):
    initial: int = 4
    min: int = 1
    max: int = 32
    latency_target_s: float = 30.0


class RetrySettings(BaseModel):
    max_attempts: int = 3
    base_delay_s: float = 1.0
    max_delay_s: float = 30.0
    rate_limit_max_wait_s: float = 60.0
    blocked_cooldown_s: float = 6 * 3600.0  # 403: do not touch that model for hours
    breaker_threshold: int = 3
    breaker_cooldown_s: float = 120.0


class BudgetSettings(BaseModel):
    tokens_per_day: int = 1_000_000
    usd_per_day: float = 5.0
    default_max_tokens: int = 4096
    reservation_ttl_s: float = 3600.0


class LanguageSettings(BaseModel):
    prompt_language: Literal["pl", "en"] = "pl"  # ADR-0003, measured by the Lab team
    output_language: str = "en"


class PromptSettings(BaseModel):
    max_input_tokens: int = 120_000
    min_cacheable_prefix_tokens: int = 1024  # below this most providers do not cache


class WebSettings(BaseModel):
    user_agent: str = "roj/0.3 (personal research agent; respects robots.txt)"
    timeout_s: float = 20.0
    max_bytes: int = 5_000_000
    per_host_interval_s: float = 1.0
    respect_robots: bool = True
    max_concurrency: int = 6


class NotifySettings(BaseModel):
    telegram_chat_id: str | None = None
    telegram_token_env: str = "ROJ_TELEGRAM_TOKEN"
    telegram_api: str = "https://api.telegram.org"


class ScheduleSpec(BaseModel):
    skill: str
    at: str = "08:00"  # delivery time in the project timezone
    lead_minutes: int = 20  # start this much earlier so delivery is on time
    grace_hours: float = 4.0  # laptop asleep at 08:00: still run until at + grace
    days: list[str] = Field(default_factory=lambda: ["mon", "tue", "wed", "thu", "fri", "sat", "sun"])
    inputs: dict[str, Any] = Field(default_factory=dict)

    @field_validator("at")
    @classmethod
    def _hhmm(cls, v: str) -> str:
        h, m = v.split(":")
        if not (0 <= int(h) < 24 and 0 <= int(m) < 60):
            raise ValueError("at must be HH:MM")
        return f"{int(h):02d}:{int(m):02d}"


class ContextSettings(BaseModel):
    """Long agent conversations are compacted instead of growing without bound."""

    compact_at_tokens: int = 24_000  # estimated size of the conversation that triggers compaction
    keep_turns: int = 3  # most recent assistant/tool turns kept verbatim
    tool_result_chars: int = 600  # older tool results are cut to this (full text stays in the CAS)
    summary_max_tokens: int = 900


class KnowledgeSettings(BaseModel):
    """Selective retrieval: irrelevant knowledge is worse than none."""

    max_tokens: int = 2500  # knowledge injected into one prompt
    min_pack_coverage: float = 0.25  # share of query terms a pack must know to be searched
    min_hit_coverage: float = 0.34  # share of query terms a chunk must contain to be injected
    novelty_threshold: float = 0.34  # store a document only if the model fails at least this share of questions
    novelty_questions: int = 6


class StorageSettings(BaseModel):
    backup_dir: str | None = None  # e.g. an S3 bucket mounted as a folder; databases themselves stay on local disk
    backup_keep: int = 14


class SelfdevSettings(BaseModel):
    """roj improving its own repository through pull requests."""

    repo: str | None = None  # path of a git clone of roj on this machine (default with clone_url: <home>/repo)
    clone_url: str | None = None  # e.g. https://github.com/<you>/roj.git: cloned on first use (container installs)
    base: str = "main"
    remote: str = "origin"
    github_repo: str | None = None  # "owner/name"; without it the result is a local branch and a patch file
    github_token_env: str = "ROJ_GITHUB_TOKEN"
    github_api: str = "https://api.github.com"
    max_diff_lines: int = 400
    test_timeout_s: int = 600
    protected: list[str] = Field(default_factory=lambda: [
        "roj/core/policy.py", "roj/core/budget.py", "roj/core/eventlog.py", "roj/verify/*", "roj/lab/selfdev.py",
        "roj/lab/tasks.py", "scripts/*", ".github/*", "deploy/*", "Dockerfile", "pyproject.toml", "uv.lock", "LICENSE", "docs/CONCEPT.md"])


class Settings(BaseModel):
    timezone: str = "Europe/Warsaw"
    gateway: GatewaySettings = GatewaySettings()
    pricing: PricingSettings = PricingSettings()
    governor: GovernorSettings = GovernorSettings()
    retry: RetrySettings = RetrySettings()
    budget: BudgetSettings = BudgetSettings()
    language: LanguageSettings = LanguageSettings()
    prompt: PromptSettings = PromptSettings()
    web: WebSettings = WebSettings()
    notify: NotifySettings = NotifySettings()
    context: ContextSettings = ContextSettings()
    knowledge: KnowledgeSettings = KnowledgeSettings()
    storage: StorageSettings = StorageSettings()
    selfdev: SelfdevSettings = SelfdevSettings()
    permissions: dict[str, Any] = Field(default_factory=dict)
    models: dict[str, "ModelBinding"] = Field(default_factory=dict)  # default binding per role

    @classmethod
    def load(cls, home: Path) -> "Settings":
        path = home / "roj.toml"
        data = _read_toml(path) if path.exists() else {}
        return cls.model_validate(data)


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        return tomllib.loads(path.read_text("utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise ValueError(f"{path}: {e}. Hint: in TOML a backslash starts an escape; write Windows paths with forward "
                         "slashes (\"D:/roj-home\") or in single quotes ('D:\\roj-home').") from e


class ModelBinding(BaseModel):
    """A concrete model plus its fallback chain, chosen by the Architect."""

    model: str
    fallbacks: list[str] = Field(default_factory=list)

    def candidates(self) -> list[str]:
        seen: list[str] = []
        for m in [self.model, *self.fallbacks]:
            if m not in seen:
                seen.append(m)
        return seen


class ProjectSettings(BaseModel):
    name: str
    timezone: str | None = None
    budget: dict[str, Any] = Field(default_factory=dict)  # overrides of BudgetSettings
    language: dict[str, Any] = Field(default_factory=dict)  # overrides of LanguageSettings
    permissions: dict[str, Any] = Field(default_factory=dict)
    leader: ModelBinding | None = None
    models: dict[str, ModelBinding] = Field(default_factory=dict)  # per-role bindings for this project
    skills: dict[str, dict[str, Any]] = Field(default_factory=dict)  # per-skill settings, e.g. models
    schedules: list[ScheduleSpec] = Field(default_factory=list)
    notify: dict[str, Any] = Field(default_factory=dict)
    web: dict[str, Any] = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def _valid_name(cls, v: str) -> str:
        if not v or not all(c.isalnum() or c in "-_" for c in v):
            raise ValueError("project name: letters, digits, '-' and '_' only")
        return v

    @classmethod
    def load(cls, project_dir: Path) -> "ProjectSettings":
        data = _read_toml(project_dir / "project.toml")
        return cls.model_validate(data)


class EffectiveSettings(BaseModel):
    """Global settings with project overrides applied."""

    project: str
    timezone: str
    gateway: GatewaySettings
    pricing: PricingSettings
    governor: GovernorSettings
    retry: RetrySettings
    budget: BudgetSettings
    language: LanguageSettings
    prompt: PromptSettings
    permissions: dict[str, Any]
    leader: ModelBinding | None
    models: dict[str, ModelBinding]
    skills: dict[str, dict[str, Any]]
    schedules: list[ScheduleSpec]
    notify: NotifySettings
    web: WebSettings
    context: ContextSettings = ContextSettings()
    knowledge: KnowledgeSettings = KnowledgeSettings()
    selfdev: SelfdevSettings = SelfdevSettings()

    def binding_for(self, role: str, skill: str | None = None) -> ModelBinding:
        """Skill-specific binding > project role binding > installation role binding."""
        if skill:
            raw = (self.skills.get(skill, {}).get("models") or {}).get(role)
            if raw:
                return ModelBinding.model_validate(raw)
        if role in self.models:
            return self.models[role]
        raise KeyError(
            f"no model bound to role {role!r}"
            + (f" for skill {skill!r}" if skill else "")
            + f"; add [skills.{skill or '<skill>'}.models.{role}] or [models.{role}] with model and fallbacks"
        )

    @classmethod
    def merge(cls, g: Settings, p: ProjectSettings) -> "EffectiveSettings":
        return cls(
            project=p.name,
            timezone=p.timezone or g.timezone,
            gateway=g.gateway,
            pricing=g.pricing,
            governor=g.governor,
            retry=g.retry,
            budget=BudgetSettings(**{**g.budget.model_dump(), **p.budget}),
            language=LanguageSettings(**{**g.language.model_dump(), **p.language}),
            prompt=g.prompt,
            permissions={**g.permissions, **p.permissions},
            leader=p.leader,
            models={**g.models, **p.models},
            skills=p.skills,
            schedules=p.schedules,
            notify=NotifySettings(**{**g.notify.model_dump(), **p.notify}),
            web=WebSettings(**{**g.web.model_dump(), **p.web}),
            context=g.context, knowledge=g.knowledge, selfdev=g.selfdev,
        )


def resolve_home(explicit: str | os.PathLike[str] | None = None) -> Path:
    if explicit:
        return Path(explicit).expanduser().resolve()
    env = os.environ.get("ROJ_HOME")
    if env:
        return Path(env).expanduser().resolve()
    return (Path.home() / ".roj").resolve()


ROJ_TOML_TEMPLATE = """\
# roj installation settings. Edit freely; `roj` reads this on every start.
timezone = "Europe/Warsaw"

[gateway]
# Any OpenAI-compatible endpoint. Include the /v1 suffix if your gateway uses it.
base_url = "http://localhost:8045/v1"
# The key is read from this environment variable, never stored in files or logs.
api_key_env = "ROJ_API_KEY"
timeout_s = 120

[budget]
# Defaults per project per day (ADR-0004). Projects can override in project.toml.
tokens_per_day = 1000000
usd_per_day = 5.0
default_max_tokens = 4096

[language]
# ADR-0003: scaffolding of prompts vs language of deliverables.
prompt_language = "pl"
output_language = "en"

[pricing]
# USD per 1M tokens. Unknown models use [pricing.default] (deliberately expensive),
# or are refused when strict = true. Keys may be exact ids or globs.
strict = false

[pricing.default]
input = 15.0
output = 75.0

# Examples, replace with your gateway's real prices:
# [pricing.models."gemini-*-flash-lite*"]
# input = 0.10
# cached_input = 0.025
# output = 0.40
#
# [pricing.models."claude-opus*"]
# input = 5.0
# cached_input = 0.5
# output = 25.0

[governor]
initial = 4
min = 1
max = 32
latency_target_s = 30

[retry]
max_attempts = 3
blocked_cooldown_s = 21600

[web]
# Polite fetching for agents' read-only web access.
user_agent = "roj/0.1 (personal research agent; respects robots.txt)"
per_host_interval_s = 1.0
respect_robots = true

[notify]
# Owner notifications via your own Telegram bot (token in the environment variable below).
# telegram_chat_id = "123456789"
telegram_token_env = "ROJ_TELEGRAM_TOKEN"

# Model per role. The Architect will assign these per agent later; for now the owner does.
# Use different model families for planner/critic and analyst/checker.
# [models.planner]
# model = "claude-opus-5-5"
# fallbacks = ["gemini-pro"]
# [models.critic]
# model = "gpt-sol"
# [models.coder]
# model = "claude-sonnet-5-5"
# [models.classifier]
# model = "gemini-flash-lite"
# [models.analyst]
# model = "claude-sonnet-5-5"
# fallbacks = ["deepseek-pro"]
# [models.checker]
# model = "gemini-flash"

[permissions]
# Everything from T2 up is "manual" by default. Flip per action when ready:
# "t3.send_message" = "auto"
# "t4.payment" = { mode = "auto_with_limits", per_action = 10.0, per_day = 20.0, allow = ["shop.example"] }
"""


def project_toml(name: str, tokens: int | None, usd: float | None, timezone: str | None) -> str:
    lines = [f'name = "{name}"']
    if timezone:
        lines.append(f'timezone = "{timezone}"')
    if tokens is not None or usd is not None:
        lines.append("")
        lines.append("[budget]")
        if tokens is not None:
            lines.append(f"tokens_per_day = {int(tokens)}")
        if usd is not None:
            lines.append(f"usd_per_day = {float(usd)}")
    lines += [
        "",
        "# Leader model is chosen per project and can be changed at any time:",
        "# [leader]",
        '# model = "claude-opus-5-5"',
        '# fallbacks = ["gemini-pro", "deepseek-pro"]',
        "",
        "[permissions]",
        "# Project-level overrides of the installation permissions.",
        "",
    ]
    return "\n".join(lines)


Settings.model_rebuild()
