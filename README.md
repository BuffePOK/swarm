# roj

A multi-agent system where language models decide and create, and code executes, counts
and checks.

roj gives a Leader model, its Architect and their teams a durable kernel: every step is a
checkpoint, every model call is budgeted in tokens **and** money, every external action
goes through owner-controlled permission flags, and everything is written to a
tamper-evident event log.

> **Status: v0.5.0, 123 tests, verified offline only (fake gateway); not yet run against real models.**
> Server install (rootless Podman, image from GitHub Packages, auto-update): [docs/SERVER.md](docs/SERVER.md).
> **Start here:** [docs/TUTORIAL.pl.md](docs/TUTORIAL.pl.md), from the zip to a running server and the first mission, with a check after every step (`roj selftest` verifies an installation offline).
> From zero to the first mission, step by step: [docs/WALKTHROUGH.md](docs/WALKTHROUGH.md).
> The whole concept, state and objectives in one place (also what the improvement loop reads): [docs/CONCEPT.md](docs/CONCEPT.md).

## Principles

1. Goal over process: every task has measurable acceptance criteria or it does not start.
2. LLMs decide and create; code executes, counts and verifies.
3. State lives outside the agents (blueprints, ledgers, ADRs, CAS), so any agent,
   including the Leader, can be replaced at any time.
4. There is no single best architecture: the Architect picks a team mode per goal.
5. Context is the most expensive resource: agents get only what they need, the rest by hash.
6. Truth comes from evidence, and sources have credibility; hypotheses are kept and labelled
   ([ADR-0002](docs/adr/0002-evidence-model.md)).
7. Quality gates are a ratchet: they may tighten automatically, only the owner loosens them.
8. One owner per artifact, many eyes to check it.
9. What repeats becomes a skill. What breaks becomes a test.
10. Runs on a laptop with no services except the model API; scales to servers without code changes.

## What works today

| Area | What you get |
|---|---|
| Kernel | Durable workflows; completed steps never re-run; resume after `kill -9`; tasks pause on budget exhaustion or API outage and park while waiting for approval |
| Event log | SQLite, hash-chained; `roj log verify` finds the first edited or deleted event; secrets masked before writing |
| CAS | Content-addressed blobs (prompts, responses, results) with hash check on read |
| Budgets | Tokens and USD per project per day (default 1M tokens, 5 USD), hierarchical scopes `team:x/agent:y`, reservations so concurrent agents cannot overshoot |
| Policy | Tiers T0–T4, `manual` / `auto` / `auto_with_limits` / `deny` flags per action, approval queue; no answer means "no"; T4 can never be plain `auto` |
| Model stack | OpenAI-compatible client, concrete model + fallback chain per agent, exact cache, provider prefix-cache accounting, AIMD governor, circuit breaker, persistent 403 blocks |
| Prompt compiler | TaskSpec → messages with a byte-stable prefix (shared provider cache), injection-safe documents, goal and format repeated at the end, Polish or English scaffolding |
| Linters | TaskSpec (measurable criteria, deliverable, budget) and prompt (timestamps or UUIDs in the prefix, size, required clauses) — errors block the call |
| Registry | Model discovery via `GET /models`, gone/returned models, blocks |
| Supply chain | ≤ 5 core deps (today 3), license allow-list, `.pth` detector, hashed `uv.lock` |
| Skills (1A) | Compile once, run cheaply: bootstrap researches and writes a declarative spec plus gated adapters; daily runs are code with 2–3 model calls; repair rediscovers moved feeds without tokens ([ADR-0005](docs/adr/0005-skill-compilation.md)) |
| Slots (1A) | Model calls with an output schema, validation and bounded repair loop |
| Sandbox (1A) | Model-written parsers: AST allow-list, `python -I -S` subprocess, timeout, CPU/memory limits, output schema |
| Hands (1A) | Polite fetcher (robots.txt, conditional GET, per-host pacing, key masking), RSS/Atom/RDF parser, feed discovery, market data (Frankfurter, CoinGecko, FRED, generic JSON/CSV) |
| Delivery (1A) | Markdown report per day, Telegram to the owner's chat, scheduler with lead time and catch-up after sleep |
| Deep bootstrap (1B) | Discovery rounds against coverage gaps, teacher labels as an eval set (20% hidden), rule induction measured by code, NDCG ranking calibration, research report ([ADR-0006](docs/adr/0006-bootstrap-cost.md)) |
| Logs (1B) | Readable `.log` + raw `.jsonl` per task, rotating `roj.log`, `roj doctor [--live]`, `roj logs export` diagnostic zip with secrets masked |
| Ratchet (1B) | Eval suites whose thresholds rise on their own; only the owner lowers them, with a note; hidden cases |
| Owner channels (1B) | Telegram approval and rating buttons (owner chat only), outbox, local web panel with token auth |
| Knowledge packs (1B) | Chunking with context, SQLite FTS5 BM25 + gateway embeddings fused by RRF, query rewriter, reranker, recall@k gate |
| Agents (1B) | Tool registry with signature-generated cards, roles registry, tool-calling agent loop with autonomy levels and loop guards |
| Blueprint (1B) | Project system as YAML: linter, versions with owner approval, ADR per activation, auto mode only after a 90% approval record; runner with strict, graph and orchestrator modes, memo checkpoints, guardian |

