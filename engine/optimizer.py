"""Autonomous improvement loop — one eval-reflect-eval cycle.

This is the self-improvement keystone of the platform. It is fully
domain-agnostic: everything domain-specific (the rubric, the coordinator
fragment, the scenarios) arrives via the loaded ``pack``. No domain literal
appears in this file — the generality gate enforces it.

Public API::

    result = await improve_once(pack)

    # result shape:
    {
      "before":           {"misroute": float, "missed": float, "time_to_action": float},
      "after":            {"misroute": float, "missed": float, "time_to_action": float},
      "prompt_diff":      str,    # human-readable unified-ish diff (old -> new)
      "revised_fragment": str,    # the new coordinator fragment text
      "revised_path":     str,    # domains/<domain>/routing_policy.revised.md (sibling)
      "scored_by":        str,    # honest label of WHAT scored the cycle
      "optimizer_model":  str,    # which model rewrote the fragment
    }

The cycle:
  1. Score the ACTIVE domain against ITS OWN rubric (loaded via pack.rubric).
  2. Reflect on the failures and rewrite the ACTIVE domain's coordinator config.
  3. Re-test with the revised fragment in-process (hot reload, no restart).
  4. Return before/after curves + a per-domain diff for the dashboard.

Two extension seams are intentionally provided:
  * ``reflect(...)`` — a single pure function replaceable with any
    (fragment, feedback) -> fragment optimizer (e.g. dspy.GEPA via a thin adapter).
  * ``ScoreSource`` Protocol — the default implementation scores locally;
    a Cekura source satisfies the same Protocol and is passed via
    ``improve_once(..., score_source=...)`` with no engine change.
"""
from __future__ import annotations

import difflib
import json
import os
import time
from pathlib import Path
from typing import Optional, Protocol

from engine.interfaces import DomainPack
from engine.eval_runner import run_eval


# ---------------------------------------------------------------------------
# Scoring source seam — honest labelling
# ---------------------------------------------------------------------------

#: Default label. The loop scores locally against the active domain's rubric;
#: it does NOT claim Cekura. When a Cekura-in-loop source is wired, it supplies
#: its own honest label via ScoreSource.scored_by.
LOCAL_SCORED_BY = "local rubric"


class ScoreSource(Protocol):
    """Pluggable scoring backend for one improvement cycle.

    The default ``LocalRubricScoreSource`` runs the in-process eval_runner and
    scores against the active domain's rubric. A future Cekura source satisfies
    this same Protocol (different ``scored_by`` label, same call shape) and is
    passed via ``improve_once(..., score_source=...)`` with no engine change.
    """

    @property
    def scored_by(self) -> str:
        """Honest label of what produced the scores (shown on the dashboard)."""
        ...

    async def score(
        self,
        pack: DomainPack,
        *,
        prompt_fragment: Optional[str] = None,
        prompts_dir: str = "prompts",
    ) -> dict:
        """Return run_eval-shaped result: misroute/missed/time_to_action + records."""
        ...


class LocalRubricScoreSource:
    """Score the active domain against ITS OWN rubric using the local eval_runner.

    The scorer (engine.eval_runner) computes misroute / missed / time_to_action
    from each scenario's ``expect`` labels — i.e. the local realisation of the
    rubric's metrics. This is honest: the label says "local rubric", not Cekura.
    """

    @property
    def scored_by(self) -> str:
        return LOCAL_SCORED_BY

    async def score(
        self,
        pack: DomainPack,
        *,
        prompt_fragment: Optional[str] = None,
        prompts_dir: str = "prompts",
    ) -> dict:
        return await run_eval(
            pack, prompt_fragment=prompt_fragment, prompts_dir=prompts_dir
        )


# ---------------------------------------------------------------------------
# Rubric-aware feedback — cite the ACTIVE domain's metric names
# ---------------------------------------------------------------------------

