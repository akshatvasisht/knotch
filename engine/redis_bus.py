"""Redis-backed bus — the distributed transport for process-per-worker.

Implements the same `Bus` Protocol as InMemoryBus, so engine code is unchanged:
flip KNOTCH_BUS=redis. Participant workers, the coordinator, and the dashboard run
as separate processes and meet only on this bus (Upstash Redis over TLS).

Wire format: each Envelope is JSON-serialised and PUBLISHed on a channel named
after its type (interfaces.TYPE_TO_CHANNEL). A subscriber SUBSCRIBEs to a channel
and yields decoded Envelopes.

Idle-resilient: managed pub/sub providers (Upstash) drop idle read sockets, so the
subscriber uses a get_message poll-loop (tolerates timeouts) + periodic health
checks instead of a bare listen() — a raised idle-timeout must not end the loop.
Redis pub/sub has no backlog: a subscriber only gets messages published after its
SUBSCRIBE lands, so start subscribers before driving traffic.
"""
from __future__ import annotations

import asyncio
import json
from typing import AsyncIterator

import redis.asyncio as aioredis

from engine.interfaces import Bus, Envelope


class RedisBus(Bus):
    def __init__(self, url: str) -> None:
        if not url:
            raise RuntimeError(
                "REDIS_URL is not set. Provide the Upstash protocol endpoint "
                "(rediss://default:<password>@<host>:6379) via .env."
            )
        self._url = url
        self._client = aioredis.from_url(
            url,
            decode_responses=True,
            health_check_interval=15,   # keep the idle pubsub socket alive
            socket_keepalive=True,
        )
        self._pubsubs: list = []
        self._closed = False

    async def publish(self, env: Envelope) -> None:
        if self._closed:
            return
        await self._client.publish(env.channel, json.dumps(env.to_dict()))

    def subscribe(self, channel: str) -> AsyncIterator[Envelope]:
        ps = self._client.pubsub()
        self._pubsubs.append(ps)
        return self._iter(ps, channel)

    async def _iter(self, ps, channel: str) -> AsyncIterator[Envelope]:
        await ps.subscribe(channel)
        try:
            while not self._closed:
                try:
                    message = await ps.get_message(
                        ignore_subscribe_messages=True, timeout=1.0
                    )
                except Exception:
                    # Idle read timeout / transient blip — pace and keep waiting.
                    # Upstash recycles idle sockets; redis-py reconnects on the
                    # next get_message, so we must NOT end the subscriber loop.
                    if self._closed:
                        return
                    await asyncio.sleep(0.2)
                    continue
                if not message:
                    continue  # idle tick (timeout returned None)
                if message.get("type") != "message":
                    continue
                data = message.get("data")
                try:
                    env = Envelope.from_dict(json.loads(data))
                except (TypeError, ValueError):
                    continue
                yield env
        finally:
            try:
                await ps.aclose()
            except Exception:
                pass

    async def close(self) -> None:
        self._closed = True
        for ps in list(self._pubsubs):
            try:
                await ps.unsubscribe()
                await ps.aclose()
            except Exception:
                pass
        try:
            await self._client.aclose()
        except Exception:
            pass
