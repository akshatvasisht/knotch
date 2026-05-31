"""Routing contract tests for engine.routing.

Validates all invariants described in SCHEMA §4 and the routing.py docstring.

Signatures assumed (do not change):

    validate_decision(raw: dict, *, source: str, participants: list[str]) -> RoutingDecision
        - recipients must be a subset of participants; any recipient not in
          participants is DROPPED.
        - source must never appear in recipients (dropped if present).
        - if recipients ends up empty => message is forced to "" (held).
        - on malformed raw (missing keys, wrong types, not a dict) => returns a
          fail-safe held decision (recipients=[], message="") with source
          preserved; never raises.
        - urgency defaults to "low" if missing/invalid (valid: low|med|high).

    self_select(decision: RoutingDecision, role_id: str) -> bool
        - True iff role_id in decision.recipients.
"""
from __future__ import annotations

import pytest

from engine.routing import validate_decision, self_select
from engine.interfaces import RoutingDecision

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

PARTICIPANTS = ["role_a", "role_b", "role_c", "role_d"]
SOURCE = "role_a"


def _raw(
    *,
    source: str = SOURCE,
    recipients: list | object = None,
    message: str = "do the thing",
    signal_type: str = "status",
    urgency: str = "med",
    rationale: str = "because tests",
) -> dict:
    """Build a well-formed raw dict with optional field overrides."""
    d: dict = {
        "source": source,
        "message": message,
        "signal_type": signal_type,
        "urgency": urgency,
        "rationale": rationale,
    }
    if recipients is None:
        d["recipients"] = ["role_b", "role_c"]
    else:
        d["recipients"] = recipients
    return d


# ---------------------------------------------------------------------------
# 1. Valid multi-recipient decision passes through untouched
# ---------------------------------------------------------------------------


def test_valid_decision_passes_through():
    raw = _raw(recipients=["role_b", "role_c"])
    decision = validate_decision(raw, source=SOURCE, participants=PARTICIPANTS)

    assert isinstance(decision, RoutingDecision)
    assert decision.source == SOURCE
    assert set(decision.recipients) == {"role_b", "role_c"}
    assert decision.message == "do the thing"
    assert decision.urgency == "med"
    assert decision.signal_type == "status"
    assert decision.rationale == "because tests"


# ---------------------------------------------------------------------------
# 2. Recipient not in participants is dropped
# ---------------------------------------------------------------------------


def test_unknown_recipient_is_dropped():
    raw = _raw(recipients=["role_b", "role_UNKNOWN"])
    decision = validate_decision(raw, source=SOURCE, participants=PARTICIPANTS)

    assert "role_UNKNOWN" not in decision.recipients
    assert "role_b" in decision.recipients


def test_all_unknown_recipients_forces_held():
    raw = _raw(recipients=["role_UNKNOWN_1", "role_UNKNOWN_2"])
    decision = validate_decision(raw, source=SOURCE, participants=PARTICIPANTS)

    assert decision.recipients == []
    assert decision.message == ""


# ---------------------------------------------------------------------------
# 3. Source appearing in recipients is removed
# ---------------------------------------------------------------------------


def test_source_in_recipients_is_removed():
    raw = _raw(recipients=[SOURCE, "role_b"])
    decision = validate_decision(raw, source=SOURCE, participants=PARTICIPANTS)

    assert SOURCE not in decision.recipients
    assert "role_b" in decision.recipients


def test_source_only_in_recipients_forces_held():
    """After removing source, recipients is empty => held."""
    raw = _raw(recipients=[SOURCE])
    decision = validate_decision(raw, source=SOURCE, participants=PARTICIPANTS)

    assert decision.recipients == []
    assert decision.message == ""


# ---------------------------------------------------------------------------
# 4. Empty recipients => message forced to empty string (held)
# ---------------------------------------------------------------------------


def test_empty_recipients_forces_empty_message():
    raw = _raw(recipients=[], message="this should be wiped")
    decision = validate_decision(raw, source=SOURCE, participants=PARTICIPANTS)

    assert decision.recipients == []
    assert decision.message == ""


def test_non_empty_recipients_preserves_message():
    raw = _raw(recipients=["role_b"], message="keep me")
    decision = validate_decision(raw, source=SOURCE, participants=PARTICIPANTS)

    assert decision.message == "keep me"


