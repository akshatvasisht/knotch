"""Fake TTS for the in-process development and test harness: prints what the
dispatcher would speak, no API key required. Not a user-facing path.

The TTSService Protocol is the contract; any implementation satisfies it. Live
text-to-speech runs in a participant worker under .starter/server/ exposing the
same `speak(text, *, role_id, voice_id)` interface — the bundled worker selects
a backend by env var (Gradium when configured, else a logging stub), but the
engine is agnostic to the provider.
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
