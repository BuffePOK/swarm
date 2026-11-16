# From zero to "roj improves roj", step by step

Everything below is done by you; roj stops and waits wherever a decision is yours.
`roj` means `podman exec -it roj roj` on the server (docs/SERVER.md).

## 0. What exists after installation

Nothing but an empty home: no project, no agents, no knowledge. Check: `roj doctor --live`.

## 1. A project

```bash
roj project create lab
```

A project is an isolated workspace: its own budget (1M tokens and 5 USD per day), event log,
knowledge, approvals and reports. Look at it: `roj project list`, `~/data/projects/lab/`.

## 2. (Recommended first) Know your models

```bash
roj lab study -p lab                  # stops with status waiting_approval
```

Read `~/data/projects/lab/reports/study-*.md`: a card per model (score per task kind, tokens
per case, latency, provider cache, tool calling) and, per prompt knob and model family, the
measured effect with its interval. Approve (Telegram / panel / `roj approvals list -p lab`,
`roj approvals approve -p lab <id>`), and the Playbook becomes active: `roj lab playbook`.
The Architect sees the model cards when it staffs teams.

## 3. The goal: a mission

```bash
roj mission start -p lab "Improve the roj project using roj itself: lower token cost per result, fewer failures, better verification. Work in stages; start by understanding the code." --control full
```

One call to the Architect. It receives the goal plus facts gathered by code (tools, model
bindings, knowledge packs, model cards, Playbook, repository map) and returns what it
understood and up to 6 questions, each with a reason, options and a default.

```bash
roj mission answer -p lab             # asks one by one; Enter accepts the default
roj mission show -p lab               # state at any time
```

## 4. One stage at a time

```bash
roj mission next -p lab
```

1. **Plan** (Architect): charter (problem, success criteria, constraints, out of scope), a
   roadmap of stage titles, and the next stage in detail: objective, criteria, needs
   (knowledge, tools, roles, decisions), and a team of at most 5 agents, each with
   instructions, a concrete model and fallbacks chosen from the gateway's catalog, tools, one task
   and measurable criteria. You bind only the Architect, the Leader and a cheap model.
2. **Lint** (code): tools exist, bindings exist, criteria are measurable, no code-writing tools.
   **Critique** (Leader, another model): approve or send back. Up to 3 rounds.
3. **Your approval** of the stage (`t2.mission_stage`): you see objective, criteria, team and needs.
4. **Team works.** In control mode `full` every agent's task waits for your approval first.
   Agents read the repository, analyse and write knowledge (`kb.note`) that later agents retrieve.
5. **Review** (Leader): verdict against the criteria with evidence, lessons, questions for
   you, and at most 2 change requests.
6. **Close**: the stage report `reports/mission-<id>-stage-<n>-<date>.md`; each change request
   becomes a `selfdev` task with its own approvals, gates and pull request.

Then read the report and run `roj mission next` again: the Architect plans stage N+1 from the
lessons of stage N. Nothing is planned in detail further ahead than one stage.

## 5. Talking to agents

```bash
roj chat -p lab --to architect "Why this team and not a single agent?"
roj chat -p lab --to leader "Always prefer the cheapest model that passes the criteria."
roj chat -p lab --to w-code-analyst "Which module was hardest to describe?"
roj mission control -p lab leads      # or partial / full, at any time
```

| mode | you talk to | you approve |
|---|---|---|
| `leads` | Architect, Leader | each stage, everything from T2 up |
| `partial` | + agents of the latest stage; their questions come to you | the same |
| `full` | + every agent of any stage | + each agent's task before it runs |

A standing instruction in a chat ("always...", "never...") is recorded as a directive and
shown to the Architect and the Leader in every later planning and review step.

## 6. Where to look

| what | where |
|---|---|
| what happened, step by step, with costs per role | `roj logs show -p lab`, `~/data/logs/lab/<date>/*.log` |
| every prompt and answer | `roj logs export --days 2` (zip) |
| money and tokens | `roj budget show -p lab`, `roj stats -p lab` |
| what waits for you | `roj approvals list -p lab`, Telegram, panel |
| knowledge the agents built | `roj kb list -p lab`, `roj kb search -p lab <pack> "<question>"` |
| code changes proposed | pull requests on GitHub, `reports/selfdev-*.md` |

## 7. Letting go, gradually

When the approval history justifies it, switch single flags in `roj.toml` `[permissions]` to
`"auto"` (`t2.mission_stage`, `t2.selfdev_start`, `t3.open_pull_request`), add schedules
(`examples/project.lab.toml.example`). Merging pull requests stays yours.
