"""Turn gating: publish an utterance only on turn-stop.

`SimpleTurnGate` accumulates interim STT text and emits the complete turn when
an end-of-turn is signalled (chunk.final). A semantic VAD alternative can be
swapped in behind the same `feed(...) -> Optional[str]` interface with no engine
change — only an env flip (KNOTCH_TURN=smartturn).
"""
from __future__ import annotations

from typing import Optional


class SimpleTurnGate:
    """Accumulate interim text; return the full turn on turn-stop, else None.

    One gate instance per participant (it holds that participant's buffer).
    """

    def __init__(self) -> None:
        self._buffer: list[str] = []

    def feed(self, text: str, final: bool) -> Optional[str]:
        """Feed one STT result.

        - `final=False` (interim): buffer it, return None (NOT published).
        - `final=True` (turn-stop): append, assemble the complete turn, reset,
          and return it. Returns None if the assembled turn is empty.
        """
        if text:
            self._buffer.append(text.strip())
        if not final:
            return None
        turn = " ".join(s for s in self._buffer if s).strip()
        self._buffer.clear()
        return turn or None

    def reset(self) -> None:
        self._buffer.clear()


def make_turn_gate(kind: str = "simple") -> SimpleTurnGate:
    """Factory for turn gates. Supported kinds: 'simple' (default, no extra dependencies). Raises NotImplementedError for unrecognised kinds."""
    if kind == "simple":
        return SimpleTurnGate()
    raise NotImplementedError(
        f"turn gate '{kind}' is not available — use KNOTCH_TURN=simple. "
        "The 'smartturn' option (semantic VAD) is not yet wired."
    )
