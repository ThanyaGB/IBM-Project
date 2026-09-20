#!/usr/bin/env bash
# run_bot.sh — one-command launcher for health-myth-bot (Git Bash / macOS / Linux)
#
# Usage:
#   ./run_bot.sh setup          # migrate .env + check dependencies (safe, no servers)
#   ./run_bot.sh start          # seed DB, start Flask + dashboard + tunnel
#   ./run_bot.sh start --no-tunnel
#   ./run_bot.sh start --with-dense   # also fetch the 220 MB embedding model first
#   ./run_bot.sh stop           # stop everything this script started
#   ./run_bot.sh status         # what is running, and the tunnel URL if any
#   ./run_bot.sh logs           # tail all logs
#
# Free-stack notes:
#   * WhatsApp Cloud API: inbound is never charged; non-template replies inside
#     the 24h window a user's message opens are free. This bot only replies.
#   * Gemini free tier (GEMINI_API_KEY) or local Ollama (OLLAMA_BASE_URL).
#   * Embeddings/STT/TTS run locally; nothing here needs a paid credential.

set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

# ---------------------------------------------------------------------------
# Paths / config
# ---------------------------------------------------------------------------
VENV="$DIR/.venv"
PY="$VENV/Scripts/python.exe"                 # Windows
[[ -x "$PY" ]] || PY="$VENV/bin/python"       # macOS / Linux

LOG_DIR="$DIR/logs"
PID_DIR="$DIR/.run"
mkdir -p "$LOG_DIR" "$PID_DIR"

FLASK_PORT="${FLASK_PORT:-5000}"
STREAMLIT_PORT="${STREAMLIT_PORT:-8501}"
TUNNEL_EXE="$DIR/cloudflared.exe"
[[ -x "$TUNNEL_EXE" ]] || TUNNEL_EXE="$(command -v cloudflared || true)"

REQUIRED_ENV_KEYS=( WHATSAPP_ACCESS_TOKEN WHATSAPP_PHONE_NUMBER_ID WHATSAPP_APP_SECRET WHATSAPP_VERIFY_TOKEN )

# Carried over from an old .env when migrating (never carried empty).
CARRY_OVER_KEYS=( GEMINI_API_KEY GEMINI_MODEL GEMINI_TRANSCRIBE_MODEL OLLAMA_BASE_URL OLLAMA_MODEL DASHBOARD_PASSWORD FLASK_PORT DB_PATH TWILIO_ACCOUNT_SID TWILIO_AUTH_TOKEN TWILIO_WHATSAPP_FROM )

