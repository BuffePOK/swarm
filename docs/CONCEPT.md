# roj: the complete concept

Version of this document: 2026-11-16, describes roj v0.5.1.
Owner-only file: agents read it, they do not edit it (it is a protected path). Changes to
goals and rules come from the owner.

**How to read it.** Every capability carries a status:

- **[built]** exists in the code and is covered by tests that run offline against stand-in models
- **[partial]** exists in a reduced form; the text says what is missing
- **[planned]** agreed in the concept, not written yet

"Built" never means "proven in production". As of this version **roj has never been run
against real models or on the owner's server.** Everything below marked [built] was verified
only with a fake gateway, a synthetic web and deterministic stand-in models.

When sources disagree, the order of authority is: the owner's latest decision, then this
document, then the ADRs in `docs/adr/`, then code comments.

<!-- digest:start -->
## Digest for agents (what you are working on and what you must achieve)

**What roj is.** A general-purpose library and runtime for teams of AI agents. An owner states
a goal; an Architect and a Leader (any models, swappable at any time) design a minimal team,
ask the owner what they cannot know, and work in stages. Agents use tools, shared knowledge
and many model families through one OpenAI-compatible gateway. Code executes, counts and
verifies; models decide and create. Everything important is stored outside the agents, so any
agent, including the Leader, can be replaced at any moment. One-off tasks (research,
analysis, code) and daily tasks (collect, summarise, publish) run on the same kernel.

**The current mission.** roj improves roj. This is both the first real project and the test
of the library. The owner wants improvement by orders of magnitude while cost-effectiveness
stays high. That cannot be done in one step. It is done in stages, each of which adds
knowledge, skills or a measured improvement, and each of which is checked.

**What "better" means (measured from the event log, never asserted):**

1. Lower cost per accepted result: tokens and USD spent per task that passed its gates.
2. Higher first-pass validity: fewer invalid outputs, repairs, retries and fallbacks.
3. Less owner attention per result: more proposals approved without correction.
4. Stronger verification: more defects caught by gates before the owner sees them, and every
   escaped defect turned into a permanent test.
5. More capability: goals that could not be done before can be done, with the same rules.

A change counts as an improvement only if a measurement on cases the change was not tuned on
shows it, or if it removes a demonstrated failure and adds the test that would have caught it.

**Rules you cannot bend.**

- No baseline, no claim. If a metric has no baseline yet, the first job is to measure it.
- Code is changed only through the selfdev workflow: a small change in a git worktree, a new
  test, all gates green, review by another model family, the owner's approval, a pull request.
  The owner merges. Mission agents read, analyse and build knowledge; they do not write code.
- You cannot edit your own exam: gates, policy, budgets, the event log, the eval tasks, CI,
  deploy files, dependencies, this document and existing tests are owner-only. You may propose
  stricter gates and new tests, never weaker ones.
- Token discipline is part of the goal. The gateway has no hard limit, and that is not a
  licence to burn tokens. Budgets: 1,000,000 tokens and 5 USD per project per day.
- Every claim needs evidence: a source, a test or data. Agreement between models is not truth.
- When a decision belongs to the owner and is missing, ask a question with options and a
  default. Do not guess.
- Everything with side effects from tier T2 up waits for the owner until he flips the flag.

**Where to start.** Read section 21 (state and known weaknesses) and section 22 (objectives,
metrics, staged path). The full document is in the repository at `docs/CONCEPT.md`; read the
section you need with `repo.read` instead of loading all of it.
<!-- digest:end -->

---

## 1. Purpose

### 1.1 The problem

One model in one chat window is a poor way to do large or repeated work: the context fills
up, nothing is verified, the same research is paid for every day, and a model's mistake is
found by the human or not at all. Existing agent frameworks solve parts of this but own the
prompts and the state, change their APIs often, and make it hard to control cost.

### 1.2 What roj is

roj is a Python library plus a small runtime with which a **Leader** and an **Architect**
(both ordinary models) build and run **teams of agents** for a goal. It provides:

- a durable kernel that survives being killed and resumes from the last finished step
- one path to every model family through an OpenAI-compatible gateway, with a concrete model
  and fallbacks per agent
- a prompt compiler and linter so that agents describe tasks as data and never hand-write prompts
- shared knowledge (retrieval that is selective, measured and cheap)
- tools with generated documentation, permission tiers and an approval inbox
- budgets, a hash-chained log of everything, and gates that only get stricter
- loops in which the system studies models, improves its prompts and proposes changes to its
  own code

### 1.3 Who it is for

First the owner (Dmytro), who runs several projects at once. Then anyone: the project is MIT
licensed, named `roj`, written and documented in English, and must run anywhere with nothing
but an OpenAI-compatible endpoint.

### 1.4 Kinds of work it must handle

- **One-off**: research, analysis, design, writing code, building a knowledge base of a project.
- **Daily / recurring**: read sources and report, write posts, find groups, produce media.
  Rule: the first run is research and development (expensive); it compiles what it learned
  into code and declarative rules, so later runs cost few tokens.
- **Long-running projects**: many modules, many teams, weeks of work (the Lineage 2 remake
  project will move onto roj later).
- **Work on itself**: the current mission.

### 1.5 What roj is not

- Not a chatbot and not a wrapper around one framework.
- Not a way around provider limits or terms of service. Official API keys only. robots.txt is
  respected. No mass account creation, no CAPTCHA bypass, no downloading of things the owner
  must obtain himself.
- Not autonomous with money, messages to third parties or publication until the owner allows
  each kind of action explicitly.

---

## 2. Owner, environment and fixed decisions

### 2.1 Decisions the owner has made

