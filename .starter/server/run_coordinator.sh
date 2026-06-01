#!/usr/bin/env bash
# Launch the Coordinator multi-participant voice runtime.
#
# Default: TTS stub (no Gradium key needed) so the whole
# STT -> bus -> coordinator -> routed -> would-speak loop is observable in logs.
# To go live with audio: set GRADIUM_API_KEY and KNOTCH_VOICE_TTS=gradium.
set -euo pipefail
cd "$(dirname "$0")"

# Endpoint vars — must be set in .env (no defaults; hackathon endpoints are offline).
export NVIDIA_ASR_URL="${NVIDIA_ASR_URL:?NVIDIA_ASR_URL must be set in .env}"
export KNOTCH_LLM_URL="${KNOTCH_LLM_URL:?KNOTCH_LLM_URL must be set in .env}"
export KNOTCH_LLM_MODEL="${KNOTCH_LLM_MODEL:?KNOTCH_LLM_MODEL must be set in .env}"

# Local mode disables the Krisp filter (Pipecat-Cloud-only dependency).
export ENV="${ENV:-local}"

# TTS swap: stub (default here) | gradium.
export KNOTCH_VOICE_TTS="${KNOTCH_VOICE_TTS:-stub}"

# Domain pack to load (roles are assigned round-robin to connections).
export KNOTCH_DOMAIN="${KNOTCH_DOMAIN:-kitchen}"

exec uv run bot_coordinator.py