### How the system reacts to API problems

roj does not fight the API; it reacts and keeps the work safe.

| Gateway answer | Reaction |
|---|---|
| 429 rate limit | wait `Retry-After` (bounded), governor halves concurrency, then next model |
| 5xx / timeout | exponential backoff with jitter, breaker counts failures, then next model |
| 403 blocked | **no retry**, model blocked for hours (persisted), alert, next model |
| 401 auth | no retry, alert, next model |
| 404 model gone | registry marks it gone, next model |
| 400 bad request | raised to the caller: a fallback must not hide our own bug |
| every model failed | task paused as `paused_api` from its last checkpoint |

## Install

Python 3.11+.

```bash
git clone <repo> roj && cd roj
uv sync --locked --extra dev        # or: pip install -e ".[dev]"
```

Choose where roj keeps its state (on Windows, e.g. a data drive):

```bash
export ROJ_HOME=~/.roj              # Windows PowerShell: $env:ROJ_HOME = "D:\roj-home"
roj init
```

## Quick start without spending tokens

roj ships an offline OpenAI-compatible gateway whose model names trigger every failure path.

```bash
python -m roj.testing.fake_gateway --port 8045 &
# in roj.toml: base_url = "http://127.0.0.1:8045/v1"
roj project create demo
roj models --refresh
roj ask -p demo -m fake-blocked -f fake-ok "hello"      # 403, falls back once, model blocked
roj task run -p demo roj.examples.hello:WORKFLOW -i topic=roj -i model=fake-ok -i publish=true
roj approvals list -p demo                              # the publish step waits for you
roj approvals approve -p demo <ID>
roj task resume -p demo
roj stats -p demo                                       # tokens, cost, cache ratio, fallbacks
roj log verify -p demo
```

## Morning brief

The first skill. Every morning at 08:00 (Europe/Warsaw) you get what happened overnight and
why it matters for the world economy, in English, on Telegram and as a Markdown file.

```
bootstrap (once, expensive)                   run (daily, cheap)
probe 16 seed sources + instruments           fetch feeds, parse, cluster, rank   (code)
planner chooses portfolio + topic rules       classify only what rules missed     (cheap model, often 0 calls)
probe the planner's extra candidates          market data                         (code)
critic from another family                    analyst: top 6 events, cited claims (1 call)
planner revises                               checker from another family         (1 call)
coder writes parsers for pages w/o feeds      gates: drop unverified claims and
dry run on captured fixtures                    sentences with invented numbers   (code)
owner approves activation (T2)                Markdown + Telegram, history        (code)
```

Try it offline (synthetic world, no tokens):

```bash
python -m roj.testing.fake_gateway --port 8045 &
python -m roj.testing.fake_web --port 8046 &
# roj.toml: bind every role to fake-smart / fake-smart-b (see tests/conftest.py)
roj project create morning
roj skill bootstrap -p morning news_digest --seeds offline:http://127.0.0.1:8046
roj approvals list -p morning && roj approvals approve -p morning <ID> && roj task resume -p morning
roj skill run -p morning news_digest --no-send
```

Real setup on Windows:

1. Bind models per role in `roj.toml` (`[models.planner]`, `critic`, `coder`, `classifier`,
   `analyst`, `checker`), with the checker and critic from a different family than the analyst
   and planner.
2. Optional but recommended: a free FRED key (`FRED_API_KEY`) for S&P 500, Nasdaq, 10y yield,
   Brent and VIX. FX (Frankfurter) and Bitcoin (CoinGecko) need no key.
3. Telegram: create a bot with @BotFather, set `ROJ_TELEGRAM_TOKEN`, send the bot a message,
   read your chat id from `https://api.telegram.org/bot<TOKEN>/getUpdates`, put it in
   `[notify] telegram_chat_id`.
