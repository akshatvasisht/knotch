"""Tests for InMemoryBus and Envelope from engine.bus / engine.interfaces.

asyncio_mode = "auto" (pyproject.toml) — no @pytest.mark.asyncio needed.
All async-for loops are guarded with asyncio.wait_for(..., timeout=1.0) so
tests never hang on a broken sentinel or missed publish.
"""
from __future__ import annotations

import asyncio

import pytest

from engine.bus import InMemoryBus
from engine.interfaces import (
    CHAN_ROUTED,
    CHAN_SYSTEM,
    CHAN_UTTERANCES,
    TYPE_ROUTING_DECISION,
    TYPE_SYSTEM,
    TYPE_UTTERANCE,
    Envelope,
)


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #

def _env(type_: str = TYPE_UTTERANCE, payload: dict | None = None, domain: str = "") -> Envelope:
    return Envelope(type=type_, payload=payload or {}, domain=domain)


async def _collect_one(aiter) -> Envelope:
    """Pull exactly one envelope from an async iterator; timeout-safe."""
    return await asyncio.wait_for(aiter.__anext__(), timeout=1.0)


# --------------------------------------------------------------------------- #
# InMemoryBus — §1  single subscriber receives published envelope             #
# --------------------------------------------------------------------------- #

async def test_single_subscriber_receives_envelope():
    bus = InMemoryBus()
    env = _env(TYPE_UTTERANCE)
    sub = bus.subscribe(CHAN_UTTERANCES)

    await bus.publish(env)
    received = await _collect_one(sub)

    assert received is env
    await bus.close()


# --------------------------------------------------------------------------- #
# InMemoryBus — §2  fan-out: multiple subscribers each receive the envelope   #
# --------------------------------------------------------------------------- #

async def test_fanout_all_subscribers_receive():
    bus = InMemoryBus()
    env = _env(TYPE_UTTERANCE)

    sub_a = bus.subscribe(CHAN_UTTERANCES)
    sub_b = bus.subscribe(CHAN_UTTERANCES)
    sub_c = bus.subscribe(CHAN_UTTERANCES)

    await bus.publish(env)

    got_a = await _collect_one(sub_a)
    got_b = await _collect_one(sub_b)
    got_c = await _collect_one(sub_c)

    assert got_a is env
    assert got_b is env
    assert got_c is env
    await bus.close()


# --------------------------------------------------------------------------- #
# InMemoryBus — §3  subscriber on a DIFFERENT channel does not receive msg    #
# --------------------------------------------------------------------------- #

async def test_wrong_channel_subscriber_does_not_receive():
    bus = InMemoryBus()
    env = _env(TYPE_UTTERANCE)           # goes to CHAN_UTTERANCES

    wrong_sub = bus.subscribe(CHAN_ROUTED)   # different channel
    await bus.publish(env)

    # Queue on wrong channel should be empty; close and confirm loop ends
    # without yielding anything.
    received: list[Envelope] = []

    async def drain():
        async for item in wrong_sub:
            received.append(item)

    await bus.close()
    await asyncio.wait_for(drain(), timeout=1.0)

    assert received == []


# --------------------------------------------------------------------------- #
# InMemoryBus — §4  close() terminates all async-for loops (sentinel)         #
# --------------------------------------------------------------------------- #

async def test_close_terminates_subscriber_loops():
    bus = InMemoryBus()
    collected: list[Envelope] = []

    async def consume():
        async for env in bus.subscribe(CHAN_UTTERANCES):
            collected.append(env)

    task = asyncio.create_task(consume())

    # Yield control so the task can start and reach its first await q.get().
    await asyncio.sleep(0)

    # Publish two envelopes, then close.
    await bus.publish(_env(TYPE_UTTERANCE))
    await bus.publish(_env(TYPE_UTTERANCE))
    await bus.close()

    # Give the event loop a tick to drain the sentinel through the generator.
    await asyncio.sleep(0)
    await asyncio.wait_for(task, timeout=1.0)

    assert len(collected) == 2


# --------------------------------------------------------------------------- #
# InMemoryBus — §5  publish() after close() is a silent no-op                #
# --------------------------------------------------------------------------- #

