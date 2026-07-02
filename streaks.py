"""streaks.py — habit streaks over the unified daily frame (the "daily visible loop").

A reversal is won or lost on daily consistency, so the cockpit needs a habit mechanic that is
honest rather than gamified-cheap: a streak counts only *consecutive calendar days* that meet a
clinically meaningful bar, and a day with missing data breaks the streak (missing ≠ met — §A).

Each streak reports its current run (ending at the most recent day) and its best-ever run, so
the user sees both momentum and a target to beat. Nothing here invents numbers; predicates read
the integrity-routed daily frame.
"""

from __future__ import annotations

from datetime import date

# out of the diabetic range: GMI < 6.5  ⇔  mean glucose < (6.5 - 3.31) / 0.02392 ≈ 133.4 mg/dL
REMISSION_MEAN = 133.4


def _ge(v, t):
    return v is not None and v >= t


def _le(v, t):
    return v is not None and v <= t


def _lt(v, t):
    return v is not None and v < t


# (key, friendly label, one-line meaning, predicate over a daily-frame row)
STREAK_DEFS = (
    ("remission", "Non-diabetic days", "daily average glucose below the diabetic range",
     lambda r: _lt(r.get("mean_mgdl"), REMISSION_MEAN)),
    ("titr", "Tight-range days", "≥50% of the day between 70–140 mg/dL",
     lambda r: _ge(r.get("titr_pct"), 50.0)),
    ("tir", "In-range days", "≥70% of the day between 70–180 mg/dL",
     lambda r: _ge(r.get("tir_pct"), 70.0)),
    ("safe", "Hypo-safe days", "≤4% of the day below 70 mg/dL",
     lambda r: r.get("tbr_pct") is not None and r["tbr_pct"] <= 4.0),
    ("steady", "Steady days", "glucose variability (CV) at or below 36%",
     lambda r: _le(r.get("cv_pct"), 36.0)),
    ("walk", "Post-meal-walk days", "a walk logged after a meal",
     lambda r: r.get("walk") == 1.0),
    ("logged", "Days logged", "at least one thing captured in the journal",
     lambda r: (r.get("meal_count") or 0) > 0 or r.get("mood") is not None
     or r.get("symptom_count") not in (None, 0.0)),
)


def _run(frame, pred) -> tuple[int, int, bool]:
    """(current run ending at the last day, best-ever run, met_on_latest_day).

    A run is consecutive CALENDAR days all satisfying *pred*; a gap or an unmet/missing day
    resets it. ``current`` is the run ending at the frame's most recent day (0 if that day
    doesn't satisfy the predicate)."""
    best = cur = 0
    prev = None
    for r in frame:
        try:
            d = date.fromisoformat(str(r["date"]))
        except (ValueError, KeyError, TypeError):
            prev, cur = None, 0
            continue
        ok = bool(pred(r))
        adjacent = prev is not None and (d - prev).days == 1
        cur = (cur + 1) if (ok and adjacent) else (1 if ok else 0)
        best = max(best, cur)
        prev = d
    met_today = bool(frame and pred(frame[-1]))
    return cur, best, met_today


def compute(frame: list[dict]) -> list[dict]:
    """Return one entry per streak: {key,label,meaning,current,best,met_today}, best first."""
    out = []
    for key, label, meaning, pred in STREAK_DEFS:
        cur, best, met = _run(frame or [], pred)
        if best == 0:
            continue  # never once met in-window -> don't show a dead streak
        out.append({"key": key, "label": label, "meaning": meaning,
                    "current": cur, "best": best, "met_today": met})
    out.sort(key=lambda s: (s["current"], s["best"]), reverse=True)
    return out


def headline(streaks: list[dict]) -> dict | None:
    """The single most motivating live streak (prefer an active remission streak)."""
    active = [s for s in streaks if s["current"] > 0]
    if not active:
        return None
    order = {"remission": 0, "titr": 1, "tir": 2, "safe": 3, "steady": 4, "walk": 5, "logged": 6}
    active.sort(key=lambda s: (order.get(s["key"], 9), -s["current"]))
    return active[0]


def self_check(streaks: list[dict]) -> list[str]:
    violations = []
    for s in streaks:
        if s["current"] < 0 or s["best"] < 0:
            violations.append(f"{s['key']}: negative streak")
        if s["current"] > s["best"]:
            violations.append(f"{s['key']}: current {s['current']} > best {s['best']}")
    return violations
