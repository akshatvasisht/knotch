#
# Copyright (c) 2024–2026, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Coordinator Agent — multi-participant voice coordination runtime.

One-to-many-to-one. Each connected browser is a single participant with its own
pipecat pipeline (STT -> BusBridge -> TTS). All BusBridges share ONE module-level
``InMemoryBus`` and ONE ``Coordinator`` (the central brain), so the coordinator
sees every participant's turns and routes a single derived dispatcher message
back into the right channels.

Per-participant pipeline (mirrors bot-nemotron.py's SmallWebRTC setup, but with
NO local LLM — the only LLM hop is the shared coordinator)::

    transport.input()
      -> NVidiaWebSocketSTTService(url=NVIDIA_ASR_URL, strip_interim_prefix=True)
      -> BusBridge(role_id, bus)        # the splice point (directly after STT)
      -> user_aggregator (SileroVAD + FilterIncompleteUserTurnStrategies)
      -> tts                            # Gradium OR a logging stub (env swap)
      -> transport.output()

BusBridge (a pipecat FrameProcessor):
  * On a finalized TranscriptionFrame: triage -> if routable, publish an
    Utterance Envelope to bus:utterances, then forward the frame (the downstream
    aggregator consumes it; the TTS only speaks routed TTSSpeakFrames, so there
    is no echo of the speaker's own words).
  * On StartFrame: spawn a background task subscribed to bus:routed. For each
    RoutingDecision, if self_select(decision, role_id) and decision.message, push
    a TTSSpeakFrame DOWNSTREAM so the dispatcher voice speaks into this channel.
  * All other frames pass through.

TTS swap (so the loop runs WITHOUT the Gradium key) via KNOTCH_VOICE_TTS:
  * "gradium" — GradiumTTSService (needs GRADIUM_API_KEY).
  * "stub"    — StubTTS: logs ``🔊 dispatcher → <role>: <text>`` on a TTSSpeakFrame,
                no audio. Default is gradium iff GRADIUM_API_KEY is set, else stub.

Run::

    KNOTCH_VOICE_TTS=stub uv run bot_coordinator.py
"""

import asyncio
import os
import sys
from pathlib import Path

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
from pipecat.runner.types import RunnerArguments, SmallWebRTCRunnerArguments
from pipecat.transports.base_transport import BaseTransport, TransportParams
from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport
from pipecat.turns.user_turn_strategies import FilterIncompleteUserTurnStrategies
from pipecat.workers.runner import WorkerRunner

from nvidia_stt import NVidiaWebSocketSTTService

# --------------------------------------------------------------------------- #
# Add the repo root to sys.path so the domain-agnostic engine can be imported. #
# The engine lives two levels up and must remain pipecat-free.                 #
# --------------------------------------------------------------------------- #
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from adapters.openai_llm import OpenAICoordinatorLLM  # noqa: E402
from engine.bus import InMemoryBus  # noqa: E402
from engine.coordinator import Coordinator  # noqa: E402
from engine.interfaces import (  # noqa: E402
    CHAN_ROUTED,
    TYPE_UTTERANCE,
    Envelope,
    RoutingDecision,
    Utterance,
)
from engine.domain_loader import (  # noqa: E402
    assemble_system_prompt,
    load_pack,
    load_scaffold,
)
from engine.routing import self_select  # noqa: E402
from engine.triage import triage  # noqa: E402

load_dotenv(override=True)

DOMAIN = os.getenv("KNOTCH_DOMAIN", "kitchen")

# Dashboard feature flags (read once at import time so they're visible below)
_DASHBOARD_ENABLED = os.getenv("KNOTCH_DASHBOARD", "on").strip().lower() != "off"
_DASHBOARD_PORT = int(os.getenv("KNOTCH_DASHBOARD_PORT", "7861"))


# --------------------------------------------------------------------------- #
# Central singletons: one bus + one coordinator worker, started eagerly on boot.  #
# Every per-connection BusBridge shares these so the coordinator sees every       #
# participant. The dashboard also starts here so it is reachable immediately.  #
# --------------------------------------------------------------------------- #
class _Central:
    """Module-level shared state: the bus, the loaded pack, and the coordinator."""

    bus: InMemoryBus | None = None
    pack = None
    system_prompt: str = ""
    worker_task: asyncio.Task | None = None
    dashboard_task: asyncio.Task | None = None
    _role_cursor: int = 0
    _lock = asyncio.Lock()


_central = _Central()


async def _run_dashboard_safe(bus: InMemoryBus, pack, port: int) -> None:
    """Wrap attach_dashboard so a crash never propagates into the voice pipeline."""
    try:
        from engine.dashboard.app import attach_dashboard  # lazy import
        logger.info(f"📊 Starting ops dashboard on port {port}")
        await attach_dashboard(bus, pack, port=port)
    except Exception as exc:  # noqa: BLE001
        logger.error(f"Dashboard error (voice pipeline unaffected): {exc}")


async def ensure_central() -> _Central:
    """Build the bus + coordinator exactly once (idempotent, lock-guarded).

    Called eagerly at server startup so the voice signalling endpoint and the
    ops dashboard are both reachable before any browser connects.
    """
    async with _central._lock:
        if _central.bus is not None:
            return _central

        # Use absolute paths so pack/prompt loading works regardless of process CWD.
        pack = load_pack(
            DOMAIN,
            domains_dir=str(_REPO_ROOT / "domains"),
            prompts_dir=str(_REPO_ROOT / "prompts"),
        )
        scaffold = load_scaffold(prompts_dir=str(_REPO_ROOT / "prompts"))
        system_prompt = assemble_system_prompt(scaffold, pack)

        bus = InMemoryBus()
        llm = OpenAICoordinatorLLM()
        worker = Coordinator(
            bus=bus,
            llm=llm,
            system_prompt=system_prompt,
            participants=pack.roles,
            domain=pack.domain,
        )

        _central.bus = bus
        _central.pack = pack
        _central.system_prompt = system_prompt
        _central.worker_task = asyncio.create_task(worker.run())

        # Start the dashboard eagerly (once) so it is reachable right after boot.
        if _DASHBOARD_ENABLED:
            _central.dashboard_task = asyncio.create_task(
                _run_dashboard_safe(bus, pack, _DASHBOARD_PORT)
            )

        logger.info(
            f"🧠 Coordinator initialized — domain='{pack.domain}', "
            f"roles={pack.role_ids}, LLM={llm.model} @ {llm.base_url}"
        )
        return _central


def assign_role() -> str:
    """Round-robin the next role id from the loaded pack to a new connection."""
    roles = _central.pack.role_ids
    role = roles[_central._role_cursor % len(roles)]
    _central._role_cursor += 1
    return role


# --------------------------------------------------------------------------- #
# TTS: Gradium (real audio) or a logging stub when no key is present.         #
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

        return GradiumTTSService(
            api_key=os.environ["GRADIUM_API_KEY"],
            settings=GradiumTTSService.Settings(
                voice=voice_id or os.getenv("GRADIUM_VOICE_ID", "Eu9iL_CYe8N-Gkx_"),
            ),
        )

    logger.warning(
        f"KNOTCH_VOICE_TTS='{choice or 'stub'}' — using StubTTS (no audio). "
        f"dispatcher messages for {role_id} will be logged, not spoken."
    )
    return StubTTS(role_id=role_id)


# --------------------------------------------------------------------------- #
# BusBridge — splice point between a participant's pipeline and the shared bus #
# --------------------------------------------------------------------------- #
class BusBridge(FrameProcessor):
    def __init__(self, *, role_id: str, bus: InMemoryBus, domain: str = "", **kwargs):
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
# Per-connection pipeline                                                      #
# --------------------------------------------------------------------------- #
async def run_participant(
    transport: BaseTransport,
    *,
    audio_in_sample_rate: int = 16000,
    audio_out_sample_rate: int = 24000,
):
    central = await ensure_central()
    bus = central.bus
    pack = central.pack

    role_id = assign_role()
    role = pack.role(role_id)
    voice_id = role.voice_id if role else ""
    logger.info(f"👤 New participant assigned role '{role_id}' ({role.display_name if role else role_id})")

    stt = NVidiaWebSocketSTTService(
        url=os.environ["NVIDIA_ASR_URL"],
        strip_interim_prefix=True,
    )

    # The user aggregator provides SileroVAD and the turn strategy that treats
    # a finalized STT transcript as end-of-turn. There is no per-participant LLM
    # in this pipeline — the coordinator is the only reasoning step. The aggregator
    # is needed to host VAD + turn strategy; its LLM context is unused.
    context = LLMContext()
    user_aggregator, _assistant_aggregator = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(
            vad_analyzer=SileroVADAnalyzer(),
            user_turn_strategies=FilterIncompleteUserTurnStrategies(),
        ),
    )

    bridge = BusBridge(role_id=role_id, bus=bus, domain=pack.domain)
    tts = build_tts(role_id, voice_id)

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
        params=PipelineParams(
            enable_metrics=True,
            enable_usage_metrics=True,
            audio_in_sample_rate=audio_in_sample_rate,
            audio_out_sample_rate=audio_out_sample_rate,
        ),
    )

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):
        logger.info(f"Client connected (role={role_id})")

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        logger.info(f"Client disconnected (role={role_id})")
        await worker.cancel()

    runner = WorkerRunner(handle_sigint=False)
    await runner.add_workers(worker)
    await runner.run()


async def bot(runner_args: RunnerArguments):
    """Per-connection entry point (SmallWebRTC only — browser tabs)."""
    if os.environ.get("ENV") != "local":
        from pipecat.audio.filters.krisp_viva_filter import KrispVivaFilter

        krisp_filter = KrispVivaFilter()
    else:
        krisp_filter = None

    match runner_args:
        case SmallWebRTCRunnerArguments():
            webrtc_connection: SmallWebRTCConnection = runner_args.webrtc_connection
            transport = SmallWebRTCTransport(
                webrtc_connection=webrtc_connection,
                params=TransportParams(
                    audio_in_enabled=True,
                    audio_in_filter=krisp_filter,
                    audio_out_enabled=True,
                ),
            )
        case _:
            logger.error(
                f"Coordinator supports SmallWebRTC (browser) only; got {type(runner_args)}"
            )
            return

    await run_participant(transport)


if __name__ == "__main__":
    from contextlib import asynccontextmanager

    from pipecat.runner.run import _add_lifespan_to_app, app, main

    @asynccontextmanager
    async def _coordinator_lifespan(_app):
        """Initialize the shared bus, coordinator worker, and ops dashboard on startup."""
        await ensure_central()
        logger.info("Central singletons ready (bus + coordinator + dashboard scheduled)")
        yield

    _add_lifespan_to_app(app, _coordinator_lifespan)

    main()
