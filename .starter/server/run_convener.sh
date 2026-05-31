#!/usr/bin/env bash
# Launch the Convener multi-participant voice runtime.
#
# Default: TTS stub (no Gradium key needed) so the whole
# STT -> bus -> convener -> routed -> would-speak loop is observable in logs.
# To go live with audio: set GRADIUM_API_KEY and CONVENER_VOICE_TTS=gradium.
set -euo pipefail
cd "$(dirname "$0")"

# Endpoint vars — must be set in .env (no defaults; hackathon endpoints are offline).
export NVIDIA_ASR_URL="${NVIDIA_ASR_URL:?NVIDIA_ASR_URL must be set in .env}"
export CONVENER_LLM_URL="${CONVENER_LLM_URL:?CONVENER_LLM_URL must be set in .env}"
export CONVENER_LLM_MODEL="${CONVENER_LLM_MODEL:?CONVENER_LLM_MODEL must be set in .env}"

# Local mode disables the Krisp filter (Pipecat-Cloud-only dependency).
export ENV="${ENV:-local}"

# TTS swap: stub (default here) | gradium.
export CONVENER_VOICE_TTS="${CONVENER_VOICE_TTS:-stub}"

# Domain pack to load (roles are assigned round-robin to connections).
export CONVENER_DOMAIN="${CONVENER_DOMAIN:-kitchen}"

exec uv run bot_convener.py
