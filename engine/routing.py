"""RoutingDecision validation + output-worker self-select.

Domain-agnostic. The coordinator LLM emits a raw dict; `validate_decision` coerces
it into a safe, contract-valid RoutingDecision (fail-safe to no-route). Output
workers call `self_select` — a cheap deterministic address match, not an LLM
call.
"""
from __future__ import annotations

from engine.interfaces import RoutingDecision, URGENCY_VALUES


def validate_decision(
    raw: dict, *, source: str, participants: list[str]
) -> RoutingDecision:
    """Coerce a raw LLM decision dict into a contract-valid RoutingDecision.

    Rules (all enforced here, never trusts the LLM):
      - recipients ⊆ participants (unknown ids dropped), de-duplicated, order kept.
      - source is never a recipient (dropped if present).
      - empty recipients ⇒ empty message (a valid, encouraged "held" outcome).
      - malformed input (not a dict / missing keys / wrong types) ⇒ fail-safe
        held decision with `source` preserved. NEVER raises.
      - urgency defaults to "low" if missing/invalid.
    """
    if not isinstance(raw, dict):
        return RoutingDecision.held(source, rationale="invalid decision (non-dict)")

    allowed = set(participants)

    raw_recipients = raw.get("recipients", [])
    if not isinstance(raw_recipients, (list, tuple)):
        # Malformed recipients => fail safe to held.
        return RoutingDecision.held(source, rationale="invalid recipients")

    recipients: list[str] = []
    for r in raw_recipients:
        if not isinstance(r, str):
            continue
        if r == source:           # source ∉ recipients
            continue
        if r not in allowed:      # recipients ⊆ participants
            continue
        if r in recipients:       # de-dupe, keep order
            continue
        recipients.append(r)

    message = raw.get("message", "")
    if not isinstance(message, str):
        message = ""

    # empty recipients ⇒ empty message (held)
    if not recipients:
        message = ""

    urgency = raw.get("urgency", "low")
    if urgency not in URGENCY_VALUES:
        urgency = "low"

    signal_type = raw.get("signal_type", "")
    if not isinstance(signal_type, str):
        signal_type = ""

    rationale = raw.get("rationale", "")
    if not isinstance(rationale, str):
        rationale = ""

    decision_id = raw.get("decision_id")
    kwargs = dict(
        source=source,
        recipients=recipients,
        message=message,
        signal_type=signal_type,
        urgency=urgency,
        rationale=rationale,
    )
    if isinstance(decision_id, str) and decision_id:
        kwargs["decision_id"] = decision_id
    return RoutingDecision(**kwargs)


def self_select(decision: RoutingDecision, role_id: str) -> bool:
    """Output-worker address match: does this worker need to speak this decision?

    Cheap + deterministic — each output worker filters the broadcast by its own role_id.
    """
    return role_id in decision.recipients
