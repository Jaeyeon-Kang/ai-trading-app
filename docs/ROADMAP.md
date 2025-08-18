# Product Roadmap & Universe Expansion

## Executive Summary
- Focus: raise communication cadence, strengthen LLM transparency, and enable paper-trading with guardrails.
- Drivers: resolve "quiet market" anxiety, ensure strong-signal coverage, and keep costs predictable.

## Phase 1.5 Priorities
- LLM usage: strong-signal coverage + daily briefings; detailed trigger/summary logging.
- Paper trading: auto stop-loss/take-profit, daily report, Slack approvals optional.
- Monitoring: clear suppress reasons and rate/cost telemetry.

## Universe Expansion (5 → 9)
- Tier A (30s): NVDA, TSLA, AAPL
- Tier B (60s): MSFT, AMZN, META
- Bench (event-driven): GOOGL, AMD, AVGO
- Token budget: A(6) + B(3) + Reserve(1) = 10/min exact.
- LLM gating: daily 120 calls, min score threshold, 30m cache.

## Recent Changes (Merged from Plans)
- Added tier-based scheduler, Redis token buckets, and gating.
- Roadmapped “quiet market” messaging and stronger Slack UX.
- Risk caps: 0.5% per trade, 2% concurrent; 80% exposure cap, min 3 slots.

## References
- Planning history: `docs/archive/EVOLUTION_ROADMAP.md`.
- Implementation details and changes: `docs/IMPLEMENTATION_LOG.md`.
