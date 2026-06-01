#
# Copyright (c) 2024–2026, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Daily-transport participant voice worker — ONE process == ONE participant.

This is the distributed, process-per-worker counterpart of ``bot_coordinator.py``.
Where bot_coordinator ran every participant inside ONE process on an in-memory bus,
this file runs a SINGLE participant in its OWN process, joins its OWN Daily room,
and meets the coordinator + every other participant only on the SHARED Redis bus.

Daily is a cloud SFU, so there is no NAT to punch: the bot dials out to Daily and
a human opens the same room URL on a phone and speaks. N=3 == launch three of
these workers with three roles -> three rooms -> three phones; the separate
``engine.proc_coordinator`` process coordinates them over the bus, and
``python -m engine.dashboard --domain kitchen --live`` visualises it.

Per-participant pipeline (identical ordering to bot_coordinator)::

    transport.input()
      -> NVidiaWebSocketSTTService(url=NVIDIA_ASR_URL, strip_interim_prefix=True)
      -> BusBridge(role_id, bus)        # the splice point (directly after STT)
      -> user_aggregator (SileroVAD + FilterIncompleteUserTurnStrategies)
      -> tts                            # Gradium OR a logging stub (env swap)
      -> transport.output()

There is NO coordinator and NO dashboard in THIS process. The BusBridge:
  * On a finalized TranscriptionFrame: triage -> if routable, publish an
    Utterance Envelope to bus:utterances.
  * On StartFrame: subscribe to bus:routed; for each decision self_select-ed for
    this role, push a TTSSpeakFrame so the dispatcher voice speaks into the room.

Run (creates its own room, prints the URL the human opens)::

    KNOTCH_BUS=redis uv run worker_daily.py --role role_grill --domain kitchen

Run against an existing room::

    KNOTCH_BUS=redis uv run worker_daily.py --role role_grill --room https://you.daily.co/abc
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

