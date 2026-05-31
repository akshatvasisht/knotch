"""Fake transport: text-injection harness — no API key required.

A driver calls `inject(text)` to push a complete turn (or `inject(text,
final=False)` for an interim fragment) into this participant's input stream;
`close()` ends the stream. `output(text)` is a no-op (FakeTTS prints instead)
but exists to satisfy the Transport interface.

The real adapter (DailyTransport, one WebRTC room per participant) satisfies the
same `input_stream()` / `output()` interface.
"""
from __future__ import annotations

import asyncio
from typing import AsyncIterator

from engine.interfaces import InputChunk, Transport

_SENTINEL = object()


class FakeTransport(Transport):
    def __init__(self, role_id: str) -> None:
        self.role_id = role_id
        self._q: asyncio.Queue = asyncio.Queue()

    def inject(self, text: str, *, final: bool = True) -> None:
        """Push a turn (final=True) or interim fragment (final=False)."""
        self._q.put_nowait(InputChunk(text=text, final=final))

    def close(self) -> None:
        self._q.put_nowait(_SENTINEL)

    async def input_stream(self) -> AsyncIterator[InputChunk]:
        while True:
            item = await self._q.get()
            if item is _SENTINEL:
                return
            yield item

    async def output(self, text: str) -> None:
        # No-op: FakeTTS renders dispatcher speech directly via print.
        return None