| Topic | Decision |
|---|---|
| Name, licence | `roj`, MIT, public on GitHub |
| Languages | Code, docs, reports, README in English. Prompts to agents and messages between agents in Polish by default (the owner's experience is that they work best); the Lab measures this per model family and the Playbook may change it per family |
| Leader | Any model on the gateway, chosen per project, swappable at any time; routed by difficulty |
| Models per agent | A concrete model plus a fallback chain for every agent, assigned by the Architect; no abstract "profiles" as the main mechanism |
| Providers | Official API keys only, through the owner's gateway |
| Budgets | 1,000,000 tokens and 5 USD per project per day; later the Architect/Leader distributes this among agents; the owner can edit |
| Approvals | For now everything goes through the owner. Later single flags switch to automatic, even some T4 actions, but T4 only with limits |
| Approval channels | Web panel and Telegram |
| Panel | Localhost first |
| API outages | They are the gateway's problem. roj only reacts: classify, checkpoint, fall back, notify, resume |
| Shared pool | New tools and algorithms go to a shared base, with statuses (draft, candidate, stable, retired) |
| Sources | Not every source is true, and valuable facts are sometimes at the bottom. Sources have tiers; hypotheses are kept and labelled, not discarded |
| Start | A clean package; proven parts of the old `roj` package are ported; Lineage 2 migrates later |
| Testing | No local testing. The project goes straight to the server |
| Deployment | Like the gateway project: GitHub Actions builds a container image into GitHub Packages; the server checks every 2 minutes and pulls. No SSH keys or deploy secrets in GitHub |
| Pilot | Changed on 2026-11-15: no market-analysis project. The first project is roj improving roj |
| Entry point | A goal given to the Architect and the Leader (minimal team). They decide, with questions to the owner, what is needed. Everything proceeds in stages |
| Owner's distance | Selectable: talk to every agent and approve each task; partial; or only the leads |
| Knowledge | Retrieval is selective; agents of the same speciality share knowledge; what a model already knows is not stored; long conversations are shortened |

### 2.2 Environment

- **Server (target)**: one VPS, Ubuntu, 4 cores / 8 GB, about 30 GB free disk, many projects
  behind one host-level nginx. Each project is a rootless Podman container under its own Linux
  user, capped at about 1 CPU / 2 GB.
- **Gateway**: the owner's own OpenAI-compatible gateway ("apigate") on the same host at
  `http://127.0.0.1:18731/v1`. It pools the owner's provider keys (several model families),
  lists models at `GET /models`, and also serves ordinary bot traffic. Requests need a gateway
  key. It has no hard limit on tokens or requests per second, which must not be abused.
- **Storage**: an S3-compatible bucket that can be mounted as a folder (through SFTPGo), and
  PostgreSQL on the VPS. roj currently uses the bucket only for backups and does not use
  PostgreSQL (ADR-0007 states when that changes).
- **Earlier assumption, still supported**: a weak Windows laptop. Libraries must stay light
  and headless. `docs/FIRST_RUN.md` covers a local run.

### 2.3 Secrets

Only in environment variables, never in files, logs or exports:
`ROJ_API_KEY` (gateway key, required), `ROJ_TELEGRAM_TOKEN` (optional),
`ROJ_GITHUB_TOKEN` (optional, fine-grained, for selfdev pull requests),
`FRED_API_KEY` (optional, only for the market-data example skill).

---

## 3. Principles

1. **Goal over process.** Every entity (project, mission, stage, team, task) has a measurable
   goal and acceptance criteria. Without them it does not start.
2. **Models decide and create. Code executes, counts and verifies.**
3. **State lives outside agents' heads**: in the mission state, Blueprint, registries, event
   log, ADRs and the content store. Every agent, including the Leader, is replaceable at any moment.
4. **There is no single best architecture.** The Architect picks a working mode per team and combines modes.
5. **Context is the most expensive resource.** An agent gets only what it needs, in layers;
   the rest is available on request by reference.
6. **Truth comes from sources, tests and data, not from votes.**
7. **Gates are a ratchet.** They may rise on their own; only the owner lowers them.
8. **One owner per artifact, many eyes to check it.**
9. **What repeats becomes a skill. What breaks becomes a test.**
10. **Runs on one small machine with no service other than the model API; scales to servers without code changes.**
11. **Stages, not leaps.** Plan in detail only the next stage; let its results shape the one after.
12. **Proposals are cheap, changes are gated.** Any agent may propose; only measured, reviewed and approved changes land.

---

## 4. Conceptual model

```
Installation (ROJ_HOME; on the server ~/data of the Linux user roj)
├── shared for all projects
│     roj.toml (settings, model bindings, prices, permissions)
│     model registry, Playbook + model cards, pool of generated code, role registry, tool registry
│     logs/, backups/, exports/
└── projects/<name>/        isolated, run independently and in parallel
      project.toml (overrides: budget, leader, models, schedules, permissions)
      state.db   tasks, steps, events, budgets, approvals, knowledge, missions, caches
      cas/       content store (every prompt, answer, fetched page, step result)
      reports/   what humans read
      workspace/ files agents may write
```

- **Isolation** [built]: a project never sees another project's memory, logs or secrets. It
  shares only what it publishes to a pool, through gates. Flow is one way: project to pool.
- **Project** [built]: budget, event log, knowledge, approvals, reports, schedules.
- **Mission** [built]: one goal inside a project, with its questions, charter, roadmap, stages, lessons and directives.
- **Blueprint** [built]: a fixed, repeatable system of teams described as data (YAML), versioned, approved by the owner.
- **Skill** [built]: a repeatable task compiled once into a declarative spec plus gated code, then executed by code with a few model calls.
- **Task** [built]: one run of a workflow on the kernel; the unit of logging, cost and resumption.

---

## 5. Actors

| Actor | What it is | Status |
|---|---|---|
| **Owner** | The human. Highest authority. His corrections and directives outrank any plan | - |
| **Architect** | Model role that designs the way to a goal: stages, teams, roles, tools, knowledge, and a concrete model per agent. The owner binds it as `[models.architect]` | [built] in missions |
| **Leader** | Model role answerable for the result: critiques plans, reviews stage results against criteria with evidence, records lessons. Per-project `[leader]`, else `[models.leader]`. Must be another model family than the Architect (the doctor warns) | [built] in missions |
| **Workers** | Agents defined by the Architect for a stage: name, instructions, a concrete model and fallbacks chosen from the gateway's catalog, tools, one task, measurable criteria | [built] |
| **Guardian** | Consistency checker for multi-module projects; sees contracts, outputs, contract tests and a one-page report, never the inside of a module | [partial] exists in the Blueprint runner; contract tests and glossary are planned |
| **Lab** | Internal team that measures models and prompt knobs and maintains the Playbook. (Formerly called "Meta team"; renamed because it has nothing to do with the company Meta) | [built] as a workflow |
| **Librarian, rewriter, reranker, summarizer, examiner** | Small helper roles around knowledge and context | [built] as slots |

### 5.1 Leader without state [built in missions]

At every call the leads receive a brief assembled by code: goal, answers, owner directives,
charter, roadmap, lessons, summaries of finished stages, plus facts about the project (tools,
model bindings, knowledge packs, model cards, active Playbook, repository map). Swapping the
Leader's model means the next call goes to the new model with the same brief. Nothing is lost.

### 5.2 Routing by difficulty [planned]

Routine decisions go to a cheap model, medium ones to a mid model, hard ones (contradiction,
architecture, irreversible) to the strongest. In doubt, one level up. Today the binding per
role is fixed in settings.

### 5.3 Control modes

Two separate mechanisms exist and should not be confused:

- **Mission control mode** [built], per mission, switchable any time:

  | mode | owner talks to | owner approves |
  |---|---|---|
  | `leads` | Architect, Leader | each stage; everything from T2 up |
  | `partial` | + agents of the latest stage; their questions come to the owner | the same |
  | `full` | + every agent of any stage | + each agent's task before it runs |

- **Blueprint control mode** [built]: `manual` / `assist` (default) / `auto`. `auto` unlocks
  only after at least 90% of the last 20 proposed changes were approved without correction.

### 5.4 Autonomy levels [built in contracts and the agent loop]

L0 strict (follow the script, deviation is an error), L1 limited (chooses how inside its
step, may ask), L2 collaborative (may propose changes to its part of the plan), L3 owner of a
module end to end within budget. Decisions inside one's own scope are recorded as decision records.

---

## 6. From goal to result, step by step

This is the entry point of all new work. [built]

1. **Project.** `roj project create <name>` creates the isolated workspace.
2. **(Recommended) Know the models.** `roj lab study -p <name>` produces model cards and a
   Playbook proposal (section 15.1). The Architect sees the cards when staffing teams.
3. **Goal.** `roj mission start -p <name> "<goal>" [--control leads|partial|full]`.
   One call to the Architect with the brief and facts. It returns what it understood, names
   the biggest unknown, and asks up to 6 questions, each with a reason, options and a default.
4. **Answers.** `roj mission answer`. An unanswered question takes its default and is recorded
   as answered by default. Planning does not start while questions are open.
5. **Stage.** `roj mission next` runs one stage as a durable workflow:
   1. **Plan** (Architect): a charter (problem, success criteria, constraints, out of scope),
      a roadmap of 1 to 10 stage titles, and the next stage in detail: objective, criteria,
      needs (knowledge, tool, role, access, decision, data), a team of at most 5 agents, each with
      a concrete model and fallbacks from the catalog, risks.
   2. **Lint** (code): agent names unique, tools exist, models exist in the catalog and can call
      tools when the agent uses tools, each task has measurable criteria, no code-writing tools.
      **Critique** (Leader). Failures go back to
      the Architect; at most 3 rounds, then the stage fails loudly.
   3. **Gate**: the owner approves the stage (`t2.mission_stage`). Nothing runs before that.
   4. **Team**: agents run in order; each receives the earlier agents' results. In `full`
      mode each agent's task waits for approval (`t2.mission_agent_run`). Agents read the
      repository (read-only), search and write knowledge, fetch public pages, write workspace files.
   5. **Review** (Leader): verdict `done` / `rework` / `blocked` with evidence per criterion,
      lessons, questions for the owner, at most 2 change requests, and whether the mission is complete.
   6. **Close** (code): state updated, lessons appended, the stage report written to
      `reports/mission-<id>-stage-<n>-<date>.md`, each change request submitted as a selfdev task.
6. **Repeat.** The owner reads the report and runs `roj mission next`. The Architect plans
   stage N+1 from the lessons of stage N. Only one stage ahead is ever planned in detail.
7. **Talk.** `roj chat -p <name> --to architect|leader|<agent> "<text>"`. A chat message gets
   an answer and executes nothing. A standing instruction ("always", "never") is stored as a
   directive that every later planning and review step receives.

**Interfaces** [built]: the web panel (start a mission, answer questions, plan the next stage,
approve, see stages and reports, talk to agents) and the CLI share one state; Telegram carries approvals.

**Known limits of the mission layer** [partial]: agents of a stage run sequentially and do not
message each other; a stage team is not yet run through a Blueprint mode; stages are started by
the owner, not automatically.

---

## 7. Execution substrate

### 7.1 Durable kernel [built]

- A **workflow** is an ordered list of steps; a step is an async function receiving a context
  (project, task id, inputs, results of earlier steps).
- Each finished step is a **checkpoint**: its result goes to the content store. After a crash
  or `kill -9`, `resume` continues from the first unfinished step.
- Inside a long step, `ctx.memo(key, fn)` checkpoints sub-results (one per agent, per
  experiment stage) so a retried step does not redo paid work.
- Task statuses: `pending`, `running`, `waiting_approval`, `paused_budget`, `paused_api`,
  `failed`, `done`, `cancelled`. A task is locked by one process at a time.
- Every event of a task (model call, tool call, approval, budget) is attributed to that task.

### 7.2 Event log [built]

Append-only, in SQLite, each record chained by hash to the previous one; `verify` detects
tampering or loss. Secrets are masked before writing. The log is the single source of truth:
task logs, statistics and reports are rendered from it.

### 7.3 Content store (CAS) [built]

Blobs named by their sha256: prompts, answers, fetched pages, step results, elided tool
results. Identical content is stored once. References by hash replace repeated content.

### 7.4 Budgets [built]

Hierarchical scopes (`team:<t>/agent:<a>`, `skill:<s>/role:<r>`), in tokens and USD, per
project per day. A call reserves its worst case before it is sent and settles the real cost
after. An exhausted budget parks the task as `paused_budget`; it does not fail. Models without
a configured price are charged at a deliberately expensive default.

### 7.5 Permission tiers and approvals [built]

| Tier | Meaning | Default |
|---|---|---|
| T0 | Read, compute, no side effects | automatic |
| T1 | Write inside the project's own workspace or knowledge | automatic |
| T2 | Change the system: activate a skill, a Playbook, a stage, run a shell command | manual |
| T3 | Reach outside: send a message, publish, open a pull request | manual |
| T4 | Money or irreversible actions | manual; never plain `auto`, only `auto_with_limits` (per-action and per-day caps, allow-list) |

Action names start with their tier (`t3.open_pull_request`). Modes per action: `manual`,
`auto`, `auto_with_limits`, `deny`. A task that needs approval parks as `waiting_approval`;
the request goes to the outbox (Telegram buttons, panel, CLI). No answer before expiry means
no. Reports sent to the owner's own bot are not T3.

### 7.6 Reaction to gateway errors [built]

The class of the error decides, not its text:

| Error | Reaction |
|---|---|
| 429 rate limit | Wait and retry with backoff, respect the server's hint, then fall back |
| 5xx, timeout, reset | Retry with backoff, then fall back |
| 403 blocked | No retry; the model is blocked for hours; fall back immediately |
| 401 auth | No retry; report |
| 404 model gone | Mark gone; fall back |
| 400 about a sampling or limit parameter (`temperature` deprecated, `top_p must be 1`, `max_completion_tokens`) | Fixed automatically: the client applies the fix the error names, retries, and remembers it for that model (`roj models --quirks`); messages, tools and schemas are never touched |
| any other 400 bad request | Raised to the caller: it is our bug |

A circuit breaker opens after repeated failures of one model. If every candidate fails, the
task parks as `paused_api`, the owner is notified and the task resumes from its checkpoint later.

---

## 8. Models

- **Client** [built]: minimal OpenAI-compatible (chat completions, embeddings, model list).
  No provider SDKs and no LiteLLM in the core (ADR-0001).
- **Binding** [built]: `model` plus `fallbacks`. The owner binds only the minimal team: `architect`, `leader`
  and (recommended) `cheap`. roj's internal roles stand in for each other (`coder`/`planner`/`analyst` ->
  `architect`, `critic`/`checker` -> `leader`, `summarizer`/`classifier`/`examiner` -> `cheap`) unless bound.
