#!/usr/bin/env bash
# Start the AI German Tutor on http://127.0.0.1:${PORT:-9950}
#
#   ./run.sh          start in the background (logs: .run/tutor.log)
#   ./run.sh --fg     run in the foreground (what systemd uses)
#   ./run.sh --stop   stop it
#
# Voice: this also starts the project's own XTTS server (./xtts/run.sh, port
# 8300) if it is set up and not running. It takes ~40 s to load; until then
# Mira uses the local piper voices. --stop leaves it running, so restarting the
# tutor does not reload the voice (./xtts/run.sh --stop stops it).
set -u
cd "$(dirname "$0")"
PORT="${PORT:-9950}"
mkdir -p .run

if [ "${1:-}" = "--stop" ]; then
  if [ -f .run/tutor.pid ] && kill -0 "$(cat .run/tutor.pid)" 2>/dev/null; then
    kill "$(cat .run/tutor.pid)" && echo "stopped tutor (pid $(cat .run/tutor.pid))"
  fi
  rm -f .run/tutor.pid
  exit 0
fi

start_voice() {
  [ -x xtts/venv/bin/python ] || return 0
  curl -sf -m 2 "http://127.0.0.1:${XTTS_PORT:-8300}/health" >/dev/null && return 0
  echo "Starting the XTTS voice in the background (logs: .run/xtts.log)"
  nohup ./xtts/run.sh >/dev/null 2>&1 &
}

if [ "${1:-}" = "--fg" ]; then
  start_voice
  exec ./venv/bin/uvicorn main:app --host 127.0.0.1 --port "$PORT"
fi

"$0" --stop >/dev/null
start_voice
# A stopped server takes a moment to let go of its port.
for _ in $(seq 10); do fuser -s "$PORT/tcp" 2>/dev/null || break; sleep 0.5; done
if fuser -s "$PORT/tcp" 2>/dev/null; then
  echo "Port $PORT is already in use by something else. Set PORT=... and retry."
  exit 1
fi
./venv/bin/uvicorn main:app --host 127.0.0.1 --port "$PORT" > .run/tutor.log 2>&1 &
echo $! > .run/tutor.pid

for _ in $(seq 20); do
  if curl -sf -m 2 "http://127.0.0.1:$PORT/health" >/dev/null; then
    echo "AI German Tutor running"
    echo "  Test page   http://localhost:$PORT/test.html"
    echo "  WebSocket   ws://localhost:$PORT/ws/tutor"
    echo "  Health      $(curl -s http://127.0.0.1:$PORT/health)"
    echo "  Stop with   ./run.sh --stop"
    exit 0
  fi
  sleep 1
done
echo "Did not start. Last lines of .run/tutor.log:"; tail -15 .run/tutor.log
exit 1
