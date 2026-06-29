"""insights.py — rank cross-stream findings; optionally narrate via sanitize_narration.

Pulls the strongest signals from food rankings, intervention experiments, symptom lift and
mood/energy correlations into one ranked list scored by effect size × evidence (n). The
optional natural-language narration is routed through ``integrity.sanitize_narration`` so a
narrator (e.g. an LLM) cannot introduce any number that isn't grounded in the findings.
"""

from __future__ import annotations

import integrity


def _finding(kind, label, value, *, n=0, signal=None, detail=None, score=0.0):
    return {"kind": kind, "label": label, "value": value, "n": n,
            "signal": signal, "detail": detail, "score": float(score)}


def rank(*, food_ranking=None, experiment_results=None, symptom_result=None,
         mood_result=None, top: int = 10) -> list[dict]:
    """Combine and rank findings across streams (highest score first)."""
    findings: list[dict] = []

    for f in (food_ranking or []):
        dp = f.get("mean_delta_peak_mgdl")
        if dp is not None:
            findings.append(_finding(
                "food", f"{f['item']} raises glucose ~{dp:.0f} mg/dL", round(dp, 1),
                n=f.get("n", 0), detail=f"n={f.get('n')} meals",
                score=abs(dp) * min(f.get("n", 0), 5)))

    for e in (experiment_results or []):
        eff = e.get("effect_abs")
        if eff is not None:
            n_min = min(e.get("n_treated", 0), e.get("n_control", 0))
            findings.append(_finding(
                "experiment", f"{e['tag']} effect {eff:+.0f} mg/dL on {e['outcome']}",
                round(eff, 1), n=n_min, signal=e.get("signal_strength"),
                detail=e.get("causal_label"), score=abs(eff) * min(n_min, 5)))

    if symptom_result and symptom_result.get("lift", {}).get("lift") is not None:
        lift = symptom_result["lift"]
        findings.append(_finding(
            "symptom", f"symptoms {lift['lift']:.1f}× more likely on high-glucose days",
            round(lift["lift"], 2), n=symptom_result.get("n_days", 0),
            detail=f"n_days={symptom_result.get('n_days')}",
            score=abs(lift["lift"] - 1.0) * 10 * min(symptom_result.get("n_days", 0), 10)))

    for c in (mood_result or {}).get("correlations", []):
        if c.get("rho") is not None and c.get("n", 0) >= 5:
            findings.append(_finding(
                "mood", f"{c['affect']} vs {c['glucose_metric']} rho={c['rho']:.2f}",
                round(c["rho"], 2), n=c["n"], detail=f"n={c['n']}",
                score=abs(c["rho"]) * min(c["n"], 10)))

    findings.sort(key=lambda x: x["score"], reverse=True)
    return findings[:top]


def grounded_numbers(findings: list[dict], metrics: dict | None = None) -> list[float]:
    nums: list[float] = []
    for f in findings:
        if isinstance(f.get("value"), (int, float)) and not isinstance(f["value"], bool):
            nums.append(float(f["value"]))
        if isinstance(f.get("n"), (int, float)):
            nums.append(float(f["n"]))
    for entry in (metrics or {}).values():
        v = entry.get("value")
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            nums.append(float(v))
    return nums


def narrate(findings: list[dict], *, metrics: dict | None = None, redact: bool = False) -> str:
    """Build a grounded text summary and verify it through the integrity narration guard."""
    if not findings:
        text = "No findings cleared the evidence threshold this cycle."
        return integrity.sanitize_narration(text, [], redact=redact)
    # bullets (not "1.", "2.") so list indices don't read as ungrounded numbers
    parts = ["Top findings:"]
    for f in findings:
        parts.append("• " + f["label"] + (f" ({f['detail']})." if f.get("detail") else "."))
    text = " ".join(parts)
    return integrity.sanitize_narration(text, grounded_numbers(findings, metrics),
                                        redact=redact, allow_dates=True)


def self_check(findings: list[dict]) -> list[str]:
    violations = []
    scores = [f["score"] for f in findings]
    if scores != sorted(scores, reverse=True):
        violations.append("findings not sorted by descending score")
    return violations
