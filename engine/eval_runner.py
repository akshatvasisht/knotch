"""Evaluation runner — domain-agnostic.

Replays a DomainPack's scenarios through a ConvenerWorker (backed by the
live LLM endpoint) and scores each routing decision against the scenario's
`expect` labels.

Public API::

    results = await run_eval(pack, prompt_fragment=None)

    # results shape:
    {
      "misroute":        float,   # rate of decisions that routed to unlabelled recipients
      "missed":          float,   # rate of expected routes that were never delivered
      "time_to_action":  float,   # avg ms from utterance publish to matching decision
      "records":         list[dict],  # one per decision
    }

Scoring is generic — no domain literals appear here. Only role_ids (opaque
strings from the pack) are compared; signal/about fields are soft hints only.
"""
from __future__ import annotations

import asyncio
import time
from typing import Optional

from engine.bus import InMemoryBus
from engine.convener_worker import ConvenerWorker
from engine.interfaces import (
    CHAN_ROUTED,
    DomainPack,
    Envelope,
    EvalScore,
    RoutingDecision,
    TYPE_EVAL_SCORE,
    TYPE_UTTERANCE,
    Utterance,
)
from engine.packloader import assemble_system_prompt, load_scaffold
from engine.triage import triage


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _normalise_route_to(value) -> list[str]:
    """Normalise an expect `route_to` value to a list of role_id strings.

    * A non-empty string → [string]
    * An empty string / empty list / None → []
    * A list → filtered list of non-empty strings
    """
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v) for v in value if v]
    s = str(value).strip()
    return [s] if s else []


def _score_scenario(
    scenario: dict,
    decisions: list[tuple[str, RoutingDecision, float]],  # (utterance_text, decision, latency_ms)
) -> list[dict]:
    """Score one scenario's decisions against its `expect` list.

    Matching strategy (generic — no domain knowledge):
    - For each expect entry, look for a decision whose `source` matches
      expect.about (if present) and whose `recipients` overlaps expect.route_to.
    - `signal` is a soft hint — a signal mismatch never hard-fails a match.
    - A decision whose recipients are non-empty but not covered by any expect
      entry is tagged misroute.
    - An expect entry whose route_to is non-empty and was never matched is
      tagged missed.

    Returns a list of per-decision score dicts.
    """
    expect_entries = list(scenario.get("expect", []))
    records: list[dict] = []

    # Track which expect entries have been consumed (to detect missed-routes).
    matched_expect_indices: set[int] = set()

    for utt_text, decision, latency_ms in decisions:
        expected_for_source = [
            (i, e) for i, e in enumerate(expect_entries)
            if str(e.get("about", "")) == decision.source
        ]

        if not decision.recipients:
            # Convener held — check whether any expect for this source wanted
            # an empty route_to (held is correct) or a real route (missed).
            held_expected = [
                (i, e) for i, e in expected_for_source
                if not _normalise_route_to(e.get("route_to"))
            ]
            if held_expected:
                idx = held_expected[0][0]
                matched_expect_indices.add(idx)
                outcome = "acted"  # correct hold
                hold_misroute = False
            else:
                # Convener held but a routing was expected — wrong hold.
                outcome = "misroute"
                hold_misroute = True
            records.append({
                "decision_id": decision.decision_id,
                "scenario_id": scenario.get("id", ""),
                "utt_text": utt_text,
                "source": decision.source,
                "recipients": [],
                "outcome": outcome,
                "time_to_action_ms": None,
                "misroute": hold_misroute,
                "missed": False,
            })
            continue

        # Decision has recipients — consume ALL expect entries whose route_to
        # overlaps actual recipients (one utterance can have multiple expect
        # entries for different recipients).
        # A routed decision is "acted" if it matched at least one expect entry
        # for this source; otherwise it's a misroute. (Consume each matched
        # expect slot so missed-route detection below stays correct.)
        matched = False
        for idx, exp in expected_for_source:
            if idx in matched_expect_indices:
                continue
            exp_recipients = _normalise_route_to(exp.get("route_to"))
            if not exp_recipients:
                # This expect was a "held" expectation; actual decision routed.
                # Don't consume this slot — leave it for the misroute verdict.
                continue
            # Match if any expected recipient appears in actual recipients.
            if any(r in decision.recipients for r in exp_recipients):
                matched_expect_indices.add(idx)
                matched = True
        outcome = "acted" if matched else "ignored"
        misroute = not matched

        records.append({
            "decision_id": decision.decision_id,
            "scenario_id": scenario.get("id", ""),
            "utt_text": utt_text,
            "source": decision.source,
            "recipients": list(decision.recipients),
            "outcome": outcome,
            "time_to_action_ms": int(latency_ms),
            "misroute": misroute,
            "missed": False,
        })

    # Detect missed-routes: expect entries with non-empty route_to that were
    # never matched.
    for idx, exp in enumerate(expect_entries):
        if idx in matched_expect_indices:
            continue
        exp_recipients = _normalise_route_to(exp.get("route_to"))
        if not exp_recipients:
            continue  # "held" expectation — not a missed route
        records.append({
            "decision_id": f"missed-{scenario.get('id', '')}-{idx}",
            "scenario_id": scenario.get("id", ""),
            "utt_text": "",
            "source": str(exp.get("about", "")),
            "recipients": exp_recipients,
            "outcome": "missed",
            "time_to_action_ms": None,
            "misroute": False,
            "missed": True,
        })

    return records


