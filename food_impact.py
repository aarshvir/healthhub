"""food_impact.py — per-meal glucose response + food ranking, routed through integrity.

For each meal (from the journal) the glucose store is queried around the meal time:

  * baseline       = mean glucose in the pre-meal window [t-15m, t]
  * peak           = max glucose in [t, t+120m]
  * delta_peak     = peak - baseline
  * time_to_peak_m = minutes from t to the peak
  * iauc_120       = incremental AUC of (glucose-baseline, clipped at >=0) over [t, t+120m]
                     by the trapezoidal rule (mg/dL·min)
  * per_gram       = delta_peak / net_carbs_g  (glucose rise per gram of carb)

Food ranking aggregates responses by item; an item is only ranked once it has n>=3 meals
(``MIN_N``), so single anecdotes are never presented as a food's "impact".
"""

from __future__ import annotations

from datetime import timedelta

import numpy as np
import pandas as pd

import integrity
import journal

PRE_MIN = 15            # baseline window (minutes before the meal)
POST_MIN = 120          # response window (minutes after the meal)
MIN_N = 3               # minimum meals before an item is ranked


def _series(store, center, *, pre=PRE_MIN, post=POST_MIN, now=None):
    """Return (minutes_from_meal, glucose) numpy arrays around *center* (tz-aware)."""
    now = integrity.now_utc() if now is None else now
    center = integrity.freshness(center, now=now).measured_at
    df = store.glucose_window(center - timedelta(minutes=pre),
                              center + timedelta(minutes=post), now=now)
    if df.empty:
        return np.array([]), np.array([])
    measured = pd.to_datetime(df["measured_at"], utc=True)
    epoch0 = pd.Timestamp("1970-01-01", tz="UTC")
    ns = ((measured - epoch0) // pd.Timedelta(1, "ns")).to_numpy()
    center_ns = int((pd.Timestamp(center) - epoch0) // pd.Timedelta(1, "ns"))
    minutes = (ns - center_ns) / 6e10  # ns -> minutes
    values = df["glucose_mgdl"].to_numpy(dtype=float)
    order = np.argsort(minutes, kind="stable")
    return minutes[order], values[order]


def meal_response(store, meal: dict, *, now=None) -> dict:
    """Compute the glucose response metrics for a single meal record."""
    now = integrity.now_utc() if now is None else now
    ts = meal.get("measured_at") or meal.get("ts")
    carbs = meal.get("net_carbs_g")
    minutes, values = _series(store, ts, now=now)

    base_mask = (minutes >= -PRE_MIN) & (minutes <= 0)
    post_mask = (minutes >= 0) & (minutes <= POST_MIN)
    result = {
        "item": meal.get("item"), "measured_at": integrity.freshness(ts, now=now).measured_at.isoformat(),
        "net_carbs_g": carbs, "n_points": int(post_mask.sum()),
        "baseline_mgdl": None, "peak_mgdl": None, "delta_peak_mgdl": None,
        "time_to_peak_min": None, "iauc_120": None, "per_gram": None, "valid": False,
    }
    if base_mask.sum() == 0 or post_mask.sum() == 0:
        return result

    baseline = float(np.mean(values[base_mask]))
    pm, pv = minutes[post_mask], values[post_mask]
    peak_i = int(np.argmax(pv))
    peak = float(pv[peak_i])
    delta = peak - baseline
    excess = np.clip(pv - baseline, 0.0, None)
    iauc = float(np.trapezoid(excess, pm)) if pm.size > 1 else 0.0

    result.update({
        "baseline_mgdl": baseline, "peak_mgdl": peak, "delta_peak_mgdl": delta,
        "time_to_peak_min": float(pm[peak_i]), "iauc_120": iauc,
        "per_gram": (delta / carbs) if (carbs and carbs > 0) else None,
        "valid": True,
    })
    return result


def all_meal_responses(store, *, now=None) -> list[dict]:
    now = integrity.now_utc() if now is None else now
    return [meal_response(store, m, now=now) for m in journal.meals(store)]


def rank_foods(store, *, now=None, min_n: int = MIN_N) -> list[dict]:
    """Rank foods by mean delta-peak; only items with >= min_n valid meals are ranked."""
    now = integrity.now_utc() if now is None else now
    by_item: dict[str, list[dict]] = {}
    for r in all_meal_responses(store, now=now):
        if r["valid"] and r["item"]:
            by_item.setdefault(str(r["item"]).strip().lower(), []).append(r)

    ranked = []
    for item, rs in by_item.items():
        n = len(rs)
        if n < min_n:
            continue
        deltas = [x["delta_peak_mgdl"] for x in rs]
        iaucs = [x["iauc_120"] for x in rs]
        pergram = [x["per_gram"] for x in rs if x["per_gram"] is not None]
        ranked.append({
            "item": item, "n": n,
            "mean_delta_peak_mgdl": float(np.mean(deltas)),
            "mean_iauc_120": float(np.mean(iaucs)),
            "mean_per_gram": float(np.mean(pergram)) if pergram else None,
            "rankable": True,
        })
    ranked.sort(key=lambda x: x["mean_delta_peak_mgdl"], reverse=True)
    return ranked


def self_check(responses: list[dict]) -> list[str]:
    """Invariants: peak >= baseline, iAUC >= 0, time-to-peak within window."""
    violations: list[str] = []
    for r in responses:
        if not r.get("valid"):
            continue
        if r["peak_mgdl"] + 1e-6 < r["baseline_mgdl"]:
            violations.append(f"{r['item']}: peak<baseline")
        if r["iauc_120"] < -1e-6:
            violations.append(f"{r['item']}: negative iAUC")
        if not (0 <= r["time_to_peak_min"] <= POST_MIN + 1e-6):
            violations.append(f"{r['item']}: time-to-peak out of window")
    return violations
