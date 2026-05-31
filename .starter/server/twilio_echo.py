#
# twilio_echo.py — Standalone Twilio media-stream smoke-test.
#
# Architecture:
#   POST /twiml  →  returns TwiML <Connect><Stream url="wss://$PUBLIC_WSS_URL/ws">
#   WS   /ws     →  pipecat FastAPIWebsocketTransport + TwilioFrameSerializer
#                   → trivial greeting bot (Gradium TTS says hello, then stays silent)
#
# Usage:
#   PUBLIC_WSS_URL=<cloudflared-host>  (just the host, e.g. abc123.trycloudflare.com)
#   uv run twilio_echo.py              (or: .venv/bin/python twilio_echo.py)
#
# What this verifies WITHOUT a real Twilio call:
#   - POST /twiml returns valid TwiML with <Connect><Stream ...
#   - GET  /health returns 200
#   - The /ws endpoint accepts a WebSocket and the pipecat transport/serializer
#     constructs without error (headless WebSocket client test possible with wscat).
#
# What needs a real Twilio number:
#   - Full call flow (Twilio dials, media stream opens, bot speaks greeting).
#

import asyncio
import os
import sys
from pathlib import Path

import uvicorn
from contextlib import asynccontextmanager
from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket
from fastapi.responses import PlainTextResponse
from loguru import logger

# ── pipecat imports ────────────────────────────────────────────────────────────
from pipecat.frames.frames import EndFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.serializers.twilio import TwilioFrameSerializer
from pipecat.transports.websocket.fastapi import (
    FastAPIWebsocketParams,
    FastAPIWebsocketTransport,
)

# ── optional: Gradium TTS for the greeting ────────────────────────────────────
# Falls back to a no-op stub if GRADIUM_API_KEY is absent.
from pipecat.services.gradium.tts import GradiumTTSService

load_dotenv(override=True)


# ── Startup banner ────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("=" * 60)
    logger.info("Twilio Echo Smoke-Test")
    logger.info(f"  Listening on port : {PORT}")
    logger.info(f"  TwiML endpoint    : POST http://localhost:{PORT}/twiml")
    logger.info(f"  WebSocket endpoint: ws://localhost:{PORT}/ws")
    if PUBLIC_WSS_HOST:
        logger.info(f"  Public WSS host   : {PUBLIC_WSS_HOST}")
    else:
        logger.warning(
            "  PUBLIC_WSS_URL not set — set it to your cloudflared hostname before "
            "pointing Twilio at this server."
        )
    logger.info("=" * 60)
    yield


# ── env ───────────────────────────────────────────────────────────────────────
# PUBLIC_WSS_URL: just the hostname, e.g. "abc123.trycloudflare.com"
# The script builds the full wss:// URL from it.
PUBLIC_WSS_HOST = os.getenv("PUBLIC_WSS_URL", "").strip()
PORT = int(os.getenv("TWILIO_ECHO_PORT", "7860"))

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
GRADIUM_API_KEY = os.getenv("GRADIUM_API_KEY", "")
GRADIUM_VOICE_ID = os.getenv("GRADIUM_VOICE_ID", "KWJiFWu2O9nMPYcR")

# ── Twilio telephony uses 8 kHz μ-law ─────────────────────────────────────────
TWILIO_SAMPLE_RATE = 8000

app = FastAPI(lifespan=lifespan)


# ── 1. TwiML webhook ──────────────────────────────────────────────────────────

@app.post("/twiml", response_class=PlainTextResponse)
async def twiml_webhook():
    """Return TwiML that tells Twilio to open a media-stream WebSocket to /ws."""
    if not PUBLIC_WSS_HOST:
        logger.warning(
            "PUBLIC_WSS_URL is not set — TwiML will reference a placeholder host. "
            "Set PUBLIC_WSS_URL=<cloudflared-host> before pointing Twilio at this server."
        )
        wss_url = "wss://SET-PUBLIC_WSS_URL.trycloudflare.com/ws"
    else:
        # Accept both bare hostname and full wss:// URL in the env var.
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