def _aggregate(all_records: list[dict]) -> dict:
    """Aggregate per-decision records into three-metric summary (misroute, missed, time_to_action)."""
    total = len(all_records)
    if total == 0:
        return {"misroute": 0.0, "missed": 0.0, "time_to_action": 0.0}

    misroute_count = sum(1 for r in all_records if r["misroute"])
    missed_count = sum(1 for r in all_records if r["missed"])
    latencies = [
        r["time_to_action_ms"]
        for r in all_records
        if r["time_to_action_ms"] is not None
    ]
    avg_latency = sum(latencies) / len(latencies) if latencies else 0.0

    return {
        "misroute": round(misroute_count / total, 4),
        "missed": round(missed_count / total, 4),
        "time_to_action": round(avg_latency, 1),
    }


# ---------------------------------------------------------------------------
# Core: replay one scenario
# ---------------------------------------------------------------------------

async def _replay_scenario(
    scenario: dict,
    bus: InMemoryBus,
    pack: DomainPack,
) -> list[tuple[str, RoutingDecision, float]]:
    """Inject a scenario's utterances and collect the convener's decisions.

    Returns a list of (utterance_text, RoutingDecision, latency_ms) tuples in
    the order decisions arrive — one decision per routable utterance.
    """
    utterances_raw = scenario.get("utterances", [])
    # Pre-compute which utterances are routable (triage pass).
    routable_utts: list[Utterance] = []
    for raw in utterances_raw:
        text = raw.get("text", "")
        participant = raw.get("participant", "")
        routable, triage_class = triage(text)
        if routable:
            routable_utts.append(
                Utterance(
                    participant=participant,
                    text=text,
                    final=True,
                    triage_class=triage_class,
                    routable=True,
                )
            )

    if not routable_utts:
        return []

    collected: list[tuple[str, RoutingDecision, float]] = []
    utt_publish_times: dict[int, float] = {}  # index → publish_ts

    # Subscribe BEFORE we inject anything so we don't miss fast decisions.
    decision_queue: asyncio.Queue = asyncio.Queue()

    async def _collect_decisions() -> None:
        async for env in bus.subscribe(CHAN_ROUTED):
            decision = RoutingDecision.from_dict(env.payload)
            received_ts = time.monotonic()
            decision_queue.put_nowait((decision, received_ts))

    collector_task = asyncio.create_task(_collect_decisions())

    # Inject utterances onto the bus with a short yield between each to allow
    # the convener worker (running concurrently) to process them in order.
    for i, utt in enumerate(routable_utts):
        publish_ts = time.monotonic()
        utt_publish_times[i] = publish_ts
        await bus.publish(
            Envelope(type=TYPE_UTTERANCE, payload=utt.to_dict(), domain=pack.domain)
        )
        # Yield so the convener worker can start processing this utterance before
        # the next one is injected. LLM network latency is ~1–3 s so we don't
        # need to sleep between utterances — just one event-loop yield.
        await asyncio.sleep(0)

    # Wait for all expected decisions (one per routable utterance), bounded by
    # a generous per-utterance budget to handle network latency.
    per_utt_budget_s = 15.0
    total_budget_s = len(routable_utts) * per_utt_budget_s
    deadline = time.monotonic() + total_budget_s

    next_utt_idx = 0
    while next_utt_idx < len(routable_utts):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            decision, received_ts = await asyncio.wait_for(
                decision_queue.get(), timeout=min(remaining, per_utt_budget_s)
            )
        except asyncio.TimeoutError:
            break

        utt = routable_utts[next_utt_idx]
        publish_ts = utt_publish_times[next_utt_idx]
        latency_ms = (received_ts - publish_ts) * 1000.0
        collected.append((utt.text, decision, latency_ms))
        next_utt_idx += 1

    collector_task.cancel()
    try:
        await collector_task
    except (asyncio.CancelledError, Exception):
        pass

    return collected


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def run_eval(
    pack: DomainPack,
    *,
    prompt_fragment: Optional[str] = None,
    prompts_dir: str = "prompts",
    eval_bus: Optional["Bus"] = None,
) -> dict:
    """Evaluate a domain pack's scenarios against the live LLM endpoint.

    Parameters
    ----------
    pack:
        A fully-loaded DomainPack (use packloader.load_pack).
    prompt_fragment:
        If provided, use this fragment instead of pack.convener_fragment.
        Useful for the hot-reload pass in the improve loop.
    prompts_dir:
        Where to find convener_scaffold.md (default: "prompts").

    eval_bus:
        Optional bus to publish per-decision EvalScore envelopes to (for live
        dashboard outcome badges). Scores are always in the returned `records`.

    Returns
    -------
    dict with keys: misroute, missed, time_to_action (curves), plus `records`
    (list of per-decision score dicts).
    """
    from adapters import factory as _llm_factory

    fragment = prompt_fragment if prompt_fragment is not None else pack.convener_fragment
    scaffold = load_scaffold(prompts_dir=prompts_dir)

    # Assemble a temporary DomainPack with the (possibly overridden) fragment
    # so assemble_system_prompt works correctly.
    import dataclasses
    effective_pack = dataclasses.replace(pack, convener_fragment=fragment)

    system_prompt = assemble_system_prompt(scaffold, effective_pack)

    all_records: list[dict] = []

    for scenario in pack.scenarios:
        # Each scenario gets its own bus + worker so state doesn't bleed across
        # scenarios (mirrors the real session-per-scenario isolation).
        bus = InMemoryBus()
        llm = _llm_factory.make_llm()
        worker = ConvenerWorker(
            bus=bus,
            llm=llm,
            system_prompt=system_prompt,
            participants=pack.roles,
            domain=pack.domain,
        )

        worker_task = asyncio.create_task(worker.run())
        # Give the worker a tick to register its subscriber before injecting.
        await asyncio.sleep(0.02)

        try:
            decisions = await _replay_scenario(scenario, bus, pack)
        finally:
            worker_task.cancel()
            try:
                await worker_task
            except (asyncio.CancelledError, Exception):
                pass
            await bus.close()

        scenario_records = _score_scenario(scenario, decisions)
        all_records.extend(scenario_records)

        # Publish EvalScore envelopes to the caller's eval bus (e.g. a
        # dashboard-attached session bus) so outcome badges can render live.
        # Scores are also returned in the result dict, so they're available
        # even when no bus is wired.
        if eval_bus is not None:
            for rec in scenario_records:
                score = EvalScore(
                    decision_id=rec["decision_id"],
                    outcome=rec["outcome"],
                    time_to_action_ms=rec.get("time_to_action_ms"),
                    metric_scores={
                        "misroute": int(rec["misroute"]),
                        "missed": int(rec["missed"]),
                    },
                )
                await eval_bus.publish(
                    Envelope(
                        type=TYPE_EVAL_SCORE,
                        payload=score.to_dict(),
                        domain=pack.domain,
                    )
                )

    curves = _aggregate(all_records)
    return {**curves, "records": all_records}
