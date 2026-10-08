#!/usr/bin/env bash
# Watchdog: supervise the single fleet grid-bot process (fleet.py).
# Runs every 5 minutes via cron. Idempotent - safe to run any number of times.
#
# Checks:
#   1. liveness via .fleet.pid
#   2. heartbeat via logs/fleet.heartbeat (touched every fleet cycle, ~15-20s,
#      so a 5-minute stale heartbeat means the loop is genuinely hung)
# A dead or stalled fleet is restarted detached (setsid+nohup) so it survives
# the end of this cron session.
#
# (The old 20-process supervisor is kept as watchdog_multi_backup.sh.)
set -u
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"
WLOG="$ROOT/logs-watchdog.out"
PIDF="$ROOT/.fleet.pid"
HB="$ROOT/logs/fleet.heartbeat"

pid=""
alive=0
if [ -f "$PIDF" ]; then
  pid="$(cat "$PIDF")"
  if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
    alive=1
  fi
fi

stalled=0
if [ "$alive" = 1 ] && [ -f "$HB" ]; then
  if [ -z "$(find "$HB" -mmin -5 2>/dev/null)" ]; then
    stalled=1
  fi
fi

if [ "$alive" = 1 ] && [ "$stalled" = 0 ]; then
  exit 0  # healthy, nothing to do
fi

if [ "$stalled" = 1 ]; then
  echo "[watchdog:fleet] stalled (heartbeat untouched 5m) - killing $pid at $(date -u +%FT%TZ)" >> "$WLOG"
  kill -9 "$pid" 2>/dev/null || true
  sleep 2
fi

echo "[watchdog:fleet] (re)starting detached at $(date -u +%FT%TZ)" >> "$WLOG"
( setsid nohup "$ROOT/run_fleet.sh" >> "$WLOG" 2>&1 < /dev/null & )
disown 2>/dev/null || true

# Wait for the new fleet process to record its PID (up to ~25s).
for _ in $(seq 1 25); do
  sleep 1
  if [ -f "$PIDF" ]; then
    cand="$(cat "$PIDF")"
    if [ -n "$cand" ] && [ "$cand" != "$pid" ] && kill -0 "$cand" 2>/dev/null; then
      echo "[watchdog:fleet] running as PID $cand" >> "$WLOG"
      exit 0
    fi
  fi
done
echo "[watchdog:fleet] WARNING: no live fleet process after start!" >> "$WLOG"
exit 1