def _rubric_metric_names(rubric: dict) -> dict[str, str]:
    """Map a generic outcome label -> the active rubric's metric name.

    The local scorer emits two failure categories: ``misroute`` and ``missed``.
    A rubric lists its metrics by name; we look those names up so feedback
    strings CITE the active domain's own rubric vocabulary instead of hardcoded
    words. Falls back to the generic label if the rubric doesn't define it.
    """
    names = {m.get("name", "") for m in rubric.get("metrics", []) if isinstance(m, dict)}
    # Map our internal outcome flags to the rubric metric that describes them.
    # 'misroute' is described by the rubric's 'ignored' metric (irrelevant route);
    # 'missed' is described by the rubric's 'missed' metric. We honour whatever
    # the active rubric actually names these — no domain literals, just lookups.
    misroute_metric = "ignored" if "ignored" in names else "misroute"
    missed_metric = "missed" if "missed" in names else "missed"
    return {"misroute": misroute_metric, "missed": missed_metric}


def build_feedback(records: list[dict], rubric: dict) -> list[str]:
    """Build one textual feedback string per failing case, citing rubric metrics.

    Each string names the active rubric's metric and states what was expected
    vs. what was routed — e.g.::

        missed: expected route_to=role_X about=Y per rubric metric 'missed',
        but no decision delivered it.

    Only misroutes and missed-routes are included (the categories reflection is
    asked to fix). Correct holds are omitted.
    """
    metric = _rubric_metric_names(rubric)
    feedback: list[str] = []
    for rec in records:
        if rec.get("misroute"):
            line = (
                f"{metric['misroute']}: source={rec['source']!r} was routed to "
                f"recipients={rec['recipients']!r} but no expect entry matched "
                f"that routing per rubric metric {metric['misroute']!r}. "
                f"Utterance: {rec.get('utt_text', '')!r}"
            )
        elif rec.get("missed"):
            line = (
                f"{metric['missed']}: expected route_to={rec['recipients']!r} "
                f"about={rec['source']!r} per rubric metric {metric['missed']!r}, "
                f"but no decision delivered it."
            )
        else:
            continue
        # If a Cekura (or any) judge attached per-record explanation notes,
        # cite them verbatim so the reflection step gets the judge's reasoning.
        for note in rec.get("cekura_notes", []) or []:
            line += f"\n  judge: {note}"
        feedback.append(line)
    return feedback


# ---------------------------------------------------------------------------
# Optimizer — same OpenAI-compatible endpoint as the routing LLM
# ---------------------------------------------------------------------------

DEFAULT_OPTIMIZER_MODEL = "llm"


def optimizer_model_name() -> str:
    """The model identifier used for policy optimization (reads KNOTCH_LLM_MODEL)."""
    return os.getenv("KNOTCH_LLM_MODEL", DEFAULT_OPTIMIZER_MODEL)


async def optimizer_complete(system: str, user: str, *, model: Optional[str] = None) -> str:
    """Run one reflection completion on the configured LLM endpoint.

    Uses the same KNOTCH_LLM_URL / KNOTCH_LLM_MODEL env vars as the routing
    adapter. `model` is accepted for call-site compatibility but ignored.
    """
    from openai import AsyncOpenAI

    base_url = os.getenv("KNOTCH_LLM_URL")
    if not base_url:
        raise RuntimeError(
            "KNOTCH_LLM_URL is not set. Provide it via .env — endpoints are "
            "never hardcoded."
        )
    llm_model = os.getenv("KNOTCH_LLM_MODEL")
    if not llm_model:
        raise RuntimeError("KNOTCH_LLM_MODEL is not set. Provide it via .env.")
    client = AsyncOpenAI(base_url=base_url, api_key="EMPTY", timeout=60.0)
    resp = await client.chat.completions.create(
        model=llm_model,
        temperature=0.3,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    )
    return resp.choices[0].message.content or ""


def _strip_fences(text: str) -> str:
    """Remove accidental leading markdown fences from an LLM completion."""
    text = text.strip()
    if text.startswith("```"):
        first_nl = text.find("\n")
        if first_nl != -1:
            text = text[first_nl + 1:]
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3].rstrip("\n")
    return text.strip()


# ---------------------------------------------------------------------------
# Reflection seam — the GEPA-swap point
# ---------------------------------------------------------------------------

