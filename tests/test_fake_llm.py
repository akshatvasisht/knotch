"""Tests for adapters.fake_llm.FakeCoordinatorLLM — deterministic routing rules."""
from __future__ import annotations

import pytest

from adapters.fake_llm import FakeCoordinatorLLM
from engine.interfaces import RoleSpec, RoutingRequest, Utterance


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #

_PARTICIPANTS = [
    RoleSpec(role_id="role_a", display_name="Alice"),
    RoleSpec(role_id="role_b", display_name="Bob"),
]


def _req(
    text: str,
    *,
    source: str = "role_a",
    triage_class: str = "",
    participants: list[RoleSpec] | None = None,
) -> RoutingRequest:
    """Build a minimal RoutingRequest with the two default participants."""
    return RoutingRequest(
        system_prompt="",
        utterance=Utterance(participant=source, text=text, triage_class=triage_class),
        state={},
        recent=[],
        participants=participants if participants is not None else _PARTICIPANTS,
    )


_llm = FakeCoordinatorLLM()


# --------------------------------------------------------------------------- #
# Test cases                                                                   #
# --------------------------------------------------------------------------- #

async def test_chatter_is_held():
    """Neutral text with no keyword cues → held (recipients=[], message='')."""
    result = await _llm.decide(_req("The weather looks fine today."))
    assert result["recipients"] == []
    assert result["message"] == ""


async def test_alert_cue_broadcasts():
    """High-urgency cue → broadcast to all others, signal_type='dependency'."""
    result = await _llm.decide(_req("We're stuck behind the delivery truck."))
    assert "role_b" in result["recipients"]
    assert result["signal_type"] == "dependency"


async def test_direct_address_by_display_name():
    """Mentioning another participant's display name routes only to that role."""
    result = await _llm.decide(_req("Bob can you check the temperatures?"))
    assert result["recipients"] == ["role_b"]
    # Alice (source) must not appear even though she's a participant
    assert "role_a" not in result["recipients"]


async def test_question_form_routes_as_request():
    """A question-form utterance routes to others with signal_type='request'."""
    result = await _llm.decide(_req("Where is the current order?"))
    assert result["signal_type"] == "request"
    assert len(result["recipients"]) > 0


async def test_source_never_in_recipients():
    """Source role is never included in recipients regardless of text content."""
    # Alice says her own name — should still not route back to herself
    result = await _llm.decide(_req("Alice needs help immediately.", source="role_a"))
    assert "role_a" not in result["recipients"]


async def test_return_dict_has_required_keys():
    """Decision dict always contains the six required keys."""
    result = await _llm.decide(_req("Need a status update?"))
    for key in ("source", "recipients", "message", "signal_type", "urgency", "rationale"):
        assert key in result, f"Missing key: {key}"


async def test_held_decision_has_empty_message():
    """When held (recipients=[]), message is always an empty string."""
    result = await _llm.decide(_req("All good, nothing special."))
    if result["recipients"] == []:
        assert result["message"] == ""
