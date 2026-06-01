"""In-memory async pub/sub bus.

Implements the `Bus` Protocol from interfaces.py. A Redis-backed bus satisfies
the same interface — flip KNOTCH_BUS=redis, no engine change needed.
Channels are the logical topics in interfaces.ALL_CHANNELS.

Design: each `subscribe(channel)` gets its own asyncio.Queue. `publish` fans an
envelope out to every subscriber queue on that channel. `close()` pushes a
sentinel to every queue so all `async for` subscriber loops terminate cleanly.
"""
from __future__ import annotations

import asyncio
from typing import AsyncIterator

from engine.interfaces import Bus, Envelope

_SENTINEL = object()  # pushed on close() to end subscriber loops


class InMemoryBus(Bus):
    def __init__(self) -> None:
        self._subs: dict[str, list[asyncio.Queue]] = {}
        self._closed = False

    async def publish(self, env: Envelope) -> None:
        if self._closed:
            return
        for q in list(self._subs.get(env.channel, [])):
            q.put_nowait(env)

    def subscribe(self, channel: str) -> AsyncIterator[Envelope]:
        # Register the queue EAGERLY (at call time, not first iteration) so a
        # subscriber can't miss messages published between subscribe() and the
        # first `async for` step.
        q: asyncio.Queue = asyncio.Queue()
        self._subs.setdefault(channel, []).append(q)
        return self._iter(q, channel)

    async def _iter(self, q: asyncio.Queue, channel: str) -> AsyncIterator[Envelope]:
        try:
            while True:
                item = await q.get()
                if item is _SENTINEL:
                    return
                yield item
        finally:
            subs = self._subs.get(channel)
            if subs and q in subs:
                subs.remove(q)

    async def close(self) -> None:
        self._closed = True
        for subs in self._subs.values():
            for q in subs:
                q.put_nowait(_SENTINEL)
