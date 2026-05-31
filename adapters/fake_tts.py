"""Fake TTS: prints what the dispatcher would speak — no API key required.

The real adapter (Magpie, single dispatcher voice) satisfies the same
`speak(text, *, role_id, voice_id)` interface.
"""
from __future__ import annotations

from engine.interfaces import TTSService


class FakeTTS(TTSService):
    def __init__(self, label_for=None) -> None:
        # Optional callable: role_id -> display name, for labelled terminal output.
        self._label_for = label_for

    async def speak(self, text: str, *, role_id: str, voice_id: str) -> None:
        who = self._label_for(role_id) if self._label_for else role_id
        print(f"      \033[92m🔊 dispatcher → {who}:\033[0m \"{text}\"")
