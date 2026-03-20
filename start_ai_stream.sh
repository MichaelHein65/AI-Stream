#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

HOST="${AI_STREAM_HOST:-127.0.0.1}"
PORT="${AI_STREAM_PORT:-8091}"
APP_URL="http://${HOST}:${PORT}"
LOG_FILE="${SCRIPT_DIR}/.ai-stream-launch.log"

open_app() {
  if command -v open >/dev/null 2>&1; then
    open "$APP_URL"
  elif command -v xdg-open >/dev/null 2>&1; then
    xdg-open "$APP_URL" >/dev/null 2>&1 &
  else
    echo "App bereit unter: $APP_URL"
  fi
}

if curl -fsS "${APP_URL}/api/health" >/dev/null 2>&1; then
  echo "AI Stream laeuft bereits unter: $APP_URL"
  open_app
  exit 0
fi

python3 -u server.py --host "$HOST" --port "$PORT" >>"$LOG_FILE" 2>&1 &
SERVER_PID=$!

cleanup() {
  if kill -0 "$SERVER_PID" >/dev/null 2>&1; then
    kill "$SERVER_PID" >/dev/null 2>&1 || true
  fi
}

trap cleanup EXIT INT TERM

for _ in {1..40}; do
  if ! kill -0 "$SERVER_PID" >/dev/null 2>&1; then
    break
  fi
  if curl -fsS "${APP_URL}/api/health" >/dev/null 2>&1; then
    break
  fi
  sleep 0.25
done

if ! curl -fsS "${APP_URL}/api/health" >/dev/null 2>&1; then
  echo "Server ist nicht gestartet: ${APP_URL}/api/health antwortet nicht."
  if [[ -f "$LOG_FILE" ]]; then
    echo
    echo "Letzte Server-Meldungen aus $LOG_FILE:"
    tail -n 20 "$LOG_FILE" || true
  fi
  exit 1
fi

open_app

echo "AI Stream laeuft unter: $APP_URL"
wait "$SERVER_PID"
