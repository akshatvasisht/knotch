#!/usr/bin/env bash
# Stop the Convener stack. Safe: this script's own command line ("bash
# scripts/stop_stack.sh") does not contain any of the match patterns below,
# so pkill -f will not kill the shell running it.
for pat in \
  "engine.proc_convener" \
  "engine.dashboard --domain" \
  "worker_daily.py" \
  "twilio_worker.py" \
  "cloudflared tunnel --url http://localhost:7860" ; do
  if pkill -f "$pat" 2>/dev/null; then echo "stopped: $pat"; fi
done
rm -f /tmp/convener_stack/pids 2>/dev/null
echo "stack stopped."
