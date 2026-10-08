#!/usr/bin/env bash
# Run the single-symbol grid bot: ./run_bot.sh [--config config.yaml] [--paper]
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

# Force UTC for all log timestamps, no matter which environment launches
# the bot (interactive shells and cron workers often disagree on TZ).
export TZ=UTC

# Load API keys from the locked .env file (600 permissions).
if [ -f "$ROOT/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  source "$ROOT/.env"
  set +a
fi

[ -d "$ROOT/venv" ] || { echo "[run:bot] venv missing at $ROOT/venv - run: python3 -m venv venv && source venv/bin/activate && pip install -r requirements.txt" >&2; exit 1; }

exec "$ROOT/venv/bin/python" "$ROOT/bot.py" "$@"
