#!/usr/bin/env bash
set -euo pipefail

# Simple docker compose log watcher with sensible defaults.
# Usage:
#   bin/logwatch.sh [worker|beat|api|all|<service>...]
# Env (optional):
#   SINCE=10m   FOLLOW=1   TAIL=200
#   PATTERN='generate_signals|signals_generated|Slack|postMessage|signal|skipped|reason|spread|dollar|cooldown|components_not_ready|autoinit|initialize_components|warmup_backfill'

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  echo "Usage: $(basename "$0") [worker|beat|api|all|<service>...]"
  echo "  SINCE (default: 10m), FOLLOW (1/0, default: 1), TAIL (default: 200)"
  echo "  PATTERN (egrep -i; empty to disable)"
  exit 0
fi

alias_key="${1:-worker}"
shift || true

declare -a services
case "$alias_key" in
  worker) services=(celery_worker);;
  beat)   services=(celery_beat);;
  api)    services=(trading_bot_api);;
  all)    services=(celery_worker celery_beat trading_bot_api);;
  *)      services=("$alias_key" "$@");;
esac

SINCE_VAL="${SINCE:-10m}"
FOLLOW_VAL="${FOLLOW:-1}"
TAIL_VAL="${TAIL:-200}"
PATTERN_VAL="${PATTERN:-generate_signals|signals_generated|Slack|postMessage|signal|skipped|reason|spread|dollar|cooldown|components_not_ready|autoinit|initialize_components|warmup_backfill}"

opts=(--since "${SINCE_VAL}" --tail "${TAIL_VAL}")
if [[ "${FOLLOW_VAL}" == "1" || "${FOLLOW_VAL}" == "true" ]]; then
  opts+=(-f)
fi

if [[ -n "${PATTERN_VAL}" ]]; then
  docker compose logs "${opts[@]}" "${services[@]}" 2>&1 | egrep -i "${PATTERN_VAL}"
else
  docker compose logs "${opts[@]}" "${services[@]}"
fi


