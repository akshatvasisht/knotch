"""Cekura-in-loop scoring source — a real ScoreSource behind the clean seam.

This module implements ``engine.optimizer.ScoreSource`` against the Cekura REST
API (https://api.cekura.ai). It is a drop-in alternative to the default
``LocalRubricScoreSource``: same call shape, an honest ``scored_by`` label of
``"Cekura (project <id>)"``, and a run_eval-shaped return dict.

How it scores (no live conversational agent required):
  1. Reuse ``engine.eval_runner.run_eval`` to replay the pack's scenarios
     through the coordinator and collect per-decision ``records``.
  2. Render each scenario's decisions as a Cekura transcript (the spoken
     utterance is the "Testing Agent" turn; the coordinator's routing decision is
     the "Main Agent" turn).
  3. Ensure one Cekura ``llm_judge`` metric exists per *binary* rubric metric
     (created from the rubric metric's own ``name`` + ``description`` — no
     domain literal lives here; the vocabulary comes from ``pack.rubric``).
  4. Ingest one call log per scenario, trigger evaluation, poll the call log
     until the metric results land.
  5. Map Cekura's per-metric ``score`` + ``explanation`` back onto the records:
     the failure flags (misroute / missed) are taken from the LOCAL replay (the
     ground truth of what the coordinator actually routed), and Cekura's
     ``explanation`` text is *attached* to each failing record so
     ``build_feedback`` can cite Cekura's judge verbatim in the reflection step.

Domain-agnostic: this file contains no domain literal. Every metric name and
description is read from ``pack.rubric``; roles are opaque ids from the records.
The generality gate (tests/test_no_domain_strings.py) enforces this.

Config (from .env, never hardcoded):
  CEKURA_API_KEY      — X-CEKURA-API-KEY auth header value (required)
  CEKURA_BASE_URL     — REST base (default https://api.cekura.ai)
  CEKURA_PROJECT_ID   — project id metrics + call logs live under (required)
  CEKURA_AGENT_ID     — a registered observation agent id to attach logs to
                        (required; a self-hosted observe-only agent is fine)
"""
from __future__ import annotations

import asyncio
import os
import time
from typing import Optional

from engine.interfaces import DomainPack
from engine.eval_runner import run_eval


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DEFAULT_BASE_URL = "https://api.cekura.ai"

# Cekura's binary llm_judge metrics return a small ordinal score. Observed:
# 0 == the judged property is ABSENT/FALSE (e.g. "a route was missed"),
# 5 == the judged property holds/TRUE (e.g. "nothing was missed"). We treat
# anything below this threshold as a failing judgement for that metric.
_PASS_THRESHOLD = 2.5

# How long to wait for async metric evaluation to land on a call log.
_POLL_INTERVAL_S = 4.0
_POLL_TIMEOUT_S = 90.0


def _require_env(name: str) -> str:
    val = os.getenv(name)
    if not val:
        raise RuntimeError(
            f"{name} is not set. The Cekura scoring source reads endpoints and "
            f"ids from .env — nothing is hardcoded. Set {name} and retry."
        )
    return val


# ---------------------------------------------------------------------------
# Rubric → judged-metric selection (domain-agnostic, reads pack.rubric)
# ---------------------------------------------------------------------------

#: Internal outcome flags the local replay emits per decision. Each maps to one
#: rubric metric *role*: the metric describing a missed route, and the metric
#: describing an irrelevant (misrouted) delivery. We discover which rubric
#: metric plays each role by matching the generic flag name against the rubric's
#: metric names, with a graceful fallback — exactly mirroring improve.py.
def _judged_metrics(rubric: dict) -> list[dict]:
    """Return the binary rubric metrics to send to Cekura as llm_judge metrics.

    Skips numeric metrics (e.g. a latency metric) — those are measured locally
    from wall-clock timing, not judged from a transcript. Each returned dict is
    the rubric metric verbatim: ``{name, description, type, ...}``.
    """
    out: list[dict] = []
    for m in rubric.get("metrics", []):
        if not isinstance(m, dict):
            continue
        if str(m.get("type", "")).strip().lower() == "numeric":
            continue
        if not m.get("name"):
            continue
        out.append(m)
    return out


