"""Fake STT for the in-process development and test harness: identity
passthrough of injected text, no API key required. Not a user-facing path.

The STTService Protocol is the contract; any implementation satisfies it. Live
speech-to-text runs in a participant worker under .starter/server/ exposing the
same `transcribe(chunk) -> Optional[str]` interface — the bundled worker wires
NVIDIA Parakeet over WebSocket, but the engine is agnostic to the provider.
"""
from __future__ import annotations

from typing import Optional

from engine.interfaces import InputChunk, STTService


class FakeSTT(STTService):
    async def transcribe(self, chunk: InputChunk) -> Optional[str]:
        # The fake transport puts text directly on the chunk rather than audio bytes.
        text = (chunk.text or "").strip()
        return text or None
