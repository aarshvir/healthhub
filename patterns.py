"""patterns.py — WHEN and HOW glucose behaves: time-of-day windows, the dawn effect, weekday
patterns, and where the trend changed. Pure analysis over integrity-routed inputs (the window
AGP and the unified daily frame); it invents no numbers.

These are the "what's actually going on" observations a coach would make — the worst hours of
the day to target, whether dawn is driving the morning, whether weekends slip, and the week
where control turned — surfaced plainly and always with the data behind them.
"""

from __future__ import annotations

from datetime import date

import numpy as np

# named day-parts (Asia/Dubai local hours) → the 15-min AGP slot indices they cover
DAYPARTS = (
    ("Overnight", 0, 6), ("Morning", 6, 11), ("Midday", 11, 15),
    ("Afternoon", 15, 19), ("Evening", 19, 24),
)
# Dubai working week: weekend is Saturday(5) + Sunday(6)
_WEEKEND = {5, 6}
_WEEKDAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def _agp_p50(agp, lo_h, hi_h):
    """Mean of the AGP median (p50) over the slots in [lo_h, hi_h) local hours, or None."""
    vals = [b["p50"] for i, b in enumerate(agp or [])
            if b is not None and lo_h <= (i // 4) < hi_h]
    return float(np.mean(vals)) if vals else None


def time_of_day(agp) -> dict:
    """Typical (median) glucose per day-part + the best/worst window."""
    parts = []
    for name, lo, hi in DAYPARTS:
        p50 = _agp_p50(agp, lo, hi)
        if p50 is not None:
            parts.append({"window": name, "median_mgdl": round(p50, 1),
                          "hours": f"{lo:02d}:00–{hi:02d}:00"})
    out = {"parts": parts, "worst": None, "best": None}
    if parts:
        out["worst"] = max(parts, key=lambda p: p["median_mgdl"])
        out["best"] = min(parts, key=lambda p: p["median_mgdl"])
    return out


def dawn(agp) -> dict:
    """Dawn effect = median(04:00–08:00) − median(00:00–04:00)."""
    night = _agp_p50(agp, 0, 4)
    morning = _agp_p50(agp, 4, 8)
    if night is None or morning is None:
        return {"delta_mgdl": None, "present": False}
    delta = morning - night
    return {"delta_mgdl": round(delta, 1), "present": delta >= 15.0,
            "night_mgdl": round(night, 1), "dawn_mgdl": round(morning, 1)}


def weekday_effect(frame) -> dict:
    """Weekend vs weekday mean glucose, and the highest/lowest weekday."""
    by_wd: dict[int, list] = {}
    for r in frame or []:
        v = r.get("mean_mgdl")
        if v is None:
            continue
        try:
            wd = date.fromisoformat(str(r["date"])).weekday()
        except (ValueError, KeyError, TypeError):
            continue
        by_wd.setdefault(wd, []).append(v)
    if not by_wd:
        return {"ok": False}
    means = {wd: float(np.mean(vs)) for wd, vs in by_wd.items()}
    weekend = [means[w] for w in by_wd if w in _WEEKEND]
    weekday = [means[w] for w in by_wd if w not in _WEEKEND]
    hi = max(means, key=means.get)
    lo = min(means, key=means.get)
    return {
        "ok": True,
        "weekend_minus_weekday": (round(float(np.mean(weekend) - np.mean(weekday)), 1)
                                  if weekend and weekday else None),
        "highest": {"day": _WEEKDAY_NAMES[hi], "mean_mgdl": round(means[hi], 1)},
        "lowest": {"day": _WEEKDAY_NAMES[lo], "mean_mgdl": round(means[lo], 1)},
    }


def _weekly_means(frame):
    """(iso-week-start-ordinal, mean_mgdl) per calendar week, chronological."""
    weeks: dict[int, list] = {}
    for r in frame or []:
        v = r.get("mean_mgdl")
        if v is None:
            continue
        try:
            d = date.fromisoformat(str(r["date"]))
        except (ValueError, KeyError, TypeError):
            continue
        wk = d.toordinal() - d.weekday()   # Monday of that week, as an ordinal
        weeks.setdefault(wk, []).append(v)
    return [(wk, float(np.mean(vs))) for wk, vs in sorted(weeks.items())]


def trend_change(frame) -> dict:
    """Overall direction plus the single biggest week-over-week shift (a change-point hint)."""
    wm = _weekly_means(frame)
    if len(wm) < 2:
        return {"ok": False, "n_weeks": len(wm)}
    diffs = [(wm[i][1] - wm[i - 1][1], wm[i][0]) for i in range(1, len(wm))]
    # biggest improvement = most negative week-over-week change
    best = min(diffs, key=lambda t: t[0])
    overall = wm[-1][1] - wm[0][1]
    return {
        "ok": True, "n_weeks": len(wm),
        "overall_change_mgdl": round(overall, 1),
        "direction": ("improving" if overall < -3 else "worsening" if overall > 3 else "flat"),
        "biggest_weekly_drop_mgdl": round(best[0], 1),
        "biggest_drop_week": date.fromordinal(best[1]).isoformat(),
    }


def compute(agp, frame) -> dict:
    """All patterns for a window: time-of-day, dawn, weekday, trend-change."""
    return {
        "time_of_day": time_of_day(agp),
        "dawn": dawn(agp),
        "weekday": weekday_effect(frame),
        "trend": trend_change(frame),
    }


def self_check(p: dict) -> list[str]:
    violations = []
    tod = p.get("time_of_day", {})
    if tod.get("worst") and tod.get("best"):
        if tod["worst"]["median_mgdl"] < tod["best"]["median_mgdl"]:
            violations.append("time-of-day worst < best")
    return violations
