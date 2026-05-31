"""`python -m engine --domain <name>` — terminal demo runner.

Wires the full loop on fakes, zero external services:
  inject scripted utterances -> STT -> turn gate -> triage -> convener (fake LLM)
  -> bus:routed -> output workers self-select -> dispatcher TTS (printed).

The engine never references any domain; everything domain-specific comes from
domains/<name>/ via --domain. A terminal renderer subscribes to the bus and
prints the trace (it is a pure bus subscriber — it never drives behavior).
"""
from __future__ import annotations

import argparse
import asyncio
import sys

import json

from engine.convener_worker import ConvenerWorker
from engine.envfile import load_env
from engine.interfaces import (
    ALL_CHANNELS,
    CHAN_ROUTED,
    CHAN_TURN,
    CHAN_UTTERANCES,
    RoutingDecision,
    TurnEvent,
    Utterance,
)
from engine.packloader import assemble_system_prompt, load_pack, load_scaffold
from engine.voice_worker import VoiceWorker
from adapters import factory

# ANSI
DIM = "\033[2m"
BOLD = "\033[1m"
CYAN = "\033[96m"
YEL = "\033[93m"
MAG = "\033[95m"
RED = "\033[91m"
RST = "\033[0m"


class TerminalRenderer:
    """Pure bus subscriber that prints the live trace + tallies a summary."""

    def __init__(self, bus, names: dict[str, str]) -> None:
        self.bus = bus
        self.names = names
        self.routed = 0
        self.held = 0
        self.dropped = 0
        self.utterances = 0

    def name(self, role_id: str) -> str:
        return self.names.get(role_id, role_id)

    async def run_utterances(self) -> None:
        async for env in self.bus.subscribe(CHAN_UTTERANCES):
            u = Utterance.from_dict(env.payload)
            self.utterances += 1
            print(
                f"{BOLD}{CYAN}🎙  {self.name(u.participant)}{RST}: "
                f"{u.text}  {DIM}[{u.triage_class}]{RST}"
            )

    async def run_routed(self) -> None:
        async for env in self.bus.subscribe(CHAN_ROUTED):
            d = RoutingDecision.from_dict(env.payload)
            if d.recipients:
                self.routed += 1
                to = ", ".join(self.name(r) for r in d.recipients)
                print(
                    f"   {MAG}🧠 route{RST} {self.name(d.source)} → [{to}]  "
                    f"{DIM}({d.signal_type}/{d.urgency}) why: {d.rationale}{RST}"
                )
            else:
                self.held += 1
                print(
                    f"   {DIM}🧠 held — {self.name(d.source)}: {d.rationale}{RST}"
                )

    async def run_turn(self) -> None:
        async for env in self.bus.subscribe(CHAN_TURN):
            ev = TurnEvent.from_dict(env.payload)
            if ev.state == "dropped_backchannel":
                self.dropped += 1
                print(f"   {DIM}· backchannel dropped ({self.name(ev.participant)}){RST}")

    def summary(self) -> str:
        return (
            f"{BOLD}summary{RST}: {self.utterances} utterances · "
            f"{MAG}{self.routed} routed{RST} · {self.held} held · "
            f"{self.dropped} backchannels dropped"
        )


async def run_scenario(scenario: dict, transports: dict, step_delay: float) -> None:
    sid = scenario.get("id", "?")
    print(f"\n{BOLD}{YEL}━━ scenario: {sid} ━━{RST}")
    for line in scenario.get("utterances", []):
        participant = line.get("participant")
        text = line.get("text", "")
        t = transports.get(participant)
        if t is None:
            print(f"{RED}!! unknown participant '{participant}' in scenario{RST}")
            continue
        t.inject(text, final=True)
        await asyncio.sleep(step_delay)  # let the loop flow before the next turn


async def record_bus(bus, path: str) -> None:
    """Tap every channel and append envelopes as JSONL — an event log for offline replay."""
    f = open(path, "w", encoding="utf-8")
    try:
        async def drain(ch: str) -> None:
            async for env in bus.subscribe(ch):
                f.write(json.dumps(env.to_dict()) + "\n")
                f.flush()
        await asyncio.gather(*[drain(ch) for ch in ALL_CHANNELS])
    finally:
        f.close()