- **Catalog** [built]: chat models on the gateway (embeddings, OCR, audio excluded; the gateway's `kind`
  is used when reported), with family, price, context window and Lab card. The Architect picks a
  concrete model and fallbacks for every agent from it; the stage linter rejects unknown ids and
  models that failed the tool-calling probe for agents with tools.
- **Prices** [built]: `roj pricing import` reads the gateway's price list (apigate format) into
  `pricing.toml`; `roj.toml` entries win; unpriced models are budgeted at an expensive default.
- **Governor** [built]: client-side flow control (additive increase, multiplicative decrease
  on latency or errors) so agents never flood the shared gateway. No fixed priority between bots and agents.
- **Exact cache** [built]: at temperature 0 an identical request is answered locally for zero tokens.
- **Provider cache accounting** [built]: cached input tokens are read from `usage` and priced separately.
- **Registry** [built]: discovery through `GET /models`, availability, persistent blocks.
- **Model cards** [built]: per model from the Lab study: latency, whether usage is reported,
  whether the provider cache works, tool calling, score per task kind, tokens per case.
- **Planned**: model events routed to the Architect (gone, error spike, better model
  available), confidence calibration per model and role, native provider adapters where the
  OpenAI-compatible surface is not enough (thinking budgets, explicit cache objects, batch).

