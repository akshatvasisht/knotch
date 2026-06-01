#
# Copyright (c) 2024–2026, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Pipecat Cloud entrypoint for the Coordinator PARTICIPANT voice worker.

This is the *cloud* counterpart of ``worker_daily.py``. It is the file the
Pipecat Cloud base image (``dailyco/pipecat-base``) discovers and invokes once
per session via::

    await bot(runner_args)          # runner_args: DailyRunnerArguments

Architectural difference from ``worker_daily.py``:
  * ``worker_daily.py`` is the LOCAL/off-cloud worker. It creates its OWN Daily
    room via the Daily REST API, prints the URL, and drives the pipeline with
    ``WorkerRunner``. That is the path ``run_stack.sh`` uses and the real demo.
  * THIS file does NOT create a room. Pipecat Cloud provisions the Daily room +
    token itself and hands them to us as ``runner_args.room_url`` /
    ``runner_args.token``. The base image owns the runner; we only build and
    return the pipeline task. One started agent == ONE session == ONE bot
    instance bound to that one transport — Pipecat Cloud is session-oriented.

What stays identical: the per-participant pipeline and the bus splice. The
deployed worker connects OUT to the SAME Upstash Redis bus (``REDIS_URL``,
``KNOTCH_BUS=redis``) and so joins the SAME distributed fabric as the
off-cloud coordinator (``engine.proc_coordinator``) and any local workers. That is the
whole point: a cloud-deployed participant + an off-cloud coordinator, meeting on
the bus.

Required cloud env (uploaded as a Pipecat Cloud secret set, NOT baked in):
  REDIS_URL              Upstash rediss:// URL (the shared bus)
  KNOTCH_BUS=redis     select the Redis bus backend
  KNOTCH_DASHBOARD=off the cloud worker must NOT try to host a dashboard
  NVIDIA_ASR_URL         ASR WebSocket endpoint (STT)
  KNOTCH_LLM_URL       used by the off-cloud coordinator process, harmless here
  KNOTCH_LLM_MODEL     same
  GRADIUM_API_KEY        TTS (omit -> StubTTS, no audio)
  GRADIUM_VOICE_ID       optional default voice
  KNOTCH_VOICE_TTS     gradium | stub
  KNOTCH_ROLE          which role this agent plays, e.g. role_grill
  KNOTCH_DOMAIN        domain pack, e.g. kitchen
  DAILY_API_KEY          NOT needed in cloud (Pipecat Cloud owns the room), but
                         harmless if present.

Role/domain are read from env (KNOTCH_ROLE / KNOTCH_DOMAIN) because Pipecat
Cloud invokes ``bot(runner_args)`` with no CLI args of our own. Deploy one agent
per role (e.g. coordinator-worker-grill, coordinator-worker-fry) each with its own
KNOTCH_ROLE secret, or pass the role in the session ``body`` (see below).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from loguru import logger
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.runner.types import DailyRunnerArguments, RunnerArguments
from pipecat.transports.daily.transport import DailyParams, DailyTransport
from pipecat.turns.user_turn_strategies import FilterIncompleteUserTurnStrategies

# --------------------------------------------------------------------------- #
# Load env + add the repo root to sys.path. In the container the repo subtree  #
# (engine/, adapters/, domains/, prompts/, nvidia_stt.py) is copied so the     #
# parents[1] resolution that worker_daily relies on still works.               #
# --------------------------------------------------------------------------- #
_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parents[1]

load_dotenv(_REPO_ROOT / ".env", override=False)
load_dotenv(_HERE / ".env", override=True)

if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Reuse the worker's building blocks verbatim — no logic duplicated.
from nvidia_stt import NVidiaWebSocketSTTService  # noqa: E402
from worker_daily import BusBridge, build_tts  # noqa: E402

from adapters import factory  # noqa: E402
from engine.domain_loader import load_pack  # noqa: E402


