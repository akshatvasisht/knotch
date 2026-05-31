#!/usr/bin/env bash
# Offline demo — no API keys, no external services required.
# Runs the full routing loop on fakes: in-memory bus, scripted utterances, printed TTS.
#
# Prerequisites:
#   pip install -e ".[dashboard]"
#
# Then:
#   bash scripts/demo_local.sh
#   # dashboard → http://localhost:7861
set -euo pipefail
cd "$(dirname "$0")/.."
exec env \
  CONVENER_LLM=fake \
  CONVENER_STT=fake \
  CONVENER_TTS=fake \
  CONVENER_TRANSPORT=fake \
  CONVENER_BUS=memory \
  python3 -m engine --domain _template --all-scenarios --dashboard
