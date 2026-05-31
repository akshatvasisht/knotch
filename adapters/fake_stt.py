"""Fake STT: identity passthrough of injected text — no API key required.

The real adapter (Parakeet CTC 1.1B via NIM) satisfies the same
`transcribe(chunk) -> Optional[str]` interface.
"""
from __future__ import annotations

from typing import Optional

from engine.interfaces import InputChunk, STTService


class FakeSTT(STTService):
    async def transcribe(self, chunk: InputChunk) -> Optional[str]:
        # The fake transport puts text directly on the chunk rather than audio bytes.
        text = (chunk.text or "").strip()
        return text or None