async def amain(args: argparse.Namespace) -> int:
    load_env()  # endpoints/keys from .env (never hardcoded)
    try:
        pack = load_pack(
            args.domain, domains_dir=args.domains_dir, prompts_dir=args.prompts_dir
        )
        scaffold = load_scaffold(prompts_dir=args.prompts_dir)
    except (ValueError, FileNotFoundError) as exc:
        print(f"{RED}pack load failed: {exc}{RST}", file=sys.stderr)
        return 2

    system_prompt = assemble_system_prompt(scaffold, pack)
    names = {r.role_id: r.display_name for r in pack.roles}

    if args.list_scenarios:
        print(f"{BOLD}{pack.display_name}{RST} scenarios:")
        for s in pack.scenarios:
            print(f"  - {s.get('id')}  ({len(s.get('utterances', []))} utterances)")
        return 0

    # Validate --scenario before allocating bus or spawning tasks.
    if args.scenario and not any(s.get("id") == args.scenario for s in pack.scenarios):
        print(f"{RED}no scenario '{args.scenario}' in {pack.domain}{RST}")
        return 2

    backends = (
        f"stt={factory._backend('CONVENER_STT')} "
        f"tts={factory._backend('CONVENER_TTS')} "
        f"llm={factory._backend('CONVENER_LLM')} "
        f"bus={factory._backend('CONVENER_BUS')} "
        f"turn={factory._backend('CONVENER_TURN')}"
    )
    print(
        f"{BOLD}CONVENER{RST} · domain={MAG}{pack.domain}{RST} "
        f"({pack.display_name}) · N={len(pack.roles)} "
        f"[{', '.join(names.values())}]\n{DIM}backends: {backends}{RST}"
    )

    bus = factory.make_bus()
    llm = factory.make_llm()
    convener = ConvenerWorker(
        bus=bus, llm=llm, system_prompt=system_prompt,
        participants=pack.roles, domain=pack.domain,
    )

    transports: dict = {}
    workers: list[VoiceWorker] = []
    for role in pack.roles:
        transport = factory.make_transport(role)
        transports[role.role_id] = transport
        workers.append(
            VoiceWorker(
                role=role,
                transport=transport,
                stt=factory.make_stt(),
                tts=factory.make_tts(label_for=names.get),
                bus=bus,
                dispatcher_voice_id=pack.dispatcher_voice_id,
                gate=factory.make_gate(),
                domain=pack.domain,
            )
        )

    renderer = TerminalRenderer(bus, names)

    # Register the renderer's bus:routed subscriber before the workers' output paths
    # so "route" lines print before the downstream TTS lines.
    tasks = [
        asyncio.create_task(renderer.run_utterances()),
        asyncio.create_task(renderer.run_routed()),
        asyncio.create_task(renderer.run_turn()),
    ]
    await asyncio.sleep(0)

    if args.record:
        tasks.append(asyncio.create_task(record_bus(bus, args.record)))

    if args.dashboard:
        from engine.dashboard.app import attach_dashboard
        tasks.append(
            asyncio.create_task(
                attach_dashboard(bus, pack, port=args.dashboard_port)
            )
        )

    tasks.append(asyncio.create_task(convener.run()))
    for w in workers:
        tasks.append(asyncio.create_task(w.run()))
    # Let all subscriptions register before driving. Redis SUBSCRIBE has network
    # latency (and no backlog), so give it a real grace; negligible for memory.
    grace = 0.6 if factory._backend("CONVENER_BUS") == "redis" else 0.05
    await asyncio.sleep(grace)

    if args.dashboard:
        print(
            f"{BOLD}{CYAN}dashboard live → http://localhost:{args.dashboard_port}{RST}"
            f"  {DIM}(open it, then watch the scenarios stream){RST}"
        )
        await asyncio.sleep(args.dashboard_warmup)

    # Pick scenarios to drive.
    scenarios = pack.scenarios
    if args.scenario:
        scenarios = [s for s in scenarios if s.get("id") == args.scenario]
    elif not args.all_scenarios:
        scenarios = scenarios[:1]

    for scenario in scenarios:
        await run_scenario(scenario, transports, args.step_delay)

    # Wait for the convener to finish all in-flight routing before teardown.
    # The real LLM call has network latency; decisions published after bus.close()
    # would be dropped, so we drain until every published utterance has a decision
    # (routed or held), bounded by a per-utterance time budget.
    await asyncio.sleep(0.3)  # let all utterances reach the bus first
    expected = renderer.utterances
    budget = max(20.0, expected * 8.0)
    waited = 0.0
    while (renderer.routed + renderer.held) < expected and waited < budget:
        await asyncio.sleep(0.1)
        waited += 0.1
    await asyncio.sleep(0.3)  # flush final TTS prints

    if args.dashboard:
        print(
            f"{DIM}dashboard still live at http://localhost:{args.dashboard_port} "
            f"— Ctrl-C to exit.{RST}"
        )
        try:
            await asyncio.Event().wait()  # keep serving until interrupted
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass

    for t in transports.values():
        t.close()
    await asyncio.sleep(0.05)
    await bus.close()
    await asyncio.gather(*tasks, return_exceptions=True)

    print(f"\n{renderer.summary()}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(prog="python -m engine", description="Knotch")
    p.add_argument("--domain", required=True, help="domain pack name under domains/")
    p.add_argument("--scenario", help="run a single scenario by id")
    p.add_argument("--all-scenarios", action="store_true", help="run every scenario")
    p.add_argument("--list-scenarios", action="store_true")
    p.add_argument("--domains-dir", default="domains")
    p.add_argument("--prompts-dir", default="prompts")
    p.add_argument("--step-delay", type=float, default=0.15)
    p.add_argument("--dashboard", action="store_true",
                   help="serve the live ops dashboard alongside the run")
    p.add_argument("--dashboard-port", type=int, default=7861)
    p.add_argument("--dashboard-warmup", type=float, default=1.5,
                   help="seconds to wait after boot before driving scenarios")
    p.add_argument("--record", metavar="FILE.jsonl",
                   help="record all bus envelopes to JSONL (backup / ?demo=replay)")
    args = p.parse_args()
    return asyncio.run(amain(args))


if __name__ == "__main__":
    raise SystemExit(main())
