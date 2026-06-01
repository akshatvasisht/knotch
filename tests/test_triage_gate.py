"""Tests for engine.triage and engine.turn_detector."""
from __future__ import annotations

import pytest

from engine.triage import is_backchannel, triage
from engine.turn_detector import SimpleTurnGate, make_turn_gate


# ---------------------------------------------------------------------------
# is_backchannel / triage
# ---------------------------------------------------------------------------

class TestIsBackchannel:
    def test_known_backchannels(self):
        for word in ("yeah", "ok", "got it", "mhm"):
            assert is_backchannel(word) is True, f"expected backchannel: {word!r}"

    def test_trailing_punctuation_stripped(self):
        assert is_backchannel("yeah.") is True
        assert is_backchannel("ok!") is True
        assert is_backchannel("mhm?") is True

    def test_leading_trailing_whitespace(self):
        assert is_backchannel("  yeah  ") is True
        assert is_backchannel("\tok\n") is True

    def test_empty_string(self):
        assert is_backchannel("") is True

    def test_non_backchannel_returns_false(self):
        assert is_backchannel("we need more supplies") is False
        assert is_backchannel("where is the sauce?") is False


class TestTriage:
    # --- backchannel ---
    def test_backchannel_yeah(self):
        assert triage("yeah") == (False, "backchannel")

    def test_backchannel_ok(self):
        assert triage("ok") == (False, "backchannel")

    def test_backchannel_got_it(self):
        assert triage("got it") == (False, "backchannel")

    def test_backchannel_mhm(self):
        assert triage("mhm") == (False, "backchannel")

    def test_backchannel_with_punctuation(self):
        assert triage("yeah.") == (False, "backchannel")
        assert triage("ok!") == (False, "backchannel")

    def test_backchannel_with_whitespace(self):
        assert triage("  yeah  ") == (False, "backchannel")

    def test_empty_string_is_backchannel(self):
        assert triage("") == (False, "backchannel")

    # --- alert ---
    def test_alert_out_of(self):
        routable, cls = triage("we're out of stock")
        assert routable is True
        assert cls == "alert"

    def test_alert_help_now(self):
        routable, cls = triage("help needed now")
        assert routable is True
        assert cls == "alert"

    def test_alert_stuck_behind(self):
        routable, cls = triage("stuck behind the line")
        assert routable is True
        assert cls == "alert"

    # --- request ---
    def test_request_where_question(self):
        routable, cls = triage("where is the sauce?")
        assert routable is True
        assert cls == "request"

    def test_request_can_you(self):
        routable, cls = triage("can you check the temps")
        assert routable is True
        assert cls == "request"

    def test_request_what_is(self):
        routable, cls = triage("what time does the shift end")
        assert routable is True
        assert cls == "request"

    def test_request_trailing_question_mark(self):
        routable, cls = triage("everything okay over there?")
        assert routable is True
        assert cls == "request"

    # --- status ---
    def test_status_neutral(self):
        routable, cls = triage("everything looks good")
        assert routable is True
        assert cls == "status"

    def test_status_slow(self):
        routable, cls = triage("running a bit slow")
        assert routable is True
        assert cls == "status"

    # --- alert beats question form ---
    def test_alert_priority_over_question(self):
        # "help" is an alert cue; question form should not override it
        routable, cls = triage("can you help now?")
        assert routable is True
        assert cls == "alert"


# ---------------------------------------------------------------------------
# SimpleTurnGate
# ---------------------------------------------------------------------------

class TestSimpleTurnGate:
    def test_interim_returns_none(self):
        gate = SimpleTurnGate()
        result = gate.feed("hello", final=False)
        assert result is None

    def test_interim_buffers_text(self):
        gate = SimpleTurnGate()
        gate.feed("hello", final=False)
        # Emit with an empty final chunk to flush
        turn = gate.feed("", final=True)
        assert turn == "hello"

    def test_final_assembles_turn(self):
        gate = SimpleTurnGate()
        gate.feed("first", final=False)
        gate.feed("second", final=False)
        turn = gate.feed("third", final=True)
        assert turn == "first second third"

    def test_multiple_interims_joined_with_spaces(self):
        gate = SimpleTurnGate()
        for chunk in ("alpha", "beta", "gamma"):
            assert gate.feed(chunk, final=False) is None
        turn = gate.feed("delta", final=True)
        assert turn == "alpha beta gamma delta"

    def test_empty_final_with_no_buffer_returns_none(self):
        gate = SimpleTurnGate()
        result = gate.feed("", final=True)
        assert result is None

    def test_reset_clears_buffer(self):
        gate = SimpleTurnGate()
        gate.feed("partial", final=False)
        gate.reset()
        # After reset, flushing should produce nothing
        result = gate.feed("", final=True)
        assert result is None

    def test_fresh_buffer_after_emit(self):
        gate = SimpleTurnGate()
        # First turn
        gate.feed("turn one", final=False)
        t1 = gate.feed("end", final=True)
        assert t1 == "turn one end"

        # Second turn starts fresh — previous content must not bleed through
        gate.feed("turn two", final=False)
        t2 = gate.feed("", final=True)
        assert t2 == "turn two"

    def test_whitespace_only_chunk_ignored(self):
        gate = SimpleTurnGate()
        gate.feed("   ", final=False)  # stripped to empty, not appended
        gate.feed("real", final=False)
        turn = gate.feed("", final=True)
        assert turn == "real"


# ---------------------------------------------------------------------------
# make_turn_gate
# ---------------------------------------------------------------------------

class TestMakeTurnGate:
    def test_simple_returns_simple_turn_gate(self):
        gate = make_turn_gate("simple")
        assert isinstance(gate, SimpleTurnGate)

    def test_default_kind_is_simple(self):
        gate = make_turn_gate()
        assert isinstance(gate, SimpleTurnGate)

    def test_unknown_kind_raises(self):
        with pytest.raises(NotImplementedError):
            make_turn_gate("smartturn")

    def test_another_unknown_kind_raises(self):
        with pytest.raises(NotImplementedError):
            make_turn_gate("bogus")