def _flag_for_metric(metric_name: str, rubric: dict) -> Optional[str]:
    """Map a rubric metric name to the internal failure flag it scores.

    The local replay tags each decision with ``misroute`` and/or ``missed``.
    A rubric typically names these 'ignored' (irrelevant route == misroute) and
    'missed'. We return which internal flag a given rubric metric corresponds to
    so Cekura's judgement on that metric can be reconciled with the local flags.
    Returns None for metrics that don't correspond to a failure flag (e.g. a
    positive 'acted_on' metric), which are still scored but don't drive rates.
    """
    name = metric_name.strip().lower()
    if name in ("missed",):
        return "missed"
    if name in ("ignored", "misroute"):
        return "misroute"
    return None


# ---------------------------------------------------------------------------
# Transcript rendering (records → Cekura cekura-format transcript)
# ---------------------------------------------------------------------------

def _render_transcript(scenario_records: list[dict]) -> list[dict]:
    """Render one scenario's decisions as a Cekura ``cekura``-format transcript.

    Each routable utterance becomes a "Testing Agent" turn carrying the speaker
    role id and the spoken text; the coordinator's resulting routing decision
    becomes a "Main Agent" turn stating source → recipients. Missed-route
    records (which have no spoken utterance) are surfaced as a Testing Agent
    line so the judge can see the expectation that went unrouted.
    """
    turns: list[dict] = []
    t = 0.0
    for rec in scenario_records:
        source = rec.get("source", "")
        utt = rec.get("utt_text", "") or ""
        recipients = rec.get("recipients", []) or []
        if rec.get("missed"):
            # No actual decision/utterance — represent the unmet expectation.
            turns.append({
                "role": "Testing Agent",
                "content": (
                    f"[{source}] (critical) expected this to be routed to "
                    f"{recipients} but it was never delivered."
                ),
                "start_time": t,
                "end_time": t + 2.0,
            })
            t += 2.0
            continue
        if utt:
            turns.append({
                "role": "Testing Agent",
                "content": f"[{source}]: {utt}",
                "start_time": t,
                "end_time": t + 2.0,
            })
            t += 2.0
        if recipients:
            decision = (
                f"ROUTING DECISION: source={source} recipients={list(recipients)}"
            )
        else:
            decision = (
                f"ROUTING DECISION: source={source} recipients=[] "
                f"(held, not relevant)"
            )
        turns.append({
            "role": "Main Agent",
            "content": decision,
            "start_time": t,
            "end_time": t + 2.0,
        })
        t += 2.0
    return turns


# ---------------------------------------------------------------------------
# Cekura REST client (thin; uses urllib so there's no new dependency)
# ---------------------------------------------------------------------------

