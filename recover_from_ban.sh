#!/usr/bin/env bash
# Recover the fleet grid bot after a Binance rate-limit ban.
# In fleet mode there is only ONE process, so no staggered restart is needed:
# just make sure the watchdog brings the fleet up (it starts only if the
# fleet is dead/stalled).
# Usage: bash recover_from_ban.sh
set -u
ROOT="$(cd "$(dirname "$0")" && pwd)"
"$ROOT/watchdog.sh"
echo "[recover] watchdog done; fleet pid: $(cat "$ROOT/.fleet.pid" 2>/dev/null || echo none)"