async def reflect(
    *,
    fragment: str,
    failures: list[dict],
    feedback: list[str],
    model: Optional[str] = None,
) -> str:
    """Reflect on failures and return a revised coordinator fragment (markdown).

    This is the single, pure-ish reflection seam. Inputs:
      * ``fragment``  — the active domain's current coordinator config (markdown).
      * ``failures``  — the failing, scored cases (per-decision dicts).
      * ``feedback``  — one rubric-citing textual string per failure.
      * ``model``     — accepted for compatibility; ignored (uses KNOTCH_LLM_MODEL).

    Output: a revised fragment string, ready to use verbatim as the new config.

    This function is the optimizer seam: it accepts (fragment, feedback) and
    returns a revised fragment. Keeping this signature stable allows a
    drop-in replacement (e.g. wrapping with dspy.GEPA) without engine changes.
    """
    feedback_text = "\n".join(feedback) if feedback else (
        "(no failures — all decisions matched their expected labels)"
    )
    system = "You are a prompt engineer. Output only the revised markdown fragment."
    user = (
        "You are improving a routing-coordinator prompt fragment for one domain.\n\n"
        "CURRENT FRAGMENT:\n"
        "```markdown\n"
        f"{fragment}\n"
        "```\n\n"
        "FAILURE CASES (scored against this domain's rubric):\n"
        f"{feedback_text}\n\n"
        "TASK: Rewrite the fragment to reduce these failures. Clarify the "
        "routing rules and the what-to-hold rules so the cited cases route "
        "correctly. Do NOT change the role table or the signal-vocabulary "
        "headers. Output ONLY the revised fragment in markdown — no prose, no "
        "explanation, no code fences. The output is used verbatim as the new "
        "coordinator fragment."
    )
    completion = await optimizer_complete(system, user, model=model)
    revised = _strip_fences(completion)
    return revised or fragment


# ---------------------------------------------------------------------------
# Diff + persistence helpers
# ---------------------------------------------------------------------------

def _unified_diff(old_fragment: str, new_fragment: str, domain: str) -> str:
    """Produce a compact unified diff (old -> new) for the dashboard caption."""
    diff = difflib.unified_diff(
        old_fragment.splitlines(),
        new_fragment.splitlines(),
        fromfile=f"{domain}/routing_policy.md",
        tofile=f"{domain}/routing_policy.revised.md",
        lineterm="",
        n=1,
    )
    lines = [l for l in diff]
    if not lines:
        return "no textual diff detected (fragment unchanged)"
    # Keep the caption compact for the panel; the full revised file is on disk.
    if len(lines) > 40:
        lines = lines[:40] + [f"… (+{len(lines) - 40} more diff lines)"]
    return "\n".join(lines)


def _write_revised_sibling(pack: DomainPack, fragment: str) -> Path:
    """Write the revised fragment to domains/<domain>/routing_policy.revised.md.

    Sibling of the original routing_policy.md — the original is NEVER clobbered here.
    (proc_improve --apply may overwrite the original; that's opt-in and lives
    in the process layer, not the engine.)
    """
    domain_dir = Path(pack.path) if pack.path else Path("domains") / pack.domain
    out_path = domain_dir / "routing_policy.revised.md"
    out_path.write_text(fragment, encoding="utf-8")
    return out_path


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _curves(result: dict) -> dict:
    """Extract the {misroute, missed, time_to_action} curve triple from a score."""
    return {
        "misroute": result["misroute"],
        "missed": result["missed"],
        "time_to_action": result["time_to_action"],
    }


def _is_better(candidate: dict, incumbent: dict) -> bool:
    """Is `candidate` a better score than `incumbent`?

    Better == lower (misroute + missed); tie-break on lower time_to_action.
    Both args are curve triples (misroute/missed/time_to_action). Pure ranking,
    no domain knowledge.
    """
    cand_err = candidate["misroute"] + candidate["missed"]
    inc_err = incumbent["misroute"] + incumbent["missed"]
    if cand_err != inc_err:
        return cand_err < inc_err
    return candidate["time_to_action"] < incumbent["time_to_action"]


