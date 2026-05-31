"""Fast wiring test for the improvement dashboard integration.

Tests WITHOUT running a real LLM improve cycle (~90 s). Publishes fake
curves + eval_score envelopes to an InMemoryBus while the dashboard WS
server is running, then verifies a WS client receives both message types.

Run from the project root:
    python scripts/test_improve_wiring.py
"""
from __future__ import annotations

import asyncio
import json
import sys
import time

# ---------------------------------------------------------------------------
# Minimal fake pack (no domain files needed)
# ---------------------------------------------------------------------------
from engine.bus import InMemoryBus
from engine.dashboard.app import attach_dashboard
from engine.interfaces import (
    CHAN_SYSTEM,
    DomainPack,
    Envelope,
    RoleSpec,
    TYPE_EVAL_SCORE,
)

FAKE_PACK = DomainPack(
    domain="test",
    display_name="Wiring Test",
    dispatcher_voice_id="",
    roles=[RoleSpec(role_id="r1", display_name="Role One")],
    convener_fragment="",
)

TEST_PORT = 7869   # distinct port to avoid clashing with a live dashboard


async def _publisher(bus: InMemoryBus) -> None:
    """After a short warm-up, emit one curves + two eval_score envelopes."""
    await asyncio.sleep(1.2)  # give uvicorn + WS client time to connect

    # --- curves envelope (type unknown to TYPE_TO_CHANNEL → falls to CHAN_SYSTEM)
    curves_env = Envelope(
        type="curves",
        payload={
            "before": {"misroute": 0.40, "missed": 0.30, "time_to_action": 1800.0},
            "after":  {"misroute": 0.20, "missed": 0.10, "time_to_action": 950.0},
            "prompt_diff": "added: clarify hold-on-ambiguity rule",
        },
        domain="test",
    )
    await bus.publish(curves_env)
    print("[publisher] sent curves envelope", flush=True)
    await asyncio.sleep(0.2)

    # --- eval_score envelopes (type → CHAN_EVAL)
    for i, outcome in enumerate(("acted", "missed")):
        score_env = Envelope(
            type=TYPE_EVAL_SCORE,
            payload={
                "decision_id": f"dec-{i}",
                "outcome": outcome,
                "time_to_action_ms": 1200 + i * 100,
                "metric_scores": {"misroute": 0, "missed": int(outcome == "missed")},
            },
            domain="test",
        )
        await bus.publish(score_env)
        print(f"[publisher] sent eval_score ({outcome})", flush=True)
        await asyncio.sleep(0.1)

    # Give the WS client a tick to receive everything
    await asyncio.sleep(0.5)
    await bus.close()


async def _ws_client() -> list[dict]:
    """Connect via WS and collect messages until the connection closes."""
    import websockets  # type: ignore

    received: list[dict] = []
    deadline = time.monotonic() + 8.0

    while time.monotonic() < deadline:
        try:
            async with websockets.connect(f"ws://127.0.0.1:{TEST_PORT}/ws") as ws:
                print("[ws_client] connected", flush=True)
                while True:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=3.0)
                        msg = json.loads(raw)
                        print(f"[ws_client] got type={msg.get('type')}", flush=True)
                        received.append(msg)
                    except asyncio.TimeoutError:
                        return received
            return received
        except Exception as exc:
            print(f"[ws_client] waiting for server ({exc})", flush=True)
            await asyncio.sleep(0.3)

    return received


async def run_test() -> bool:
    bus = InMemoryBus()

    # Start dashboard + publisher + WS client concurrently.
    results: list[dict] = []

    async def ws_collector() -> None:
        msgs = await _ws_client()
        results.extend(msgs)

    dashboard_task = asyncio.create_task(
        attach_dashboard(bus, FAKE_PACK, host="127.0.0.1", port=TEST_PORT)
    )
    pub_task       = asyncio.create_task(_publisher(bus))
    ws_task        = asyncio.create_task(ws_collector())

    # Wait for publisher + WS client to finish; dashboard runs until cancelled.
    await asyncio.gather(pub_task, ws_task)
    dashboard_task.cancel()
    try:
        await dashboard_task
    except (asyncio.CancelledError, Exception):
        pass

    # Verify
    types_received = {m.get("type") for m in results}
    print(f"\n[test] message types received: {types_received}", flush=True)

    has_curves = "curves" in types_received
    has_eval   = TYPE_EVAL_SCORE in types_received
    has_init   = "init" in types_received

    ok = has_curves and has_eval and has_init
    if ok:
        print("[test] PASS — curves + eval_score + init all arrived at WS client", flush=True)
    else:
        missing = []
        if not has_init:   missing.append("init")
        if not has_curves: missing.append("curves")
        if not has_eval:   missing.append(TYPE_EVAL_SCORE)
        print(f"[test] FAIL — missing: {missing}", flush=True)

    return ok


def main() -> None:
    ok = asyncio.run(run_test())
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
