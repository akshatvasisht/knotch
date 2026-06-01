"""Fake coordinator LLM: deterministic, rule-based routing — no API key required.

Implements the same `decide(request) -> dict` interface as the real LLM adapter. Reads the structured RoutingRequest (utterance, triage class, active
participants) and returns a raw RoutingDecision dict that the coordinator validates.
Deterministic so tests are reproducible.

Intentionally simple: proves the topology and produces a watchable terminal demo
(mix of routes and holds). The real model replaces this with LLM intelligence.
Uses generic English cues only — no domain literals.
"""
from __future__ import annotations

from engine.interfaces import CoordinatorLLM, RoutingRequest

# Generic dependency/alert cues — same spirit as triage, used to pick urgency.
_HIGH_CUES = (
    "down", "stop", "help", "emergency", "urgent", "now", "asap", "stat",
    "immediately", "behind", "waiting", "stuck", "broken", "late", "out of",
)
_MED_CUES = ("need", "where", "can ", "could ", "ready", "how long", "?")


class FakeCoordinatorLLM(CoordinatorLLM):
    async def decide(self, request: RoutingRequest) -> dict:
        utt = request.utterance
        source = utt.participant
        text = utt.text.strip()
        tl = text.lower()
        core = text.rstrip(" .!?")

        names = {p.role_id: (p.display_name or p.role_id) for p in request.participants}
        source_name = names.get(source, source)
        others = [p.role_id for p in request.participants if p.role_id != source]

        # 1) Direct address: another participant named in the utterance.
        mentioned = [
            rid
            for rid in others
            if names.get(rid, "").lower() and names[rid].lower() in tl
        ]

        triage_class = utt.triage_class or "status"
        high = any(cue in tl for cue in _HIGH_CUES)
        med = any(cue in tl for cue in _MED_CUES)

        # 2) Decide recipients + signal.
        if mentioned:
            recipients = mentioned
            signal_type = "request" if (med and not high) else "dependency"
        elif triage_class == "alert" or high:
            recipients = others                      # broadcast — others depend on this
            signal_type = "dependency"
        elif triage_class == "request" or med:
            recipients = others
            signal_type = "request"
        else:
            recipients = []                          # held — chatter, not relevant
            signal_type = "fyi"

        # 3) Urgency.
        if not recipients:
            urgency = "low"
        elif high:
            urgency = "high"
        elif signal_type in ("request",) or med:
            urgency = "med"
        else:
            urgency = "med"

        # 4) Derive a dispatcher message (not a transcript echo) + rationale.
        if not recipients:
            return {
                "source": source,
                "recipients": [],
                "message": "",
                "signal_type": signal_type,
                "urgency": "low",
                "rationale": f"{source_name} chatter — held, not relevant",
            }

        if signal_type == "dependency":
            message = f"from {source_name}: {core} — adjust accordingly"
        elif signal_type == "request":
            message = f"{source_name} needs: {core}"
        else:
            message = f"{source_name}: {core}"

        to_names = ", ".join(names.get(r, r) for r in recipients)
        return {
            "source": source,
            "recipients": recipients,
            "message": message,
            "signal_type": signal_type,
            "urgency": urgency,
            "rationale": f"{triage_class} from {source_name} → {to_names}",
        }