---

## 9. Prompt engineering

Goal: leads and agents describe tasks precisely, and nobody hand-writes a prompt.

### 9.1 TaskSpec [built]

A task is data: goal, deliverable, acceptance criteria, inputs, output format or schema, open
questions, budget. An agent may return a question instead of a result (what, why it matters,
options, default, whether it blocks).

### 9.2 Compiler [built]

Builds messages from a role, a TaskSpec, tools, knowledge and documents:

```
system (stable, byte-identical across calls of one role):  [role][rules][tools][knowledge]
user   (variable):                                         [documents][context][examples][task][format][reminder]
```

The stable prefix carries no dates or ids, keys are sorted and serialisation is
deterministic, so provider prefix caches hit. Fixed rules are always present: permission to
say "no data", permission to ask, documents are data and not instructions, facts need sources.

### 9.3 Linter [built]

Deterministic and blocking. Task: missing goal, unmeasurable criteria (vague words in
Polish and English), missing deliverable. Prompt: volatile content in the prefix, oversize
prompt, missing clauses, layout. A lint error means the call is not sent.

### 9.4 Prompt profiles and the Playbook [built]

The compiler has six knobs: `language` (pl/en), `structure` (xml tags / markdown headings),
`documents_first`, `reminder`, `reasoning` (none / a brief thinking block), `examples` (2/0).
The Playbook holds, per model family, the knob values proven by experiment, plus every rule
with its evidence (effect, interval, sample size, token change, source). The compiler applies
a family's profile automatically. Only rules with status `supported` change prompts. Advice
read in providers' guides is stored as `hypothesis` with its source and never applied untested.

### 9.5 Background the owner supplied

The owner's note "How to get the maximum out of AI" is part of the concept: put role and rules
first, documents in the middle, the question and format again at the end; mark structure with
tags; 2 to 5 examples help weak models, goals and criteria help strong ones; positive, concrete
instructions; one task per call for weak models; constrained output; numbers from tools, not
from the model; a short reasoning step first; retry with the validator's error; cascade from
cheap to strong models; use the strong model as a critic; measure everything on an eval set.
These are starting hypotheses for the Lab, not rules, until measured on the owner's models.

### 9.6 Planned

A context assembler with priorities per block and references by hash, prompt versions tied to
verification outcomes (prompts that cause many questions or rejections return to their
author), knobs beyond the first six, profiles per task kind.

---

## 10. Knowledge

### 10.1 Knowledge packs [built]

A pack has a manifest (description, level, chunking parameters, embedding model, audience,
test questions) and documents split along headings with overlap. Retrieval is hybrid: BM25
(SQLite FTS5) and vectors (embeddings through the gateway), fused by reciprocal rank. Optional
helpers: contextual summaries per document, a query rewriter with its own corpus description
(the original query always runs too), a reranker. Each pack has test questions and a measured
recall@k; a failed retrieval becomes a new test question.

### 10.2 Selective use [built]

- **Audience**: a pack lists the roles that share it. Two agents of one speciality share one
  pack; other roles never see it.
- **Router** (`kb.retrieve`): a pack is searched only if its vocabulary covers the question; a
  chunk is injected only if it contains enough of the question's terms; the total is capped in
  tokens. If nothing fits, nothing is injected. All of this is arithmetic, no model calls.
- **Novelty gate** (`--novelty`): before storing a document, an examiner writes short-answer
  questions from it, the consuming model answers without the document, code grades. If the
  model already knows it, the document is not stored.
- **Agents write knowledge** (`kb.note`): results of analysis become packs later agents retrieve.

### 10.3 Evidence model (ADR-0002) [built for the example skill, general use planned]

