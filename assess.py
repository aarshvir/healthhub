"""assess.py — a plain-English assessment per window, from the computed numbers ONLY.

Deterministic templating against consensus targets (Battelino 2019 / tight-range): no model,
no guessing — every sentence is derived from a metric value and its target, so the assessment
is reproducible and auditable. Output text is later run through integrity.sanitize_narration
by insights.py, but assess itself never invents a number.
"""

from __future__ import annotations

# (metric_key, friendly, target, direction)  direction: 'high_good' or 'low_good'
TARGETS = (
    ("tir_pct", "Time in range (70-180)", 70.0, "high_good"),
    ("titr_pct", "Tight time in range (70-140)", 50.0, "high_good"),
    ("tbr_pct", "Time below range (<70)", 4.0, "low_good"),
    ("vlow_pct", "Time very low (<54)", 1.0, "low_good"),
    ("tar_pct", "Time above range (>180)", 25.0, "low_good"),
    ("cv_pct", "Glucose variability (CV)", 36.0, "low_good"),
    ("gmi_pct", "Glucose Management Indicator", 7.0, "low_good"),
    ("gri", "Glycemia Risk Index", 40.0, "low_good"),
)


def _verdict(value, target, direction) -> str:
    if value is None:
        return "no_data"
    if direction == "high_good":
        if value >= target:
            return "on_target"
        return "below_target" if value >= 0.8 * target else "off_target"
    # low_good
    if value <= target:
        return "on_target"
    return "above_target" if value <= 1.25 * target else "off_target"


def _value(metrics, key):
    entry = metrics.get(key)
    if not entry:
        return None
    v = entry.get("value")
    return v if isinstance(v, (int, float)) else None


def assess(metrics: dict, *, window_days: int | None = None) -> dict:
    """Return {grade, score, lines:[...]} assessing the metrics against targets."""
    lines = []
    scored, total = 0, 0
    for key, friendly, target, direction in TARGETS:
        v = _value(metrics, key)
        verdict = _verdict(v, target, direction)
        if v is not None:
            total += 1
            scored += 1 if verdict == "on_target" else (0.5 if verdict in
                                                        ("below_target", "above_target") else 0)
        comparator = "≥" if direction == "high_good" else "≤"
        text = (f"{friendly}: no data" if v is None else
                f"{friendly} is {v:.1f} (target {comparator}{target:g}) — "
                f"{verdict.replace('_', ' ')}.")
        lines.append({"metric": key, "value": v, "target": target,
                      "verdict": verdict, "text": text})

    pct = (scored / total * 100.0) if total else 0.0
    grade = ("A" if pct >= 85 else "B" if pct >= 70 else "C" if pct >= 55
             else "D" if pct >= 40 else "F")
    headline = (f"Window {window_days}d: " if window_days else "") + \
               f"overall {grade} ({pct:.0f}% of targets met)."
    return {"window_days": window_days, "grade": grade, "score_pct": round(pct, 1),
            "headline": headline, "lines": lines}


def self_check(result: dict) -> list[str]:
    violations = []
    if not (0 <= result.get("score_pct", 0) <= 100):
        violations.append("assessment score out of [0,100]")
    if result.get("grade") not in {"A", "B", "C", "D", "F"}:
        violations.append("invalid grade")
    return violations