async def improve_once(
    pack: DomainPack,
    *,
    prompts_dir: str = "prompts",
    optimizer_model: Optional[str] = None,
    score_source: Optional[ScoreSource] = None,
) -> dict:
    """Run one improvement cycle on the ACTIVE domain pack.

    Thin wrapper over ``improve_rounds(rounds=1)``. The returned dict carries
    ``rounds`` and ``history`` keys (harmless to existing callers).
    """
    return await improve_rounds(
        pack,
        rounds=1,
        prompts_dir=prompts_dir,
        optimizer_model=optimizer_model,
        score_source=score_source,
    )


async def improve_rounds(
    pack: DomainPack,
    *,
    rounds: int = 1,
    prompts_dir: str = "prompts",
    optimizer_model: Optional[str] = None,
    score_source: Optional[ScoreSource] = None,
) -> dict:
    """Run a hand-rolled MULTI-ROUND improvement loop on the ACTIVE domain pack.

    Score the baseline once, then for each round reflect on the CURRENT best
    fragment's failures → rewrite → re-score, keeping the BEST fragment seen so
    far (best = lowest misroute+missed; tie-break lower time_to_action).

    ``rounds=1`` reproduces the original ``improve_once`` exactly.

    Returns the same shape as the historical ``improve_once`` PLUS:
      * ``rounds``  — the N requested.
      * ``history`` — per-round trace [{round, misroute, missed, time_to_action}],
        where round 0 is the baseline and rounds 1..N are each rewrite's re-score.

    ``before`` = round-0 baseline curves; ``after`` = the BEST round's curves;
    ``prompt_diff`` = baseline → best fragment.
    """
    if rounds < 1:
        raise ValueError(f"rounds must be >= 1, got {rounds!r}")

    source: ScoreSource = score_source or LocalRubricScoreSource()
    model = (optimizer_model or optimizer_model_name()).strip().lower()

    # Round 0 — baseline score against the active domain's own rubric.
    baseline_result = await source.score(pack, prompts_dir=prompts_dir)
    before_curves = _curves(baseline_result)

    # The incumbent best so far (starts as the unmodified baseline fragment).
    best_fragment = pack.routing_policy
    best_curves = dict(before_curves)
    best_result = baseline_result
    # The fragment/result we reflect ON next round (always the current best).
    cur_result = baseline_result

    history: list[dict] = [{
        "round": 0,
        "misroute": before_curves["misroute"],
        "missed": before_curves["missed"],
        "time_to_action": before_curves["time_to_action"],
    }]

    for n in range(1, rounds + 1):
        # Reflect on the CURRENT best fragment's failures (the GEPA-swap seam).
        feedback = build_feedback(cur_result["records"], pack.rubric)
        failures = [
            r for r in cur_result["records"] if r.get("misroute") or r.get("missed")
        ]
        revised_fragment = await reflect(
            fragment=best_fragment,
            failures=failures,
            feedback=feedback,
            model=model,
        )
        if not revised_fragment.strip():
            revised_fragment = best_fragment

        # Re-score with the revised fragment, in-process (no restart).
        round_result = await source.score(
            pack, prompt_fragment=revised_fragment, prompts_dir=prompts_dir
        )
        round_curves = _curves(round_result)
        history.append({
            "round": n,
            "misroute": round_curves["misroute"],
            "missed": round_curves["missed"],
            "time_to_action": round_curves["time_to_action"],
        })

        # Keep the best fragment so far; subsequent rounds build on it.
        if _is_better(round_curves, best_curves):
            best_fragment = revised_fragment
            best_curves = round_curves
            best_result = round_result
        cur_result = best_result

    after_curves = best_curves

    # Persist the BEST revised sibling + compute the baseline→best diff caption.
    revised_path = _write_revised_sibling(pack, best_fragment)
    diff_caption = _unified_diff(pack.routing_policy, best_fragment, pack.domain)

    return {
        "before": before_curves,
        "after": after_curves,
        "prompt_diff": diff_caption,
        "revised_fragment": best_fragment,
        "revised_path": str(revised_path),
        "scored_by": source.scored_by,
        "optimizer_model": model,
        "score_explanations": baseline_result.get("cekura_explanations", []),
        # Per-metric Cekura aggregates (pass_pct + explanations) when the score
        # source is a CekuraScoreSource; absent/empty for the local rubric.
        "cekura_metric_scores": baseline_result.get("cekura_metric_scores", []),
        "rounds": rounds,
        "history": history,
    }