# ---------------------------------------------------------------------------
# 5. Held (no-route) decision: recipients=[] is valid
# ---------------------------------------------------------------------------


def test_held_decision_is_valid():
    raw = _raw(recipients=[], message="")
    decision = validate_decision(raw, source=SOURCE, participants=PARTICIPANTS)

    assert isinstance(decision, RoutingDecision)
    assert decision.recipients == []
    assert decision.message == ""
    assert decision.source == SOURCE


# ---------------------------------------------------------------------------
# 6. Malformed inputs coerce to safe held decision — never raise
# ---------------------------------------------------------------------------


def test_not_a_dict_coerces_to_held():
    for bad in [None, 42, "a string", [1, 2, 3], 3.14]:
        decision = validate_decision(bad, source=SOURCE, participants=PARTICIPANTS)
        assert decision.recipients == [], f"Expected held for input {bad!r}"
        assert decision.message == ""
        assert decision.source == SOURCE


def test_missing_recipients_key_coerces_to_held():
    raw = {"source": SOURCE, "message": "oops"}  # no "recipients" key
    decision = validate_decision(raw, source=SOURCE, participants=PARTICIPANTS)

    assert decision.recipients == []
    assert decision.message == ""
    assert decision.source == SOURCE


def test_recipients_as_string_coerces_to_held():
    """recipients must be a list; a string value is malformed."""
    raw = _raw()
    raw["recipients"] = "role_b"  # wrong type: string instead of list
    decision = validate_decision(raw, source=SOURCE, participants=PARTICIPANTS)

    assert decision.recipients == []
    assert decision.message == ""
    assert decision.source == SOURCE


def test_none_raw_coerces_to_held():
    decision = validate_decision(None, source=SOURCE, participants=PARTICIPANTS)

    assert decision.recipients == []
    assert decision.message == ""
    assert decision.source == SOURCE


def test_malformed_missing_source_in_raw_uses_source_param():
    """Even if 'source' key is absent from raw, the source param is used."""
    raw = {"recipients": ["role_b"], "message": "hi"}
    decision = validate_decision(raw, source=SOURCE, participants=PARTICIPANTS)

    assert decision.source == SOURCE


# ---------------------------------------------------------------------------
# 7. self_select returns True / False correctly
# ---------------------------------------------------------------------------


def test_self_select_true_when_in_recipients():
    decision = RoutingDecision(
        source=SOURCE,
        recipients=["role_b", "role_c"],
        message="hey",
    )
    assert self_select(decision, "role_b") is True
    assert self_select(decision, "role_c") is True


def test_self_select_false_when_not_in_recipients():
    decision = RoutingDecision(
        source=SOURCE,
        recipients=["role_b"],
        message="hey",
    )
    assert self_select(decision, "role_c") is False
    assert self_select(decision, SOURCE) is False


def test_self_select_false_on_empty_recipients():
    decision = RoutingDecision(source=SOURCE, recipients=[], message="")
    assert self_select(decision, "role_b") is False


def test_self_select_false_on_unknown_role():
    decision = RoutingDecision(
        source=SOURCE,
        recipients=["role_b"],
        message="yo",
    )
    assert self_select(decision, "role_NONEXISTENT") is False


# ---------------------------------------------------------------------------
# 8. Invalid urgency coerces to "low"
# ---------------------------------------------------------------------------


def test_invalid_urgency_coerces_to_low():
    for bad_urgency in ["URGENT", "critical", "", "HIGH", None, 99]:
        raw = _raw(recipients=["role_b"])
        raw["urgency"] = bad_urgency
        decision = validate_decision(raw, source=SOURCE, participants=PARTICIPANTS)
        assert decision.urgency == "low", (
            f"Expected urgency='low' for bad value {bad_urgency!r}, "
            f"got {decision.urgency!r}"
        )


def test_valid_urgency_values_are_preserved():
    for valid in ("low", "med", "high"):
        raw = _raw(recipients=["role_b"], urgency=valid)
        decision = validate_decision(raw, source=SOURCE, participants=PARTICIPANTS)
        assert decision.urgency == valid