Sources have tiers. A claim has a status: fact, supported, hypothesis, contested, refuted.
Independence is counted by origin, not by number of copies. Hypotheses are kept and labelled.

### 10.4 Compaction of long conversations [built]

When an agent's conversation exceeds a threshold: first old tool results are cut to a short
head (full text stays in the content store, no model call); if that is not enough, older turns
are replaced by progress notes from a cheap model. The system prompt and first message are
never touched (the cached prefix survives), the latest turns stay verbatim, and a tool call is
never separated from its result.

### 10.5 Planned

Levels beyond the project (installation-wide packs shared by projects, team and personal
indexes), a librarian that promotes good personal indexes, gating by vector similarity as well
as term coverage, facts with verbatim quotes checked by code as a general mechanism.

---

## 11. Tools and hands

### 11.1 Tool registry [built]

A Python function becomes a tool. Its card is generated from the real signature in three
layers: L0 (name and one sentence), L1 (signature, limits), L2 (full, on request through
`tools.describe`), so documentation cannot drift from code. Each tool has a tier, a timeout
and a declared side effect.

Built-in tools: `calc`, `time.now`, `web.fetch` (robots.txt respected, per-host pacing,
ETag cache), `files.read|list|write` (project workspace), `kb.search`, `kb.retrieve`,
`kb.note`, `tools.describe`, `shell.run` (T2), and for work on a git checkout
`repo.list|read|search` (read) and `repo.write|replace|test` (only inside a selfdev worktree).

### 11.2 Agent loop [built]

Plan, act through tools, observe, submit a typed result (status, summary, output, confidence,
or a question). Limits on steps, loop detection, compaction, budget scope per agent.

### 11.3 Sandbox for model-written code [built]

Used by compiled skills: an allow-list on the syntax tree, then execution in a separate
interpreter with resource limits. Selfdev tests run with an environment that holds no secrets,
with a timeout, and in a private network namespace when the kernel allows it.

### 11.4 Hands hierarchy [planned, except level 1 and parts of 2]

Always the highest available level: (1) API or library; (2) CLI or script; (3a) a real browser
driven over the debugging protocol, with separate persistent profiles per purpose and the port
bound to localhost; (3b) headless browser for anonymous bulk work; (4) UI automation;
(5) vision with mouse and keyboard as the last resort. Full computer access is a goal of the
project, to be reached in this order and behind permission flags.

### 11.5 Shared pool [partial]

Statuses draft, candidate, stable, retired exist for generated adapters, with automatic
promotion after use in at least 2 projects. Roles have a registry. Planned: pool for tools,
algorithms and team patterns with benchmarks and side-effect declarations.

---

## 12. Team modes

| Mode | When | Status |
|---|---|---|
| Strict workflow | Shape known and fixed; skills | [built] |
| Graph / state machine | Shape known, with branches and bounded loops | [built] |
| Orchestrator and workers | Goal clear, path unknown; task and progress ledgers | [built] |
| Sequential stage team | Mission stages today | [built] |
| Moderated team chat, private channels | Design problems needing exchange; off by default, with budget and moderator | [planned] |
| Typed protocols, Contract Net | Many teams, scarce resources | [planned] |
| Multi-family ensemble | High stakes for truth; differences are resolved by evidence, not majority | [planned] |
| Blackboard | Many independent contributions | [planned] |
| Debate | Decisions with trade-offs | [planned] |
| Handoff | A stream of varied requests | [planned] |

The Architect chooses and may combine. Cycles: plan, execute, verify (gates), review
(guardian), retro; a cycle is never shorter than honest verification takes; every cycle ends
with a gated increment or a documented failure plus a new test [planned as a general mechanism;
mission stages already follow this shape].

---

## 13. Skills and recurring work

- **Skill = code skeleton with slots** [built]. Mechanical parts are always code. A slot is
  the only place a model works: one call with a typed, schema-validated output and a repair loop.
- **Compile once** (ADR-0005) [built]: a bootstrap researches, measures and compiles a
  declarative spec plus gated adapters; daily runs execute it; repair is deterministic first.
- **Bootstrap is R&D** (ADR-0006): it is allowed to be about ten times dearer than a daily
  run, because whatever it learns no longer has to be paid for daily.
- **Scheduler** [built]: deliver at a local time, start early, catch up after downtime.
- **Example skill**: the Morning brief (overnight events and their economic effect; numbers
  only from market data; every claim with a source; checker of another model family). It is no
  longer the pilot. It stays as a worked example of the compile-once pattern.

---

## 14. Quality gates and the ratchet

- Gates at every stage [built where the stage exists]: TaskSpec lint, prompt lint, output
  schema, sources and numbers for content, tests for code, the owner's approval for publication.
- **Ratchet** [built]: an eval suite's threshold rises automatically when results exceed it
  stably; only the owner lowers it, with a note. About 20% of cases are hidden from agents.
- **Every escaped defect becomes a permanent test** [rule; enforced in selfdev by "tests are only added"].
- **Gates are immutable for agents** [built]: protected paths (section 15.3).

---

## 15. Self-improvement

Three loops, all ending at the owner.

### 15.1 Lab study: models and prompts [built]

`roj lab study -p <project>`:

1. **Inventory** (no tokens): which models (default: every model bound to a role), which
   cases, and whether the plan fits the token budget; it shrinks itself or stops with a clear message.
2. **Probe**: a technical card per model (4 small calls).
3. **Research**: the providers' own prompting guides go into a knowledge pack; claims are
   extracted with their source; a claim whose source cannot be found is dropped.
4. **Experiment**: for each knob, the same cases and the same model with the baseline and
   with the changed knob. Eight kinds of cases are generated from seeds and scored by code:
   extraction, transformation, grounded question answering (including unanswerable ones),
   prompt-injection resistance, multi-step arithmetic, classification, a needle in long
   context, strict nested output. Statistics: paired differences with a bootstrap interval.
   Verdict per model family: supported (better, or equal and at least 10% cheaper), refuted,
   inconclusive. Only promising but unproven effects buy a second stage.
5. **Confirm**: the combined profile per family against the default on hold-out cases no
   earlier stage has seen. Not confirmed means the default stays.
6. **Report and proposal**, then the owner's approval (`t2.activate_playbook`).

Baselines are shared by all experiments, and the exact cache makes a repeated study nearly free.

### 15.2 Selfdev: code [built]

`roj selfdev run -p <project> [--goal "..."]`, one small change per run:

