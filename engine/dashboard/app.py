"""FastAPI + WebSocket dashboard server for the Coordinator engine.

Public surface
--------------
    attach_dashboard(bus, pack, *, host="0.0.0.0", port=7861) -> coroutine

The coroutine subscribes to ALL engine bus channels, fans every Envelope out
to every connected WebSocket client as JSON, and runs uvicorn in-process so the
caller can `await` it alongside the engine in one event loop.

Standalone replay mode
----------------------
    python -m engine.dashboard --domain <name> --replay <eventlog.jsonl>
        [--host 0.0.0.0] [--port 7861]

Replays a JSONL of serialised Envelopes through the same WS path so a
recorded session looks identical to live.  The ?demo=replay query param on the
client side puts the HTML page into replay-aware display mode.

Domain-agnostic contract
------------------------
This module reads ONLY:
  - generic Envelope fields (type, id, ts, domain, payload)
  - pack.domain  (string label)
  - pack.roles   (list of RoleSpec with role_id + display_name)

No domain literals (role names, station names, signal vocabulary) appear here.
"""
from __future__ import annotations

import asyncio
import json
import logging
import pathlib
import time
from typing import Any, Set

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

from engine.interfaces import ALL_CHANNELS, DomainPack, Envelope

logger = logging.getLogger(__name__)

_STATIC_DIR = pathlib.Path(__file__).parent / "static"


# ---------------------------------------------------------------------------
# Connection manager
# ---------------------------------------------------------------------------

class _ConnectionManager:
    def __init__(self) -> None:
        self._active: Set[WebSocket] = set()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self._active.add(ws)

    def disconnect(self, ws: WebSocket) -> None:
        self._active.discard(ws)

    async def broadcast(self, data: str) -> None:
        dead: list[WebSocket] = []
        for ws in list(self._active):
            try:
                await ws.send_text(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._active.discard(ws)


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def _make_app(pack: DomainPack, manager: _ConnectionManager) -> FastAPI:
    app = FastAPI(title="Knotch Dashboard")

    # Build the init payload from the pack — no domain literals here
    init_payload: dict[str, Any] = {
        "type": "init",
        "domain": pack.domain,
        "display_name": pack.display_name,
        "roles": [
            {"role_id": r.role_id, "display_name": r.display_name}
            for r in pack.roles
        ],
    }
    init_json = json.dumps(init_payload)

    @app.get("/", response_class=HTMLResponse)
    async def serve_index() -> HTMLResponse:
        html_path = _STATIC_DIR / "index.html"
        return HTMLResponse(content=html_path.read_text(encoding="utf-8"))

    @app.websocket("/ws")
    async def ws_endpoint(websocket: WebSocket) -> None:
        await manager.connect(websocket)
        try:
            # Send init so the client can render lanes from config
            await websocket.send_text(init_json)
            # Keep alive: read and discard any pings from the client
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            manager.disconnect(websocket)

    return app


# ---------------------------------------------------------------------------
# Core subscriber loop
# ---------------------------------------------------------------------------

async def _subscribe_all(bus: Any, manager: _ConnectionManager) -> None:
    """Subscribe to every channel; fan each Envelope to all WS clients."""
    async def _drain(channel: str) -> None:
        async for env in bus.subscribe(channel):
            try:
                payload = json.dumps(env.to_dict())
                await manager.broadcast(payload)
            except Exception as exc:  # noqa: BLE001
                logger.debug("broadcast error on %s: %s", channel, exc)

    await asyncio.gather(*[_drain(ch) for ch in ALL_CHANNELS])


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

async def attach_dashboard(
    bus: Any,
    pack: DomainPack,
    *,
    host: str = "0.0.0.0",
    port: int = 7861,
) -> None:
    """Subscribe to bus + run the HTTP/WS server in the current event loop.

    Intended usage (caller owns the event loop)::

        asyncio.gather(
            engine.run(),
            attach_dashboard(bus, pack, port=7861),
        )
    """
    manager = _ConnectionManager()
    app = _make_app(pack, manager)

    config = uvicorn.Config(app, host=host, port=port, log_level="warning")
    server = uvicorn.Server(config)

    await asyncio.gather(
        server.serve(),
        _subscribe_all(bus, manager),
    )


# ---------------------------------------------------------------------------
# Standalone replay mode:  python -m engine.dashboard --domain X --replay f.jsonl
# ---------------------------------------------------------------------------

async def _replay_mode(
    domain: str,
    replay_path: str,
    host: str,
    port: int,
) -> None:
    """Serve the dashboard and replay a JSONL event log through the WS."""
    # Build a minimal fake pack from the events themselves (extract unique roles)
    from engine.interfaces import RoleSpec

    lines = pathlib.Path(replay_path).read_text(encoding="utf-8").splitlines()
    envelopes = [Envelope.from_dict(json.loads(l)) for l in lines if l.strip()]

    # Collect unique participant role_ids from utterance events
    role_ids: list[str] = []
    seen: set[str] = set()
    for env in envelopes:
        pid = env.payload.get("participant") or env.payload.get("source")
        if pid and pid not in seen:
            seen.add(pid)
            role_ids.append(pid)

    # Reconstruct readable display names from role_ids for replay (live mode uses
    # the real pack). Strip a leading "role_" prefix and title-case the rest.
    def _disp(rid: str) -> str:
        base = rid[5:] if rid.startswith("role_") else rid
        return base.replace("_", " ").title()

    fake_roles = [RoleSpec(role_id=rid, display_name=_disp(rid)) for rid in role_ids]

    from engine.interfaces import DomainPack
    fake_pack = DomainPack(
        domain=domain,
        display_name=domain.replace("_", " ").title(),
        dispatcher_voice_id="",
        roles=fake_roles,
        routing_policy="",
    )

    manager = _ConnectionManager()
    app = _make_app(fake_pack, manager)

    async def _replay_feed() -> None:
        # Wait briefly for a client to connect before starting replay
        await asyncio.sleep(3)
        for env in envelopes:
            await manager.broadcast(json.dumps(env.to_dict()))
            await asyncio.sleep(0.4)  # paced replay

    config = uvicorn.Config(app, host=host, port=port, log_level="info")
    server = uvicorn.Server(config)

    await asyncio.gather(server.serve(), _replay_feed())


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Coordinator dashboard standalone / replay")
    parser.add_argument("--domain", default="demo", help="Domain label")
    parser.add_argument("--replay", default=None, metavar="FILE.jsonl",
                        help="Path to a JSONL event log to replay")
    parser.add_argument("--live", action="store_true",
                        help="Connect to the live bus (honours KNOTCH_BUS env var)")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=7861)
    args = parser.parse_args()

    if args.replay:
        asyncio.run(_replay_mode(args.domain, args.replay, args.host, args.port))
    elif args.live:
        import engine.envfile as _envfile
        import engine.domain_loader as _packloader
        import adapters.factory as _factory

        _envfile.load_env()
        _pack = _packloader.load_pack(args.domain)
        _bus = _factory.make_bus()
        asyncio.run(attach_dashboard(_bus, _pack, host=args.host, port=args.port))
    else:
        parser.error("Standalone mode requires --replay <eventlog.jsonl> or --live")
