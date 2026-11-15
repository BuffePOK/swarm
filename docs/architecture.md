# roj architecture (concept v2, condensed)

Status markers: **[done]** Phase 0, **[1A]**/**[1B]** done in Phase 1A/1B, **[0.3]** done in v0.3 (server, Meta team, selfdev), **[P1]** / **[P2]** / **[P3]** planned phase.

## 1. Conceptual model

```
Installation (ROJ_HOME)
├── shared pools: tools & algorithms, skills, roles, team patterns, Playbook,
│                 model registry [done], node registry, global knowledge packs
└── projects (isolated, run independently) [done]
      Blueprint, Leader, teams, budgets [done], memory & RAG, event log [done],
      ADRs, vault, permission settings [done]
```

A project never sees another project's memory, logs or secrets. It shares only what it
publishes to a pool, through gates. Flow is one-way: project → pool.

## 2. Leader and Architect

- **Leader model** is chosen per project and can be swapped at any time [done: `[leader]`
  in project.toml]. The Leader is stateless: each call receives a *state brief* assembled by
  code from the Blueprint, task ledger, progress ledger, open decisions and ADRs [P1].
- **Architect** designs the system for the goal: picks each team's mode, roles, autonomy
  levels, cycle length, concrete models with fallbacks, knowledge packs, tool pools, gates
  and module contracts [P1].
- **Blueprint**: the system as data (YAML in git). Path: schema → linter → critique by a
  model of another family → approval by control mode [1B: schema, linter, versions, approval, ADR; critique step in P2].
- **Control modes**: AUTO, ASSIST (default), MANUAL. AUTO for the Leader is unlocked only
  after a measured approval rate in ASSIST (≥ 90% of the last 20 changes) [1B].
- **Routing by difficulty**: routine decisions → cheap model, hard ones → strongest model;
  in doubt, one level up [P1].

## 3. Team modes [P1–P2]

STRICT workflow [done] · graph / state machine [1B] ·
orchestrator–workers (task and progress ledgers) [1B] · moderated team chat + private channels ·
interaction protocols (FIPA-like, Contract Net) · multi-family ensemble (same prompt to
several model families; evidence, not votes, decides) · blackboard · debate · handoff ·
composition of the above.

Autonomy per team and member: L0 strict, L1 limited, L2 collaborative, L3 module owner.
Decisions within one's own scope are recorded as `DecisionRecord(scope=…)` [contracts done].

## 4. Large projects

Modules with contracts (interface, acceptance, dependencies, single owner). A **consistency
guardian** sees only contracts, outputs, contract tests and a one-page report, never the
inside of a module, so its context stays constant. Contract tests are written before
implementation. A project glossary propagates changed assumptions [P2].

## 5. Cycles

Plan → execute → verify (gates) → review (guardian) → retro. Length from 10 minutes to one
day, never shorter than honest verification takes. Each cycle ends with a gated increment or
a documented failure plus a new test. WIP limits per team [P2].

## 6. Prompt engineering

- **Playbook**: rules with scope, source and our own A/B evidence; a prompt profile per model
  family that the compiler applies after owner approval [0.3].
- **Compiler** [done]: agents fill a TaskSpec; the compiler builds
  `system=[role][rules][tools][knowledge]` (stable) and
  `user=[documents][context][task][format][reminder]` (variable).
- **Linter** [done]: blocks missing goals, unmeasurable criteria, missing deliverables,
  timestamps/UUIDs in the prefix, oversize prompts, missing clauses.
- Ask-before-guessing: `Question` contract with options and a default [done].

## 7. Knowledge and multi-level RAG [1B, 0.3]

[0.3] Selective by design: packs have an audience of roles (shared knowledge of a speciality), a router
injects only relevant chunks under a token cap, and a novelty gate (closed-book exam) keeps out what the
model already knows. Long agent conversations are compacted in two layers.

Levels: global, project, team, task/role, personal. Knowledge packs carry a manifest, BM25 +
vector index, contextual chunks, reranker and a test question set with recall@k [1B]; failed
retrievals become new test questions (ratchet). The query rewriter has its own pack
(corpus description + teacher-written examples); the original query always runs in parallel.
Evidence model: [ADR-0002](adr/0002-evidence-model.md) (sources have tiers, hypotheses are
kept and labelled, independence counted by origin).

## 8. Shared pools [P1]

Statuses: draft → candidate → stable → retired. Submission needs code, tests, a card,
benchmark (if it promises speed), side-effect declaration, risk tier and license origin.
Tool cards in three layers (L0 ~15 tokens, L1 ≤ 150, L2 on demand), signatures generated
from code. Pool access per team: assigned, free, assigned+search.
**[1A]** The pool exists for generated adapters (draft/candidate/stable/retired, uses per
project, auto-promotion after use in ≥ 2 projects); role/tool cards follow in P1.

## 9. Models [done: client, fallbacks, registry; P1: probes and evals]

Each agent gets a concrete model and a fallback chain from the Architect. The registry
discovers models, and later measures them with probes and evals per task type (quality,
cost, latency, cache support, calibration). Model events (gone, error spikes, a better
model) go to the Architect; the first fallback serves meanwhile.

## 10. Token economics [done: exact cache, prefix layout, accounting, governor]

Provider prefix caching via byte-stable prefixes and shared role prefixes; exact local
cache for deterministic calls; batch mode for non-urgent work [P1]; diffs instead of
rewrites; deduplication of research before it is queued [P1]; cost per accepted result in
reports [P1].

## 11. Resources [0.3: one server, systemd, CI/CD, backups to a bucket (ADR-0007); P2: nodes]

Node registry with statuses online / offline / planned / future / retired; `roj-node`
daemon (heartbeat, resources, jobs, hash-verified transfer) or SSH-only mode; tasks declare
requirements, a scheduler picks the node; agent access per node behind a flag.

## 12. Hands [P3]

Preference order: API → CLI/script → real browser over CDP (persistent per-purpose profiles,
port bound to localhost) → headless Playwright → UI Automation → vision + mouse/keyboard.
Sandboxes by default.

## 13. Gates and ratchet [1B]

Gates at every stage. Thresholds only go up automatically; only the owner lowers them, with
an ADR. Every escaped defect becomes a permanent test. Part of the eval set is hidden from
agents and rotated. `verify/` and `evals/` are owner-only.

## 14. Self-improvement and the Meta team [0.3: study + selfdev pull requests (ADR-0008); P2: shadow mode]

Sandbox → tests → visible and hidden evals → shadow mode → promotion → ADR; every promotion
is reversible. The first internal project is the Meta team, researching what helps and what
hurts each model family, and proving each rule with A/B tests before it enters the Playbook.
Its first experiment: prompt language ([ADR-0003](adr/0003-languages.md)).

## 15. Permissions [done]

Flags per project, action and node: `manual` / `auto` / `auto_with_limits` / `deny`.
Everything from T2 up starts as manual. T4 automatic only with per-action and per-day caps
and an allow-list. Approval inbox: CLI [done], web panel + Telegram [P1]. No answer = no.

## 16. Daily tasks

A skill is a code skeleton with slots; a slot can be one call, a small graph or a whole team.
**[1A]** Implemented: bootstrap compiles a declarative spec (plus gated adapters), daily runs
execute it with 2–3 model calls, repair rediscovers moved feeds without tokens
([ADR-0005](adr/0005-skill-compilation.md)).
First skill: **Morning brief** (overnight news and its effect on the world economy; numbers
only from market data, every claim with a source, delivery to Telegram + panel, owner
rating feeds the retro) [1A, rating buttons in P1].