1. **Survey** (no tokens): repository map, `docs/BACKLOG.md`, failure signals from the event
   logs of all projects, active Playbook, the digest of this document.
2. **Propose**: up to 5 ranked proposals. Code drops those touching protected paths, too
   large, or high risk.
3. **Plan gate**: the owner approves the chosen proposal (`t2.selfdev_start`).
4. **Build**: a coder agent works in a throwaway git worktree with `repo.*` tools. Gates in
   code: the diff is not empty; protected paths untouched; existing tests unchanged; at most
   400 changed lines; a code change comes with a new test; the whole suite passes; the test
   count does not drop. Two attempts; the second receives the failed checks.
5. **Review**: a model of another family reads the diff and may veto.
6. **Publish**: commit, patch file, the owner's approval (`t3.open_pull_request`), push and
   pull request. Without a GitHub token the result is a branch and a patch.

The running installation is never edited. Code reaches the server only by: pull request, the
owner merges, CI builds the image, the server pulls it.

### 15.3 Protected paths [built]

`roj/core/policy.py`, `roj/core/budget.py`, `roj/core/eventlog.py`, `roj/verify/*`,
`roj/lab/selfdev.py`, `roj/lab/tasks.py`, `scripts/*`, `.github/*`, `deploy/*`, `Dockerfile`,
`pyproject.toml`, `uv.lock`, `LICENSE`, `docs/CONCEPT.md`, and every existing file under
`tests/` (new test files may be added).

### 15.4 Missions feed selfdev [built]

A stage review may raise at most 2 change requests; each becomes a selfdev task with all of
the above. This is the only way mission work turns into code.

### 15.5 Planned

Shadow mode (a candidate runs beside the current version before promotion), promotion and
one-command rollback as a general mechanism, improvement of roles, team patterns and model
assignments by the same path, retros that write Playbook rules.

---

## 16. Token economy

The gateway is unlimited; the design goal is still the lowest cost per accepted result.

| Mechanism | Status |
|---|---|
| Byte-stable prompt prefixes, shared per role, so provider caches hit | [built] |
| Exact local cache for deterministic calls | [built] |
| Skills compiled once; daily runs mostly code | [built] |
| Budgets with reservations at project, team, agent and role level | [built] |
| Selective retrieval, novelty gate, token cap on injected knowledge | [built] |
| Compaction of long conversations | [built] |
| Tool cards in layers; full documentation only on request | [built] |
| Edits as exact replacements instead of rewriting files | [built] for repository work |
| Experiments that grow only when an effect is promising | [built] |
| Cost reported per role and per task | [built] |
| Routing by difficulty; cascade from cheap to strong with a validator | [planned] |
| Batch mode for non-urgent work | [planned] |
| Deduplication of research before it is queued | [planned] |
| Cost per accepted result as the headline number in reports | [planned] |

---

## 17. Observability and the owner's channels [built]

- Per task: a readable `.log` (steps with times, model use per role, HTTP, problems, timeline)
  and a raw `.jsonl`, under `logs/<project>/<date>/`. A rotating process log.
- `roj selftest`: the whole chain (mission, chat, selfdev to a pull request, Lab, Telegram, backup, export, log chain) offline in a throwaway home, zero tokens; also run inside the image in CI.
- `roj doctor [--live]`: configuration, gateway, bindings, prices, storage, selfdev, Playbook,
  heartbeat; with `--live` one small call per bound model.
- `roj logs export`: one zip with logs, settings, project state, reports, the Playbook, and
  optionally every prompt and answer. Secrets are not in it.
- `roj stats`, `roj budget show`, `roj approvals`, `roj mission show`, `roj lab playbook|cards`.
- Telegram: reports, approval buttons, ratings; only the owner's chat is accepted.
- Web panel on localhost with a token: approvals, tasks, logs, reports, budgets, gates.

---

## 18. Deployment and operations

- **Image** [written, not built by the author]: Python 3.12 slim, dependencies installed from
  the hash-checked lock file, git included, test dependencies included because selfdev runs
  the suite inside the container. Runs as a non-root user; data in `/data`.
- **CI** [written, not run]: tests on three operating systems and three Python versions plus
  a supply-chain gate (at most 5 core dependencies, licence allow-list, no `.pth` files);
  on `main`, after green tests: build, smoke test, push to GitHub Packages.
- **Server** [written, not run]: a Quadlet unit for rootless Podman with auto-update every 2
  minutes, health-gated start (a broken image rolls back), host network (to reach the gateway
  on localhost), read-only root, no capabilities, 1 CPU / 2 GB. Data in the Linux user's
  `~/data`, editable on the host.
- **One process**: `roj serve` runs the scheduler tick (Telegram clicks, resume of parked
  work, due schedules, notifications), the panel, a daily backup and a heartbeat.
- **Storage** (ADR-0007): SQLite on local disk; backups to the bucket; PostgreSQL only when
  several worker nodes must share a queue, the knowledge base exceeds about 100,000 chunks,
  or other services must query roj's state.
- **Core dependencies**: pydantic, httpx, PyYAML, tzdata (time zones where the OS has none, e.g. Windows). Python 3.11 or newer.

---

## 19. Safety model

1. Least privilege by tier; everything from T2 up waits for the owner by default.
2. Agents cannot weaken gates, policy, budgets, evals, CI, deployment or their own goals.
3. No hot patching: the running code changes only through git and the image.
4. Agent-written code runs without secrets, with limits, as an unprivileged user.
5. Documents and web pages are data, never instructions (stated in every prompt, tested by
   the injection eval kind).
6. Secrets only in environment variables; masked in logs; absent from exports.
7. Supply chain: a small core, a locked and hashed dependency set, a gate in CI.
8. Everything is reversible: Playbook versions, git history, image tags, backups.
9. External conduct: official APIs only, robots.txt respected, polite pacing, no evasion of
   limits or protections.

---

## 20. Repository map