4. `roj skill bootstrap -p morning news_digest`, review the plan, approve, resume.
5. `roj schedule add -p morning news_digest --at 08:00`, then
   `roj scheduler windows-task` prints the two `schtasks` commands (daily at 07:40 and at
   logon for catch-up). Runs missed while the laptop slept still happen until 12:00.

Seed sources (checked 2026-11-07; the probe on your machine has the final word):

| Tier | Sources |
|---|---|
| primary | Federal Reserve, ECB, Bank of England (verified), US BEA |
| secondary | MarketWatch (verified), WSJ Markets, CNBC, BBC Business, The Guardian Business, NYT Business, FT Markets, The Economist, SCMP |
| unknown | OilPrice.com, Investing.com, CoinDesk |
| left out | Bloomberg and Nikkei Asia RSS: robots.txt disallows automated access |

## Use your gateway

Edit `$ROJ_HOME/roj.toml`:

```toml
[gateway]
base_url = "http://localhost:8045/v1"
api_key_env = "ROJ_API_KEY"   # the key is read from the environment, never stored

[pricing.models."claude-opus*"]   # USD per 1M tokens; exact ids or globs
input = 5.0
cached_input = 0.5
output = 25.0
```

Unknown models use a deliberately expensive default price, or are refused with
`strict = true`. Per-project overrides live in `projects/<name>/project.toml`
(budget, timezone, language, permissions, leader model).

## Write a workflow

```python
from roj.config import ModelBinding
from roj.core.kernel import Step, StepContext, Workflow

async def fetch(ctx: StepContext) -> dict:          # deterministic code
    return {"items": ["..."]}

async def analyse(ctx: StepContext) -> dict:        # a model call, budgeted and traced
    r = await ctx.chat(binding=ModelBinding(model="gemini-flash", fallbacks=["deepseek-flash"]),
                       messages=[...], agent="analyst", scope="team:news/agent:analyst",
                       params={"temperature": 0})
    return {"text": r.text}

async def publish(ctx: StepContext) -> dict:        # side effect: gated by policy
    ctx.require("t3.publish_post", {"text": ctx.results["analyse"]["text"]})
    return {"published": True}

WORKFLOW = Workflow("news", (Step("fetch", fetch), Step("analyse", analyse), Step("publish", publish)))
```

Each step's return value is stored in CAS. If the process dies, `roj task resume`
continues from the first unfinished step.

## Commands

```
roj init
roj project create NAME [--tokens N] [--usd X] [--timezone TZ] | list
roj models [--refresh] [--unblock MODEL]
roj ask -p P -m MODEL [-f FALLBACK]... "prompt"
roj task run -p P module:WORKFLOW [-i key=value]... | resume [ID] [--include-paused] | list | show ID | cancel ID
roj approvals list | approve ID | reject ID [--note]
roj budget show | allocate SCOPE [--tokens N] [--usd X]
roj log verify [-p P] | tail -p P [-n N] [--type T]
roj stats -p P [--task ID]
roj skill list | bootstrap -p P SKILL [--seeds S] | run -p P SKILL [--no-send] | repair -p P SKILL --source ID | status -p P SKILL
roj schedule add -p P SKILL --at HH:MM [--lead MIN] [--grace H] | list -p P
roj scheduler tick | run [--interval S] | windows-task [--start HH:MM]
roj pool list
roj logs show -p P [TASK] | list -p P | export [--days N] [--no-prompts]
roj doctor [--live]
roj panel [--port 8765]
roj telegram poll [--forever] | test
roj rate -p P TASK SCORE [--comment ...]
roj kb create|add|search|question|eval|list -p P ...
roj gates show -p P | lower -p P SUITE METRIC VALUE --note ...
roj blueprint lint|propose|activate|show|run -p P ...
roj roles list | add FILE
roj tools list [--level 0|1|2]
```

Add `--json` before the command for machine-readable output.

## Layout

