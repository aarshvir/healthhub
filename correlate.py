"""correlate.py — cross-stream correlation intelligence over the unified daily frame.

Consumes :func:`daily.build`'s per-day frame and surfaces the strongest ASSOCIATIONS across
every stream at once — glucose, sleep, heart rate, SpO₂, steps, weight, mood, energy,
symptoms, food/carbs, post-meal walks and intimacy:

  * **same-day** Spearman rho for every numeric field pair (rank correlation, ties averaged),
  * **lag-1** Spearman — does a field on day *d* track another field on day *d+1*? A directional
    *hint*, aligned on real calendar adjacency so gaps never fabricate a pairing,
  * **lever** effect sizes — Cohen's d + lift for the binary lifestyle levers (a post-meal walk,
    intimacy) on the continuous outcomes, same-day and next-day.

Design guarantees:
  * Every finding carries its sample size ``n``; findings under the evidence/effect floor are
    dropped, never shown weakly.
  * Nothing is ever described as *causal* — this is observational data, so the vocabulary is
    "tracks with" / "is followed by" / "is higher on", never "causes" / "because".
  * Effects are put on one comparable scale (correlation-equivalent ``r``) so a rank correlation
    and a group-difference can be ranked in the same list.
  * A symmetric correlation matrix is emitted for the dashboard heatmap (full matrix, including
    the definitional within-stream cells the ranked findings deliberately hide).

Narration is routed through :func:`integrity.sanitize_narration`, so a narrator can never
introduce a number that isn't grounded in the findings.
"""

from __future__ import annotations

import math
from datetime import date, timedelta

import numpy as np

import daily
import integrity
import statutil

# evidence / effect floors -------------------------------------------------------------------
MIN_N = 5              # minimum paired days before a correlation is trustworthy enough to show
MIN_RHO = 0.15        # |rho| floor for a same-day finding to make the ranked list
MIN_RHO_LAG = 0.20    # lag-1 is noisier -> a higher floor
MIN_D = 0.25          # |Cohen's d| floor for a lever finding
EVIDENCE_CAP = 30     # n at which the evidence weight saturates to 1.0

# the binary lifestyle levers and the continuous outcomes we test them against
LEVERS = ("walk", "sex")
LEVER_OUTCOMES = ("mean_mgdl", "tir_pct", "cv_pct", "gri", "mage_mgdl", "tar_pct",
                  "mood", "energy", "symptom_count", "sleep_total_min", "sleep_deep_min",
                  "hr_avg")

# finer-grained stream membership than daily's three buckets, so "cross-stream" is meaningful
# (steps↔active_calories or tir↔mean are the SAME stream and definitionally correlated).
STREAMS = {
    "mean_mgdl": "glucose", "tir_pct": "glucose", "titr_pct": "glucose", "tbr_pct": "glucose",
    "tar_pct": "glucose", "cv_pct": "glucose", "gri": "glucose", "mage_mgdl": "glucose",
    "steps": "activity", "active_calories": "activity",
    "sleep_total_min": "sleep", "sleep_deep_min": "sleep", "sleep_rem_min": "sleep",
    "hr_avg": "cardio", "spo2_avg": "cardio",
    "weight_kg": "body",
    "mood": "affect", "energy": "affect",
    "symptom_count": "symptom", "symptom_severity": "symptom",
    "carbs_g": "food", "meal_count": "food",
    "sex": "lifestyle", "walk": "lifestyle", "supplement_count": "lifestyle",
}


def _stream(field: str) -> str:
    return STREAMS.get(field, "other")


def _cross(a: str, b: str) -> bool:
    return _stream(a) != _stream(b)


def _column(frame, field) -> np.ndarray:
    """One field as a float column, missing -> NaN (never guessed)."""
    return np.array([r.get(field) if r.get(field) is not None else np.nan for r in frame],
                    dtype=float)


def _evidence(n: int) -> float:
    """Sample-size weight in [0, 1] — saturates at EVIDENCE_CAP so a huge n can't dwarf effect."""
    return min(int(n), EVIDENCE_CAP) / EVIDENCE_CAP


def _d_to_r(d: float) -> float:
    """Cohen's d -> point-biserial-style r, so group-differences share the rank scale of rho."""
    return d / math.sqrt(d * d + 4.0)


def _r_strength(r: float) -> str:
    a = abs(r)
    if a >= 0.5:
        return "strong"
    if a >= 0.3:
        return "moderate"
    if a >= 0.15:
        return "weak"
    return "negligible"


def _pair_label(a, b, r) -> str:
    la, lb = daily.LABELS.get(a, a), daily.LABELS.get(b, b)
    return f"{la} {'tracks with' if r >= 0 else 'runs opposite to'} {lb}"


def _lag_label(a, b, r) -> str:
    la, lb = daily.LABELS.get(a, a), daily.LABELS.get(b, b)
    return f"Higher {la} is followed by {'higher' if r >= 0 else 'lower'} {lb} the next day"


