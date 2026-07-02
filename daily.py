"""daily.py — the unified per-day frame that joins EVERY stream, routed through integrity.

One row per calendar day (Asia/Dubai), merging:
  * glucose day-metrics (from analytics.compute → mean/TIR/TITR/CV/GRI/MAGE/TAR/TBR),
  * wearables (steps, active kcal, sleep light/deep/rem/total, resting-ish HR avg, SpO2, weight),
  * journal aggregates (mood, energy, symptom count/severity, carbs, meals, sex, post-meal walk,
    supplements).

This frame is the backbone for cross-stream correlation (correlate.py) and the dashboard's
multi-stream panels. It computes nothing itself beyond simple aggregation; every metric comes
from an integrity-routed source. Missing values are ``None`` (never guessed).
"""

from __future__ import annotations

import analytics
import integrity
import journal

# numeric fields available for correlation, grouped by stream (for the dashboard heatmap order)
GLUCOSE_FIELDS = ("mean_mgdl", "tir_pct", "titr_pct", "tbr_pct", "tar_pct", "cv_pct",
                  "gri", "mage_mgdl")
WEARABLE_FIELDS = ("steps", "active_calories", "sleep_total_min", "sleep_deep_min",
                   "sleep_rem_min", "hr_avg", "spo2_avg", "weight_kg")
LOG_FIELDS = ("mood", "energy", "symptom_count", "symptom_severity", "carbs_g",
              "meal_count", "sex", "walk", "supplement_count")
NUMERIC_FIELDS = GLUCOSE_FIELDS + WEARABLE_FIELDS + LOG_FIELDS
# friendly labels for the UI
LABELS = {
    "mean_mgdl": "Mean glucose", "tir_pct": "Time in range", "titr_pct": "Tight range",
    "tbr_pct": "Time below", "tar_pct": "Time above", "cv_pct": "Variability (CV)",
    "gri": "Glycemia Risk Index", "mage_mgdl": "MAGE",
    "steps": "Steps", "active_calories": "Active kcal", "sleep_total_min": "Sleep",
    "sleep_deep_min": "Deep sleep", "sleep_rem_min": "REM sleep", "hr_avg": "Heart rate",
    "spo2_avg": "SpO₂", "weight_kg": "Weight",
    "mood": "Mood", "energy": "Energy", "symptom_count": "Symptoms",
    "symptom_severity": "Symptom severity", "carbs_g": "Carbs", "meal_count": "Meals",
    "sex": "Intimacy", "walk": "Post-meal walk", "supplement_count": "Supplements",
}
SEX_TYPES = {"sex", "intimacy"}


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def _log_aggregates(store) -> dict:
    agg: dict[str, dict] = {}
    for r in journal.records(store):
        d = journal.local_date(r.get("measured_at") or r.get("ts"))
        if not d:
            continue
        a = agg.setdefault(d, {"mood": [], "energy": [], "sym": 0, "symsev": [],
                               "carbs": 0.0, "meals": 0, "sex": False, "walk": False, "supp": 0})
        tags = [str(t).lower() for t in (r.get("tags") or [])]
        rtype = r.get("type")
        if r.get("mood_1to5") is not None:
            a["mood"].append(r["mood_1to5"])
        if r.get("energy_1to5") is not None:
            a["energy"].append(r["energy_1to5"])
        if r.get("symptom") or r.get("symptom_sev_1to5") is not None:
            a["sym"] += 1
            if r.get("symptom_sev_1to5") is not None:
                a["symsev"].append(r["symptom_sev_1to5"])
        if rtype == "meal" or (r.get("net_carbs_g") or 0) > 0:
            a["meals"] += 1
            a["carbs"] += r.get("net_carbs_g") or 0
        if rtype in SEX_TYPES or SEX_TYPES & set(tags):
            a["sex"] = True
        if "+walk" in tags or "walk" in tags:
            a["walk"] = True
        if rtype == "supplement" or r.get("supplements"):
            a["supp"] += 1
    return agg


def build(store, *, window_days: int = 90, now=None, wearables=None) -> list[dict]:
    """Return the unified daily frame over the window (list of per-day dicts, date-sorted)."""
    now = integrity.now_utc() if now is None else now
    trend = analytics.compute(store, window_days=window_days, now=now, group_by="day")
    g_by_day = {r["group"]: r for r in trend["series"]}
    w_by_day = (wearables or {}).get("days", {})
    log = _log_aggregates(store)

    rows = []
    for d in sorted(set(g_by_day) | set(w_by_day) | set(log)):
        g, w, a = g_by_day.get(d, {}), w_by_day.get(d, {}), log.get(d)
        row = {"date": d}
        for f in GLUCOSE_FIELDS:
            row[f] = g.get(f)
        for f in WEARABLE_FIELDS:
            row[f] = w.get(f)
        row["mood"] = _mean(a["mood"]) if a else None
        row["energy"] = _mean(a["energy"]) if a else None
        row["symptom_count"] = float(a["sym"]) if a else None
        row["symptom_severity"] = _mean(a["symsev"]) if a else None
        row["carbs_g"] = a["carbs"] if (a and a["meals"]) else None
        row["meal_count"] = float(a["meals"]) if a else None
        row["sex"] = (1.0 if a["sex"] else 0.0) if a else None
        row["walk"] = (1.0 if a["walk"] else 0.0) if a else None
        row["supplement_count"] = float(a["supp"]) if a else None
        rows.append(row)
    return rows


def series(frame: list[dict], field: str) -> list[tuple[str, float]]:
    """(date, value) pairs for one field (value may be None)."""
    return [(r["date"], r.get(field)) for r in frame]


def coverage(frame: list[dict]) -> dict:
    """How many non-null days each field has (for the dashboard 'what's tracked' view)."""
    return {f: sum(1 for r in frame if r.get(f) is not None) for f in NUMERIC_FIELDS}


def self_check(frame: list[dict]) -> list[str]:
    violations = []
    dates = [r["date"] for r in frame]
    if dates != sorted(dates):
        violations.append("daily frame not date-sorted")
    if len(dates) != len(set(dates)):
        violations.append("duplicate dates in daily frame")
    for r in frame:
        for pct in ("tir_pct", "titr_pct", "cv_pct"):
            v = r.get(pct)
            if v is not None and v < 0:
                violations.append(f"{r['date']}: negative {pct}")
    return violations