info()  { printf '\033[1;34m[bot]\033[0m %s\n' "$*"; }
warn()  { printf '\033[1;33m[bot]\033[0m %s\n' "$*"; }
fail()  { printf '\033[1;31m[bot]\033[0m %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------
ensure_venv() {
  if [[ ! -x "$PY" ]]; then
    info "No .venv found — creating one (one time)…"
    python -m venv "$VENV" || fail "Could not create venv. Install Python 3.11+ first."
  fi
  info "Python: $("$PY" -V 2>&1)"
}

install_deps() {
  info "Installing dependencies from requirements.txt (skips already-installed)…"
  "$PY" -m pip install -q -r requirements.txt || fail "pip install failed"
}

env_value() { # env_value KEY [FILE]  -> prints value or empty
  local key="$1" file="${2:-$DIR/.env}"
  [[ -f "$file" ]] || return 0
  grep -E "^${key}=" "$file" 2>/dev/null | head -1 | cut -d= -f2- | tr -d '\r' || true
}

migrate_env() {
  local env_file="$DIR/.env"
  local have_token
  have_token="$(env_value WHATSAPP_ACCESS_TOKEN)"

  if [[ -n "${have_token// /}" ]]; then
    info ".env already has WHATSAPP_ACCESS_TOKEN — leaving it alone."
  else
    if [[ -f "$env_file" ]]; then
      local backup="$env_file.backup.$(date +%Y%m%d-%H%M%S)"
      cp "$env_file" "$backup"
      warn "Old .env backed up to $(basename "$backup") (it predates the WhatsApp rewrite)."
    fi
    cp "$DIR/.env.example" "$env_file"
    # Carry over still-valid values from the backup, when there is one.
    if [[ -n "${backup:-}" ]]; then
      "$PY" -X utf8 - "$backup" "$env_file" "${CARRY_OVER_KEYS[@]}" <<'PYEOF'
import sys
src, dst, keys = sys.argv[1], sys.argv[2], sys.argv[3:]
carried = {}
try:
    with open(src, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                if k in keys and v.strip():
                    carried[k] = v.strip()
except FileNotFoundError:
    pass
lines = open(dst, encoding="utf-8").read().splitlines()
seen = set()
out = []
for line in lines:
    key = line.split("=", 1)[0] if "=" in line else None
    if key in carried:
        out.append(f"{key}={carried[key]}")
        seen.add(key)
    else:
        out.append(line)
out += [f"{k}={carried[k]}" for k in keys if k in carried and k not in seen]
open(dst, "w", encoding="utf-8", newline="\n").write("\n".join(out) + "\n")
print("carried over:", ", ".join(sorted(carried)) or "(nothing)")
PYEOF
    fi
    warn "Fill in the WHATSAPP_* values in .env (see the checklist printed by 'start')."
  fi

  # Sanity: keys the server cannot answer webhooks without.
  local missing=""
  for key in "${REQUIRED_ENV_KEYS[@]}"; do
    [[ -z "$(env_value "$key" | sed 's/your_.*//;s/choose_.*//')" ]] && missing="$missing $key"
  done
  if [[ -n "$missing" ]]; then
    warn "Placeholder/missing in .env:$missing — webhooks will be REJECTED until these are real."
    warn "Edit .env now, or run './run_bot.sh start' anyway to test Flask + dashboard locally."
  else
    info ".env: all WhatsApp Cloud API keys present."
  fi
  # Old key that nothing uses any more.
  if grep -qE "^OPENAI_API_KEY=" "$env_file" 2>/dev/null; then
    sed -i.bak '/^OPENAI_API_KEY=/d' "$env_file" && rm -f "$env_file.bak"
    info "Removed unused OPENAI_API_KEY from .env (no code calls OpenAI)."
  fi
}

download_dense_model() {
  info "Downloading multilingual embedding model (~220 MB, one time, local-only)…"
  "$PY" retrieval.py --download-model || warn "Download failed — the bot runs lexical-only without it."
}

check_generation_backend() {
  if [[ -n "$(env_value GEMINI_API_KEY)" || -n "$(env_value OLLAMA_BASE_URL)" ]]; then
    info "Answer backend: $( [[ -n "$(env_value GEMINI_API_KEY)" ]] && echo "Gemini free tier" || echo "local Ollama" )."
  else
    warn "No GEMINI_API_KEY and no OLLAMA_BASE_URL — every reply will be the fallback message."
  fi
}

seed_db() {
  info "Seeding database (idempotent)…"
  "$PY" -X utf8 seed_data.py || warn "seed_data.py failed — continuing."
}

wait_for_http() { # wait_for_http URL LABEL [tries]
  local url="$1" label="$2" tries="${3:-40}"
  local i
  for ((i = 1; i <= tries; i++)); do
    if curl -fsS -o /dev/null "$url" 2>/dev/null; then
      info "$label is up: $url"
      return 0
    fi
    sleep 1
  done
  warn "$label did not answer at $url after ${tries}s — check logs/"
  return 1
}

pid_alive() { [[ -f "$1" ]] && kill -0 "$(cat "$1")" 2>/dev/null; }

start_bg() { # start_bg NAME PIDFILE LOG cmd...
  local name="$1" pidfile="$2" log="$3"; shift 3
  if pid_alive "$pidfile"; then
    info "$name already running (pid $(cat "$pidfile"))."
    return 0
  fi
  info "Starting $name…"
  "$@" >"$log" 2>&1 &
  echo $! >"$pidfile"
}

start_flask() {
  start_bg "Flask webhook server" "$PID_DIR/flask.pid" "$LOG_DIR/flask.log" \
    "$PY" -X utf8 app.py
  wait_for_http "http://localhost:$FLASK_PORT/health" "Flask" 40 || true
  curl -fsS "http://localhost:$FLASK_PORT/health" 2>/dev/null | head -c 400 && echo || true
}

start_dashboard() {
  start_bg "Streamlit dashboard" "$PID_DIR/dashboard.pid" "$LOG_DIR/dashboard.log" \
    "$PY" -m streamlit run dashboard.py --server.port "$STREAMLIT_PORT" --server.headless true
  wait_for_http "http://localhost:$STREAMLIT_PORT" "Dashboard" 40 || true
}

start_tunnel() {
  if [[ -z "$TUNNEL_EXE" || ! -x "$TUNNEL_EXE" ]]; then
    warn "cloudflared not found next to run_bot.sh — no tunnel. Use ngrok or any tunnel you like."
    return 0
  fi
  start_bg "cloudflared tunnel" "$PID_DIR/tunnel.pid" "$LOG_DIR/tunnel.log" \
    "$TUNNEL_EXE" tunnel --url "http://localhost:$FLASK_PORT" --no-autoupdate
  local url="" i
  for ((i = 1; i <= 20; i++)); do
    url="$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$LOG_DIR/tunnel.log" 2>/dev/null | head -1 || true)"
    [[ -n "$url" ]] && break
    sleep 1
  done
  if [[ -n "$url" ]]; then
    info "Tunnel URL: $url"
    echo "$url" >"$PID_DIR/tunnel.url"
  else
    warn "Tunnel started but no trycloudflare.com URL appeared yet — check logs/tunnel.log"
  fi
}

print_checklist() {
  local url=""
  [[ -f "$PID_DIR/tunnel.url" ]] && url="$(cat "$PID_DIR/tunnel.url")"
  cat <<EOF

════════════════════════════════════════════════════════════════════
 NEXT STEPS (one-time, in the Meta app dashboard — developers.facebook.com)
════════════════════════════════════════════════════════════════════
 1. WhatsApp → API Setup: copy the test-number Phone number ID and the
    temporary access token into .env (WHATSAPP_PHONE_NUMBER_ID, WHATSAPP_ACCESS_TOKEN).
    Also add up to 5 verified recipient numbers there (dev-mode limit).
 2. App Settings → Basic: copy App Secret into .env (WHATSAPP_APP_SECRET).
    Every webhook request is HMAC-checked against it — keep VALIDATE_WEBHOOK_SIGNATURES=true.
 3. Put any random string in .env (WHATSAPP_VERIFY_TOKEN).
 4. WhatsApp → Configuration → Webhook → Edit:
      Callback URL:  ${url:-<tunnel-url>}/webhook
      Verify token:  the exact value of WHATSAPP_VERIFY_TOKEN
    Click "Verify and save" — Flask answers the GET handshake automatically.
 5. Subscribe to the "messages" webhook field, then send the test number a
    WhatsApp message from a verified recipient phone.
 6. Watch replies arrive:  ./run_bot.sh logs

 Dashboard:  http://localhost:$STREAMLIT_PORT
 Flask health: http://localhost:$FLASK_PORT/health
 Stop it all:  ./run_bot.sh stop
════════════════════════════════════════════════════════════════════
EOF
}

stop_all() {
  local stopped=0 name pid
  for name in tunnel dashboard flask; do
    local pidfile="$PID_DIR/$name.pid"
    if pid_alive "$pidfile"; then
      pid="$(cat "$pidfile")"
      info "Stopping $name (pid $pid)…"
      kill "$pid" 2>/dev/null || true
      sleep 1
      kill -0 "$pid" 2>/dev/null && taskkill //PID "$pid" //F >/dev/null 2>&1 || true
      stopped=1
    fi
    rm -f "$pidfile"
  done
  rm -f "$PID_DIR/tunnel.url"
  [[ "$stopped" == 1 ]] || info "Nothing was running."
}

show_status() {
  local name pidfile url
  for name in flask dashboard tunnel; do
    pidfile="$PID_DIR/$name.pid"
    if pid_alive "$pidfile"; then
      info "$name: running (pid $(cat "$pidfile"))"
    else
      info "$name: stopped"
    fi
  done
  if [[ -f "$PID_DIR/tunnel.url" ]]; then
    url="$(cat "$PID_DIR/tunnel.url")"
    info "tunnel URL: $url  (webhook: $url/webhook)"
  fi
}

tail_logs() {
  tail -n 40 -f "$LOG_DIR/flask.log" "$LOG_DIR/dashboard.log" "$LOG_DIR/tunnel.log" 2>/dev/null
}

smoke_test() {
  info "Firing signed test webhooks at http://localhost:$FLASK_PORT/webhook …"
  "$PY" -X utf8 - "$FLASK_PORT" <<'PYEOF'
import hashlib, hmac, json, os, sqlite3, sys, time, urllib.request

from dotenv import load_dotenv

load_dotenv(os.path.join(os.getcwd(), ".env"))
port = sys.argv[1]
secret = os.environ.get("WHATSAPP_APP_SECRET", "").encode()

# Unique ids per run (the bot de-duplicates retried webhooks by message id)
# and a distinct sender per case (one user's next message is consumed by the
# feedback prompt, which is correct behaviour).
run_id = str(int(time.time()))
CASES = [
    (f"wamid.SMOKE-{run_id}-1", "911234567890",
     "kya polio ki dawa safe hai mere bache ke liye", False),
    (f"wamid.SMOKE-{run_id}-2", "911234567891",
     "mere papa ko seene mein bahut dard ho raha hai", True),
]

def send(msg_id, sender, text):
    payload = {
        "object": "whatsapp_business_account",
        "entry": [{"id": "ENTRY", "changes": [{"field": "messages", "value": {
            "messaging_product": "whatsapp",
            "metadata": {"display_phone_number": "15550000000", "phone_number_id": "test"},
            "contacts": [{"profile": {"name": "Smoke"}, "wa_id": sender}],
            "messages": [{"from": sender, "id": msg_id, "timestamp": "1758000000",
                          "text": {"body": text}, "type": "text"}],
        }}]}],
    }
    body = json.dumps(payload).encode()
    sig = hmac.new(secret, body, hashlib.sha256).hexdigest()
    req = urllib.request.Request(
        f"http://localhost:{port}/webhook",
        data=body,
        headers={"Content-Type": "application/json",
                 "X-Hub-Signature-256": "sha256=" + sig},
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        print(f"  {msg_id} -> HTTP {resp.status}")

for msg_id, sender, text, _ in CASES:
    send(msg_id, sender, text)

db = os.environ.get("DB_PATH", os.path.join("data", "health_myth_bot.db"))
conn = sqlite3.connect(db)
conn.row_factory = sqlite3.Row
print("\n  what the bot recorded:")
for msg_id, sender, text, expected_emerg in CASES:
    row = conn.execute(
        "SELECT language, category, is_emergency FROM logs "
        "WHERE query_text = ? ORDER BY id DESC LIMIT 1",
        (text,),
    ).fetchone()
    if row is None:
        print(f"  FAIL {msg_id}: not logged")
    else:
        ok = (row["is_emergency"] == int(expected_emerg))
        print(f"  {'OK  ' if ok else 'FAIL'} {msg_id}: lang={row['language']} "
              f"category={row['category']} emergency={row['is_emergency']}")
PYEOF
  info "Note: outbound replies go to Meta, so they fail with 401 until real WHATSAPP_* credentials are in .env — that is expected here."
}

# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------
CMD="${1:-help}"
shift || true

case "$CMD" in
  setup)
    ensure_venv
    install_deps
    migrate_env
    check_generation_backend
    info "Setup complete. Next: fill .env, then './run_bot.sh start'."
    ;;
  start)
    WITH_DENSE=0; WITH_TUNNEL=1
    for arg in "$@"; do
      case "$arg" in
        --with-dense) WITH_DENSE=1 ;;
        --no-tunnel)  WITH_TUNNEL=0 ;;
        *) warn "unknown flag: $arg" ;;
      esac
    done
    ensure_venv
    migrate_env
    check_generation_backend
    (( WITH_DENSE == 1 )) && download_dense_model
    seed_db
    start_flask
    start_dashboard
    (( WITH_TUNNEL == 1 )) && start_tunnel
    print_checklist
    ;;
  stop)     stop_all ;;
  status)   show_status ;;
  logs)     tail_logs ;;
  smoke)    smoke_test ;;
  guide)    print_checklist ;;
  restart)  stop_all; "$0" start "$@" ;;
  help|*)
    sed -n '2,20p' "$0"
    ;;
esac