def _lever_when(lever) -> str:
    return "days with a post-meal walk" if lever == "walk" else "intimacy days"


def _lever_label(lever, outcome, d, *, lag) -> str:
    lo = daily.LABELS.get(outcome, outcome)
    tail = " the next day" if lag else ""
    return f"{lo} is {'higher' if d >= 0 else 'lower'} on {_lever_when(lever)}{tail}"


def _finding(kind, a, b, *, r, n, cross, detail, p=None, ci=None, extra=None) -> dict:
    out = {
        "kind": kind, "a": a, "b": b,
        "label": {"same-day": _pair_label, "lag-1": _lag_label}.get(kind, lambda *_: "")(a, b, r)
        if kind in ("same-day", "lag-1") else "",
        "r": round(float(r), 3),
        "direction": "+" if r >= 0 else "-",
        "n": int(n),
        "cross_stream": bool(cross),
        "strength": _r_strength(r),
        "detail": detail,
        "p": None if p is None else round(float(p), 4),
        "ci": list(ci) if ci and ci[0] is not None else None,
        "significant": False,   # set by the FDR pass in analyze()
        "score": round(abs(float(r)) * _evidence(n), 4),
    }
    if extra:
        out.update(extra)
    return out


def _lag_pairs(frame, a, b):
    """(a on day d, b on day d+1) aligned on real calendar adjacency (gaps skipped)."""
    by_date = {r["date"]: r for r in frame}
    xs, ys = [], []
    for r in frame:
        nxt = (date.fromisoformat(r["date"]) + timedelta(days=1)).isoformat()
        if nxt in by_date:
            xs.append(r.get(a))
            ys.append(by_date[nxt].get(b))
    to_f = lambda v: np.nan if v is None else float(v)  # noqa: E731
    return np.array([to_f(v) for v in xs]), np.array([to_f(v) for v in ys])


def _detrend(a):
    """Linearly detrend a series (remove the slow common drift) before lag-correlating, so a
    reversal's shared downward trend doesn't manufacture spurious next-day 'findings'."""
    a = np.asarray(a, dtype=float)
    mask = ~np.isnan(a)
    if int(mask.sum()) < 4:
        return a
    idx = np.arange(a.size, dtype=float)
    coef = np.polyfit(idx[mask], a[mask], 1)
    out = a.copy()
    out[mask] = a[mask] - np.polyval(coef, idx[mask])
    return out


def matrix(frame, *, fields=None, min_n: int = MIN_N) -> dict:
    """Symmetric same-day Spearman matrix for the heatmap (rho None where n < min_n)."""
    fields = list(fields or daily.NUMERIC_FIELDS)
    cols = {f: _column(frame, f) for f in fields}
    grid = []
    for a in fields:
        row = []
        for b in fields:
            sp = statutil.spearman(cols[a], cols[b])
            rho = sp["rho"] if (sp["n"] >= min_n and sp["rho"] is not None) else None
            row.append({"rho": None if rho is None else round(rho, 3), "n": sp["n"]})
        grid.append(row)
    return {"fields": fields, "labels": [daily.LABELS.get(f, f) for f in fields],
            "grid": grid, "min_n": min_n}


def _same_day_findings(frame, cols, *, cross_only, min_n, min_rho):
    fields = list(cols)
    out = []
    for i, a in enumerate(fields):
        for b in fields[i + 1:]:
            if cross_only and not _cross(a, b):
                continue
            sp = statutil.spearman(cols[a], cols[b])
            if sp["rho"] is None or sp["n"] < min_n or abs(sp["rho"]) < min_rho:
                continue
            fs = statutil.fisher(sp["rho"], sp["n"])
            out.append(_finding("same-day", a, b, r=sp["rho"], n=sp["n"],
                                cross=_cross(a, b), detail=f"n={sp['n']} days",
                                p=fs["p"], ci=fs["ci"]))
    return out


def _lag_findings(frame, *, fields, cross_only, min_n, min_rho):
    out = []
    for a in fields:
        for b in fields:
            if a == b:
                continue
            if cross_only and not _cross(a, b):
                continue
            x, y = _lag_pairs(frame, a, b)
            # detrend both sides so the reversal's shared drift can't fake a next-day link
            sp = statutil.spearman(_detrend(x), _detrend(y))
            if sp["rho"] is None or sp["n"] < min_n or abs(sp["rho"]) < min_rho:
                continue
            fs = statutil.fisher(sp["rho"], sp["n"])
            out.append(_finding("lag-1", a, b, r=sp["rho"], n=sp["n"],
                                cross=_cross(a, b), detail=f"n={sp['n']} day-pairs",
                                p=fs["p"], ci=fs["ci"]))
    return out


