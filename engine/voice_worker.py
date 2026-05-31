"""VoiceWorker — one per participant, owns that participant's transport.

Two cleanly-separated paths inside one worker:

  INPUT path:  transport input -> STT (interim, not published) -> turn gate
               (publish only on turn-stop) -> triage (drop backchannels, tag
               class) -> publish one turn-complete Utterance to bus:utterances.

  OUTPUT path: subscribe bus:routed -> self-select by `role_id in recipients`
               (cheap deterministic match, no LLM) -> render decision.message
               via the single dispatcher TTS voice. recipients==[] => silent.

Domain-agnostic: the worker only ever sees an opaque role_id + a voice_id.
"""
from __future__ import annotations

import asyncio

from engine.interfaces import (
    Bus,
    CHAN_ROUTED,
    CHAN_TURN,
    Envelope,
    InputChunk,
    RoleSpec,
    RoutingDecision,
    STTService,
    TTSService,
    Transport,
    TurnEvent,
    TYPE_TURN_EVENT,
    TYPE_UTTERANCE,
    Utterance,
)
from engine.routing import self_select
from engine.triage import triage
from engine.turn_gate import SimpleTurnGate


class VoiceWorker:
    def __init__(
        self,
        *,
        role: RoleSpec,
        transport: Transport,
        stt: STTService,
        tts: TTSService,
        bus: Bus,
        dispatcher_voice_id: str,
        gate: SimpleTurnGate,
        domain: str = "",
    ) -> None:
        self.role = role
        self.transport = transport
        self.stt = stt
        self.tts = tts
        self.bus = bus
        self.dispatcher_voice_id = dispatcher_voice_id
        self.gate = gate
        self.domain = domain

    @property
    def role_id(self) -> str:
        return self.role.role_id

    async def run(self) -> None:
        await asyncio.gather(self._input_path(), self._output_path())

    # -- input: mic -> STT -> turn gate -> triage -> publish utterance -------- #
    async def _input_path(self) -> None:
        async for chunk in self.transport.input_stream():  # type: InputChunk
            text = await self.stt.transcribe(chunk)
            if text is None:
                continue
            turn = self.gate.feed(text, chunk.final)
            if turn is None:
                continue  # interim or empty — nothing to route yet
            await self._publish_turn_event("turn_stop")

            routable, triage_class = triage(turn)
            if not routable:
                await self._publish_turn_event("dropped_backchannel")
                continue

            utt = Utterance(
                participant=self.role_id,
                text=turn,
                final=True,
                triage_class=triage_class,
                routable=True,
            )
            await self.bus.publish(
                Envelope(type=TYPE_UTTERANCE, payload=utt.to_dict(), domain=self.domain)
            )

    # -- output: subscribe bus:routed -> self-select -> dispatcher TTS -------- #
    async def _output_path(self) -> None:
        async for env in self.bus.subscribe(CHAN_ROUTED):
            decision = RoutingDecision.from_dict(env.payload)
            if self_select(decision, self.role_id) and decision.message:
                await self.tts.speak(
                    decision.message,
                    role_id=self.role_id,
                    voice_id=self.dispatcher_voice_id,
                )

    async def _publish_turn_event(self, state: str) -> None:
        ev = TurnEvent(participant=self.role_id, state=state)
        await self.bus.publish(
            Envelope(type=TYPE_TURN_EVENT, payload=ev.to_dict(), domain=self.domain)
        )
