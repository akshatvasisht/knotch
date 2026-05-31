#
# twilio_worker.py — Twilio media-stream participant worker on the shared Redis bus.
#
# Architecture:
#   POST /twiml  →  returns TwiML <Connect><Stream url="wss://$PUBLIC_WSS_URL/ws">
#   WS   /ws     →  real convener-connected pipeline (STT → BusBridge → TTS)
#   GET  /health →  simple liveness probe
#
# Pipeline per call (mirrors worker_daily.py with Twilio transport instead of Daily):
#
#   transport.input()
#     → NVidiaWebSocketSTTService(url=NVIDIA_ASR_URL, strip_interim_prefix=True)
#     → BusBridge(role_id, bus, domain)
#     → user_aggregator (SileroVAD + FilterIncompleteUserTurnStrategies)
#     → build_tts(role_id, voice_id)    # Gradium or StubTTS
#     → transport.output()
#
# Role: env CONVENER_TWILIO_ROLE (default: role_grill)
# Bus:  factory.make_bus()  (CONVENER_BUS=redis → RedisBus on Upstash)
#
# Usage:
#   PUBLIC_WSS_URL=<cloudflared-host>  CONVENER_BUS=redis  \
#   CONVENER_TWILIO_ROLE=role_grill    uv run twilio_worker.py
#
# Headless import test (no real call needed):
#   CONVENER_BUS=redis PUBLIC_WSS_URL=example.trycloudflare.com \
#   uv run python -c "import twilio_worker"
#

from __future__ import annotations

import asyncio
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket
from fastapi.responses import PlainTextResponse
from loguru import logger

# ── pipecat ───────────────────────────────────────────────────────────────────
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import EndFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.serializers.twilio import TwilioFrameSerializer
from pipecat.transports.websocket.fastapi import (
    FastAPIWebsocketParams,
    FastAPIWebsocketTransport,
)
from pipecat.turns.user_turn_strategies import FilterIncompleteUserTurnStrategies

# ── local server module ───────────────────────────────────────────────────────
from nvidia_stt import NVidiaWebSocketSTTService

# --------------------------------------------------------------------------- #
# Env loading — identical strategy to worker_daily.py:                        #
#   1. repo-root .env (DAILY_API_KEY, REDIS_URL, NVIDIA_ASR_URL, Gradium…)   #
#   2. .starter/server/.env (pipeline knobs) — may override root values       #
# --------------------------------------------------------------------------- #
_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parents[1]

load_dotenv(_REPO_ROOT / ".env", override=False)
load_dotenv(_HERE / ".env", override=True)

# Add repo root so engine/ and adapters/ are importable
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# ── engine imports (pipecat-free; live two levels up) ─────────────────────────
from adapters import factory  # noqa: E402
from engine.interfaces import Bus  # noqa: E402
from engine.packloader import load_pack  # noqa: E402

# BusBridge + build_tts — reuse from worker_daily.py (they are module-level there)
from worker_daily import BusBridge, build_tts  # noqa: E402

# --------------------------------------------------------------------------- #
# Env                                                                          #
# --------------------------------------------------------------------------- #
PUBLIC_WSS_HOST = os.getenv("PUBLIC_WSS_URL", "").strip()
PORT = int(os.getenv("PORT", "7860"))

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")

NVIDIA_ASR_URL = os.environ["NVIDIA_ASR_URL"]

# The role this Twilio worker represents — one inbound call = one participant.
CONVENER_TWILIO_ROLE = os.getenv("CONVENER_TWILIO_ROLE", "role_grill")
CONVENER_DOMAIN = os.getenv("CONVENER_DOMAIN", "kitchen")

# Twilio telephony: 8 kHz μ-law
TWILIO_SAMPLE_RATE = 8000

# --------------------------------------------------------------------------- #
# Shared pack + bus — loaded once at import time (lazy-safe: no I/O until the  #
# first websocket connects, but we resolve paths now so the import test works). #
# --------------------------------------------------------------------------- #
_bus: Bus | None = None
_pack = None


def _get_pack():
    """Return the loaded pack (kitchen by default). Called lazily inside ws handler."""
    global _pack
    if _pack is None:
        _pack = load_pack(
            CONVENER_DOMAIN,
            domains_dir=str(_REPO_ROOT / "domains"),
            prompts_dir=str(_REPO_ROOT / "prompts"),
        )
        logger.info(
            f"Domain pack loaded: domain={_pack.domain} "
            f"roles={_pack.role_ids} "
            f"dispatcher_voice={_pack.dispatcher_voice_id}"
        )
    return _pack


