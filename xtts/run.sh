#!/usr/bin/env bash
# Start the German tutor's own XTTS voice server on http://127.0.0.1:${XTTS_PORT:-8300}
#
#   ./xtts/run.sh          start in the background (logs: .run/xtts.log)
#   ./xtts/run.sh --fg     run in the foreground
#   ./xtts/run.sh --stop   stop it
#
# Setup (once): see README.md, "Voice".
set -u
cd "$(dirname "$0")/.."
PORT="${XTTS_PORT:-8300}"
mkdir -p .run
export COQUI_TOS_AGREED=1            # XTTS-v2 licence (CPML), non-interactive
export HF_HUB_DISABLE_PROGRESS_BARS=1

if [ ! -x xtts/venv/bin/python ]; then
  echo "xtts/venv is missing. Set it up first (README.md, \"Voice\")."
  exit 1
fi

if [ "${1:-}" = "--stop" ]; then
  if [ -f .run/xtts.pid ] && kill -0 "$(cat .run/xtts.pid)" 2>/dev/null; then
    # The pid is the restart loop's, which leads its own process group.
    kill -- -"$(cat .run/xtts.pid)" 2>/dev/null || kill "$(cat .run/xtts.pid)"
    echo "stopped xtts (pid $(cat .run/xtts.pid))"
  fi
  rm -f .run/xtts.pid
  exit 0
fi

if [ "${1:-}" = "--fg" ]; then
  exec xtts/venv/bin/python -m uvicorn worker:app --app-dir xtts --host 127.0.0.1 --port "$PORT"
fi

"$0" --stop >/dev/null
# A stopped server takes a moment to let go of its port; until then /health
# would still be answered by the old one.
for _ in $(seq 20); do fuser -s "$PORT/tcp" 2>/dev/null || break; sleep 0.5; done
if fuser -s "$PORT/tcp" 2>/dev/null; then
  echo "Port $PORT is already in use by something else. Set XTTS_PORT=... and retry."
  exit 1
fi
# In a restart loop: the worker exits on purpose if the GPU gets into a bad state.
: > .run/xtts.log
setsid bash -c 'while :; do
  xtts/venv/bin/python -m uvicorn worker:app --app-dir xtts --host 127.0.0.1 --port "$0"
  echo "xtts exited, restarting in 3 s"; sleep 3
done' "$PORT" >> .run/xtts.log 2>&1 &
echo $! > .run/xtts.pid

echo "Loading XTTS (about 40 s) ..."
for _ in $(seq 120); do
  if curl -sf -m 2 "http://127.0.0.1:$PORT/health" | grep -q '"ok"'; then
    echo "XTTS running: $(curl -s http://127.0.0.1:$PORT/health)"
    exit 0
  fi
  kill -0 "$(cat .run/xtts.pid)" 2>/dev/null || break
  sleep 1
done
echo "Did not start. Last lines of .run/xtts.log:"; tail -15 .run/xtts.log
exit 1
