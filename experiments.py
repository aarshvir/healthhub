"""experiments.py — matched with/without comparisons per intervention tag.

For a tag (e.g. ``+walk``, ``+acv``, ``+methi``, ``vegfirst``) the meals are split into
*treated* (carry the tag) and *control* (do not), optionally matched on carbohydrate load so
like is compared with like. We report the effect on a glucose outcome (absolute + %),
n_treated / n_control, and a signal strength (Cohen's d).

CAUSAL GUARD: a comparison is **never** labelled causal when either arm has n<5
(``MIN_CAUSAL_N``). Below that it is reported as associational / insufficient-n only.
"""

from __future__ import annotations

import numpy as np

import food_impact
import integrity
import journal
import statutil

MIN_CAUSAL_N = 5
OUTCOMES = ("delta_peak_mgdl", "iauc_120", "per_gram")


def _meal_outcomes(store, *, now=None) -> list[dict]:
    """Join each meal's tags with its computed glucose response."""
    now = integrity.now_utc() if now is None else now
    out = []
    for meal in journal.meals(store):
        resp = food_impact.meal_response(store, meal, now=now)
        if resp["valid"]:
            resp = dict(resp)
            resp["tags"] = meal.get("tags", [])
            out.append(resp)
    return out


def compare_tag(store, tag: str, *, outcome: str = "delta_peak_mgdl",
                match_on: str = "net_carbs_g", caliper: float = 15.0, now=None) -> dict:
    """Compare *outcome* for meals with vs without *tag* (optionally carb-matched)."""
    now = integrity.now_utc() if now is None else now
    if outcome not in OUTCOMES:
        raise ValueError(f"outcome must be one of {OUTCOMES}")
    tag = tag.strip().lower()
    meals = _meal_outcomes(store, now=now)

    treated = [m for m in meals if tag in (m.get("tags") or [])]
    control = [m for m in meals if tag not in (m.get("tags") or [])]

    # optional matching: keep only control meals whose carb load is within caliper of
    # some treated meal (so the comparison is like-for-like, not confounded by carbs)
    matched = False
    if match_on and treated:
        t_vals = [m.get(match_on) for m in treated if m.get(match_on) is not None]
        if t_vals:
            control = [c for c in control if c.get(match_on) is not None and
                       min(abs(c[match_on] - tv) for tv in t_vals) <= caliper]
            matched = True

    t_out = np.array([m[outcome] for m in treated if m.get(outcome) is not None], dtype=float)
    c_out = np.array([m[outcome] for m in control if m.get(outcome) is not None], dtype=float)
    n_t, n_c = int(t_out.size), int(c_out.size)

    result = {
        "tag": tag, "outcome": outcome, "matched_on": match_on if matched else None,
        "n_treated": n_t, "n_control": n_c,
        "mean_treated": float(t_out.mean()) if n_t else None,
        "mean_control": float(c_out.mean()) if n_c else None,
        "effect_abs": None, "effect_pct": None,
        "cohens_d": None, "signal_strength": "undefined",
        "causal": False, "causal_label": "insufficient_n (not causal)",
    }
    if n_t and n_c:
        eff = float(t_out.mean() - c_out.mean())
        result["effect_abs"] = eff
        result["effect_pct"] = (eff / c_out.mean() * 100.0) if c_out.mean() else None
        d = statutil.cohens_d(t_out, c_out)
        result["cohens_d"] = d
        result["signal_strength"] = statutil.strength_label(d)

    if min(n_t, n_c) >= MIN_CAUSAL_N:
        result["causal_label"] = "eligible_for_causal_review"  # still associational by design
    return result


def compare_all_tags(store, *, outcome="delta_peak_mgdl", now=None) -> list[dict]:
    """Run compare_tag for every tag that appears on at least one meal."""
    now = integrity.now_utc() if now is None else now
    tags = set()
    for m in journal.meals(store):
        tags.update(m.get("tags") or [])
    results = [compare_tag(store, t, outcome=outcome, now=now) for t in sorted(tags)]
    results.sort(key=lambda r: abs(r["effect_abs"]) if r["effect_abs"] is not None else -1,
                 reverse=True)
    return results


def self_check(results: list[dict]) -> list[str]:
    """Invariant: nothing with n<5 in either arm may be flagged causal."""
    violations = []
    for r in results:
        if r.get("causal") and min(r["n_treated"], r["n_control"]) < MIN_CAUSAL_N:
            violations.append(f"{r['tag']}: labelled causal with n<{MIN_CAUSAL_N}")
    return violations
