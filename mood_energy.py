"""mood_energy.py — correlate daily mood/energy (1-5) with glucose, via Spearman + n.

Day-level (Asia/Dubai): mean logged mood and energy vs that day's glucose summary
(mean / TIR / CV). Rank correlations with sample sizes; nothing causal.
"""

from __future__ import annotations

import numpy as np

import analytics
import integrity
import journal
import statutil

GLUCOSE_KEYS = ("mean_mgdl", "tir_pct", "cv_pct")


def _daily(store, *, window_days, now):
    trend = analytics.compute(store, window_days=window_days, now=now, group_by="day")
    glucose_by_day = {row["group"]: row for row in trend["series"]}

    mood, energy = {}, {}
    for rec in journal.records(store):
        d = journal.local_date(rec.get("measured_at") or rec.get("ts"))
        if d is None:
            continue
        if rec.get("mood_1to5") is not None:
            mood.setdefault(d, []).append(float(rec["mood_1to5"]))
        if rec.get("energy_1to5") is not None:
            energy.setdefault(d, []).append(float(rec["energy_1to5"]))

    rows = []
    for d, g in glucose_by_day.items():
        rows.append({
            "day": d,
            "mood": float(np.mean(mood[d])) if d in mood else None,
            "energy": float(np.mean(energy[d])) if d in energy else None,
            **{k: g.get(k) for k in GLUCOSE_KEYS},
        })
    return sorted(rows, key=lambda r: r["day"])


def analyze(store, *, window_days: int = 90, now=None) -> dict:
    """Spearman of mood/energy vs each glucose key over the window, with n."""
    now = integrity.now_utc() if now is None else now
    rows = _daily(store, window_days=window_days, now=now)
    out = {"window_days": window_days, "n_days": len(rows),
           "correlations": [], "note": "associational only; n reported"}
    for affect in ("mood", "energy"):
        a = [r[affect] if r[affect] is not None else np.nan for r in rows]
        for gk in GLUCOSE_KEYS:
            g = [r[gk] if r[gk] is not None else np.nan for r in rows]
            sp = statutil.spearman(a, g)
            out["correlations"].append({"affect": affect, "glucose_metric": gk,
                                        "rho": sp["rho"], "n": sp["n"]})
    return out


def self_check(result: dict) -> list[str]:
    violations = []
    for c in result.get("correlations", []):
        if c["rho"] is not None and not (-1.0001 <= c["rho"] <= 1.0001):
            violations.append(f"{c['affect']}/{c['glucose_metric']}: rho out of range")
    return violations