```
roj/
  contracts.py   Envelope, TaskSpec, Question, DecisionRecord, Source, Claim
  config.py      roj.toml / project.toml, pricing, bindings
  core/          kernel, event log, CAS, store, budget, policy, bus   (owner-only)
  models/        client, errors, caller, governor, registry
  prompting/     compiler, linter, templates (pl, en)
  hands/         http fetcher, feeds, market data
  skills/        slots, sandbox, spec registry + pool, catalog
    news_digest/ spec, seeds, engine (deterministic), roles, workflows
  notify/        Telegram
  scheduler.py   daily schedules
  obs/           task logs, doctor, diagnostic export
  verify/        ratchet gates (owner-only)
  knowledge/     knowledge packs
  tools/         tool registry and built-in tools
  agents/        roles and the agent loop
  blueprint/     schema, linter, versions, runner and modes
  panel/         local web panel
  testing/       fake gateway, fake brains, synthetic web
  examples/      hello workflow
docs/adr/        architecture decisions
scripts/         supply-chain gate
```

## roj improves roj (v0.3)

```bash
roj project create lab
roj mission start -p lab "<goal>"   # the entry point: Architect asks, plans one stage at a time
roj mission answer -p lab ; roj mission next -p lab
roj chat -p lab --to leader "<message>"
roj lab study -p lab      # what each model can do, which prompt layout each family wants
roj lab playbook           # the active prompt profiles and the evidence behind them
roj selfdev run -p lab     # one gated pull request to roj's own repository
roj serve                   # server mode: schedules, Telegram approvals, panel, daily backup
```

- **Mission**: a goal for the Architect and the Leader. Questions to the owner, a charter, a
  roadmap, then one stage at a time: plan, lint, critique, owner approval, a minimal team,
  review with evidence, lessons, change requests. Control modes `leads` / `partial` / `full`
  decide whom the owner talks to and whether each agent's task needs his approval
  ([ADR-0009](docs/adr/0009-missions-and-control-modes.md)).
- **Study**: paired A/B experiments on prompt knobs (language, structure, document position,
  reminder, reasoning step, examples), scored by code on generated cases, confirmed on hold-out
  cases. The result is a Playbook: one prompt profile per model family, applied by the
  compiler only after the owner approves. Reading providers' guides produces labelled
  hypotheses, never rules.
- **Selfdev**: survey -> proposals -> owner approves the plan -> coder agent in a throwaway git
  worktree -> gates in code (protected paths, new test required, whole suite, size limit) ->
  review by another model family -> owner approves -> pull request. Merge and deploy stay with
  the owner and CI.
- **Selective knowledge**: packs have an audience of roles, a router injects only relevant
  chunks under a token cap (`kb.retrieve`), and `roj kb add --novelty` stores a document only
  if the model fails a closed-book exam on it.
- **Compaction**: long agent conversations keep the cached prefix and the latest turns; old
  tool results are cut, older turns become progress notes.

## Roadmap

- **Phase 1A (done)** — skills, slots, sandbox, shared pool with statuses, hands for the web,
  market data, Telegram delivery, scheduler, pilot skill "Morning brief".
- **Phase 1B (done)** — deep bootstrap, task logs and diagnostics, ratchet gates, Telegram
  buttons and local panel, knowledge packs, tools and roles, tool-calling agents, Blueprint
  with strict / graph / orchestrator modes and a guardian.
- **v0.3 (done, offline)** — server runtime and CI/CD, Lab study and Playbook, selfdev pull
  requests, selective knowledge, compaction. The Morning brief stays as an example skill.
- **Next** — first real study and first real pull request on the server; then the items in
  [docs/BACKLOG.md](docs/BACKLOG.md), most of them through selfdev. Remaining team modes (chat,
  protocols, ensemble, blackboard, debate, handoff), rework cycles and remote nodes.
- **Phase 3** — Browser (real CDP sessions and headless), skill compiler, desktop automation,
  improvement loop with shadow mode; Lineage 2 project migrates onto roj.

## Decisions

- [ADR-0001](docs/adr/0001-build-vs-reuse.md) own thin core, reuse libraries for commodity layers
- [ADR-0002](docs/adr/0002-evidence-model.md) evidence model, hypotheses are first-class
- [ADR-0003](docs/adr/0003-languages.md) English artifacts, configurable agent language
- [ADR-0004](docs/adr/0004-budgets.md) budgets in tokens and USD
- [ADR-0005](docs/adr/0005-skill-compilation.md) skills compiled once, generated code gated
- [ADR-0006](docs/adr/0006-bootstrap-cost.md) the bootstrap is R&D and is allowed to be expensive
- [ADR-0007](docs/adr/0007-server-and-storage.md) server first; SQLite stays, the bucket holds backups, PostgreSQL waits
- [ADR-0008](docs/adr/0008-self-improvement.md) self-improvement through measured prompts and gated pull requests
- [ADR-0009](docs/adr/0009-missions-and-control-modes.md) missions are the entry point; control modes

## License

MIT
