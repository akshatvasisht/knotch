"""End-to-end test: runs the real improve flow on a shared InMemoryBus
alongside the dashboard and a WS client, all in ONE process.

This avoids the two-process memory-bus isolation issue.

Run from project root:
    PYTHONPATH=/path/to/voice-agents python3 scripts/test_improve_e2e.py --domain <name>

Prints before/after curve numbers and confirms curves + eval_score arrive
at a WebSocket client.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time

from engine.bus import InMemoryBus
from engine.dashboard.app import attach_dashboard
from engine.envfile import load_env
from engine.eval_runner import run_eval
from engine.improve import improve_once
from engine.interfaces import CHAN_SYSTEM, Envelope, TYPE_EVAL_SCORE
from engine.packloader import load_pack

TEST_PORT = 7870


async def run_e2e(domain: str) -> bool:
    load_env()
    pack = load_pack(domain)
    bus = InMemoryBus()

    received: list[dict] = []

    # ── WS client collector ──────────────────────────────────────────────────
    async def ws_collector() -> None:
        import websockets  # type: ignore
        deadline = time.monotonic() + 200.0   # generous — real LLM run is ~90 s
        while time.monotonic() < deadline:
            try:
                async with websockets.connect(f"ws://127.0.0.1:{TEST_PORT}/ws") as ws:
                    print("[ws_client] connected", flush=True)
                    while True:
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
                            msg = json.loads(raw)
                            print(f"[ws_client] got type={msg.get('type')}", flush=True)
                            received.append(msg)
                        except asyncio.TimeoutError:
                            # Exit only after we've seen both curves AND at least
                            # one eval_score, or after 4 consecutive timeouts.
                            has_curves = any(m.get("type") == "curves" for m in received)
                            has_eval   = any(m.get("type") == TYPE_EVAL_SCORE for m in received)
                            if has_curves and has_eval:
                                return
                return
            except Exception as exc:
                await asyncio.sleep(0.3)

    # ── Improve + publish ────────────────────────────────────────────────────
    async def improve_and_publish() -> None:
        await asyncio.sleep(1.5)  # let dashboard + WS client warm up

        print(f"[improve] starting improve_once for domain={domain}", flush=True)
        res = await improve_once(pack)

        before = res["before"]
        after  = res["after"]

        print(
            f"\n[improve] DONE\n"
            f"  misroute:       {before['misroute']:.4f}  →  {after['misroute']:.4f}\n"
            f"  missed:         {before['missed']:.4f}  →  {after['missed']:.4f}\n"
            f"  time_to_action: {before['time_to_action']:.1f} ms  →  {after['time_to_action']:.1f} ms\n"
            f"  prompt_diff:    {res['prompt_diff']}\n"
            f"  saved_to:       {res.get('saved_to', '?')}",
            flush=True,
        )

        curves_env = Envelope(
            type="curves",
            payload={
                "before": before,
                "after":  after,
                "prompt_diff": res["prompt_diff"],
            },
            domain=pack.domain,
        )
        await bus.publish(curves_env)
        print("[improve] curves envelope published", flush=True)

        await run_eval(
            pack,
            prompt_fragment=res["revised_fragment"],
            eval_bus=bus,
        )
        print("[improve] eval_score envelopes published", flush=True)

        await asyncio.sleep(1.0)
        await bus.close()

    dashboard_task = asyncio.create_task(
        attach_dashboard(bus, pack, host="127.0.0.1", port=TEST_PORT)
    )
    improve_task = asyncio.create_task(improve_and_publish())
    ws_task      = asyncio.create_task(ws_collector())

    await asyncio.gather(improve_task, ws_task)
    dashboard_task.cancel()
    try:
        await dashboard_task
    except (asyncio.CancelledError, Exception):
        pass

    types_received = {m.get("type") for m in received}
    print(f"\n[test] message types received: {types_received}", flush=True)

    ok = "curves" in types_received and TYPE_EVAL_SCORE in types_received and "init" in types_received
    if ok:
        print("[test] PASS — curves + eval_score + init arrived at WS client", flush=True)
    else:
        print(f"[test] FAIL — missing types", flush=True)

    return ok


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--domain", required=True)
    args = p.parse_args()
    ok = asyncio.run(run_e2e(args.domain))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
