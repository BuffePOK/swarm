# ADR-0009: Missions are the entry point; the owner chooses how close he stands

- Status: accepted
- Date: 2026-11-15

## Context

The owner wants to start from a goal given to the Architect and the Leader (a minimal team),
have them decide, with questions to him, what the goal needs (agents, tools, knowledge), and
proceed in stages. He also wants to choose his distance: from talking to every agent and
approving each task, to talking only to the leads. `roj lab study` and `roj selfdev run` are
fixed workflows, not an entry point.

## Decision

1. **Mission** = goal + questions and answers + charter + roadmap + stages + lessons +
   directives, stored as one state document per mission. Agents are stateless; every call gets
   a brief assembled by code.
2. **Rolling wave.** Only the next stage is planned in detail. The Leader's review of stage N
   (verdict with evidence, lessons, questions, change requests) is the input of stage N+1.
3. **Two leads, two models.** The Architect plans; the Leader critiques the plan and reviews
   the results. Code lints the plan before the owner sees it.
4. **Mission agents do not change code.** They read, analyse and build knowledge. A stage can
   raise change requests, which run as selfdev tasks (ADR-0008). One path for code changes.
5. **Control modes** `leads` / `partial` / `full` per mission, switchable at any time, decide
   who the owner can talk to and whether each agent's task needs his approval.
6. **Chat is not command execution.** A message gets an answer; a standing instruction is
   recorded as a directive that later planning and review must honour.

## Consequences

- The Blueprint machinery (strict / graph / orchestrator runners) stays for fixed, repeatable
  teams; a stage's team is currently run sequentially by the stage workflow. Running a stage
  through a Blueprint is a backlog item.
- The "Meta team" was renamed **Lab** (commands `roj lab ...`): it had nothing to do with the
  company Meta, whose models may be among those studied.
