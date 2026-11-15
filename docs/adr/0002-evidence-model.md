# ADR-0002: Evidence model — sources are not truth, hypotheses are first-class

- Status: accepted
- Date: 2026-11-07

## Context

Concept v2 said "truth comes from sources, tests and data, not from voting". The owner
added two corrections:

1. Not every source is true.
2. Diamonds hide at the bottom: sometimes the valuable answer is not in the first results,
   and research must dig deep and test suspicious or unconfirmed theories.

Both are right. A system that trusts the first source is naive; a system that discards
everything unconfirmed never finds anything new.

## Decision

Every piece of knowledge is a **Claim** with an explicit **status** and **evidence**,
and every **Source** has a credibility assessment.

### Source

`Source {id, url_or_ref, content_sha256, retrieved_at, origin, tier, independence_key}`

- `tier`: `primary` (official data, papers, filings, code, measurements), `secondary`
  (reputable reporting), `unknown`, `low_trust` (anonymous, known unreliable, SEO).
- `independence_key`: the original origin (syndicated copies of one wire story share a key
  and count once).

### Claim status

| Status | Meaning | May appear in deliverables as |
|---|---|---|
| `fact` | verbatim quote checked by code + (primary source, or ≥2 independent secondary) | a statement |
| `supported` | evidence exists but below the fact bar | a statement with qualifier |
| `hypothesis` | unconfirmed, possibly from low-trust sources, worth testing | explicitly labelled hypothesis |
| `contested` | credible evidence on both sides | both sides with sources |
| `refuted` | credible evidence against | only when relevant, as refuted |

Rules:

- Hypotheses are **kept**, not discarded. They are tracked in the project's hypothesis
  ledger with the test that would confirm or refute them.
- A hypothesis never silently becomes a fact. Promotion requires new evidence meeting the
  fact bar, checked by code where possible.
- Agreement of several models is **not** evidence. It can raise priority for verification.
- Counting sources uses `independence_key`, not URLs.

### Research depth is a task parameter

`TaskSpec.research = {depth: shallow|standard|deep, include_low_trust: bool,
min_independent_sources: int, explore_budget_share: float}`

- `deep` allocates part of the budget (`explore_budget_share`, e.g. 0.2) to following weak
  signals: low-ranked results, forums, preprints, contrarian views.
- Findings from exploration enter as `hypothesis` and go to a verification step.

## Consequences

- Reports can contain labelled hypotheses, which is more honest and more useful than
  either hiding or overstating them.
- Verification work is explicit and budgeted instead of implicit.
- Phase 0 ships the contracts (`roj.contracts.Source`, `Claim`, `ResearchSpec`); scoring
  and promotion logic arrive with knowledge packs in Phase 1.