# Convenience alias: some Twilio configs hit POST /
@app.post("/", response_class=PlainTextResponse)
async def twiml_root():
    return await twiml_webhook()


@app.get("/health")
async def health():
    return {"status": "ok", "port": PORT, "public_wss_host": PUBLIC_WSS_HOST or "(unset)"}


# ── 2. Media-stream WebSocket endpoint ───────────────────────────────────────

@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    """Twilio media-stream WebSocket.  Twilio sends the 'start' event first,
    which includes stream_sid / call_sid.  We parse that before handing off to
    pipecat so the TwilioFrameSerializer is constructed with real SIDs."""
    await websocket.accept()
    logger.info("Twilio media-stream WebSocket connected")

    # ── Read Twilio 'start' event to get stream_sid / call_sid ───────────────
    # Twilio always sends a JSON 'connected' then a 'start' message before any
    # media frames.  We need stream_sid (required) from the 'start' event.
    stream_sid: str | None = None
    call_sid: str | None = None

    try:
        import json as _json

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

    # ── Build TwilioFrameSerializer ───────────────────────────────────────────
    # auto_hang_up requires call_sid + account_sid + auth_token.
    # If any credential is missing, disable auto_hang_up so the serializer
    # still constructs without error.
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

    # ── Build pipecat transport ───────────────────────────────────────────────
    transport = FastAPIWebsocketTransport(
        websocket=websocket,
        params=FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            add_wav_header=False,       # Twilio needs raw μ-law, not WAV
            serializer=serializer,
            audio_in_sample_rate=TWILIO_SAMPLE_RATE,
            audio_out_sample_rate=TWILIO_SAMPLE_RATE,
            audio_in_channels=1,
            audio_out_channels=1,
        ),
    )

    # ── Build greeting TTS ────────────────────────────────────────────────────
    if GRADIUM_API_KEY:
        tts = GradiumTTSService(
            api_key=GRADIUM_API_KEY,
            settings=GradiumTTSService.Settings(voice=GRADIUM_VOICE_ID),
        )
        logger.info("Using Gradium TTS for greeting")
    else:
        tts = None
        logger.warning("GRADIUM_API_KEY not set — no audio greeting will be spoken")

    # ── Pipeline: transport.input → (optional TTS) → transport.output ────────
    # The greeting is pushed as a TTSSpeakFrame on client-connected.
    # Pipeline is deliberately minimal: input → tts → output.
    if tts:
        pipeline = Pipeline([
            transport.input(),
            tts,
            transport.output(),
        ])
    else:
        pipeline = Pipeline([
            transport.input(),
            transport.output(),
        ])

    worker = PipelineWorker(
        pipeline,
        params=PipelineParams(
            audio_in_sample_rate=TWILIO_SAMPLE_RATE,
            audio_out_sample_rate=TWILIO_SAMPLE_RATE,
        ),
    )

    # ── Speak greeting on connect, then close after a short pause ─────────────
    @transport.event_handler("on_client_connected")
    async def on_connected(transport_obj, ws):
        if tts:
            from pipecat.frames.frames import TTSSpeakFrame
            logger.info("Client connected — queueing greeting")
            await transport_obj.input().push_frame(
                TTSSpeakFrame("Hello! Twilio echo smoke-test confirmed. The pipecat transport is working correctly.")
            )
        else:
            logger.info("Client connected (no TTS configured — silent echo mode)")

        # After a generous pause, end the call gracefully.
        await asyncio.sleep(8)
        await worker.queue_frame(EndFrame())

    @transport.event_handler("on_client_disconnected")
    async def on_disconnected(transport_obj, ws):
        logger.info("Twilio WebSocket disconnected")
        await worker.queue_frame(EndFrame())

    # ── Run ───────────────────────────────────────────────────────────────────
    logger.info("Starting pipecat pipeline for Twilio echo bot")
    await worker.run()
    logger.info("Pipecat pipeline finished")


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")