import aiohttp
from dotenv import load_dotenv
from loguru import logger
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import (
    CancelFrame,
    EndFrame,
    Frame,
    StartFrame,
    TranscriptionFrame,
    TTSSpeakFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.transports.daily.transport import DailyParams, DailyTransport
from pipecat.turns.user_turn_strategies import FilterIncompleteUserTurnStrategies
from pipecat.workers.runner import WorkerRunner

from nvidia_stt import NVidiaWebSocketSTTService

# --------------------------------------------------------------------------- #
# Load THIS server's .env (DAILY_API_KEY, REDIS_URL, NVIDIA_ASR_URL, Gradium). #
# Then add the repo root to sys.path so the pipecat-free engine/adapters can   #
# be imported (they live two levels up).                                       #
# --------------------------------------------------------------------------- #
_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parents[1]

# Secrets (DAILY_API_KEY, REDIS_URL, NVIDIA_ASR_URL, Gradium) live in the repo-root
# .env; this server's local .env carries the coordinator/pipeline knobs. Load root
# first, then let the server-local .env override any overlapping knobs.
load_dotenv(_REPO_ROOT / ".env", override=False)
load_dotenv(_HERE / ".env", override=True)

if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from adapters import factory  # noqa: E402
from engine.interfaces import (  # noqa: E402
    CHAN_ROUTED,
    TYPE_UTTERANCE,
    Bus,
    Envelope,
    RoutingDecision,
    Utterance,
)
from engine.domain_loader import load_pack  # noqa: E402
from engine.routing import self_select  # noqa: E402
from engine.triage import triage  # noqa: E402

DAILY_API_URL = "https://api.daily.co/v1"


# --------------------------------------------------------------------------- #
# Daily REST: create a room + an owner meeting token via the public REST API.  #
# pipecat ships helpers, but the raw REST calls are the fastest thing to stand #
# up and need nothing beyond aiohttp + DAILY_API_KEY.                          #
# --------------------------------------------------------------------------- #
async def create_daily_room_and_token(
    *, bot_name: str, ttl_secs: int = 2 * 60 * 60
) -> tuple[str, str, str]:
    """Create a fresh Daily room + an owner token for the bot.

    Returns (room_url, room_name, token). Raises RuntimeError on a non-2xx
    response so the worker fails loudly instead of joining nothing.
    """
    api_key = os.environ.get("DAILY_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError(
            "DAILY_API_KEY is not set. Add it to .starter/server/.env so the "
            "worker can create its Daily room."
        )

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    exp = int(time.time()) + ttl_secs

    async with aiohttp.ClientSession() as session:
        # 1) Create the room.
        room_body = {
            "privacy": "public",
            "properties": {
                "exp": exp,
                "enable_chat": False,
                "enable_prejoin_ui": False,
                "start_audio_off": False,
                "start_video_off": True,
            },
        }
        async with session.post(
            f"{DAILY_API_URL}/rooms", headers=headers, json=room_body
        ) as resp:
            text = await resp.text()
            if resp.status not in (200, 201):
                raise RuntimeError(
                    f"Daily create-room failed ({resp.status}): {text}"
                )
            room = await _json(resp, text)
        room_url = room["url"]
        room_name = room["name"]

        # 2) Owner token for the bot.
        token_body = {
            "properties": {
                "room_name": room_name,
                "is_owner": True,
                "user_name": bot_name,
                "exp": exp,
            }
        }
        async with session.post(
            f"{DAILY_API_URL}/meeting-tokens", headers=headers, json=token_body
        ) as resp:
            text = await resp.text()
            if resp.status not in (200, 201):
                raise RuntimeError(
                    f"Daily create-token failed ({resp.status}): {text}"
                )
            tok = await _json(resp, text)
        token = tok["token"]

    return room_url, room_name, token


async def token_for_room(*, room_url: str, bot_name: str, ttl_secs: int = 2 * 60 * 60) -> str:
    """Mint an owner token for an already-existing room URL (--room path)."""
    api_key = os.environ.get("DAILY_API_KEY", "").strip()
    if not api_key:
        # An existing public room may still be joinable token-less.
        logger.warning("DAILY_API_KEY unset; joining --room without a token.")
        return ""
    room_name = room_url.rstrip("/").rsplit("/", 1)[-1]
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    body = {
        "properties": {
            "room_name": room_name,
            "is_owner": True,
            "user_name": bot_name,
            "exp": int(time.time()) + ttl_secs,
        }
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{DAILY_API_URL}/meeting-tokens", headers=headers, json=body
        ) as resp:
            text = await resp.text()
            if resp.status not in (200, 201):
                logger.warning(
                    f"Token mint for existing room failed ({resp.status}): {text}; "
                    "joining without a token."
                )
                return ""
            tok = await _json(resp, text)
    return tok.get("token", "")


async def _json(resp, text: str) -> dict:
    import json

    try:
        return json.loads(text)
    except ValueError:
        return await resp.json()


# --------------------------------------------------------------------------- #
# TTS: Gradium (real audio) or a logging stub when no Gradium key is present. #
# --------------------------------------------------------------------------- #
class StubTTS(FrameProcessor):
    """No-audio TTS stub. On a TTSSpeakFrame, logs the dispatcher line so the
    full STT -> bus -> coordinator -> routed -> speak path is observable without a
    Gradium key. All other frames pass through."""

    def __init__(self, *, role_id: str, **kwargs):
        super().__init__(**kwargs)
        self._role_id = role_id

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, TTSSpeakFrame):
            logger.info(f"🔊 dispatcher → {self._role_id}: {frame.text}")
            return  # swallow — no audio backend
        await self.push_frame(frame, direction)


def build_tts(role_id: str, voice_id: str):
    """Return the TTS service for this role, selected by KNOTCH_VOICE_TTS (gradium | stub).

    Defaults to gradium when GRADIUM_API_KEY is set, else stub.
    """
    choice = os.getenv("KNOTCH_VOICE_TTS", "").strip().lower()
    if not choice:
        choice = "gradium" if os.getenv("GRADIUM_API_KEY") else "stub"

    if choice == "gradium":
        from pipecat.services.gradium.tts import GradiumTTSService

        # The dispatcher is ONE Gradium voice (GRADIUM_VOICE_ID) — authoritative.
        # The pack's per-role voice_id is the participant's own identity, NOT a
        # Gradium id (e.g. NVIDIA Magpie placeholders), so it must NOT be sent to
        # Gradium or it errors "Embeddings not found". Env wins; voice_id is only
        # a last-resort fallback.
        gradium_voice = os.getenv("GRADIUM_VOICE_ID") or voice_id
        return GradiumTTSService(
            api_key=os.environ["GRADIUM_API_KEY"],
            settings=GradiumTTSService.Settings(voice=gradium_voice),
        )

    logger.warning(
        f"KNOTCH_VOICE_TTS='{choice or 'stub'}' — using StubTTS (no audio). "
        f"dispatcher messages for {role_id} will be logged, not spoken."
    )
    return StubTTS(role_id=role_id)


