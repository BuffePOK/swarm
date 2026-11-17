# Mission: roj improves roj. Part 1: research and development (R&D)

## Why

roj must become an excellent and cost-effective system of AI agents (docs/CONCEPT.md, sections 1, 3 and 22). Before anything in roj is changed, find out what is already known and built in the world, then measure where roj stands against it. The findings decide the next steps; the roadmap is expected to change after this analysis. This is many actions, not one: split the work into small stages and build knowledge step by step.

## What to research

Research from external sources (official documentation, engineering blogs, papers, public repositories, benchmarks, independent evaluations), found with web.search and papers.search, read with web.fetch; documentation sites worth keeping are indexed with web.crawl and then searched locally. The model's own memory is a hypothesis, not a finding.

1. **Model families on our gateway** (Claude, DeepSeek, Gemini and Gemma, Mistral, Qwen, Kimi, GLM and any other family in the catalog): strengths and weaknesses, the providers' official prompting guidance, tool calling behaviour, long context, prompt caching, reasoning modes, pricing, known request quirks. Result: how to prompt and use each family, and for which kinds of work.
2. **Single-agent harnesses**: how Claude (chat and Claude Code), OpenAI Codex, Google Antigravity and Gemini CLI, DeepSeek's agents, Cursor, Aider, OpenHands, SWE-agent and similar tools are built, as far as publicly documented: agent loop, tools, planning, sub-agents, context management and compaction, memory, permissions and sandboxing, verification, prompt structure. What makes a harness around a model good.
3. **Multi-agent systems**: frameworks (LangGraph, Microsoft Agent Framework / AutoGen, CrewAI, OpenAI Agents SDK, Google ADK, MetaGPT, CAMEL and others) and research: orchestration patterns, debate, mixture of agents, communication between agents, shared memory, failure taxonomies of multi-agent systems, cost. When several agents beat one agent and when they do not.
4. **Prompting and context engineering**: research and vendor guidance, automatic prompt optimisation (e.g. DSPy-style methods), evaluation of prompts and agents.
5. **Knowledge and RAG**: chunking, contextual and hybrid retrieval, reranking, agentic RAG, memory for agents; which knowledge is worth storing at all. **Security of knowledge**: prompt injection through retrieved documents, knowledge poisoning, access control per agent and per team, whether embeddings and vector stores must be protected (embedding inversion, membership inference) and whether agents should ever receive raw vectors.
6. **Security and data protection** for a system like roj on one server: secrets, sandboxing of agent-written code, permissions, supply chain, backups, data at rest, audit.
7. **Owner interface**: how leading systems show agent activity, traces, costs and decisions; patterns for a web site where the owner sees absolutely everything (every agent with its knowledge, the collaboration between agents, costs), can talk to each agent separately and can change all data; Telegram as a second channel.
8. **roj itself**: read roj's code (repo tools) and docs/CONCEPT.md, compare with the findings, and record baselines of the metrics M1 to M10 (CONCEPT section 22.3) from the event logs wherever data exists.

## Deliverables

- One knowledge pack per area: rnd-models, rnd-harness, rnd-multiagent, rnd-prompting, rnd-rag, rnd-security, rnd-ui, rnd-roj. Every note carries the source URL, the publication date and an evidence label: primary source, secondary report, or hypothesis. Vendor marketing is labelled as such.
- Per area a short synthesis: what is established, what is disputed, what applies to roj and why.
- A ranked list of improvement proposals for roj: problem, evidence, expected gain (tokens, quality, owner time, security), cost and risk, how to measure it. Grouped into quick wins, things that need an experiment first, and large changes (including the web interface).
- A proposal for the next stages after the R&D. The owner decides.

## Rules

- Sources over memory. Prefer primary sources and material from 2025 and 2026; always record the date.
- No code changes during the R&D. Changes to roj come later as change requests through selfdev.
- Small stages, at most 5 agents each. First the areas that shape everything else: model families, harness patterns and the roj baseline.
- Tokens are part of the goal: cheap models for searching, reading and note-taking, strong models only for synthesis. If a stage produces thin results, stop and report instead of spending more.
- Ask the owner when a decision is his, with options and a default.
