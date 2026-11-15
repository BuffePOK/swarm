"""Role registry: built-in roles plus roles added to the shared pool (YAML).

A role is data: goal, decision scope, instructions (pl/en), default tools, the model
binding key and the default autonomy. The Architect composes teams from roles; new
roles enter the pool as `candidate` and are used only after review.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml
from pydantic import BaseModel, Field

from ..contracts import Autonomy
from ..prompting.compiler import RoleSpec

if TYPE_CHECKING:
    from ..project import Installation

BUILTIN_YAML = """
- name: researcher
  summary: finds and verifies facts with tools, cites sources
  decision_scope: how to research its own task
  binding: analyst
  tools: [web.fetch, kb.search, calc, time.now]
  instructions:
    en: You research the task with the tools you have. Every fact you report cites the tool result it came from. If the tools cannot answer, say so.
    pl: Badasz zadanie dostępnymi narzędziami. Każdy podany fakt wskazuje wynik narzędzia, z którego pochodzi. Jeśli narzędzia nie dają odpowiedzi, powiedz to wprost.
- name: analyst
  summary: turns facts into conclusions with explicit reasoning
  decision_scope: interpretation of the facts it was given
  binding: analyst
  tools: [kb.search, calc]
  instructions:
    en: You draw conclusions only from the facts provided or retrieved, state uncertainty, and separate facts from interpretation.
    pl: Wyciągasz wnioski tylko z dostarczonych lub pobranych faktów, podajesz niepewność i oddzielasz fakty od interpretacji.
- name: writer
  summary: writes the deliverable in the requested format and language
  decision_scope: wording and structure
  binding: analyst
  tools: [files.write]
  instructions:
    en: You write clear, concise text in your own words, keeping every claim tied to its source.
    pl: Piszesz jasno i zwięźle, własnymi słowami, zachowując powiązanie każdego twierdzenia ze źródłem.
- name: coder
  summary: writes and tests code in the workspace
  decision_scope: implementation inside its module
  binding: coder
  tools: [files.read, files.write, files.list, shell.run]
  autonomy: L2
  instructions:
    en: You write small, tested changes in the project workspace. Run the tests before you report success.
    pl: Wprowadzasz małe, przetestowane zmiany w przestrzeni roboczej projektu. Przed zgłoszeniem sukcesu uruchom testy.
- name: critic
  summary: finds the weakest points of a plan or result
  decision_scope: none (advisory)
  binding: critic
  tools: []
  instructions:
    en: You find the three weakest points of what you review and propose a concrete fix for each. Be specific.
    pl: Znajdujesz trzy najsłabsze punkty recenzowanej pracy i dla każdego proponujesz konkretną poprawkę.
- name: verifier
  summary: checks a result against its acceptance criteria and sources
  decision_scope: pass or fail with evidence
  binding: checker
  tools: [kb.search, web.fetch, calc]
  instructions:
    en: You check each acceptance criterion against evidence. A criterion passes only with evidence.
    pl: Sprawdzasz każde kryterium akceptacji względem dowodów. Kryterium przechodzi tylko z dowodem.
- name: orchestrator
  summary: team lead that plans, delegates to workers and decides when the goal is met
  decision_scope: division of work and next steps inside the team
  binding: planner
  tools: []
  autonomy: L2
  instructions:
    en: You lead a team. Keep a ledger of facts, guesses and the plan. Each round pick one worker and give a precise instruction, or finish when the acceptance criteria are met. Replan when there is no progress.
    pl: Kierujesz zespołem. Prowadzisz księgę faktów, przypuszczeń i planu. W każdej rundzie wybierz jednego wykonawcę i daj mu precyzyjne polecenie albo zakończ, gdy kryteria akceptacji są spełnione. Zmień plan, gdy nie ma postępu.
- name: guardian
  summary: checks consistency between modules from the outside (contracts and outputs only)
  decision_scope: issues with evidence, sent back to the owning team
  binding: critic
  tools: []
  instructions:
    en: You check team outputs against their contracts and against each other (names, units, assumptions). You never look inside a module; report issues with evidence and the owning team.
    pl: Sprawdzasz wyniki zespołów względem ich kontraktów i względem siebie nawzajem (nazwy, jednostki, założenia). Nie zaglądasz do środka modułów; zgłaszasz problemy z dowodem i zespołem-właścicielem.
"""


class RoleDef(BaseModel):
    name: str
    summary: str
    decision_scope: str = ""
    binding: str
    tools: list[str] = Field(default_factory=list)
    autonomy: Autonomy = Autonomy.L1
    instructions: dict[str, str] | str
    status: str = "stable"
    version: str = "1"

    def spec(self, language: str) -> RoleSpec:
        text = self.instructions if isinstance(self.instructions, str) else (self.instructions.get(language) or self.instructions.get("en", ""))
        return RoleSpec(name=self.name, instructions=text, version=self.version)


class RoleRegistry:
    def __init__(self, inst: "Installation | None" = None):
        self.roles: dict[str, RoleDef] = {r["name"]: RoleDef(**r) for r in yaml.safe_load(BUILTIN_YAML)}
        self.pool_dir = (inst.home / "pools" / "roles") if inst else None
        if self.pool_dir and self.pool_dir.exists():
            for f in sorted(self.pool_dir.glob("*.yaml")):
                for raw in yaml.safe_load(f.read_text("utf-8")) or []:
                    raw.setdefault("status", "candidate")
                    self.roles[raw["name"]] = RoleDef(**raw)

    def get(self, name: str) -> RoleDef:
        if name not in self.roles:
            raise KeyError(f"unknown role {name!r}; known: {', '.join(sorted(self.roles))}")
        return self.roles[name]

    def add(self, raw: dict[str, Any]) -> RoleDef:
        from ..tools.registry import all_tools

        role = RoleDef(**{**raw, "status": "candidate"})
        unknown = [t for t in role.tools if t not in all_tools()]
        if unknown:
            raise ValueError(f"role {role.name}: unknown tools {unknown}")
        if self.pool_dir is None:
            raise ValueError("no installation to store the role in")
        self.pool_dir.mkdir(parents=True, exist_ok=True)
        path = self.pool_dir / f"{role.name}.yaml"
        path.write_text(yaml.safe_dump([role.model_dump(mode="json", exclude={"status"})], allow_unicode=True), encoding="utf-8")
        self.roles[role.name] = role
        return role

    def list(self) -> list[dict[str, Any]]:
        return [{"role": r.name, "status": r.status, "binding": r.binding, "autonomy": r.autonomy.value, "tools": ", ".join(r.tools),
                 "summary": r.summary} for r in self.roles.values()]


def pool_dir(home: Path) -> Path:
    return home / "pools" / "roles"