def _get_bus() -> Bus:
    """Return the shared bus singleton (RedisBus when CONVENER_BUS=redis)."""
    global _bus
    if _bus is None:
        _bus = factory.make_bus()
        backend = factory._backend("CONVENER_BUS")
        logger.info(f"Bus initialised: backend={backend}")
    return _bus


# --------------------------------------------------------------------------- #
# FastAPI app                                                                  #
# --------------------------------------------------------------------------- #

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("=" * 60)
    logger.info("Twilio Convener Participant Worker")
    logger.info(f"  Port               : {PORT}")
    logger.info(f"  TwiML endpoint     : POST http://localhost:{PORT}/twiml")
    logger.info(f"  WebSocket endpoint : ws://localhost:{PORT}/ws")
    logger.info(f"  Role               : {CONVENER_TWILIO_ROLE}")
    logger.info(f"  Domain             : {CONVENER_DOMAIN}")
    if PUBLIC_WSS_HOST:
        logger.info(f"  Public WSS host    : {PUBLIC_WSS_HOST}")
    else:
        logger.warning(
            "  PUBLIC_WSS_URL not set — set it to your cloudflared hostname before "
            "pointing Twilio at this server."
        )
    logger.info("=" * 60)
    yield


app = FastAPI(lifespan=lifespan)


# --------------------------------------------------------------------------- #
# 1. TwiML webhook                                                             #
# --------------------------------------------------------------------------- #

@app.post("/twiml", response_class=PlainTextResponse)
async def twiml_webhook():
    """Return TwiML that tells Twilio to open a media-stream WebSocket to /ws."""
    if not PUBLIC_WSS_HOST:
        logger.warning(
            "PUBLIC_WSS_URL is not set — TwiML will reference a placeholder host."
        )
        wss_url = "wss://SET-PUBLIC_WSS_URL.trycloudflare.com/ws"
    else:
        if PUBLIC_WSS_HOST.startswith("wss://") or PUBLIC_WSS_HOST.startswith("ws://"):
            wss_url = PUBLIC_WSS_HOST.rstrip("/") + "/ws"
        else:
            wss_url = f"wss://{PUBLIC_WSS_HOST}/ws"

    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Connect>
    <Stream url="{wss_url}" />
  </Connect>
