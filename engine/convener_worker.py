"""ConvenerWorker — the brain. One instance per system.

Subscribes to every turn-complete utterance on bus:utterances. For each:
  1. Update the shared cross-channel state (a generic key/value surface spanning
     all workers).
  2. Make one routing LLM call (the only LLM hop).
  3. Validate the raw decision into a contract-valid RoutingDecision (fail-safe
     to no-route on any error — never push a guessed route into someone's ear).
  4. Publish one enriched, addressed decision to bus:routed. Output workers
     self-select. Also publish a state_update for the dashboard.

Domain-agnostic: roles are opaque ids; the only domain knowledge is the
already-assembled `system_prompt` (scaffold + pack fragment), which the worker
treats as an opaque string handed to the LLM.
"""
from __future__ import annotations

from collections import deque

from engine.interfaces import (
    Bus,
    CHAN_UTTERANCES,
    ConvenerLLM,
    Envelope,
    RoleSpec,
    RoutingDecision,
    RoutingRequest,
    StateUpdate,
    TYPE_ROUTING_DECISION,
    TYPE_STATE_UPDATE,
    Utterance,
)
from engine.routing import validate_decision


class ConvenerWorker:
    def __init__(
        self,
        *,
        bus: Bus,
        llm: ConvenerLLM,
        system_prompt: str,
        participants: list[RoleSpec],
        zone: str = "z0",
        domain: str = "",
        context_window: int = 8,
    ) -> None:
        self.bus = bus
        self.llm = llm
        self.system_prompt = system_prompt
        # `participants` is the FULL active roster — recipients may be addressed
        # across zones. `owns` is the partition THIS convener is responsible for
        # routing (the zone-shard seam: scales to ~100 channels via N conveners,
        # each owning a zone, sharing state over the same bus).
        self.participants = participants
        self.participant_ids = [r.role_id for r in participants]
        self.zone = zone
        self.owns = {r.role_id for r in participants if r.zone_id == zone}
        self.domain = domain
        self.recent: deque[Utterance] = deque(maxlen=context_window)
        self.state: dict = {}

    async def run(self) -> None:
        async for env in self.bus.subscribe(CHAN_UTTERANCES):
            utt = Utterance.from_dict(env.payload)
            # Zone sharding: only route utterances from roles this convener owns.
            if utt.participant not in self.owns:
                continue
            self._update_state(utt)

            request = RoutingRequest(
                system_prompt=self.system_prompt,
                utterance=utt,
                state=dict(self.state),
                recent=list(self.recent),
                participants=self.participants,
            )
            self.recent.append(utt)

            # ONE LLM call. Fail-safe to no-route on ANY error.
            try:
                raw = await self.llm.decide(request)
            except Exception as exc:  # noqa: BLE001 — deliberate catch-all fail-safe
                raw = None
                decision = RoutingDecision.held(
                    utt.participant, rationale=f"convener error: {exc}"
                )
            else:
                decision = validate_decision(
                    raw, source=utt.participant, participants=self.participant_ids
                )

            await self.bus.publish(
                Envelope(
                    type=TYPE_ROUTING_DECISION,
                    payload=decision.to_dict(),
                    domain=self.domain,
                )
            )
            await self.bus.publish(
                Envelope(
                    type=TYPE_STATE_UPDATE,
                    payload=StateUpdate(state=dict(self.state)).to_dict(),
                    domain=self.domain,
                )
            )

    def _update_state(self, utt: Utterance) -> None:
        """Update the shared state store, namespaced by zone so multiple
        convener shards can write to the same bus without key collisions.
        No domain logic — opaque keys."""
        z = self.zone
        self.state[f"{z}:last_speaker"] = utt.participant
        self.state[f"{z}:turns"] = self.state.get(f"{z}:turns", 0) + 1
        # Per-role latest coarse class (role_id is opaque; domain-neutral).
        self.state[f"{utt.participant}:last"] = utt.triage_class
