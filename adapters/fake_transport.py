"""Fake transport for the in-process development and test harness: a
text-injection driver, no API key required. Not a user-facing path.

A driver calls `inject(text)` to push a complete turn (or `inject(text,
final=False)` for an interim fragment) into this participant's input stream;
`close()` ends the stream. `output(text)` is a no-op (FakeTTS prints instead)
but exists to satisfy the Transport interface.

The Transport Protocol is the contract; any implementation satisfies it. Live
transport runs in a participant worker under .starter/server/ exposing the same
`input_stream()` / `output()` interface — the bundled workers wire Daily
(WebRTC) and Twilio (telephony), one participant per worker, but the engine is
agnostic to the medium.
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
