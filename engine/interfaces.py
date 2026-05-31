"""Frozen contracts for the Convener engine — the single source of truth.

Everything domain-agnostic. No domain literal (a specific domain's role names,
station names, or signal vocabulary) may ever appear in this file or anywhere
under engine/ — the generality gate (tests/test_no_domain_strings.py) enforces
it. Roles are opaque ids loaded from a domain pack at startup.

This module defines:
  - Channel names + envelope types (the bus wire format).
  - Payload dataclasses: Utterance, RoutingDecision, StateUpdate, EvalScore,
    TurnEvent.
  - Domain-pack value objects: RoleSpec, DomainPack.
  - Service Protocols every adapter (fake or real) implements: Bus, Transport,
    STTService, TTSService, ConvenerLLM. Fakes and real services are
    interchangeable — selected by env var via adapters/factory.py.

All payloads are plain dataclasses with `to_dict`/`from_dict` so the bus can
carry JSON and the dashboard can read the envelopes verbatim.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any, AsyncIterator, Optional, Protocol, runtime_checkable


# --------------------------------------------------------------------------- #
# Bus channels: two logical topics carry the live path                        #
# --------------------------------------------------------------------------- #
CHAN_UTTERANCES = "bus:utterances"   # turn-complete, triaged utterances (input -> convener)
CHAN_ROUTED = "bus:routed"           # the convener's ONE enriched addressed decision
CHAN_STATE = "bus:state"             # shared-state updates (convener -> dashboard)
CHAN_EVAL = "bus:eval"               # eval scores (eval_runner -> dashboard)
CHAN_SYSTEM = "bus:system"           # lifecycle / system messages
CHAN_TURN = "bus:turn"               # optional turn_event ticks (dashboard-only)
CHAN_CURVES = "bus:curves"           # before/after improvement curves (proc_improve -> dashboard)

ALL_CHANNELS = (
    CHAN_UTTERANCES,
    CHAN_ROUTED,
    CHAN_STATE,
    CHAN_EVAL,
    CHAN_SYSTEM,
    CHAN_TURN,
    CHAN_CURVES,
)

# Envelope `type` values.
TYPE_UTTERANCE = "utterance"
TYPE_ROUTING_DECISION = "routing_decision"
TYPE_STATE_UPDATE = "state_update"
TYPE_EVAL_SCORE = "eval_score"
TYPE_TURN_EVENT = "turn_event"
TYPE_SYSTEM = "system"
TYPE_CURVES = "curves"

# Which channel each envelope type travels on.
TYPE_TO_CHANNEL = {
    TYPE_UTTERANCE: CHAN_UTTERANCES,
    TYPE_ROUTING_DECISION: CHAN_ROUTED,
    TYPE_STATE_UPDATE: CHAN_STATE,
    TYPE_EVAL_SCORE: CHAN_EVAL,
    TYPE_TURN_EVENT: CHAN_TURN,
    TYPE_SYSTEM: CHAN_SYSTEM,
    TYPE_CURVES: CHAN_CURVES,
}


# --------------------------------------------------------------------------- #
# Env var names (adapters/factory.py reads these to pick fake vs real)        #
# --------------------------------------------------------------------------- #
ENV_STT = "CONVENER_STT"              # fake | nvidia
ENV_TTS = "CONVENER_TTS"              # fake | nvidia
ENV_LLM = "CONVENER_LLM"              # fake | nemotron
ENV_TRANSPORT = "CONVENER_TRANSPORT"  # fake | daily
ENV_BUS = "CONVENER_BUS"              # memory | redis
ENV_TURN = "CONVENER_TURN"            # simple | smartturn

DEFAULT_BACKEND = {
    ENV_STT: "fake",
    ENV_TTS: "fake",
    ENV_LLM: "nemotron",   # the REAL convener brain by default (public URL, no key)
    ENV_TRANSPORT: "fake",
    ENV_BUS: "memory",
    ENV_TURN: "simple",
}


# --------------------------------------------------------------------------- #
# Envelope + payloads                                                         #
# --------------------------------------------------------------------------- #
@dataclass
class Envelope:
    """Every bus message. `payload` is a type-specific dict."""

    type: str
    payload: dict
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    ts: float = field(default_factory=time.time)
    domain: str = ""  # label only; engine logic NEVER branches on this value

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Envelope":
        return cls(
            type=d["type"],
            payload=d.get("payload", {}),
            id=d.get("id", str(uuid.uuid4())),
            ts=d.get("ts", time.time()),
            domain=d.get("domain", ""),
        )

    @property
    def channel(self) -> str:
        return TYPE_TO_CHANNEL.get(self.type, CHAN_SYSTEM)


@dataclass
class Utterance:
    """A turn-complete, triaged utterance."""

    participant: str          # role_id of the speaker
    text: str
    final: bool = True        # only published on turn-stop
    triage_class: str = ""    # coarse tag from triage; "" before triage
    routable: bool = True     # backchannels => False (never published)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Utterance":
        return cls(
            participant=d["participant"],
            text=d.get("text", ""),
            final=d.get("final", True),
            triage_class=d.get("triage_class", ""),
            routable=d.get("routable", True),
        )


# RoutingDecision urgency + signal vocab are open — signal_type values come from
# the domain fragment and are treated as opaque labels by the engine.
URGENCY_VALUES = ("low", "med", "high")


@dataclass
class RoutingDecision:
    """The convener's only output. Validate before publishing."""

    source: str                         # who spoke (role_id)
    recipients: list[str]               # [] is valid + common = "held, not relevant"
    message: str                        # DERIVED dispatcher message; "" iff recipients==[]
    signal_type: str = ""               # opaque label from the domain fragment
    urgency: str = "low"                # low | med | high
    rationale: str = ""                 # short phrase; shown on dashboard + used by eval
    decision_id: str = field(default_factory=lambda: str(uuid.uuid4()))

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "RoutingDecision":
        return cls(
            source=d.get("source", ""),
            recipients=list(d.get("recipients", [])),
            message=d.get("message", ""),
            signal_type=d.get("signal_type", ""),
            urgency=d.get("urgency", "low"),
            rationale=d.get("rationale", ""),
            decision_id=d.get("decision_id", str(uuid.uuid4())),
        )

    @staticmethod
    def held(source: str, rationale: str = "not relevant") -> "RoutingDecision":
        """Fail-safe / no-route decision: nobody hears anything."""
        return RoutingDecision(
            source=source, recipients=[], message="", rationale=rationale
        )


