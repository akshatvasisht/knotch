"""Run the dashboard as a standalone process.

    python -m engine.dashboard --domain demo --live              # subscribe live bus (BUS from .env)
    python -m engine.dashboard --domain demo --replay log.jsonl  # replay a recorded event log
"""
import argparse
import asyncio

from engine.dashboard.app import _replay_mode, attach_dashboard


async def _live_mode(domain: str, host: str, port: int) -> None:
    from adapters import factory
    from engine.envfile import load_env
    from engine.packloader import load_pack

    load_env()
    pack = load_pack(domain)
    bus = factory.make_bus()  # redis or memory, from env
    print(f"[dashboard] live · domain={domain} bus={factory._backend('CONVENER_BUS')} "
          f"→ http://localhost:{port}", flush=True)
    await attach_dashboard(bus, pack, host=host, port=port)


def main() -> None:
    p = argparse.ArgumentParser(description="Convener dashboard standalone")
    p.add_argument("--domain", default="demo", help="domain pack / label")
    p.add_argument("--live", action="store_true",
                   help="subscribe to the live bus (CONVENER_BUS from .env)")
    p.add_argument("--replay", default=None, metavar="FILE.jsonl",
                   help="path to a JSONL event log to replay")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=7861)
    args = p.parse_args()

    if args.live:
        asyncio.run(_live_mode(args.domain, args.host, args.port))
    elif args.replay:
        asyncio.run(_replay_mode(args.domain, args.replay, args.host, args.port))
    else:
        p.error("need --live (subscribe live bus) or --replay <eventlog.jsonl>")


if __name__ == "__main__":
    main()