def _lever_findings(frame, *, min_n, min_d):
    out = []
    for lever in LEVERS:
        cond = _column(frame, lever)
        cond_bool = cond == 1.0
        for outcome in LEVER_OUTCOMES:
            oc = _column(frame, outcome)
            for lag in (False, True):
                if lag:
                    # lever flag on day d, aligned to the outcome on day d+1
                    lx, ly = _lag_pairs(frame, lever, outcome)
                    treated = ly[lx == 1.0]
                    control = ly[lx == 0.0]
                else:
                    treated = oc[cond_bool & ~np.isnan(oc)]
                    control = oc[(cond == 0.0) & ~np.isnan(oc)]
                treated = treated[~np.isnan(treated)]
                control = control[~np.isnan(control)]
                d = statutil.cohens_d(treated, control)
                nt, nc = int(treated.size), int(control.size)
                if d is None or min(nt, nc) < min_n or abs(d) < min_d:
                    continue
                r = _d_to_r(d)
                fs = statutil.fisher(r, min(nt, nc))
                dci = statutil.cohens_d_ci(d, nt, nc)
                out.append(_finding(
                    "lever", lever, outcome, r=r, n=min(nt, nc), cross=True,
                    detail=f"n={nt} vs {nc} days", p=fs["p"], ci=fs["ci"],
                    extra={"label": _lever_label(lever, outcome, d, lag=lag),
                           "effect_d": round(float(d), 3), "effect_d_ci": list(dci["ci"])
                           if dci["ci"][0] is not None else None, "lag": lag,
                           "n_treated": nt, "n_control": nc}))
    return out


def analyze(frame, *, top: int = 12, min_n: int = MIN_N, cross_stream_only: bool = True) -> dict:
    """Full cross-stream analysis: ranked findings + heatmap matrix + coverage.

    ``cross_stream_only`` hides the definitional within-stream correlations (e.g. TIR vs mean
    glucose) from the ranked list; the returned matrix always keeps every cell.
    """
    fields = list(daily.NUMERIC_FIELDS)
    cols = {f: _column(frame, f) for f in fields}

    findings = []
    findings += _same_day_findings(frame, cols, cross_only=cross_stream_only,
                                   min_n=min_n, min_rho=MIN_RHO)
    findings += _lag_findings(frame, fields=fields, cross_only=cross_stream_only,
                              min_n=min_n, min_rho=MIN_RHO_LAG)
    findings += _lever_findings(frame, min_n=min_n, min_d=MIN_D)

    # Multiple-comparison control: with hundreds of pairwise tests, raw |rho| thresholds are a
    # false-discovery machine. Mark which findings survive Benjamini-Hochberg (FDR 10%).
    flags = statutil.bh_fdr([f.get("p") for f in findings], q=0.10)
    for f, sig in zip(findings, flags):
        f["significant"] = bool(sig)
    findings.sort(key=lambda f: (f["significant"], f["score"]), reverse=True)

    n_sig = sum(1 for f in findings if f["significant"])
    # headline prefers FDR-significant findings; if none clear, still show the top few (flagged)
    headline = [f for f in findings if f["significant"]][:top] or findings[:min(top, 6)]

    return {
        "findings": headline,
        "all_findings": findings,
        "n_significant": n_sig,
        "matrix": matrix(frame, fields=fields, min_n=min_n),
        "coverage": daily.coverage(frame),
        "n_days": len(frame),
        "min_n": min_n,
    }


def grounded_numbers(findings) -> list[float]:
    """Every number a narrator is allowed to repeat (sample sizes carried by the findings)."""
    nums: list[float] = []
    for f in findings:
        for key in ("n", "n_treated", "n_control"):
            v = f.get(key)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                nums.append(float(v))
    return nums


def narrate(findings, *, top: int = 6, redact: bool = False) -> str:
    """Grounded, non-causal prose summary, verified through the integrity narration guard."""
    picked = findings[:top]
    if not picked:
        text = "No cross-stream associations cleared the evidence threshold yet — keep logging."
        return integrity.sanitize_narration(text, [], redact=redact)
    parts = ["Cross-stream associations (observational, not causal):"]
    for f in picked:
        parts.append("• " + f["label"] + f" ({f['detail']}).")
    text = " ".join(parts)
    return integrity.sanitize_narration(text, grounded_numbers(picked), redact=redact,
                                        allow_dates=True)


def self_check(result: dict) -> list[str]:
    violations = []
    scores = [f["score"] for f in result.get("findings", [])]
    if scores != sorted(scores, reverse=True):
        violations.append("findings not sorted by descending score")
    for f in result.get("all_findings", []):
        if abs(f["r"]) > 1.0:
            violations.append(f"|r|>1 in finding {f['a']}~{f['b']}: {f['r']}")
        if f["n"] < result.get("min_n", MIN_N):
            violations.append(f"finding below min_n: {f['a']}~{f['b']} n={f['n']}")
    mtx = result.get("matrix", {})
    fields = mtx.get("fields", [])
    grid = mtx.get("grid", [])
    if len(grid) != len(fields) or any(len(row) != len(fields) for row in grid):
        violations.append("correlation matrix is not square")
    return violations