| Path | Content |
|---|---|
| `roj/core/` | store (SQLite), event log, content store, budgets, policy, kernel, message bus |
| `roj/models/` | gateway client, error classes, governor, registry, catalog for the Architect, the full call stack |
| `roj/prompting/` | templates (pl/en), compiler, linter, profiles |
| `roj/agents/` | tool-calling agent loop, role registry, compaction |
| `roj/tools/` | registry with generated cards, built-in tools, repository tools |
| `roj/knowledge/` | packs with hybrid retrieval, router, novelty gate |
| `roj/skills/` | slots, sandbox, compiled-spec registry and pool, catalog, the Morning brief example |
| `roj/blueprint/` | schema, linter, versions with approval, runner (strict, graph, orchestrator, guardian) |
| `roj/lab/` | eval tasks, experiments, study, Playbook, selfdev |
| `roj/mission.py` | missions, stages, control modes, chat |
| `roj/verify/` | ratchet gates |
| `roj/hands/` | polite HTTP fetcher, feed parsing, market data |
| `roj/notify/`, `roj/panel/` | Telegram and outbox; local web panel |
| `roj/obs/` | task logs, doctor, diagnostic export |
| `roj/server.py`, `roj/scheduler.py` | `roj serve`, backups, schedules |
| `roj/testing/` | fake gateway, stand-in models, synthetic web (offline tests) |
| `roj/cli.py`, `roj/config.py`, `roj/project.py`, `roj/contracts.py` | command line, settings, wiring, typed contracts |
| `docs/` | this document, ADR-0001 to 0009, SERVER, WALKTHROUGH, FIRST_RUN, BACKLOG, architecture |
| `deploy/podman/`, `Dockerfile`, `.github/workflows/ci.yml` | deployment |
| `tests/` | 127 tests |

---

## 21. Current state, honestly

### 21.1 What exists

About 13,500 lines of library code, 127 tests, versions 0.1 to 0.5.1 written between
2026-11-06 and 2026-11-15 in one continuous design-and-build conversation between the owner
and an AI assistant. Phases: kernel and model stack; the Morning brief with a research
bootstrap; gates, Telegram, panel, knowledge packs, agent loop, Blueprint; then server
runtime, Lab, selfdev, selective knowledge, compaction; then missions, control modes and
container deployment.

### 21.2 What has never happened

- No call to a real model. All token and cost figures in older documents are either measured
  on stand-in models (which answer in a few dozen tokens) or estimates.
- No run on the server. The image was never built; the Quadlet unit and CI image job were
  never executed.
- No real web research except manual verification of a few feed addresses.
- No real pull request, no real Telegram message, no Windows run.

### 21.3 Known weaknesses (starting material for the improvement loop)

1. **No baselines.** None of the metrics in section 22 has a real value yet.
2. **Stand-in models hide real failure modes**: malformed JSON, refusals, tool-call formats
   that differ between families, long answers, slow responses, partial outages.
3. `roj serve` runs long tasks inside its tick, so Telegram clicks wait until a study or a stage ends.
4. Mission stage teams are sequential; agents cannot message each other; no Blueprint mode is used.
5. The Architect's model choices rest on prices and Lab cards; without a study it picks from names and prices only.
6. The knowledge router gates on term coverage only; cross-language or paraphrased matches can be missed.
7. Prompt profiles are per model family, not per task kind, and a fallback model receives the primary model's profile.
8. Six prompt knobs is a small space; the eval cases are synthetic and short, so a profile
   that wins there is only evidence, not proof, for real tasks.
9. Leader routing by difficulty, model events, calibration and batch mode are not built.
10. Six of ten team modes, cycles with retros, remote nodes, browser and desktop hands are not built.
11. Selfdev tests are not network-isolated where the kernel forbids unprivileged namespaces.
12. That the Architect and the Leader are different families is only warned about by the doctor, not enforced.
13. The repository has no generated description of its own modules beyond first docstring lines; agents must read code to understand it.
14. Only one change per selfdev run and a 400-line limit: safe, and slow.

---

## 22. Objectives of the improvement loop

### 22.1 The objective

Make roj better at reaching goals for the same or lower cost, in a way that is measured,
reviewable and reversible, until the owner can hand it a goal and mostly approve.

### 22.2 What "orders of magnitude" can honestly mean

The models' raw ability is fixed by the providers. What roj controls, and where tenfold
changes are realistic over many stages:

- tokens per accepted result on recurring work (compile once, cache, route to cheaper models)
- the owner's minutes per accepted result (better plans, better verification, automatic flags)
- the share of failures caught before the owner sees them
- the range of goals that can be attempted at all (hands, modes, knowledge)

No single change will deliver that. A long series of small measured ones can.

### 22.3 Metrics (all computable from the event log and the content store)

| # | Metric | Definition | Direction |
|---|---|---|---|
| M1 | Cost per accepted result | tokens and USD of tasks that ended `done` and passed their gates, divided by their count, per workflow kind | down |
| M2 | First-pass validity | share of slot and agent outputs accepted without repair, retry or fallback | up |
| M3 | Cached input share | provider-cached plus exact-cache input tokens over all input tokens, per role | up |
| M4 | Owner attention | approvals needed per accepted result; share approved without correction | down; up |
| M5 | Failure rate | failed tasks, exhausted model chains, agents hitting the step limit or looping, per 100 tasks | down |
| M6 | Escaped defects | defects found after the gates (by the owner, a later stage or production) per accepted result | down; each becomes a test |
| M7 | Lab score | mean eval score per model with the active Playbook, including hold-out cases | up |
| M8 | Time to result | wall time from task start to accepted result | down, never at the cost of M6 |
| M9 | Selfdev yield | pull requests merged over proposals started; tokens per merged pull request | up; down |
| M10 | Knowledge value | share of retrievals that returned something the agent then cited; tokens of knowledge injected per accepted result | up; down |

### 22.4 Expected path (the Architect decides the actual stages)

1. **Come alive**: install on the server, `roj doctor --live`, fix whatever the first contact with real models breaks.
2. **Measure**: Lab study on the owner's models; record baselines for M1 to M5 and M7.
   Deliverable: model cards, a first Playbook, a baseline table.
3. **Know itself**: agents describe every module, its contracts and its weak points into
   knowledge packs, so later agents retrieve instead of re-reading code.
   Deliverable: packs with test questions and a measured recall.
4. **See itself**: a report that computes M1 to M10 from the logs on demand.
5. **First measured improvements**: cheap, low-risk items (repair malformed JSON in code
   before paying for a retry; send fewer tool schemas; cheaper summarizer). Each through selfdev, each with before and after.
6. **Remove the bottlenecks of the loop itself**: long tasks beside the tick, parallel stage
   agents, missions in the panel, more than one change in flight.
7. **Widen the lab**: new knobs from the hypotheses, profiles per task kind, eval cases drawn
   from real failures, model routing by card.
