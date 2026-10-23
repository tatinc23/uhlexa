#!/usr/bin/env bash
# Keep Uhlexa reachable for judging (docs/HOSTING.md, "Judging window"). Restarts the app + tunnel and the voice worker
# whenever either exits, and logs to logs/. Stop with Ctrl-C.
#   scripts/keep_alive.sh
# Also run `caffeinate -dims scripts/keep_alive.sh` so the Mac doesn't sleep.
set -uo pipefail
cd "$(dirname "$0")/.."
mkdir -p logs
pids=()
cleanup() { for p in "${pids[@]:-}"; do kill "$p" 2>/dev/null || true; done; exit 0; }
trap cleanup INT TERM EXIT

supervise() {  # supervise <name> <command...>: rerun after 5 s whenever it exits
  local name="$1"; shift
  while true; do
    echo "$(date '+%F %T') start $name" >> "logs/$name.log"
    "$@" >> "logs/$name.log" 2>&1
    echo "$(date '+%F %T') $name exited ($?), restarting in 5 s" >> "logs/$name.log"
    sleep 5
  done
}

supervise public scripts/run_public.sh & pids+=($!)
supervise voice scripts/voice_worker.sh start & pids+=($!)
echo "Supervising: public app + tunnel and the voice worker. Logs in logs/. Ctrl-C to stop."
wait
