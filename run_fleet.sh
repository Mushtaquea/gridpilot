#!/usr/bin/env bash
# Run the single-process fleet grid bot: ./run_fleet.sh [--symbols ...] [--paper]
# One process manages ALL symbols in instances/*/ (replaces run_instance.sh).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

# Force UTC for all log timestamps, no matter which environment launches the
# fleet (interactive exec uses IST, cron workers use UTC).
export TZ=UTC

# Load API keys from the locked .env file (600 permissions).
if [ -f "$ROOT/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  source "$ROOT/.env"
  set +a
fi

[ -d "$ROOT/venv" ] || { echo "[run:fleet] venv missing at $ROOT/venv" >&2; exit 1; }

# Stale per-instance PID files are meaningless in fleet mode; remove them so
# nothing mistakes them for live bots.
rm -f instances/*/.bot.pid

echo $$ > .fleet.pid
exec "$ROOT/venv/bin/python" "$ROOT/fleet.py" "$@"
