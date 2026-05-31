"""Backend factory: env-selected services (fake by default, real when wired).

Engine code never imports a concrete service — it asks the factory, which reads
env vars (interfaces.ENV_*) and returns fake or real implementations behind the
same Protocols. Swapping to a real backend is a single env-var change; no engine
change. Real adapters raise a clear error until they're wired.
"""
from __future__ import annotations

import os
from typing import Callable, Optional

from engine.interfaces import (
    Bus,
    ConvenerLLM,
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
from engine.turn_gate import make_turn_gate

from adapters.fake_llm import FakeConvenerLLM
from adapters.fake_stt import FakeSTT
from adapters.fake_transport import FakeTransport
from adapters.fake_tts import FakeTTS


def _backend(env_key: str) -> str:
    return os.environ.get(env_key, DEFAULT_BACKEND[env_key]).strip().lower()


def _phase2(name: str, backend: str, env_key: str) -> NotImplementedError:
    return NotImplementedError(
        f"{name} backend '{backend}' is not wired yet. "
        f"Set {env_key}=fake to use the fake adapter."
    )


def make_bus() -> Bus:
    backend = _backend(ENV_BUS)
    if backend == "memory":
        return InMemoryBus()
    if backend == "redis":
        from engine.redis_bus import RedisBus
        return RedisBus(os.environ.get("REDIS_URL", ""))
    raise _phase2("Bus", backend, ENV_BUS)


def make_llm() -> ConvenerLLM:
    backend = _backend(ENV_LLM)
    if backend == "nemotron":
        from adapters.nemotron_llm import NemotronConvenerLLM
        return NemotronConvenerLLM()
    if backend == "fake":
        return FakeConvenerLLM()
    raise _phase2("LLM", backend, ENV_LLM)


def make_stt() -> STTService:
    backend = _backend(ENV_STT)
    if backend == "fake":
        return FakeSTT()
    raise _phase2("STT", backend, ENV_STT)


def make_tts(label_for: Optional[Callable[[str], str]] = None) -> TTSService:
    backend = _backend(ENV_TTS)
    if backend == "fake":
        return FakeTTS(label_for=label_for)
    raise _phase2("TTS", backend, ENV_TTS)


def make_transport(role: RoleSpec) -> Transport:
    backend = _backend(ENV_TRANSPORT)
    if backend == "fake":
        return FakeTransport(role.role_id)
    raise _phase2("Transport", backend, ENV_TRANSPORT)


def make_gate():
    return make_turn_gate(_backend(ENV_TURN))