# --------------------------------------------------------------------------- #
# BusBridge — splice point between this participant's pipeline and the SHARED  #
# bus. Accepts the engine `Bus` Protocol — both RedisBus and InMemoryBus satisfy it.  #
# --------------------------------------------------------------------------- #
class BusBridge(FrameProcessor):
    def __init__(self, *, role_id: str, bus: Bus, domain: str = "", **kwargs):
        super().__init__(**kwargs)
        self._role_id = role_id
        self._bus = bus
        self._domain = domain
        self._routed_task: asyncio.Task | None = None

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        # Start the routed-decision listener on StartFrame; tear it down on
        # EndFrame/CancelFrame. Lifecycle frames still flow through.
        if isinstance(frame, StartFrame):
            if self._routed_task is None:
                self._routed_task = asyncio.create_task(self._consume_routed())
                logger.info(f"🔗 BusBridge[{self._role_id}] subscribed to bus:routed")
            await self.push_frame(frame, direction)
            return

        if isinstance(frame, (EndFrame, CancelFrame)):
            await self._stop_routed_task()
            await self.push_frame(frame, direction)
            return

        # A finalized transcript signals end-of-turn. Publish it to the bus, then
        # forward the frame so the downstream aggregator's VAD/turn strategy still
        # runs. No echo risk: TTS only speaks TTSSpeakFrames from the routed-
        # decision consumer, never a raw TranscriptionFrame.
        if isinstance(frame, TranscriptionFrame) and getattr(frame, "finalized", False):
            await self._on_final_transcript(frame)
            await self.push_frame(frame, direction)
            return

        await self.push_frame(frame, direction)

    async def _on_final_transcript(self, frame: TranscriptionFrame):
        text = (frame.text or "").strip()
        if not text:
            return

        routable, triage_class = triage(text)
        if not routable:
            logger.debug(
                f"🛑 {self._role_id} backchannel held: '{text}' ({triage_class})"
            )
            return

        utt = Utterance(
            participant=self._role_id,
            text=text,
            final=True,
            triage_class=triage_class,
            routable=True,
        )
        logger.info(f"🎙️  {self._role_id} → bus ({triage_class}): {text}")
        await self._bus.publish(
            Envelope(type=TYPE_UTTERANCE, payload=utt.to_dict(), domain=self._domain)
        )

    async def _consume_routed(self):
        """Background: for each coordinator decision addressed to this role, speak it."""
        try:
            async for env in self._bus.subscribe(CHAN_ROUTED):
                decision = RoutingDecision.from_dict(env.payload)
                if self_select(decision, self._role_id) and decision.message:
                    logger.info(
                        f"📡 routed → {self._role_id} "
                        f"[{decision.signal_type}/{decision.urgency}]: {decision.message}"
                    )
                    await self.push_frame(
                        TTSSpeakFrame(decision.message), FrameDirection.DOWNSTREAM
                    )
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001
            logger.error(f"BusBridge[{self._role_id}] routed consumer error: {exc}")

    async def _stop_routed_task(self):
        if self._routed_task is not None:
            self._routed_task.cancel()
            try:
                await self._routed_task
            except asyncio.CancelledError:
                pass
            self._routed_task = None


