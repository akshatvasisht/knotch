#!/usr/bin/env bash
# demo_drive.sh — hybrid demo driver for the Knotch live stack.
#
# Usage: scripts/demo_drive.sh [LIVE_ROLE] [DOMAIN]
#   LIVE_ROLE  role the human plays on Daily (default: role_grill)
#   DOMAIN     domain pack to inject (default: kitchen)
#
# Run this in a SECOND terminal while the live stack is already up
# (scripts/run_stack.sh) and while the human is in the Daily room.
#
# What it does:
#   • Connects to the same Upstash Redis bus as the live stack.
#   • Drives EVERY scripted scenario for <DOMAIN>, skipping utterances
#     assigned to <LIVE_ROLE> so the human's live mic is never double-driven.
#   • Loops indefinitely (~6 s gap between replays) so the demo sustains
#     while the human talks.
#   • The convener routes the other roles' utterances back to <LIVE_ROLE>
#     → the Daily worker speaks the dispatcher message in John's voice into
#     the human's Daily room.
#   • The dashboard (http://localhost:7861) lights up with live routing cards.
#
# To populate the Improvement panel (Panel 3) during the demo, use the two-step
# SAVE → instant-REPLAY flow (no ~4-min re-run on stage):
#
#   first time (slow, real Cekura + Nemotron, ~4-5 min — saves a cache file):
#     CONVENER_BUS=redis python3 -m engine.proc_improve --domain kitchen --cekura --rounds 2 --save
#
#   thereafter (INSTANT, <1s, no eval/LLM — replays the cached curves):
#     CONVENER_BUS=redis python3 -m engine.proc_curves --file runs/improve_kitchen.json
#
# (Neither is run automatically. Run the slow save once before the demo; replay
#  the cached file live as many times as you like.)

set -uo pipefail

LIVE_ROLE="${1:-role_grill}"
DOMAIN="${2:-kitchen}"
REPO="$(cd "$(dirname "$0")/.." && pwd)"

echo ""
echo "=================================================="
echo "  KNOTCH HYBRID DEMO DRIVER"
echo "  Human role (Daily): ${LIVE_ROLE}"
echo "  Domain:             ${DOMAIN}"
echo "  Bus:                redis (Upstash)"
echo ""
echo "  Injecting ALL other roles' scripted utterances."
echo "  The convener will route to ${LIVE_ROLE};"
echo "  you will hear the dispatcher in your Daily room."
echo "  Dashboard: http://localhost:7861"
echo ""
echo "  Press Ctrl-C to stop."
echo "=================================================="
echo ""

cd "$REPO"

exec env CONVENER_BUS=redis python3 -m engine.proc_inject \
    --domain "$DOMAIN" \
    --all-scenarios \
    --exclude-role "$LIVE_ROLE" \
    --loop \
    --step-delay 3 \
    --loop-delay 6

# ------------------------------------------------------------------
# Improvement panel (Panel 3) — SAVE once, REPLAY instantly:
#
#   first time (slow ~4-5 min; real Cekura + Nemotron; writes a cache file):
#     CONVENER_BUS=redis python3 -m engine.proc_improve --domain kitchen --cekura --rounds 2 --save
#
#   thereafter (INSTANT <1s; no eval/LLM; replays the cached curves):
#     CONVENER_BUS=redis python3 -m engine.proc_curves --file runs/improve_kitchen.json
# ------------------------------------------------------------------
