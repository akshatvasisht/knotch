"""Standalone coordinator process — transport-less, pure Redis subscriber.

Runs anywhere that can reach the bus (outbound pub/sub only, no NAT, no transport,
not a Pipecat Cloud session). Subscribes to utterances on the bus, routes via the
LLM, publishes decisions back. One process; shard by --zone to scale.

    python -m engine.proc_coordinator --domain demo          # BUS/LLM from .env
    KNOTCH_BUS=redis KNOTCH_LLM=nemotron python -m engine.proc_coordinator --domain demo
"""
from __future__ import annotations

import argparse
import asyncio

from adapters import factory
from engine.coordinator import Coordinator
from engine.envfile import load_env
from engine.domain_loader import assemble_system_prompt, load_pack, load_scaffold


async def amain(args: argparse.Namespace) -> None:
    load_env()
    pack = load_pack(args.domain, domains_dir=args.domains_dir, prompts_dir=args.prompts_dir)
    system_prompt = assemble_system_prompt(load_scaffold(prompts_dir=args.prompts_dir), pack)

    bus = factory.make_bus()
    llm = factory.make_llm()
    coordinator = Coordinator(
        bus=bus, llm=llm, system_prompt=system_prompt,
        participants=pack.roles, zone=args.zone, domain=pack.domain,
    )
    print(
        f"[coordinator] up · domain={pack.domain} zone={args.zone} "
        f"bus={factory._backend('KNOTCH_BUS')} llm={factory._backend('KNOTCH_LLM')} "
        f"owns={sorted(coordinator.owns)}",
        flush=True,
    )
    await coordinator.run()  # runs until killed


def main() -> None:
    p = argparse.ArgumentParser(prog="python -m engine.proc_coordinator")
    p.add_argument("--domain", required=True)
    p.add_argument("--zone", default="z0")
    p.add_argument("--domains-dir", default="domains")
    p.add_argument("--prompts-dir", default="prompts")
    asyncio.run(amain(p.parse_args()))


if __name__ == "__main__":
    main()
