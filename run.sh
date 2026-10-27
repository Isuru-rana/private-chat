#!/usr/bin/env bash
# Start Private Chat and open it in its own clean Brave profile.
# Starts llama-server too if it isn't running. Everything this script started
# stops when the app window closes (or on Ctrl+C). A llama-server that was
# already running is left alone.
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
PORT="${PC_PORT:-3050}"
URL="http://127.0.0.1:$PORT"
LLAMA_DIR="${PC_LLAMA_DIR:-$DIR/../../tools/llama.cpp}"  # folder with a serve.sh that starts llama-server on :8088
LLAMA_HEALTH="http://127.0.0.1:8088/health"
WEB_DIR="${PC_WEB_DIR:-$DIR/web-stack}"
WEB_COMPOSE="$WEB_DIR/docker-compose.yaml"

cd "$DIR"

# Refuse to start if something else already holds the port: the browser would
# otherwise open that program's page and you could type the vault password into it.
if curl -s -m 1 -o /dev/null "$URL/"; then
  echo "error: port $PORT is already in use; not starting" >&2
  exit 1
fi

BACKEND=""
LLAMA=""
STARTED_WEB=0
cleanup() {
  trap - EXIT INT TERM
  [[ -n "$BACKEND" ]] && kill "$BACKEND" 2>/dev/null || true
  if [[ "$STARTED_WEB" == 1 ]]; then
    echo "stopping private web stack"
    docker compose -f "$WEB_COMPOSE" down >/dev/null 2>&1 || true
  fi
  if [[ -n "$LLAMA" ]]; then
    echo "stopping llama-server"
    kill "$LLAMA" 2>/dev/null || true
    wait "$LLAMA" 2>/dev/null || true
  fi
}
trap cleanup EXIT
trap 'exit 130' INT TERM

if curl -s -m 2 "$LLAMA_HEALTH" | grep -q '"ok"'; then
  echo "llama-server already running; leaving it running when the app closes"
elif [[ ! -x "$LLAMA_DIR/serve.sh" ]]; then
  echo "error: llama-server is not running on :8088 and $LLAMA_DIR/serve.sh was not found." >&2
  echo "       Start llama-server yourself, or set PC_LLAMA_DIR (see README)." >&2
  exit 1
else
  echo "starting llama-server (Gemma takes ~30 s to load; log: $LLAMA_DIR/server.log)"
  "$LLAMA_DIR/serve.sh" > "$LLAMA_DIR/server.log" 2>&1 &
  LLAMA=$!  # serve.sh execs llama-server, so this is the server's own PID
  for _ in $(seq 240); do
    curl -s -m 1 "$LLAMA_HEALTH" 2>/dev/null | grep -q '"ok"' && break
    if ! kill -0 "$LLAMA" 2>/dev/null; then
      echo "error: llama-server exited during startup; see $LLAMA_DIR/server.log" >&2
      LLAMA=""
      exit 1
    fi
    sleep 1
  done
  curl -s -m 1 "$LLAMA_HEALTH" | grep -q '"ok"' || { echo "error: llama-server not ready after 4 min" >&2; exit 1; }
  echo "llama-server ready"
fi

# Private web stack (Tor + SearXNG). Failure is a warning: the app still runs,
# web tools just report Tor unavailable.
if curl -s -m 2 http://127.0.0.1:8888/healthz >/dev/null 2>&1; then
  echo "private web stack already running; leaving it running when the app closes"
else
  echo "starting private web stack (Tor + SearXNG); a first start can take a few minutes (image download)"
  [[ -f "$WEB_DIR/.env" ]] || echo "SEARXNG_SECRET=$(openssl rand -hex 32)" > "$WEB_DIR/.env"
  STARTED_WEB=1  # set before up: cleanup's idempotent `down` also covers partial starts and Ctrl+C
  if docker compose -f "$WEB_COMPOSE" up -d --build >/dev/null 2>&1; then
    web_ok=0
    for _ in $(seq 120); do
      if curl -s -m 1 http://127.0.0.1:8888/healthz >/dev/null 2>&1 \
         && { tor_log="$(docker logs private-web-tor 2>&1 || true)"; [[ "$tor_log" == *"Bootstrapped 100%"* ]]; }; then
        web_ok=1; break
      fi
      sleep 1
    done
    if [[ "$web_ok" == 1 ]]; then echo "private web stack ready"
    else echo "warning: private web stack not ready after 120 s; web tools will report Tor unavailable" >&2; fi
  else
    echo "warning: could not start private web stack; web tools will report Tor unavailable" >&2
  fi
fi

# Turn off the browser's password manager so the vault password is never saved.
mkdir -p "$DIR/profile/Default"
python3 - "$DIR/profile/Default/Preferences" <<'PY'
import json, sys
path = sys.argv[1]
try:
    prefs = json.load(open(path))
except (OSError, ValueError):
    prefs = {}
prefs["credentials_enable_service"] = False
prefs["credentials_enable_autosignin"] = False
prefs.setdefault("profile", {})["password_manager_enabled"] = False
prefs.setdefault("autofill", {}).update({"profile_enabled": False, "credit_card_enabled": False})
json.dump(prefs, open(path, "w"))
PY

PC_PORT="$PORT" uv run python -m privatechat &
BACKEND=$!

for _ in $(seq 50); do
  curl -s -o /dev/null "$URL/" && break
  kill -0 "$BACKEND" 2>/dev/null || break
  sleep 0.2
done
if ! kill -0 "$BACKEND" 2>/dev/null; then
  echo "error: backend failed to start" >&2
  exit 1
fi

echo "app running at $URL — close the window (or press Ctrl+C) to stop"
# Brave's console output is harmless GLib/GPU noise; keep the terminal clean.
brave-browser \
  --user-data-dir="$DIR/profile" \
  --app="$URL" \
  --no-first-run --no-default-browser-check \
  --disable-extensions --disable-sync \
  --disable-features=Translate,SpellcheckService,AutofillServerCommunication \
  --password-store=basic \
  "$@" > /dev/null 2>&1
