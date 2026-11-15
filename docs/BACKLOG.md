# Backlog

Input for `roj selfdev` (the planner reads this file) and for the owner. One line per idea:
what, why, how to measure. Agents may append ideas through pull requests; the owner reorders.

## Token economy
- Agent loop: send tool schemas only for tools the role used in the last N runs plus `tools.describe`; measure prompt tokens per agent step.
- Slots: skip the repair round when the only error is a trailing comma or a code fence (repair in code); measure `slot.invalid` followed by a paid retry.
- Compaction: summarise with the cheapest model that passes a recall check on 20 recorded conversations; measure tokens per finished agent task.
- Knowledge router: when a pack has embeddings, gate hits by cosine similarity as well as term coverage; measure recall@k on pack questions.
- Batch mode for non-urgent calls (studies at night) when the gateway supports it.

## Quality
- Meta study: add knobs suggested by hypotheses in the Playbook (e.g. role line on/off, output prefill, criteria as checklist); each knob needs a compiler switch and a test.
- Meta study: per-task-kind profiles (a family may want reasoning only for multi-step kinds).
- Model cards: route roles to the cheapest model whose card clears the role's threshold; propose bindings to the owner.
- Blueprint critique step by another model family before owner approval.

## Platform
- Missions: run a stage's team through a Blueprint (graph / orchestrator) instead of sequentially; let agents of one stage message each other.
- Missions and model cards in the web panel (state, questions, chat).
- `roj serve`: run long tasks concurrently with the tick so Telegram clicks are handled during a study.
- Installation-level knowledge packs shared by several projects (audience by role).
- PostgreSQL backend behind the Store interface when a trigger in ADR-0007 is met.
- Remote worker nodes (`roj-node`) with heartbeat and hash-verified transfer.
- Browser over CDP with persistent profiles; desktop automation (Phase 3).
