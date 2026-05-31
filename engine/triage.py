"""Triage: the cheap gate before the expensive convener call.

Two jobs, both domain-agnostic:
  1. Drop backchannels ("uh huh", "yep", "ok") so they never reach the convener.
  2. Tag a coarse, generic class on routable turns (a hint, not a decision — the
     convener LLM still makes the real call; `signal_type` ultimately comes from
     the domain fragment).

Protects the convener's call frequency and stops routing on filler. Generic
English cue words are fine here — they are not domain literals. No role names
appear in this file.
"""
from __future__ import annotations

# Pure backchannels / acknowledgements — never worth a routing call.
BACKCHANNELS: frozenset[str] = frozenset(
    {
        "uh huh", "uhhuh", "uh-huh", "mhm", "mm", "mmhm", "hmm", "huh",
        "yep", "yup", "yeah", "yes", "no", "nope", "ok", "okay", "k",
        "right", "sure", "got it", "gotcha", "copy", "roger", "cool",
        "thanks", "thank you", "nice", "alright", "aight", "word",
    }
)

# Coarse generic classes (NOT the domain's signal vocabulary).
CLASS_REQUEST = "request"
CLASS_ALERT = "alert"
CLASS_STATUS = "status"

# Generic urgency/alert cue words (plain English, domain-neutral).
_ALERT_CUES = (
    "down", "stop", "help", "now", "hurry", "behind", "waiting", "late",
    "slammed", "stuck", "broken", "emergency", "urgent", "asap", "immediately",
    "out of", "need", "stat",
)
_QUESTION_STARTS = (
    "where", "what", "when", "who", "how", "why", "can ", "could ", "do ",
    "does ", "is ", "are ", "should ", "any ",
)


def is_backchannel(text: str) -> bool:
    t = text.strip().lower().rstrip(".!?")
    if not t:
        return True
    return t in BACKCHANNELS


def triage(text: str) -> tuple[bool, str]:
    """Return (routable, triage_class).

    Backchannels / empty => (False, "backchannel") and never get published.
    Otherwise (True, <coarse class>).
    """
    if is_backchannel(text):
        return False, "backchannel"

    t = text.strip().lower()

    if any(cue in t for cue in _ALERT_CUES):
        return True, CLASS_ALERT
    if t.endswith("?") or t.startswith(_QUESTION_STARTS):
        return True, CLASS_REQUEST
    return True, CLASS_STATUS
