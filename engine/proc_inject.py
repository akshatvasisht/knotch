"""Standalone injector process — publishes scripted utterances onto the bus.

A separate-process substitute for a participant's live input path; useful for
transcript injection and evaluation replay without a running transport. This
process publishes, the convener process routes, and the dashboard process
renders — all communicating only through the bus.

    python -m engine.proc_inject --domain demo --scenario example_scenario

Optional flags:
  --all-scenarios          run every scenario in the pack (default: first only)
  --exclude-role ROLE_ID   skip utterances whose participant matches ROLE_ID;
                           repeatable.  Use when a live participant is already
                           on the bus and should not be double-driven by the
                           script (e.g. the human's Daily role in a hybrid demo).
  --loop                   replay indefinitely after finishing all chosen
                           scenarios, pausing --loop-delay seconds between runs.
"""
from __future__ import annotations

import argparse
import asyncio

from adapters import factory
from engine.envfile import load_env
from engine.interfaces import Envelope, TYPE_UTTERANCE, Utterance
from engine.packloader import load_pack
from engine.triage import triage


async def _run_scenarios(
    scenarios: list[dict],
    *,
    bus,
    pack,
    exclude: set[str],
    step_delay: float,
) -> None:
    """Publish one pass through *scenarios*, skipping any participant in *exclude*."""
    for scen in scenarios:
        print(
            f"[inject] scenario '{scen.get('id')}' "
            f"bus={factory._backend('CONVENER_BUS')}",
            flush=True,
        )
        for line in scen.get("utterances", []):
            part, text = line.get("participant"), line.get("text", "")
            if part in exclude:
                print(f"[inject]   · excluded role skipped: {part}: {text}", flush=True)
                continue
            routable, klass = triage(text)
            if not routable:
                print(f"[inject]   · backchannel dropped: {part}: {text}", flush=True)
                continue
            utt = Utterance(participant=part, text=text, triage_class=klass)
            await bus.publish(
                Envelope(type=TYPE_UTTERANCE, payload=utt.to_dict(), domain=pack.domain)
            )
            print(f"[inject]   🎙 {part}: {text} [{klass}]", flush=True)
            await asyncio.sleep(step_delay)


async def amain(args: argparse.Namespace) -> None:
    load_env()
    pack = load_pack(args.domain, domains_dir=args.domains_dir, prompts_dir=args.prompts_dir)
    bus = factory.make_bus()

    exclude: set[str] = set(args.exclude_role) if args.exclude_role else set()

    scenarios = pack.scenarios
    if args.scenario:
        scenarios = [s for s in scenarios if s.get("id") == args.scenario] or scenarios[:1]
    elif not args.all_scenarios:
        scenarios = scenarios[:1]

    if exclude:
        print(f"[inject] excluding roles: {sorted(exclude)}", flush=True)

    await asyncio.sleep(args.warmup)  # let subscribers register on the bus

    await _run_scenarios(scenarios, bus=bus, pack=pack, exclude=exclude, step_delay=args.step_delay)

    if args.loop:
        run = 1
        while True:
            print(
                f"[inject] loop: waiting {args.loop_delay}s before replay #{run + 1} …",
                flush=True,
            )
            await asyncio.sleep(args.loop_delay)
            run += 1
            await _run_scenarios(
                scenarios, bus=bus, pack=pack, exclude=exclude, step_delay=args.step_delay
            )
    else:
        await asyncio.sleep(1.0)
        await bus.close()


def main() -> None:
    p = argparse.ArgumentParser(prog="python -m engine.proc_inject")
    p.add_argument("--domain", required=True)
    p.add_argument("--scenario")
    p.add_argument("--all-scenarios", action="store_true",
                   help="run every scenario in the pack (default: first only)")
    p.add_argument(
        "--exclude-role",
        metavar="ROLE_ID",
        action="append",
        default=[],
        help="skip utterances from this participant; repeatable",
    )
    p.add_argument("--loop", action="store_true",
                   help="replay scenarios indefinitely after the first pass")
    p.add_argument("--loop-delay", type=float, default=6.0,
                   help="seconds to pause between loop replays (default 6)")
    p.add_argument("--step-delay", type=float, default=0.4)
    p.add_argument("--warmup", type=float, default=0.5)
    p.add_argument("--domains-dir", default="domains")
    p.add_argument("--prompts-dir", default="prompts")
    asyncio.run(amain(p.parse_args()))


if __name__ == "__main__":
    main()
