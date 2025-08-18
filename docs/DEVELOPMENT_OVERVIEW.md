# Development Overview & Timeline

## High-Level Journey
- Goal: stable E2E pipeline with transparent risk and responsive UX.
- Milestones:
  - Autoinit components (quotes, mixer, risk, Slack) to reduce cold starts.
  - Redis Streams + Celery: reliable ingestion and scheduling.
  - Debug sprints: YAML/env precedence, Docker cache, type coercion, zsh quoting.

## Key Fixes (Highlights)
- Scheduler syntax errors and import order; closed-session EXT checks.
- Redis type coercion for streams; Alpaca market-closed handling.
- Celery task registration via `include=[...]` to avoid unregistered tasks.

## Current Tuning (Live Testing)
- RTH daily-cap counter moved after cutoff/risk to count actionable signals only.
- `.env`: `RTH_DAILY_CAP=100`, `LLM_MIN_SIGNAL_SCORE=0.6` for faster data accumulation.
- Monitoring: suppressed reasons, risk gates, and signal summaries in logs.

## Detailed Logs
- Implementation details, metrics, and incident notes live in `docs/IMPLEMENTATION_LOG.md`.
- Earlier debugging journey archived from `DEVELOPMENT_JOURNEY.md`.