class _CekuraClient:
    def __init__(self, *, base_url: str, api_key: str, project_id: int) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.project_id = project_id

    async def _request(self, method: str, path: str, body: Optional[dict] = None) -> dict:
        """Run a blocking urllib request off the event loop."""
        return await asyncio.to_thread(self._request_sync, method, path, body)

    def _request_sync(self, method: str, path: str, body: Optional[dict]) -> dict:
        import json as _json
        import urllib.error
        import urllib.request

        url = f"{self.base_url}{path}"
        data = _json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("X-CEKURA-API-KEY", self.api_key)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:  # surface body for debugging
            detail = exc.read().decode("utf-8", "replace")[:400]
            raise RuntimeError(
                f"Cekura {method} {path} failed: HTTP {exc.code}: {detail}"
            ) from exc
        if not raw:
            return {}
        return _json.loads(raw)

    # --- metrics ---------------------------------------------------------- #
    async def list_metrics(self) -> list[dict]:
        res = await self._request(
            "GET", f"/test_framework/v1/metrics/?project={self.project_id}"
        )
        return res if isinstance(res, list) else res.get("results", [])

    async def create_llm_judge_metric(self, *, name: str, description: str) -> dict:
        return await self._request(
            "POST",
            "/test_framework/v1/metrics/",
            {
                "name": name,
                "description": description,
                "type": "llm_judge",
                "eval_type": "binary_qualitative",
                "project": self.project_id,
                "observability_enabled": True,
            },
        )

    # --- call logs -------------------------------------------------------- #
    async def ingest_call_log(
        self, *, agent_id: int, call_id: str, transcript: list[dict]
    ) -> dict:
        return await self._request(
            "POST",
            "/observability/v1/observe/",
            {
                "agent": agent_id,
                "call_id": call_id,
                "transcript_type": "cekura",
                "transcript_json": transcript,
                "call_ended_reason": "completed",
            },
        )

    async def evaluate_metrics(self, *, call_log_ids: list[int], metric_ids: list[int]) -> None:
        await self._request(
            "POST",
            "/observability/v1/call-logs-external/evaluate_metrics/",
            {
                "call_logs": call_log_ids,
                "metrics": metric_ids,
                "project_id": self.project_id,
            },
        )

    async def retrieve_call_log(self, call_log_id: int) -> dict:
        return await self._request(
            "GET", f"/observability/v1/call-logs-external/{call_log_id}/"
        )

    async def wait_until_ready(self, call_log_ids: list[int]) -> None:
        """Block until every call log has left the initial 'evaluating' state.

        Cekura rejects an explicit evaluate-metrics trigger while a freshly
        ingested log is still in its first evaluation pass (HTTP 400 "still
        being evaluated"). We poll each log's top-level status until it settles.
        """
        pending = set(call_log_ids)
        deadline = time.monotonic() + _POLL_TIMEOUT_S
        while pending and time.monotonic() < deadline:
            for lid in list(pending):
                log = await self.retrieve_call_log(lid)
                if log.get("status") in ("success", "failed", "completed"):
                    pending.discard(lid)
            if pending:
                await asyncio.sleep(_POLL_INTERVAL_S)

    async def poll_metric_results(
        self, call_log_id: int, metric_ids: set[int]
    ) -> dict[int, dict]:
        """Poll a call log until the requested metric ids have landed.

        Returns ``{metric_id: {score, explanation}}`` for whichever requested
        metrics appeared before the timeout (may be a subset on timeout).
        """
        deadline = time.monotonic() + _POLL_TIMEOUT_S
        found: dict[int, dict] = {}
        while time.monotonic() < deadline:
            log = await self.retrieve_call_log(call_log_id)
            metrics = (log.get("evaluation") or {}).get("metrics", []) or []
            for m in metrics:
                if m.get("id") in metric_ids and m.get("id") not in found:
                    expl = m.get("explanation")
                    if isinstance(expl, list):
                        expl = " ".join(str(e) for e in expl)
                    found[m["id"]] = {
                        "score": m.get("score"),
                        "explanation": expl or "",
                    }
            if metric_ids.issubset(found.keys()):
                return found
            await asyncio.sleep(_POLL_INTERVAL_S)
        return found


# ---------------------------------------------------------------------------
# The ScoreSource implementation
# ---------------------------------------------------------------------------