def _build_pipeline(transport: DailyTransport, *, role, pack):
    """Construct the participant pipeline — identical ordering to worker_daily."""
    bus = factory.make_bus()

    stt = NVidiaWebSocketSTTService(
        url=os.environ["NVIDIA_ASR_URL"],
        strip_interim_prefix=True,
    )

    context = LLMContext()
    user_aggregator, _assistant_aggregator = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(
            vad_analyzer=SileroVADAnalyzer(),
            user_turn_strategies=FilterIncompleteUserTurnStrategies(),
        ),
    )

    bridge = BusBridge(role_id=role.role_id, bus=bus, domain=pack.domain)
    tts = build_tts(role.role_id, role.voice_id)

    return Pipeline(
        [
            transport.input(),
            stt,
            bridge,
            user_aggregator,
            tts,
            transport.output(),
        ]
    )


async def bot(runner_args: RunnerArguments) -> None:
    """Pipecat Cloud entrypoint. Invoked once per session by the base image.

    Pipecat Cloud provisions the Daily room and token and passes them via
    ``DailyRunnerArguments``. The optional session ``body`` may carry
    {"role": "role_grill", "domain": "kitchen"} to override the env defaults.
    """
    if not isinstance(runner_args, DailyRunnerArguments):
        raise RuntimeError(
            "coordinator-worker only supports the Daily transport; got "
            f"{type(runner_args).__name__}. Start the session with the Daily "
            "transport on Pipecat Cloud."
        )

    # Role + domain come from the session body or env — no domain literal default,
    # so this entrypoint stays domain-agnostic (fail loudly if unset, don't
    # silently load one specific domain).
    body = getattr(runner_args, "body", None) or {}
    role_id = body.get("role") or os.environ.get("KNOTCH_ROLE")
    domain = body.get("domain") or os.environ.get("KNOTCH_DOMAIN")
    if not role_id or not domain:
        raise SystemExit(
            "bot.py: role/domain not set. Provide them in the session body "
            "({'role': ..., 'domain': ...}) or via KNOTCH_ROLE / KNOTCH_DOMAIN."
        )

    pack = load_pack(
        domain,
        domains_dir=str(_REPO_ROOT / "domains"),
        prompts_dir=str(_REPO_ROOT / "prompts"),
    )
    role = pack.role(role_id)
    if role is None:
        raise SystemExit(
            f"Role '{role_id}' not found in domain '{domain}'. "
            f"Known roles: {pack.role_ids}"
        )

    backend = factory._backend("KNOTCH_BUS")
    logger.info(
        f"Cloud worker up · role={role.role_id} domain={pack.domain} "
        f"bus={backend} voice={role.voice_id or '(default)'}"
    )
    if backend != "redis":
        logger.warning(
            "KNOTCH_BUS != redis — the cloud worker will NOT join the shared "
            "Upstash bus. Set KNOTCH_BUS=redis in the secret set."
        )

    transport = DailyTransport(
        runner_args.room_url,
        runner_args.token,
        f"Coordinator · {role.display_name}",
        params=DailyParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            transcription_enabled=False,  # STT is the explicit NVidia ws service
        ),
    )

    pipeline = _build_pipeline(transport, role=role, pack=pack)

    task = PipelineTask(
        pipeline,
        params=PipelineParams(
            enable_metrics=True,
            enable_usage_metrics=True,
            audio_in_sample_rate=16000,
            audio_out_sample_rate=24000,
        ),
    )

    @transport.event_handler("on_first_participant_joined")
    async def _on_join(_t, participant):
        logger.info(f"👤 Human joined (role={role.role_id}): {participant.get('id', '?')}")

    @transport.event_handler("on_participant_left")
    async def _on_leave(_t, _participant, reason):
        logger.info(f"👋 Participant left (role={role.role_id}): {reason}")
        await task.cancel()

    runner = PipelineRunner(handle_sigint=runner_args.handle_sigint)
    await runner.run(task)


if __name__ == "__main__":
    # Local smoke-test path: `LOCAL_RUN=1 python bot.py` uses the pipecat dev
    # runner, which will create a Daily room for you (needs DAILY_API_KEY).
    # The REAL local path remains worker_daily.py via run_stack.sh.
    from pipecat.runner.run import main

    main()
