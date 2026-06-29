"""symptoms.py — associate logged symptoms with glucose, routed through integrity.

Day-level analysis: each calendar day (Asia/Dubai) gets a glucose summary (from the trend
engine) and a symptom summary (from the journal). We report:

  * lift     = P(symptom-day | high-glucose day) / P(symptom-day | not-high) with both n's,
  * spearman = rank correlation of daily symptom severity vs daily mean glucose, with n.

Everything carries ``n``; nothing is presented as causal.
"""

from __future__ import annotations

import numpy as np

import analytics
import integrity
import journal
import statutil


def _daily(store, *, window_days, now):
    trend = analytics.compute(store, window_days=window_days, now=now, group_by="day")
    glucose_by_day = {row["group"]: row for row in trend["series"]}

    sym_by_day: dict[str, list[float]] = {}
    for rec in journal.records(store):
        d = journal.local_date(rec.get("measured_at") or rec.get("ts"))
        if d is None:
            continue
        sev = rec.get("symptom_sev_1to5")
        has_symptom = bool(rec.get("symptom")) or (sev is not None)
        if has_symptom:
            sym_by_day.setdefault(d, []).append(float(sev) if sev is not None else 1.0)

    days = sorted(glucose_by_day.keys())
    rows = []
    for d in days:
        g = glucose_by_day[d]
        sevs = sym_by_day.get(d, [])
        rows.append({
            "day": d,
            "mean_mgdl": g.get("mean_mgdl"),
            "tir_pct": g.get("tir_pct"),
            "symptom_present": len(sevs) > 0,
            "symptom_severity": (float(np.mean(sevs)) if sevs else None),
        })
    return rows


def analyze(store, *, window_days: int = 90, now=None, high_quantile: float = 0.66) -> dict:
    """Lift + Spearman for symptoms vs daily glucose over the window."""
    now = integrity.now_utc() if now is None else now
    rows = _daily(store, window_days=window_days, now=now)
    means = np.array([r["mean_mgdl"] for r in rows if r["mean_mgdl"] is not None], dtype=float)
    n_days = int(means.size)

    out = {"window_days": window_days, "n_days": n_days,
           "lift": None, "spearman_severity_vs_mean": {"rho": None, "n": 0},
           "note": "associational only; n reported"}
    if n_days < 3:
        return out

    thresh = float(np.quantile(means, high_quantile))
    flag = np.array([bool(r["symptom_present"]) for r in rows if r["mean_mgdl"] is not None])
    high = np.array([r["mean_mgdl"] >= thresh for r in rows if r["mean_mgdl"] is not None])
    out["high_glucose_threshold_mgdl"] = thresh
    out["lift"] = statutil.lift(flag, high)

    sev = [r["symptom_severity"] if r["symptom_severity"] is not None else np.nan for r in rows]
    out["spearman_severity_vs_mean"] = statutil.spearman(
        sev, [r["mean_mgdl"] for r in rows])
    return out


def self_check(result: dict) -> list[str]:
    violations = []
    lift = result.get("lift") or {}
    if lift.get("p_treated") is not None and not (0 <= lift["p_treated"] <= 1):
        violations.append("symptom lift probability out of [0,1]")
    rho = result.get("spearman_severity_vs_mean", {}).get("rho")
    if rho is not None and not (-1.0001 <= rho <= 1.0001):
        violations.append("spearman rho out of [-1,1]")
    return violations
