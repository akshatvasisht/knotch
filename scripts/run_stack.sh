#!/usr/bin/env bash
# Launch the full distributed Coordinator stack (one of each) over Upstash Redis.
#   coordinator (transport-less)  +  dashboard  +  Daily worker (role_grill)
#   +  Twilio worker (role_fryer)  +  cloudflared tunnel  (+ Twilio webhook).
# Daily + Twilio are different participants on the SAME bus + one coordinator
# = the mixed-transport demo. Re-run any time; it stops the prior stack first.
set -uo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
SERVER="$REPO/.starter/server"
LOG=/tmp/coordinator_stack; mkdir -p "$LOG"
PIDFILE="$LOG/pids"
export PATH="$HOME/.local/bin:$PATH"
export KNOTCH_BUS=redis

"$REPO/scripts/stop_stack.sh" >/dev/null 2>&1
sleep 2
: > "$PIDFILE"

cd "$REPO"
echo "starting coordinator…"
nohup env KNOTCH_BUS=redis KNOTCH_LLM=nemotron python3 -m engine.proc_coordinator --domain kitchen >"$LOG/coordinator.log" 2>&1 & echo $! >> "$PIDFILE"
echo "starting dashboard…"
nohup env KNOTCH_BUS=redis python3 -m engine.dashboard --domain kitchen --live --port 7861 >"$LOG/dashboard.log" 2>&1 & echo $! >> "$PIDFILE"
echo "starting cloudflared tunnel…"
nohup cloudflared tunnel --url http://localhost:7860 >"$LOG/cloudflared.log" 2>&1 & echo $! >> "$PIDFILE"
sleep 9
URL=$(grep -oE "https://[a-z0-9-]+\.trycloudflare\.com" "$LOG/cloudflared.log" | head -1)
HOST=${URL#https://}
echo "tunnel: ${URL:-FAILED}"

# Point the Twilio number's voice webhook at this tunnel (creds from .env).
SID=$(grep -E '^TWILIO_ACCOUNT_SID=' "$REPO/.env" 2>/dev/null | cut -d= -f2)
TOKEN=$(grep -E '^TWILIO_AUTH_TOKEN=' "$REPO/.env" 2>/dev/null | cut -d= -f2)
PHONE=$(grep -E '^TWILIO_PHONE_NUMBER=' "$REPO/.env" 2>/dev/null | cut -d= -f2)
PN=$(grep -E '^TWILIO_PHONE_SID=' "$REPO/.env" 2>/dev/null | cut -d= -f2)
if [ -n "${URL:-}" ] && [ -n "${PN:-}" ]; then
  curl -s -u "$SID:$TOKEN" -X POST \
    "https://api.twilio.com/2010-04-01/Accounts/$SID/IncomingPhoneNumbers/$PN.json" \
    --data-urlencode "VoiceUrl=$URL/twiml" --data-urlencode "VoiceMethod=POST" >/dev/null \
    && echo "twilio webhook -> $URL/twiml"
fi

cd "$SERVER"
echo "starting twilio_worker (role_fryer)…"
nohup env PATH="$HOME/.local/bin:$PATH" PUBLIC_WSS_URL="$HOST" KNOTCH_BUS=redis KNOTCH_TWILIO_ROLE=role_fryer \
  uv run twilio_worker.py >"$LOG/twilio_worker.log" 2>&1 & echo $! >> "$PIDFILE"
echo "starting worker_daily (role_grill)…"
nohup env PATH="$HOME/.local/bin:$PATH" KNOTCH_BUS=redis \
  uv run worker_daily.py --role role_grill --domain kitchen >"$LOG/worker_daily.log" 2>&1 & echo $! >> "$PIDFILE"

cd "$REPO"
sleep 20
ROOM=$(grep -oE "https://[a-z0-9.-]+\.daily\.co/[A-Za-z0-9]+" "$LOG/worker_daily.log" | head -1)
echo "${URL:-}" > "$LOG/tunnel_url.txt"; echo "${ROOM:-}" > "$LOG/daily_room.txt"
cat <<EOF

================= COORDINATOR STACK UP =================
 Dashboard:   http://localhost:7861
 Daily room:  ${ROOM:-"(see $LOG/worker_daily.log)"}
              ^ open in a browser/phone, allow mic = participant GRILL
 Twilio:      call ${PHONE:-<TWILIO_PHONE_NUMBER>}  = participant FRYER
 Tunnel:      ${URL:-FAILED}
 Logs:        $LOG/*.log      Stop: scripts/stop_stack.sh
====================================================
EOF
