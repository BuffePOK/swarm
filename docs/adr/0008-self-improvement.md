# ADR-0008: roj improves itself through measured prompts and gated pull requests

- Status: accepted
- Date: 2026-11-15

## Context

The pilot changed: instead of a market brief, the first project is roj improving roj. That is
also the test of the library. "Orders of magnitude better" cannot be designed in one step; it
can only come from a loop that runs often, measures, and cannot fool itself.

## Decision

Two loops, both ending at the owner:

**A. Prompts (Meta study).** The compiler has knobs (language, structure, document position,
reminder, reasoning step, examples). A study runs paired A/B experiments per model, scored by
code on generated cases, confirms adopted changes on hold-out cases and proposes a Playbook
with a prompt profile per model family. What the team reads in providers' guides is stored
with its source as a *hypothesis* and is never applied without an experiment (ADR-0002).

**B. Code (selfdev).** One small change per run: survey (repository map, backlog, failure
signals from logs) -> ranked proposals -> owner approves the plan -> a coder agent works in a
throwaway git worktree -> deterministic gates -> review by another model family -> owner
approves -> pull request. Merging is the owner's; CI/CD deploys.

Rules that keep the loop honest:

1. **Agents cannot edit their own exam.** Protected paths: policy, budgets, event log,
   `verify/`, the eval tasks, `selfdev.py`, CI, deploy scripts, dependencies. Existing tests
   may not be changed or deleted, only new ones added (ratchet).
2. **Code decides, models propose.** Gates: non-empty diff, protected paths untouched, size
   limit, a new test with every code change, the whole suite passes, the test count does not
   drop. A model's review can only add a veto.
3. **The running installation is never modified.** No hot patching: only git.
4. **Agent-written code runs without secrets**, with a timeout, as an unprivileged user, and
   without network when the kernel allows it.
5. **Everything is reversible**: Playbook versions, git history, deploy rollback.
6. **Token discipline**: baselines are shared, the exact cache makes repeated studies almost
   free, experiments grow only when an effect is promising, knowledge is retrieved only when
   relevant, stored only when the model fails a closed-book exam on it, and long agent
   conversations are compacted.

## Consequences

- Speed is bounded by the owner's approvals at first. The flags `t2.selfdev_start`,
  `t2.activate_playbook` and `t3.open_pull_request` can be switched to `auto` one by one once
  the approval history justifies it; merging stays manual by design (branch protection).
- New prompt knobs and new eval task kinds are code changes: knobs can come from selfdev,
  eval tasks only from the owner.
