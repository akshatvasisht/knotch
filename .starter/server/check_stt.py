#!/usr/bin/env python3
"""Tiny reachability probe for the NVIDIA ASR websocket.

Opens the STT websocket (NVIDIA_ASR_URL) and waits for the server's
``{"type": "ready"}`` handshake. Prints PASS/FAIL and exits 0/1 so it can
gate the smoke test.

    uv run check_stt.py
"""

import asyncio
import json
import os
import sys

import websockets

async def main() -> int:
    url = os.environ.get("NVIDIA_ASR_URL")
    if not url:
        print("[check_stt] FAIL — NVIDIA_ASR_URL is not set")
        return 1
    print(f"[check_stt] connecting to {url} ...")
    try:
        async with websockets.connect(url, ping_interval=20, ping_timeout=20) as ws:
            raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
            data = json.loads(raw)
            if data.get("type") == "ready":
                print(f"[check_stt] PASS — server ready: {data}")
                return 0
            print(f"[check_stt] FAIL — unexpected first message: {data}")
            return 1
    except Exception as exc:  # noqa: BLE001
        print(f"[check_stt] FAIL — {type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