class CekuraScoreSource:
    """Score one improvement cycle with Cekura as the judge (real, in-loop).

    Satisfies ``engine.optimizer.ScoreSource``. The failure *rates* and timing
    come from the local replay (ground truth of what the coordinator routed); the
    per-failure *explanations* come from Cekura's llm_judge metrics built from
    the active rubric. The honest label is ``"Cekura (project <id>)"``.
    """

    def __init__(
        self,
        *,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        project_id: Optional[int] = None,
        agent_id: Optional[int] = None,
    ) -> None:
        self._base_url = base_url or os.getenv("CEKURA_BASE_URL", DEFAULT_BASE_URL)
        self._api_key = api_key or _require_env("CEKURA_API_KEY")
        self._project_id = int(project_id or _require_env("CEKURA_PROJECT_ID"))
        self._agent_id = int(agent_id or _require_env("CEKURA_AGENT_ID"))
        self._client = _CekuraClient(
            base_url=self._base_url,
            api_key=self._api_key,
            project_id=self._project_id,
        )

    @property
    def scored_by(self) -> str:
        return f"Cekura (project {self._project_id})"

    async def _ensure_metrics(self, rubric: dict) -> dict[str, int]:
        """Create-or-reuse a Cekura llm_judge metric per binary rubric metric.

        Metrics are named ``"coordinator_<rubric_metric_name>"`` so they're stable
        across cycles (reused, not duplicated) and namespaced away from the
        project's predefined metrics. Returns ``{rubric_metric_name: cekura_id}``.
        """
        judged = _judged_metrics(rubric)
        existing = {m.get("name"): m.get("id") for m in await self._client.list_metrics()}
        name_to_id: dict[str, int] = {}
        for metric in judged:
            rname = metric["name"]
            cekura_name = f"coordinator_{rname}"
            if cekura_name in existing and existing[cekura_name]:
                name_to_id[rname] = existing[cekura_name]
                continue
            desc = self._judge_prompt(metric)
            created = await self._client.create_llm_judge_metric(
                name=cekura_name, description=desc
            )
            name_to_id[rname] = created["id"]
        return name_to_id

    @staticmethod
    def _judge_prompt(metric: dict) -> str:
        """Build an llm_judge description from a rubric metric (generic).

        The transcript shows Testing-Agent utterances (each prefixed with the
        speaker role id) and Main-Agent ROUTING DECISION lines. The prompt asks
        the judge to score the rubric metric's property TRUE/FALSE and explain
        which decision triggered the judgement, citing the role ids it sees.
        """
        name = metric.get("name", "metric")
        description = metric.get("description", "")
        return (
            f"You are auditing a message-routing coordinator from a transcript. "
            f"Testing Agent lines are spoken utterances prefixed with the "
            f"speaker's role id in brackets. Main Agent lines are the coordinator's "
            f"ROUTING DECISION for the preceding utterance, listing the source "
            f"role and the recipient role ids it routed to (an empty recipients "
            f"list means the coordinator held the message as not relevant).\n\n"
            f"Metric '{name}': {description}\n\n"
            f"Score TRUE if the routing in this transcript is correct with "
            f"respect to this metric, FALSE if it is violated. In your "
            f"explanation, quote the specific utterance and name the role ids "
            f"involved so an engineer can fix the routing rule."
        )

    async def score(
        self,
        pack: DomainPack,
        *,
        prompt_fragment: Optional[str] = None,
        prompts_dir: str = "prompts",
    ) -> dict:
        # Step 1 — replay locally to get the ground-truth routing decisions.
        local = await run_eval(
            pack, prompt_fragment=prompt_fragment, prompts_dir=prompts_dir
        )
        records: list[dict] = local["records"]

        # Step 2 — ensure the rubric's binary metrics exist in Cekura.
        metric_ids_by_name = await self._ensure_metrics(pack.rubric)
        if not metric_ids_by_name:
            # Nothing judgeable in the rubric — return the local result as-is,
            # still under the Cekura label (it WAS submitted; nothing to judge).
            return local
        cekura_metric_ids = set(metric_ids_by_name.values())

        # Group records by scenario so each call log is one coherent transcript.
        by_scenario: dict[str, list[dict]] = {}
        for rec in records:
            by_scenario.setdefault(rec.get("scenario_id", ""), []).append(rec)

        # Step 3 — ingest one call log per scenario, then trigger evaluation.
        run_tag = str(int(time.time()))
        call_logs: list[tuple[str, int, list[dict]]] = []  # (scenario_id, log_id, recs)
        for scenario_id, recs in by_scenario.items():
            transcript = _render_transcript(recs)
            if not transcript:
                continue
            ingest = await self._client.ingest_call_log(
                agent_id=self._agent_id,
                call_id=f"coordinator-{scenario_id}-{run_tag}",
                transcript=transcript,
            )
            log_id = ingest.get("id")
            if log_id is None:
                continue
            call_logs.append((scenario_id, log_id, recs))

        if call_logs:
            log_ids = [lid for _, lid, _ in call_logs]
            # Wait for the ingest's own first pass to settle, then trigger an
            # explicit evaluation of our rubric metrics (the reliable path —
            # ingest-time auto-eval of custom metrics is not guaranteed).
            await self._client.wait_until_ready(log_ids)
            await self._client.evaluate_metrics(
                call_log_ids=log_ids,
                metric_ids=list(cekura_metric_ids),
            )

        # Step 4 — poll each call log and attach Cekura's explanation to records.
        # We map rubric-metric → internal failure flag, and for each scenario
        # whose records carry that flag, stamp Cekura's explanation onto them.
        name_by_id = {v: k for k, v in metric_ids_by_name.items()}
        cekura_explanations: list[str] = []
        # Per-metric aggregation for the dashboard's Cekura-scores block. Keyed by
        # the rubric metric name; values accumulate pass/total counts + the
        # judge's per-scenario explanation. Domain-agnostic: names come from the
        # rubric/Cekura data, never hardcoded.
        metric_agg: dict[str, dict] = {}
        for scenario_id, log_id, recs in call_logs:
            results = await self._client.poll_metric_results(log_id, cekura_metric_ids)
            for cekura_id, payload in results.items():
                rubric_name = name_by_id.get(cekura_id, "")
                flag = _flag_for_metric(rubric_name, pack.rubric)
                explanation = payload.get("explanation", "")
                score = payload.get("score")
                # Audio-dependent / un-scored metrics return null — skip them
                # entirely so the dashboard only shows real llm_judge results.
                if not isinstance(score, (int, float)):
                    continue
                judged_pass = score >= _PASS_THRESHOLD
                judged_fail = not judged_pass

                agg = metric_agg.setdefault(
                    rubric_name,
                    {"name": rubric_name, "pass_count": 0, "total": 0, "explanations": []},
                )
                agg["total"] += 1
                if judged_pass:
                    agg["pass_count"] += 1
                if explanation:
                    agg["explanations"].append({
                        "scenario": scenario_id,
                        "score": score,
                        "text": explanation,
                    })

                if not explanation:
                    continue
                for rec in recs:
                    # Attach the judge's text to the records this metric covers.
                    # If the metric maps to a failure flag, only stamp the
                    # records carrying that flag; otherwise stamp all records.
                    if flag is None or rec.get(flag):
                        notes = rec.setdefault("cekura_notes", [])
                        notes.append(
                            f"[{rubric_name} score={score}] {explanation}"
                        )
                if judged_fail and explanation:
                    cekura_explanations.append(
                        f"{rubric_name}: {explanation}"
                    )

        # Finalise the per-metric aggregates: compute pass_pct and keep only
        # metrics that actually produced at least one scored result.
        cekura_metric_scores: list[dict] = []
        for agg in metric_agg.values():
            if agg["total"] <= 0:
                continue
            agg["pass_pct"] = round(100.0 * agg["pass_count"] / agg["total"], 1)
            cekura_metric_scores.append(agg)
        cekura_metric_scores.sort(key=lambda a: a["name"])

        # Step 5 — return a run_eval-shaped dict. Rates + timing are the local
        # ground truth; records now also carry Cekura's per-failure explanation
        # text (in rec['cekura_notes']) for build_feedback to cite.
        result = {
            "misroute": local["misroute"],
            "missed": local["missed"],
            "time_to_action": local["time_to_action"],
            "records": records,
        }
        # Surface the collected judge explanations so callers/diagnostics can
        # show a sample of Cekura's reasoning.
        result["cekura_explanations"] = cekura_explanations
        # Surface the REAL per-metric aggregates (pass_count/total/pass_pct +
        # per-scenario explanations) so the dashboard can render named,
        # explained pass-rate bars. Empty list when nothing was scored.
        result["cekura_metric_scores"] = cekura_metric_scores
        return result
