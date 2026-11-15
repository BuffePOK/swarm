# ADR-0001: Own thin core, reuse libraries for commodity layers

- Status: accepted
- Date: 2026-11-07
- Deciders: owner (Dmytro), architect (Claude)

## Context

roj needs: many independent projects, a stateless Leader, ten team modes, hierarchical
budgets (tokens and USD), hard quality gates, byte-stable prompts for provider prefix
caching, a tamper-evident event log, checkpoint/resume after a crash, and permission
flags that keep every external action manual until the owner flips them.

State of the ecosystem in November 2026 (checked before this decision):

- **AutoGen** has been in maintenance mode since October 2025. Its successor,
  **Microsoft Agent Framework**, reached 1.0 in April 2026 and ships at a fast cadence.
  Two framework migrations in one year is the churn we would inherit.
- **LangGraph** (MIT, 1.2.x) is mature: graph execution, checkpointing, human-in-the-loop.
- **Pydantic AI** (MIT) offers a typed agent loop, MCP client and durable execution on
  several engines (Temporal, DBOS, Prefect, ...).
- **LiteLLM** was compromised on PyPI on 2026-03-24 (versions 1.82.7 and 1.82.8 stole
  credentials). DSPy, CrewAI and browser-use pulled it in transitively. An agent library
  that holds API keys and has shell access is exactly the target of such attacks.

## Options

A. Adopt one framework (LangGraph, Pydantic AI or MAF) as the core.
B. Write everything from scratch.
C. Own a thin core; reuse proven libraries for commodity layers; integrate frameworks
   later as optional plugins behind our interfaces.

## Decision

**Option C.**

We own the parts that make roj different, because frameworks either hide them or
implement them in a way that conflicts with our guarantees:

| Own (in `roj/`) | Why not from a framework |
|---|---|
| contracts (TaskSpec, Envelope, Decision, Claim) | they are our protocol |
| kernel, checkpoints, resume | one durability model for all 10 modes, not one per framework |
| hash-chained event log, CAS | audit and replay are core requirements |
| budgets (tokens + USD, hierarchical) | frameworks count tokens, they do not enforce money per team/agent |
| policy and approvals (T0–T4 flags) | must be impossible to bypass from agent code |
| prompt compiler and linter | frameworks assemble prompts themselves, which breaks byte-stable prefixes |
| gates and ratchet | must stay immutable for agents |
| blueprint and modes | the Architect composes them; a framework would fix one shape |

We reuse libraries where the problem is solved and stable:

| Layer | Library | License | Where |
|---|---|---|---|
| validation | pydantic | MIT | core |
| HTTP | httpx | BSD-3 | core |
| YAML (blueprints, roles) | PyYAML | MIT | core |
| tools protocol | mcp (official SDK) | MIT | extra `[mcp]` |
| browser | playwright (CDP + headless) | Apache-2.0 | extra `[browser]` |
| BM25 | bm25s or tantivy | MIT | extra `[rag]` |
| vectors | lancedb (embedded) | Apache-2.0 | extra `[rag]` |
| reranker (local) | sentence-transformers | Apache-2.0 | extra `[rerank]` |
| web panel | fastapi + uvicorn | MIT / BSD-3 | extra `[web]` |
| Windows UI | pywinauto | BSD-3 | extra `[desktop]` |
| prompt optimisation (Meta team) | DSPy | MIT | optional, isolated env |

Frameworks become **plugins**, not foundations: a LangGraph-backed graph mode or a
Pydantic AI worker can be added behind `roj.modes` / `roj.agents` interfaces when a
measured benefit appears. They never own state, budgets, policy or the event log.

## Dependency policy

1. Core has at most 5 runtime dependencies (today: 3).
2. Only MIT-compatible licenses (MIT, BSD, Apache-2.0, ISC, PSF). Checked in CI.
3. No LLM proxy libraries in core. Talking to the gateway is ~300 lines of our own code.
4. Locked versions with hashes (`uv lock`, `--require-hashes` in CI and installs).
5. CI fails on unexpected `.pth` files in site-packages (the LiteLLM attack vector).
6. Optional extras are opt-in; a laptop install pulls only the core.

## Consequences

- More code to own in the core (kernel, budgets, compiler), but each piece is small and
  covered by tests.
- No lock-in: changing the agent loop or graph engine later does not touch state, logs,
  budgets or policy.
- Fast install on a weak laptop; smaller attack surface for a library holding API keys.
- We must keep watching the ecosystem; this ADR is revisited when a framework offers
  something measurable we cannot cheaply build (record it in a new ADR).