# --------------------------------------------------------------------------- #
# The participant worker: one Daily room, one pipeline, one shared-bus splice. #
# --------------------------------------------------------------------------- #
async def run_worker(args: argparse.Namespace) -> None:
    # The shared bus (RedisBus on Upstash when KNOTCH_BUS=redis). No coordinator
    # here — the separate engine.proc_coordinator owns that role on the same bus.
    bus = factory.make_bus()

    pack = load_pack(
        args.domain,
        domains_dir=str(_REPO_ROOT / "domains"),
        prompts_dir=str(_REPO_ROOT / "prompts"),
    )
    role = pack.role(args.role)
    if role is None:
        raise SystemExit(
            f"Role '{args.role}' not found in domain '{args.domain}'. "
            f"Known roles: {pack.role_ids}"
        )
    role_id = role.role_id
    display = role.display_name
    bot_name = f"Coordinator · {display}"

    # --- Daily room + token ------------------------------------------------- #
    if args.room:
        room_url = args.room
        token = await token_for_room(room_url=room_url, bot_name=bot_name)
        logger.info(f"Joining existing Daily room: {room_url}")
    else:
        room_url, room_name, token = await create_daily_room_and_token(bot_name=bot_name)
        logger.info(f"Created Daily room '{room_name}'")

    # Loud, parseable banner: this is the URL the human opens on their phone.
    print("\n" + "=" * 72, flush=True)
    print(f"  ROLE        : {role_id}  ({display})", flush=True)
    print(f"  OPEN ON PHONE: {room_url}", flush=True)
    print("=" * 72 + "\n", flush=True)

    backend = factory._backend("KNOTCH_BUS")
    logger.info(
        f"Worker up · role={role_id} domain={pack.domain} bus={backend} "
        f"voice={role.voice_id or '(default)'}"
    )

    # --- Transport: STT is explicit, so Daily's own VAD/transcription stay off #
    transport = DailyTransport(
        room_url,
        token,
        bot_name,
        params=DailyParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            # No transport-level VAD: turn-taking VAD lives in the user aggregator.
            transcription_enabled=False,  # STT is the explicit NVidia ws service
        ),
    )

    stt = NVidiaWebSocketSTTService(
        url=os.environ["NVIDIA_ASR_URL"],
        strip_interim_prefix=True,
    )

    # The user aggregator hosts SileroVAD + the turn strategy that treats a
    # finalized STT transcript as end-of-turn. No per-participant LLM — the
    # coordinator (separate process) is the only reasoning step; this LLM context
    # is unused but required to host VAD + turn strategy.
    context = LLMContext()
    user_aggregator, _assistant_aggregator = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(
            vad_analyzer=SileroVADAnalyzer(),
            user_turn_strategies=FilterIncompleteUserTurnStrategies(),
        ),
    )

    bridge = BusBridge(role_id=role_id, bus=bus, domain=pack.domain)
    tts = build_tts(role_id, role.voice_id)

    # BusBridge must sit directly after STT: the user_aggregator consumes
    # TranscriptionFrames into its LLM context and does not forward them, so a
    # bridge placed after it would never see the finalized transcript.
    # Pipeline order: input -> stt -> bridge -> aggregator (VAD/turn) -> tts -> output.
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
        cancel_on_idle_timeout=False,        # stay in the room until a human joins
        cancel_runner_on_idle_timeout=False,
        params=PipelineParams(
            enable_metrics=True,
            enable_usage_metrics=True,
            audio_in_sample_rate=16000,
            audio_out_sample_rate=24000,
        ),
    )

    @transport.event_handler("on_joined")
    async def on_joined(transport, data):
        logger.info(f"✅ Bot joined Daily room (role={role_id}). Waiting for a human…")

    @transport.event_handler("on_first_participant_joined")
    async def on_first_participant_joined(transport, participant):
        logger.info(f"👤 Human joined room (role={role_id}): {participant.get('id', '?')}")

    @transport.event_handler("on_participant_left")
    async def on_participant_left(transport, participant, reason):
        # Stay in the room across human reconnects (demo joins/leaves/rejoins).
        # Don't cancel the worker — it keeps listening for the next participant.
        logger.info(f"👋 Participant left (role={role_id}): {reason} — staying in room")

    @transport.event_handler("on_error")
    async def on_error(transport, error):
        logger.error(f"Daily transport error (role={role_id}): {error}")

    runner = WorkerRunner(handle_sigint=True)
    await runner.add_workers(worker)
    await runner.run()


def main() -> None:
    p = argparse.ArgumentParser(
        prog="worker_daily.py",
        description="One Daily-room participant voice worker on the shared Redis bus.",
    )
    p.add_argument("--role", required=True, help="Role id, e.g. role_grill")
    p.add_argument(
        "--room",
        default="",
        help="Existing Daily room URL. If absent, a room is CREATED and its URL printed.",
    )
    p.add_argument("--domain", required=True, help="domain pack name under domains/")
    args = p.parse_args()

    asyncio.run(run_worker(args))


if __name__ == "__main__":
    main()
