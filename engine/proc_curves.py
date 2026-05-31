"""Fast curves replay — publishes a cached improve payload to the bus in <1s.

Reads a JSON file previously written by `engine.proc_improve --save` and emits a
single `curves` Envelope onto the bus, with NO eval and NO LLM call. The live
dashboard (which subscribes to the bus) then renders the before/after curves,
Cekura per-metric scores, and judge explanations instantly.

Usage
-----
    python -m engine.proc_curves --file runs/improve_<domain>.json

    # to the live dashboard's bus:
    CONVENER_BUS=redis python -m engine.proc_curves --file runs/improve_<domain>.json

Domain-agnostic: no domain literals appear in this file. The payload (including
the domain label) is read verbatim from the cached file.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from adapters import factory
from engine.envfile import load_env
from engine.interfaces import Envelope, TYPE_CURVES


async def amain(args: argparse.Namespace) -> None:
    load_env()

    path = Path(args.file)
    payload = json.loads(path.read_text(encoding="utf-8"))

    bus = factory.make_bus()

    env = Envelope(type=TYPE_CURVES, payload=payload, domain=str(payload.get("domain", "")))
    await bus.publish(env)

    # Brief settle so the redis backend flushes the publish before we close.
    await asyncio.sleep(0.3)
    await bus.close()

    before = payload.get("before", {}) or {}
    after = payload.get("after", {}) or {}
    print(
        f"[curves] replayed {path}\n"
        f"  scored_by:  {payload.get('scored_by', '?')}\n"
        f"  misroute:   {before.get('misroute', float('nan')):.4f}  →  "
        f"{after.get('misroute', float('nan')):.4f}",
        flush=True,
    )


def main() -> None:
    p = argparse.ArgumentParser(prog="python -m engine.proc_curves")
    p.add_argument(
        "--file",
        required=True,
        help="Path to a cached curves JSON written by `proc_improve --save`.",
    )
    asyncio.run(amain(p.parse_args()))


if __name__ == "__main__":
    main()
