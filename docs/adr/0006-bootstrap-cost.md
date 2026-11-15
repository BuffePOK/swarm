# ADR-0006: The bootstrap is R&D and is allowed to be expensive

- Status: accepted
- Date: 2026-11-08

## Context

The owner noticed that the first bootstrap cost only about twice a daily run (8.8k vs 4.3k
tokens on the offline world) and asked why, since R&D is usually ~10x more expensive than
routine work. Two reasons:

1. The numbers came from deterministic fake models: prompt sizes were real, but the fake
   "models" wrote tiny answers and did no reasoning, and the synthetic world has 8 sources
   with 3-6 items each instead of 16+ sources with 20-50.
2. The bootstrap itself was shallow: one planner draft, one critique, one revision, one
   adapter. It chose sources but did not learn or measure anything.

## Decision

The bootstrap does research that pays for itself in every daily run:

| Step | What it does | Calls (deep) |
|---|---|---|
| probe | fetch and measure every seed (robots, freshness, parseability) | 0 |
| plan | planner drafts portfolio, topic rules, instruments | 1 |
| scout | discovery rounds against coverage gaps; every candidate is probed | 1-2 |
| critique, finalize | other-family critic, planner revision | 2 |
| adapters | coder writes parsers for pages without feeds, sandbox decides | 0-3 |
| assess | a strong "teacher" labels sample items (relevance, topics) and rates sources; labels become an eval set with ~20% hidden cases | 2-4 |
| rules | rule-smith edits keyword rules, code measures F1 on the labels, best version kept; hidden cases measure generalisation; the score becomes a ratchet baseline | 1-3 |
| calibrate | ranking weights fitted to the teacher (NDCG@6), pure code | 0 |
| compile | spec + research report for the owner | 0 |

Depth is a parameter (`shallow`, `standard`, `deep`; default `deep`).

Expected cost with real models and the 16 default seeds (estimates, to be confirmed by
the first real run's task log):

| | calls | tokens without reasoning | with reasoning models |
|---|---|---|---|
| bootstrap (deep) | 10-16 | ~90-120k | ~130-200k |
| daily run | 2-3 | ~12-16k | ~15-25k |

The ratio of roughly 8-12x is a consequence, not a target: the more the bootstrap learns
(rules that classify without a model, ranking that matches the teacher), the fewer and
smaller the daily calls.

## Consequences

- The research report (`reports/research-news_digest-*.md`) shows what was probed,
  rejected, learned and measured before the owner approves activation.
- Rule quality is guarded by the ratchet: a later repair or re-bootstrap may not ship
  rules that score below the recorded threshold.
- Real costs are visible per role and model in the task log; this ADR's estimates are
  replaced by measured numbers after the first real run.
