"""Backend factory for the in-process engine: env-selected services.

Engine code never imports a concrete service — it asks the factory, which reads
env vars (interfaces.ENV_*) and returns the selected implementation behind the
same Protocols. The bus and LLM have real in-process backends (`redis`,
`nemotron`); STT, TTS, and transport are fake-only here.

The fakes are a development and test harness, not a user-facing path. Real
audio I/O — live speech-to-text, text-to-speech, and transport — runs in the
Pipecat participant workers under `.starter/server/`, which build their own
pipelines and never go through this factory. Each service sits behind a
Protocol in `engine/interfaces.py`, so the provider is swappable; the workers
bundle one set of implementations.
"""
from __future__ import annotations

import os
from typing import Callable, Optional

from engine.interfaces import (
    Bus,
    CoordinatorLLM,
    DEFAULT_BACKEND,
    ENV_BUS,
    ENV_LLM,
    ENV_STT,
    ENV_TRANSPORT,
    ENV_TTS,
    ENV_TURN,
    RoleSpec,
    STTService,
    TTSService,
    Transport,
)
from engine.bus import InMemoryBus
from engine.turn_detector import make_turn_gate

from adapters.fake_llm import FakeCoordinatorLLM
from adapters.fake_stt import FakeSTT
from adapters.fake_transport import FakeTransport
from adapters.fake_tts import FakeTTS


def _backend(env_key: str) -> str:
    return os.environ.get(env_key, DEFAULT_BACKEND[env_key]).strip().lower()


def _unsupported(name: str, backend: str, env_key: str) -> NotImplementedError:
    return NotImplementedError(
        f"{name} backend '{backend}' is not available in the in-process engine "
        f"(check {env_key}). Real audio I/O runs in the Pipecat participant "
        f"workers under .starter/server/, not through this factory."
    )


def make_bus() -> Bus:
    backend = _backend(ENV_BUS)
    if backend == "memory":
        return InMemoryBus()
    if backend == "redis":
        from engine.redis_bus import RedisBus
        return RedisBus(os.environ.get("REDIS_URL", ""))
    raise _unsupported("Bus", backend, ENV_BUS)


def make_llm() -> CoordinatorLLM:
    backend = _backend(ENV_LLM)
    if backend == "nemotron":
        from adapters.openai_llm import OpenAICoordinatorLLM
        return OpenAICoordinatorLLM()
    if backend == "fake":
        return FakeCoordinatorLLM()
    raise _unsupported("LLM", backend, ENV_LLM)


def make_stt() -> STTService:
    backend = _backend(ENV_STT)
    if backend == "fake":
        return FakeSTT()
    raise _unsupported("STT", backend, ENV_STT)


def make_tts(label_for: Optional[Callable[[str], str]] = None) -> TTSService:
    backend = _backend(ENV_TTS)
    if backend == "fake":
        return FakeTTS(label_for=label_for)
    raise _unsupported("TTS", backend, ENV_TTS)


def make_transport(role: RoleSpec) -> Transport:
    backend = _backend(ENV_TRANSPORT)
    if backend == "fake":
        return FakeTransport(role.role_id)
    raise _unsupported("Transport", backend, ENV_TRANSPORT)


def make_gate():
    return make_turn_gate(_backend(ENV_TURN))
