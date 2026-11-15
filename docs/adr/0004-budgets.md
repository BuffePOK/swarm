# ADR-0004: Budgets — tokens and USD per project per day, distributed by the Architect

- Status: accepted
- Date: 2026-11-07

## Decision

- Default per project per day: **1,000,000 tokens and 5.00 USD**. Whichever limit is hit
  first stops new model calls for that scope.
- The day is computed in the project's timezone (default `Europe/Warsaw`).
- Scopes are hierarchical: `project` → `team:<name>` → `agent:<name>`. The Architect or
  Leader allocates limits to child scopes; the owner can edit any allocation. A call must
  fit every ancestor's limit, so children can never spend more than the project total.
- Cost is computed from a pricing table (USD per 1M tokens: input, cached input, output),
  matched by exact model id or glob (`claude-opus*`), because the gateway may not expose
  prices. Unknown models use a deliberately expensive default price, or are refused when
  `strict_pricing = true`.
- Before a call the kernel **reserves** a pessimistic estimate (no cache hit, full
  `max_tokens`); after the call it commits the real usage and releases the rest. This keeps
  concurrent agents from overshooting.
- Exact-cache hits cost 0 and are logged as such.

## Consequences

- Money is enforced, not only reported.
- Prices must be maintained in `roj.toml`; a stale price table makes cost reports wrong
  but never unsafe, because the default for unknown models is expensive.
