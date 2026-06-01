"""Standalone improvement process — runs one improve cycle and publishes results
to the bus so the live dashboard visualises the before/after curves.

Usage
-----
    python -m engine.proc_improve --domain <name>

    # with redis bus:
    KNOTCH_BUS=redis python -m engine.proc_improve --domain <name>

    # overwrite the original routing_policy.md with the revised fragment (opt-in):
    python -m engine.proc_improve --domain <name> --apply

Domain-agnostic: no domain literals appear in this file. Everything domain-
specific comes from the loaded pack (rubric, fragment, scenarios).
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from adapters import factory
from engine.envfile import load_env
from engine.eval_runner import run_eval
from engine.optimizer import improve_rounds, optimizer_model_name
from engine.interfaces import Envelope, TYPE_CURVES
from engine.domain_loader import load_pack


async def amain(args: argparse.Namespace) -> None:
    load_env()

    pack = load_pack(
        args.domain,
        domains_dir=args.domains_dir,
        prompts_dir=args.prompts_dir,
    )

    bus = factory.make_bus()

    optimizer = optimizer_model_name()

    # Select the scoring source. Default is the honest LOCAL rubric scorer;
    # --cekura opts into the real Cekura-in-loop judge (adapters/cekura_score.py).
    score_source = None
    if args.cekura:
        from adapters.cekura_score import CekuraScoreSource

        score_source = CekuraScoreSource()
        print(f"[improve] scoring via {score_source.scored_by}", flush=True)

    print(
        f"[improve] starting · domain={pack.domain} "
        f"scenarios={len(pack.scenarios)} optimizer={optimizer} rounds={args.rounds}",
        flush=True,
    )

    # Run the multi-round improve loop (baseline → N×(reflect → re-score), keep best).
    res = await improve_rounds(
        pack,
        rounds=args.rounds,
        prompts_dir=args.prompts_dir,
        score_source=score_source,
    )

    before = res["before"]
    after  = res["after"]

    # ── Print per-round progression to stdout ────────────────────────────────
    history = res.get("history", [])
    if history:
        print("\n[improve] per-round progression (round 0 = baseline):", flush=True)
        for h in history:
            print(
                f"  round {h['round']}:  "
                f"misroute={h['misroute']:.4f}  missed={h['missed']:.4f}  "
                f"time_to_action={h['time_to_action']:.1f} ms",
                flush=True,
            )
        print(f"  → kept best of {res.get('rounds', 1)} round(s)", flush=True)

    # ── Print results to stdout ──────────────────────────────────────────────
    print(
        f"\n[improve] DONE  domain={pack.domain}\n"
        f"  scored_by:       {res['scored_by']}\n"
        f"  optimizer_model: {res['optimizer_model']}\n"
        f"  misroute:        {before['misroute']:.4f}  →  {after['misroute']:.4f}\n"
        f"  missed:          {before['missed']:.4f}  →  {after['missed']:.4f}\n"
        f"  time_to_action:  {before['time_to_action']:.1f} ms  →  {after['time_to_action']:.1f} ms\n"
        f"  revised_path:    {res.get('revised_path', '?')}\n"
        f"  prompt_diff:\n{res['prompt_diff']}",
        flush=True,
    )

    # If the scoring source surfaced judge explanations (Cekura), show a sample.
    explanations = res.get("score_explanations") or []
    if explanations:
        print("\n[improve] sample judge explanations (from score source):", flush=True)
        for ex in explanations[:3]:
            print(f"  - {ex}", flush=True)

    # ── Optionally apply the revision over the original routing_policy.md ──────
    if args.apply:
        original = Path(pack.path) / "routing_policy.md"
        original.write_text(res["revised_fragment"], encoding="utf-8")
        print(f"[improve] --apply: overwrote {original}", flush=True)

    # ── Assemble the curves payload (published live + optionally saved to disk) ──
    curves_payload = {
        "before": before,
        "after":  after,
        "prompt_diff":     res["prompt_diff"],
        "scored_by":       res["scored_by"],
        "optimizer_model": res["optimizer_model"],
        # Full per-round history; rounds=1 yields a 2-point [baseline, after] trace.
        "rounds":          res.get("rounds", 1),
        "history":         res.get("history", []),
        # Judge explanations for baseline failures, displayed alongside the diff.
        "flagged":         (res.get("score_explanations") or [])[:5],
        # REAL per-metric Cekura scores (pass_pct + per-scenario judge
        # explanations). Present only on --cekura runs; empty/absent for
        # local-rubric runs (the dashboard hides the block when empty).
        "cekura_metric_scores": res.get("cekura_metric_scores") or [],
    }

    # ── Save the full payload to a local cache for instant replay (opt-in) ───
    if args.save is not None:
        save_path = Path(args.save or f"runs/improve_{pack.domain}.json")
        save_path.parent.mkdir(parents=True, exist_ok=True)
        save_path.write_text(json.dumps(curves_payload, indent=2), encoding="utf-8")
        print(f"[improve] saved → {save_path}", flush=True)

    # ── Publish curves envelope to the bus (consumed by the improvement dashboard) ──
    curves_env = Envelope(type=TYPE_CURVES, payload=curves_payload, domain=pack.domain)
    await bus.publish(curves_env)
    print("[improve] curves envelope published to bus", flush=True)

    # ── Publish eval-score envelopes so outcome badges appear on cards ───────
    # run_eval emits one EvalScore envelope per decision when eval_bus is given.
    await run_eval(
        pack,
        prompt_fragment=res["revised_fragment"],
        prompts_dir=args.prompts_dir,
        eval_bus=bus,
    )
    print("[improve] eval_score envelopes published to bus", flush=True)

    await bus.close()


def main() -> None:
    p = argparse.ArgumentParser(prog="python -m engine.proc_improve")
    p.add_argument("--domain",      required=True,    help="Domain name to improve")
    p.add_argument("--domains-dir", default="domains", help="Path to domains directory")
    p.add_argument("--prompts-dir", default="prompts", help="Path to prompts directory")
    p.add_argument(
        "--rounds",
        type=int,
        default=1,
        help="Number of reflect→rewrite→re-score rounds; keeps the best "
             "fragment (default 1 = single cycle, identical to before).",
    )
    p.add_argument(
        "--apply",
        action="store_true",
        help="Overwrite domains/<domain>/routing_policy.md with the revised fragment "
             "(off by default; the revised sibling is always written regardless).",
    )
    p.add_argument(
        "--save",
        nargs="?",
        const="",
        default=None,
        metavar="PATH",
        help="After the cycle, write the full curves payload to PATH as JSON for "
             "instant replay via engine.proc_curves. Bare --save uses "
             "runs/improve_<domain>.json. Off by default.",
    )
    p.add_argument(
        "--cekura",
        action="store_true",
        help="Score the cycle with Cekura (real in-loop judge) instead of the "
             "local rubric scorer. Requires CEKURA_* env vars. Default is local.",
    )
    asyncio.run(amain(p.parse_args()))


if __name__ == "__main__":
    main()