</Response>"""
    logger.info(f"TwiML served — stream URL: {wss_url}")
    return PlainTextResponse(content=twiml, media_type="text/xml")


@app.post("/", response_class=PlainTextResponse)
async def twiml_root():
    return await twiml_webhook()


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "port": PORT,
        "public_wss_host": PUBLIC_WSS_HOST or "(unset)",
        "role": CONVENER_TWILIO_ROLE,
        "domain": CONVENER_DOMAIN,
    }


# --------------------------------------------------------------------------- #
# 2. Media-stream WebSocket endpoint — the REAL participant pipeline           #
# --------------------------------------------------------------------------- #

@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    """Twilio media-stream WebSocket.

    Reads the Twilio 'start' event to get stream_sid/call_sid, then runs the
    full convener-participant pipeline (STT → BusBridge → TTS) on the Redis bus.
    """
    await websocket.accept()
    logger.info("Twilio media-stream WebSocket connected")

    # ── Parse Twilio 'start' event ────────────────────────────────────────────
    # Twilio always sends 'connected' then 'start' before any media frames.
    import json as _json

    stream_sid: str | None = None
    call_sid: str | None = None

    try:
        while stream_sid is None:
            raw = await websocket.receive_text()
            msg = _json.loads(raw)
            event = msg.get("event", "")
            logger.debug(f"Twilio pre-start event: {event}")
            if event == "start":
                stream_sid = msg["start"]["streamSid"]
                call_sid = msg["start"].get("callSid")
                logger.info(f"Stream started: stream_sid={stream_sid} call_sid={call_sid}")
            elif event == "stop":
                logger.info("Twilio sent 'stop' before 'start' — closing.")
                await websocket.close()
                return
    except Exception as exc:
        logger.error(f"Error reading Twilio start event: {exc}")
        await websocket.close()
        return

    # ── TwilioFrameSerializer ─────────────────────────────────────────────────
    have_creds = bool(call_sid and TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN)
    serializer = TwilioFrameSerializer(
        stream_sid=stream_sid,
        call_sid=call_sid if have_creds else None,
        account_sid=TWILIO_ACCOUNT_SID if have_creds else None,
        auth_token=TWILIO_AUTH_TOKEN if have_creds else None,
        params=TwilioFrameSerializer.InputParams(
            twilio_sample_rate=TWILIO_SAMPLE_RATE,
            sample_rate=TWILIO_SAMPLE_RATE,
            auto_hang_up=have_creds,
        ),
    )

    # ── FastAPIWebsocketTransport ─────────────────────────────────────────────
    transport = FastAPIWebsocketTransport(
        websocket=websocket,
        params=FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            add_wav_header=False,  # Twilio needs raw μ-law, not WAV
            serializer=serializer,
            audio_in_sample_rate=TWILIO_SAMPLE_RATE,
            audio_out_sample_rate=TWILIO_SAMPLE_RATE,
            audio_in_channels=1,
            audio_out_channels=1,
        ),
    )

    # ── Load pack + bus (lazy singletons — safe to call multiple times) ───────
    pack = _get_pack()
    bus = _get_bus()

    role_id = CONVENER_TWILIO_ROLE
    role = pack.role(role_id)
    if role is None:
        logger.error(
            f"Role '{role_id}' not found in domain '{pack.domain}'. "
            f"Known roles: {pack.role_ids}. "
            "Set CONVENER_TWILIO_ROLE to a valid role id."
        )
        await websocket.close()
        return

    voice_id = role.voice_id or pack.dispatcher_voice_id
    logger.info(
        f"Participant: role={role_id} ({role.display_name}) "
        f"domain={pack.domain} voice={voice_id or '(default)'}"
    )

    # ── STT ───────────────────────────────────────────────────────────────────
    stt = NVidiaWebSocketSTTService(
        url=NVIDIA_ASR_URL,
        strip_interim_prefix=True,
    )

    # ── User aggregator (SileroVAD + turn strategy) ───────────────────────────
    # No per-participant LLM — the convener (separate process on the same bus)
    # is the only reasoning step. The aggregator context is unused but required
    # to host VAD + turn strategy.
    context = LLMContext()
    user_aggregator, _assistant_aggregator = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(
            vad_analyzer=SileroVADAnalyzer(),
            user_turn_strategies=FilterIncompleteUserTurnStrategies(),
        ),
    )

    # ── BusBridge + TTS ───────────────────────────────────────────────────────
    bridge = BusBridge(role_id=role_id, bus=bus, domain=pack.domain)
    tts = build_tts(role_id, voice_id)

    # ── Pipeline ──────────────────────────────────────────────────────────────
    # BusBridge must sit directly after STT: user_aggregator consumes
    # TranscriptionFrames into its LLM context and does not forward them, so a
    # bridge placed after it would never see the finalized transcript.
    pipeline = Pipeline(
        [
            transport.input(),
            stt,
            bridge,
            user_aggregator,
            tts,
            transport.output(),
        ]
    )

    worker = PipelineWorker(
        pipeline,
        cancel_on_idle_timeout=False,        # don't drop the call on a quiet moment
        cancel_runner_on_idle_timeout=False,
        params=PipelineParams(
            audio_in_sample_rate=TWILIO_SAMPLE_RATE,
            audio_out_sample_rate=TWILIO_SAMPLE_RATE,
        ),
    )

    # ── Event handlers ────────────────────────────────────────────────────────
    @transport.event_handler("on_client_connected")
    async def on_connected(transport_obj, ws):
        logger.info(f"Twilio call connected (role={role_id})")

    @transport.event_handler("on_client_disconnected")
    async def on_disconnected(transport_obj, ws):
        logger.info(f"Twilio WebSocket disconnected (role={role_id})")
        await worker.queue_frame(EndFrame())

    # ── Run ───────────────────────────────────────────────────────────────────
    logger.info(f"Starting convener participant pipeline (role={role_id})")
    await worker.run()
    logger.info(f"Pipeline finished (role={role_id})")


# --------------------------------------------------------------------------- #
# Entry point                                                                  #
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")
