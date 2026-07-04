"""digest.py — the weekly "what changed" readout.

A reversal is a story told week over week, so this compares the last 7 days against the prior 7
across every stream and reports what moved — the multi-domain weekly readout the mission calls
for. Pure aggregation over the integrity-routed daily frame; deltas are computed, not guessed,
and a stream with no data in either week is simply omitted (missing ≠ zero).
"""

from __future__ import annotations

from datetime import date

import numpy as np

# (field, friendly, "lower_better"?)  the streams worth a weekly delta
_FIELDS = (
    ("mean_mgdl", "Mean glucose", True), ("tir_pct", "Time in range", False),
    ("titr_pct", "Tight range", False), ("cv_pct", "Variability (CV)", True),
    ("gri", "Glycemia Risk Index", True), ("weight_kg", "Weight", True),
    ("steps", "Steps", False), ("sleep_total_min", "Sleep", False),
    ("mood", "Mood", False), ("symptom_count", "Symptoms", True),
)
_UNIT = {"mean_mgdl": " mg/dL", "tir_pct": " pts", "titr_pct": " pts", "cv_pct": " pts",
         "weight_kg": " kg", "sleep_total_min": " min", "gri": "", "steps": "", "mood": "",
         "symptom_count": ""}


def _mean(frame, field):
    xs = [r[field] for r in frame if r.get(field) is not None]
    return float(np.mean(xs)) if xs else None


def build(frame, *, now=None) -> dict:
    """This-7-days vs prior-7-days deltas across streams. now defaults to the frame's last day."""
    dated = [r for r in (frame or []) if r.get("date")]
    if len(dated) < 8:
        return {"ok": False, "n_days": len(dated)}
    last = date.fromisoformat(str(dated[-1]["date"])) if now is None else now

    def window(lo, hi):
        return [r for r in dated
                if lo <= (last - date.fromisoformat(str(r["date"]))).days < hi]

    this7, prior7 = window(0, 7), window(7, 14)
    if not this7 or not prior7:
        return {"ok": False, "n_days": len(dated)}

    rows = []
    for field, label, lower_better in _FIELDS:
        a, b = _mean(this7, field), _mean(prior7, field)
        if a is None or b is None:
            continue
        delta = a - b
        if abs(delta) < 1e-9:
            better = None
        else:
            better = (delta < 0) if lower_better else (delta > 0)
        rows.append({"key": field, "label": label, "this": round(a, 1), "prior": round(b, 1),
                     "delta": round(delta, 1), "unit": _UNIT.get(field, ""), "better": better})
    return {"ok": True, "rows": rows, "n_this": len(this7), "n_prior": len(prior7)}


def headline(dig: dict) -> str:
    """One-line summary of the biggest glucose move (for a card header)."""
    if not dig.get("ok"):
        return "Not enough days yet for a weekly comparison."
    g = next((r for r in dig["rows"] if r["key"] == "mean_mgdl"), None)
    if not g or g["better"] is None:
        return "Your week held roughly steady."
    dirn = "down" if g["delta"] < 0 else "up"
    good = "— nice" if g["better"] else "— worth a look"
    return f"Mean glucose {dirn} {abs(g['delta']):.0f} mg/dL vs last week {good}."


def self_check(dig: dict) -> list[str]:
    violations = []
    for r in dig.get("rows", []):
        if r["better"] is not None and not isinstance(r["better"], bool):
            violations.append(f"digest {r['key']}: bad better flag")
    return violations
