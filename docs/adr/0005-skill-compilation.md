# ADR-0005: Skills are compiled once, then run by code; generated code is gated

- Status: accepted
- Date: 2026-11-07

## Context

The owner wants recurring tasks to work like this: the first invocation researches what,
where and how (many tokens); later invocations run algorithms and parsers the system built
for itself (few tokens). Letting models write and run arbitrary code every day is the
opposite of "LLM decides, code executes", and fetched pages can carry prompt injection.

## Decision

1. **Bootstrap compiles a declarative spec.** The expensive run produces data, not code:
   chosen sources with weights and tiers, topic rules (keywords), ranking weights, market
   instruments, limits. A deterministic engine shipped with roj executes it.
2. **Generated code only where data is not enough.** For a page without a feed, the coder
   writes one pure function `parse(raw, base_url) -> list[dict]`. It must pass:
   - an AST allow-list (stdlib text modules only, no I/O, no dunders, no eval/exec/open),
   - execution in `python -I -S` with stdin/stdout only, a timeout and CPU/memory limits,
   - schema validation of the output and a minimum item count on the captured fixture.
   It then enters the shared pool as `candidate`; activating a spec that uses it is a T2
   owner approval. A candidate becomes `stable` after successful use in ≥ 2 projects.
3. **Everything is verified by probes, not by the model's word.** Seeds and model-proposed
   candidates are fetched (robots.txt respected) and measured before they can be selected;
   the spec is dry-run on captured fixtures before activation.
4. **Daily runs call models only where language is the job.** Rules classify; a cheap model
   tags only what rules missed. One analyst call explains the top events; one checker call
   from another model family verifies claims against cited snippets. Code then removes
   unverified claims and any sentence with a number that appears in neither the sources nor
   the market data. Market numbers are rendered by code.
5. **Repair is deterministic first.** When a source fails N runs in a row, repair re-probes
   it and rediscovers moved feeds (0 tokens). Only a changed page layout calls the coder.
   The repaired spec is a new version that the owner activates; older versions stay for
   rollback.

## Consequences

- Measured on the offline world: bootstrap 4 model calls (~8.8k tokens), daily run 2 calls (~4.3k tokens); the classifier is skipped when
  rules already classified every candidate. Real models and feeds will cost more on both sides; the
  ratio is what the design controls.
- The static check plus subprocess is a guard-rail, not an OS-level sandbox. Running adapters
  inside a container or VM is planned with the hands layer (Phase 3).
- Specs are versioned and auditable; every source, rule and adapter in production was
  probed, tested and approved.