8. **Widen capability**: remaining team modes, cycles with retros, nodes, browser, desktop.
9. **Let go gradually**: flags to automatic where the approval record justifies it.

`docs/BACKLOG.md` lists concrete candidate items; agents may append to it through pull requests.

### 22.5 Acceptance of the mission "roj improves roj"

- A baseline table for M1 to M5 and M7 exists and is regenerated by one command.
- At least 10 Playbook rules have experimental evidence, and at least one improves the
  hold-out score or cuts tokens by 10% or more at equal quality.
- At least 5 merged pull requests originated from selfdev, each with a new test and a measured effect or a removed failure.
- M1 for mission stages has fallen by at least half from its baseline with M6 not rising.
- Every module has a description in a knowledge pack that passes its recall test.
- The owner approves at least 90% of the last 20 stage plans without correction.

These are first targets, not the end. The owner sets the next ones.

### 22.6 What is not a goal

- Rewriting what works for elegance.
- New dependencies or frameworks without an ADR accepted by the owner.
- Raising scores on the eval cases by tuning to them (the hold-out and hidden cases exist to catch this).
- Autonomy for its own sake. Automatic flags follow evidence.
- Product features unrelated to making agents' work cheaper, more reliable or more capable.

---

## 23. Roadmap by phase

| Phase | Content | Status |
|---|---|---|
| 0 | Contracts, kernel, hash-chained log, content store, budgets, policy, model stack, CLI | done offline |
| 1A | Compile-once skills, research bootstrap, Morning brief, scheduler | done offline |
| 1B | Ratchet gates, Telegram, panel, knowledge packs, tools, roles, agent loop, Blueprint with three modes | done offline |
| 0.3 | Server runtime, Lab study and Playbook, selfdev, selective knowledge, compaction | done offline |
| 0.4 | Missions, control modes, chat, container deployment | done offline |
| next | First real run; baselines; the path of section 22.4 | not started |
| later | Remaining team modes, cycles and retros, guardian with contract tests, remote nodes, native provider adapters | planned |
| later | Hands: browser over the debugging protocol, headless, UI automation, vision; shadow mode; migration of the Lineage 2 project | planned |

---

## 24. Positions held in disagreement (so nobody reopens them by accident)

1. "Straight into the shared base": yes, with statuses. One broken parser used at once by 20 agents is 20 wrong results.
2. Agreement of models is not truth. Different families make the same mistakes. Evidence decides.
3. Short cycles only where verification is short. Speed is a result of structure and tests.
4. Free team chat is off by default; it is the fastest way to a token furnace.
5. Automatic mode for the Leader only after a measured record of approvals.
6. T4 automatically only with limits.
7. Human team structures need not be copied, except one rule: one owner per artifact.
8. A real browser with sessions gets separate profiles and a local port; the owner's own profile is never available to agents.
9. Own thin core instead of a framework (ADR-0001): frameworks own prompts and state, change
   often, and a dependency in this space was compromised on the package index in March 2026.
10. PostgreSQL and the bucket are not used for live state yet (ADR-0007).
11. Later phases were deliberately not built blind before a first real run.

---

## 25. Glossary

| Term | Polish term used by the owner | Meaning |
|---|---|---|
| Owner | właściciel | The human in charge |
| Leader | Przewódca / Przywódca | Model role answerable for results |
| Architect | Architekt | Model role that designs stages and teams |
| Swarm | rój | The agents of an installation; the name of the project |
| Mission | misja | A goal worked on in stages |
| Stage | etap | One planned, approved, executed and reviewed step of a mission |
| Blueprint | Blueprint | A fixed system of teams as data |
| Skill | skill / umiejętność | A recurring task compiled into code plus slots |
| Slot | slot | The one place in a skill or workflow where a model works |
| Gate, ratchet | bramka, zapadka | A check; a threshold that only rises |
| Knowledge pack | pakiet wiedzy | A retrievable, tested body of knowledge with an audience |
| Playbook | Playbook | Proven prompt rules and profiles per model family |
| Lab | (dawniej Zespół Meta) | The team and workflow that measure models and prompts |
| Selfdev | samodoskonalenie | The gated path by which roj changes its own code |
| Gateway | bramka API (apigate) | The owner's OpenAI-compatible endpoint |
| Tier T0 to T4 | poziomy T0–T4 | Risk classes of actions |
| CAS | magazyn CAS | Content-addressed store |
| ADR | ADR | A recorded architecture decision |

---

## 26. History of the main decisions

| Date | Event |
|---|---|
| by 2026-11-06 | Concept v1 (several concepts, argued). About 30 review comments from the owner |
| 2026-11-06 | Concept v2: any Leader, Blueprint, ten team modes, autonomy levels, guardian, cycles, Playbook, multi-level knowledge, pools with statuses, concrete models with fallbacks, token economy, nodes, hands, ratchet, Lab, permissions |
| 2026-11-07 | Owner: sources are not always true; name `roj`; localhost first; English everywhere, Polish for agents; 1M tokens and 5 USD per project per day. Decision to build an own thin core (ADR-0001) |
| 2026-11-07/08 | Phases 0, 1A, 1B. Owner: the first run must research, later runs must be cheap. Owner questions why the bootstrap is not about ten times dearer: bootstrap deepened (ADR-0006) |
| 2026-11-15 | Owner: server exists (VPS, gateway, bucket, PostgreSQL); no local tests; the pilot becomes roj improving roj; selective knowledge, shared knowledge per speciality, no storing of known facts, shortening of long chats. v0.3 |
| 2026-11-15 | Owner: control modes from full to leads only; entry point is a goal to the Architect and the Leader, in stages; deployment like the gateway project (image in GitHub Packages, server pulls); "Meta" renamed. v0.4 |
| 2026-11-15 | Owner asks for this document as the reference for the improvement loop. v0.4.1 |
| 2026-11-16 | Owner: agents' roles are invented by the Architect, so the owner should not bind models per role. The owner binds only the Architect, the Leader and a cheap model; the Architect assigns models to agents from the gateway's catalog; missions in the web panel; prices imported from the gateway. v0.5.0 |
| 2026-11-16 | First real `roj doctor --live` on the server: Claude Opus/Haiku 5.5 reject `temperature`, Mistral Large 4 requires `top_p = 1` for greedy sampling. The client now learns such request fixes per model from the provider's own 400 errors. v0.5.1 |