async def test_publish_after_close_is_noop():
    bus = InMemoryBus()
    sub = bus.subscribe(CHAN_UTTERANCES)

    await bus.close()

    # Should not raise and should not deliver anything.
    await bus.publish(_env(TYPE_UTTERANCE))

    received: list[Envelope] = []
    async def drain():
        async for item in sub:
            received.append(item)

    # Sub loop already got the sentinel from close(), so drain() should finish.
    await asyncio.wait_for(drain(), timeout=1.0)
    assert received == []


# --------------------------------------------------------------------------- #
# InMemoryBus — §6  subscriber queue is cleaned up after loop exits           #
# --------------------------------------------------------------------------- #

async def test_subscriber_queue_removed_after_loop_exits():
    """Verify that the _iter finally block removes the queue when iteration ends.

    Drive the iterator manually with __anext__ + aclose() to avoid the
    CancelledError that arises when 'break' inside an async-for interrupts
    an in-progress await q.get() on Python 3.12.
    """
    bus = InMemoryBus()

    sub = bus.subscribe(CHAN_UTTERANCES)
    await bus.publish(_env(TYPE_UTTERANCE))

    # Pull the one item.
    item = await asyncio.wait_for(sub.__anext__(), timeout=1.0)
    assert item.type == TYPE_UTTERANCE

    # Explicitly close the generator — this triggers the finally block in _iter.
    await sub.aclose()

    # The queue should have been removed from the subscriber list.
    remaining = bus._subs.get(CHAN_UTTERANCES, [])
    assert remaining == [], f"Expected empty queue list, got {remaining}"

    await bus.close()


# --------------------------------------------------------------------------- #
# InMemoryBus — §7  envelope published BEFORE async-for starts is NOT lost    #
# --------------------------------------------------------------------------- #

async def test_message_before_iteration_is_not_lost():
    bus = InMemoryBus()
    env = _env(TYPE_UTTERANCE)

    # subscribe() registers the queue eagerly (before iteration).
    sub = bus.subscribe(CHAN_UTTERANCES)

    # Publish before the caller ever calls __anext__.
    await bus.publish(env)

    # Now start iterating — the message must be waiting in the queue.
    received = await _collect_one(sub)
    assert received is env

    await bus.close()


# --------------------------------------------------------------------------- #
# Envelope — §8  to_dict / from_dict round-trip preserves all fields          #
# --------------------------------------------------------------------------- #

def test_envelope_round_trip():
    original = Envelope(
        type=TYPE_UTTERANCE,
        payload={"participant": "role_a", "text": "hello"},
        id="fixed-uuid-1234",
        ts=1_700_000_000.0,
        domain="test_domain",
    )
    d = original.to_dict()
    restored = Envelope.from_dict(d)

    assert restored.type == original.type
    assert restored.payload == original.payload
    assert restored.id == original.id
    assert restored.ts == original.ts
    assert restored.domain == original.domain


# --------------------------------------------------------------------------- #
# Envelope — §9  from_dict with missing optional fields uses safe defaults     #
# --------------------------------------------------------------------------- #

def test_envelope_from_dict_missing_optionals():
    # Only the required `type` field is provided; everything else is optional.
    d = {"type": TYPE_UTTERANCE}
    env = Envelope.from_dict(d)

    assert env.type == TYPE_UTTERANCE
    assert env.payload == {}
    assert env.domain == ""
    assert isinstance(env.id, str) and len(env.id) > 0
    assert isinstance(env.ts, float) and env.ts > 0


# --------------------------------------------------------------------------- #
# Envelope — §10  channel property routes known types correctly               #
# --------------------------------------------------------------------------- #

def test_envelope_channel_known_types():
    assert Envelope(type=TYPE_UTTERANCE, payload={}).channel == CHAN_UTTERANCES
    assert Envelope(type=TYPE_ROUTING_DECISION, payload={}).channel == CHAN_ROUTED
    assert Envelope(type=TYPE_SYSTEM, payload={}).channel == CHAN_SYSTEM


# --------------------------------------------------------------------------- #
# Envelope — §11  channel falls back to CHAN_SYSTEM for unknown type          #
# --------------------------------------------------------------------------- #

def test_envelope_channel_unknown_type_fallback():
    env = Envelope(type="totally_unknown_type_xyz", payload={})
    assert env.channel == CHAN_SYSTEM