@dataclass
class StateUpdate:
    """A snapshot of the convener's shared state, broadcast to all subscribers (e.g. dashboard)."""

    state: dict

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "StateUpdate":
        return cls(state=dict(d.get("state", {})))


@dataclass
class EvalScore:
    """Per-decision score from the eval runner."""

    decision_id: str
    outcome: str                  # acted | ignored | missed
    time_to_action_ms: Optional[int] = None
    metric_scores: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "EvalScore":
        return cls(
            decision_id=d["decision_id"],
            outcome=d.get("outcome", ""),
            time_to_action_ms=d.get("time_to_action_ms"),
            metric_scores=dict(d.get("metric_scores", {})),
        )


@dataclass
class TurnEvent:
    """Optional dashboard-only signal of input-path state."""

    participant: str
    state: str   # listening | turn_stop | dropped_backchannel

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "TurnEvent":
        return cls(participant=d["participant"], state=d.get("state", ""))


# --------------------------------------------------------------------------- #
# Domain-pack value objects — loaded by packloader.py                         #
# --------------------------------------------------------------------------- #
@dataclass
class RoleSpec:
    role_id: str
    display_name: str
    voice_id: str = ""
    # Partition key for sharding: each convener instance owns the roles in one zone.
    # Default "z0" is correct for single-zone deployments.
    zone_id: str = "z0"


@dataclass
class DomainPack:
    """A fully-loaded domain pack. The ONLY place domain knowledge lives."""

    domain: str                       # label only
    display_name: str
    dispatcher_voice_id: str
    roles: list[RoleSpec]
    convener_fragment: str            # the .md fragment CONTENT (already read)
    rubric: dict = field(default_factory=dict)
    scenarios: list[dict] = field(default_factory=list)
    path: str = ""                    # source dir, for debugging

    @property
    def role_ids(self) -> list[str]:
        return [r.role_id for r in self.roles]

    def role(self, role_id: str) -> Optional[RoleSpec]:
        return next((r for r in self.roles if r.role_id == role_id), None)


# --------------------------------------------------------------------------- #
# Request object handed to the convener LLM. Keeps real + fake call           #
# signatures identical: real adapter serialises this into a prompt; fake      #
# adapter reads its structured fields.                                         #
# --------------------------------------------------------------------------- #
@dataclass
class RoutingRequest:
    system_prompt: str                # convener_scaffold + domain fragment, assembled
    utterance: Utterance              # the current turn to route
    state: dict                       # shared cross-channel state snapshot
    recent: list[Utterance]           # recent context window (oldest -> newest)
    participants: list[RoleSpec]      # active roles this session


# --------------------------------------------------------------------------- #
# Service Protocols — fakes and real adapters both satisfy these.             #
# --------------------------------------------------------------------------- #
@runtime_checkable
class Bus(Protocol):
    """Async pub/sub. In-memory by default; Redis satisfies the same interface."""

    async def publish(self, env: Envelope) -> None: ...

    def subscribe(self, channel: str) -> AsyncIterator[Envelope]:
        """Return an async iterator of envelopes on `channel`."""
        ...

    async def close(self) -> None: ...


@runtime_checkable
class Transport(Protocol):
    """Owns a participant's I/O channel. Fake = text injection; real = Daily."""

    role_id: str

    def input_stream(self) -> AsyncIterator["InputChunk"]:
        """Yield raw input chunks (fake: text lines; real: audio frames)."""
        ...

    async def output(self, text: str) -> None:
        """Deliver dispatcher audio/text into this participant's channel."""
        ...

    def close(self) -> None:
        """Stop the input stream and release any held resources."""
        ...


@dataclass
class InputChunk:
    """Raw input from a transport. For fakes, `text` carries the words and
    `final` marks an explicit end-of-turn; real audio adapters set `audio`."""

    text: str = ""
    final: bool = False
    audio: Optional[bytes] = None


@runtime_checkable
class STTService(Protocol):
    """Speech-to-text. Fake = identity passthrough of injected text."""

    async def transcribe(self, chunk: InputChunk) -> Optional[str]:
        """Return recognised text for this chunk, or None if nothing yet."""
        ...


@runtime_checkable
class TTSService(Protocol):
    """Text-to-speech in the single dispatcher voice. Fake = print."""

    async def speak(self, text: str, *, role_id: str, voice_id: str) -> None: ...


@runtime_checkable
class ConvenerLLM(Protocol):
    """The routing brain. One call per routable turn. Returns a raw decision
    dict (validated by routing.validate_decision). Fake = deterministic rules;
    real = any OpenAI-compatible LLM via `request.system_prompt`."""

    async def decide(self, request: RoutingRequest) -> dict: ...
