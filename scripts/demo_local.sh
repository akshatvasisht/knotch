#!/usr/bin/env bash
# Run the engine on all fake backends for local testing.
# Requires: pip install -e ".[dashboard]"
set -euo pipefail
cd "$(dirname "$0")/.."
exec env \
  KNOTCH_LLM=fake \
  KNOTCH_STT=fake \
  KNOTCH_TTS=fake \
  KNOTCH_TRANSPORT=fake \
  KNOTCH_BUS=memory \
  python3 -m engine --domain _template --all-scenarios --dashboard
