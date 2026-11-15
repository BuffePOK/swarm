# ADR-0003: Languages — English artifacts, configurable agent language

- Status: accepted (default may change after the Meta team experiment)
- Date: 2026-11-07

## Context

The owner wants all code, docs, READMEs and reports in English (more languages later),
and believes agents work best with Polish prompts between themselves.

Evidence: the OneRuler benchmark (Kim et al., COLM 2025) found Polish the top language and
English sixth of 26 on long-context tasks (64K–128K). It also found that when instructions
and context are in different languages, accuracy can move by up to 20% depending on the
instruction language.

Limits of that evidence: synthetic retrieval and aggregation tasks (needle-in-a-haystack,
common-word extraction), models from early 2025. It does not measure reasoning, coding or
writing quality, and it shows that mixing languages has its own cost.

## Decision

- `output_language = "en"`: deliverables, reports, docs, logs meant for humans.
- `prompt_language = "pl"`: language of the fixed scaffolding the compiler writes
  (rules, format instructions, questions policy). Configurable per installation,
  project and role.
- The compiler always adds an explicit instruction for the deliverable language, so a
  Polish prompt still produces English output.
- The first Meta team experiment A/B tests `prompt_language` (pl vs en vs "match the
  context language") per model family and task type, on our own eval set. The default
  is changed only by a new ADR with the measured result.

## Consequences

- Owner's preference is the default; it is measured rather than assumed.
- Templates exist in both languages from day one (`roj/prompting/templates.py`).
