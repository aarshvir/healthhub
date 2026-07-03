"""reversal.py — the diabetes-reversal narrative layer: remission ladder, GMI projection,
and a doctor-ready priority list. All numbers are computed deterministically from the store
(via clinical/daily), never invented.

Why this exists: for someone who just crossed into Type 2 diabetes (HbA1c 7.1), the single most
motivating number is *"how far am I from getting back out of the diabetic range?"*. GMI (the
CGM-estimated HbA1c) answers that continuously, and it maps to a clear milestone ladder:

    mean ≈ 133 → GMI < 6.5  (out of the diabetic range)
    mean ≈ 112 → GMI < 6.0  (strong control)
    mean ≈ 100 → GMI = 5.7  (non-diabetic range)

This module turns the current mean into where-you-are-on-the-ladder, projects the 90-day GMI
trajectory from the trend (clearly labelled a projection, never a promise), and assembles the
short list of findings that belong with a physician regardless of the dashboard.
"""

from __future__ import annotations

from datetime import date

import numpy as np

import clinical

# GMI milestones to drop BELOW, hardest-earned last (Bergenstal GMI = 3.31 + 0.02392*mean).
MILESTONES = (
    (6.5, "Out of the diabetic range"),
    (6.0, "Strong control"),
    (5.7, "Non-diabetic range"),
)
_LADDER_TOP_GMI = 7.5    # a reference "just-diagnosed" ceiling for the overall progress bar
_LADDER_FLOOR_GMI = MILESTONES[-1][0]


def mean_for_gmi(gmi_target: float) -> float:
    """The mean glucose (mg/dL) that yields a given GMI% — inverse of clinical.gmi()."""
    return (gmi_target - 3.31) / 0.02392


def ladder(current_mean: float | None) -> dict:
    """Where the current mean sits on the remission ladder + distance to the next rung."""
    if current_mean is None or (isinstance(current_mean, float) and np.isnan(current_mean)):
        return {"ok": False}
    current_gmi = clinical.gmi(current_mean)
    rungs = []
    for gmi_t, label in MILESTONES:
        rungs.append({"gmi": gmi_t, "label": label, "mean": round(mean_for_gmi(gmi_t), 1),
                      "achieved": current_gmi <= gmi_t})
    # next rung = the nearest milestone still above (i.e., not yet achieved), easiest first
    nxt = next((r for r in rungs if not r["achieved"]), None)
    overall = float(np.clip((_LADDER_TOP_GMI - current_gmi)
                            / (_LADDER_TOP_GMI - _LADDER_FLOOR_GMI), 0.0, 1.0))
    out = {
        "ok": True,
        "current_mean": round(float(current_mean), 1),
        "current_gmi": round(float(current_gmi), 2),
        "rungs": rungs,
        "overall_progress": round(overall, 3),
        "goal_reached": nxt is None,
    }
    if nxt is not None:
        out["next"] = {
            "gmi": nxt["gmi"], "label": nxt["label"], "mean_needed": nxt["mean"],
            "mean_gap": round(float(current_mean) - nxt["mean"], 1),
            "gmi_gap": round(float(current_gmi) - nxt["gmi"], 2),
        }
    return out


def _series(frame) -> tuple[np.ndarray, np.ndarray]:
    xs, ys = [], []
    for r in frame:
        v = r.get("mean_mgdl")
        if v is not None:
            xs.append(date.fromisoformat(r["date"]).toordinal())
            ys.append(float(v))
    return np.array(xs, dtype=float), np.array(ys, dtype=float)


def project(frame, *, horizon_days: int = 90, min_days: int = 14) -> dict:
    """Least-squares projection of mean glucose → GMI *horizon_days* out.

    Deterministic OLS on (day-ordinal, daily mean). Returns ``ok=False`` when there are too
    few days to trust a slope. This is a *trajectory at the current trend*, explicitly not a
    guarantee — the dashboard labels it as such.
    """
    x, y = _series(frame)
    n = int(x.size)
    if n < min_days or float(np.ptp(x)) == 0:
        return {"ok": False, "n": n, "horizon_days": horizon_days}
    x0 = x - x[0]
    slope, intercept = np.polyfit(x0, y, 1)
    fit = slope * x0 + intercept
    ss_res = float(np.sum((y - fit) ** 2))
    ss_tot = float(np.sum((y - y.mean()) ** 2))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
    current_fit = float(slope * x0[-1] + intercept)
    projected_mean = float(np.clip(slope * (x0[-1] + horizon_days) + intercept, 40.0, 400.0))
    # only call a direction when the line actually explains something; noise reads as flat.
    if r2 < 0.10:
        direction = "flat"
    else:
        direction = "improving" if slope < -0.05 else "worsening" if slope > 0.05 else "flat"
    return {
        "ok": True, "n": n, "horizon_days": horizon_days,
        "slope_per_month": round(float(slope) * 30.0, 2),
        "current_mean": round(current_fit, 1),
        "current_gmi": round(clinical.gmi(current_fit), 2),
        "projected_mean": round(projected_mean, 1),
        "projected_gmi": round(clinical.gmi(projected_mean), 2),
        "r2": round(float(np.clip(r2, 0.0, 1.0)), 2),
        "direction": direction,
    }


def doctor_list(*, labs_panel=None, metrics=None) -> list[dict]:
    """The short list that belongs with a physician — driven by the user's own data.

    Non-alarmist and evidence-anchored: each item names what triggered it. Standing care-note
    pairings (diabetic HbA1c; high inflammation; prolactin+testosterone) surface when the data
    supports them.
    """
    items: list[dict] = []
    markers = (labs_panel or {}).get("markers", {})

    def gmi_val():
        entry = (metrics or {}).get("gmi_pct") or {}
        v = entry.get("value")
        return v if isinstance(v, (int, float)) else None

    # 1) diabetic-range glycemia -> endocrinologist. Cite the marker that ACTUALLY crossed
    # 6.5 (a §A provenance rule: never attribute the claim to a value that didn't trigger it).
    a1c = markers.get("hba1c", {}).get("value")
    g = gmi_val()
    a1c_high = a1c is not None and a1c >= 6.5
    gmi_high = g is not None and g >= 6.5
    if a1c_high or gmi_high:
        basis = f"HbA1c {a1c:g}%" if a1c_high else f"GMI {g:.1f}%"
        items.append({"priority": 1, "area": "Glycemia",
                      "text": "Diabetic-range glycemia — confirm management with an endocrinologist.",
                      "based_on": basis})
    # 2) inflammation
    crp = markers.get("hs_crp")
    if crp and crp["status"] in ("warning", "critical"):
        items.append({"priority": 1 if crp["status"] == "critical" else 2, "area": "Inflammation",
                      "text": "Elevated hs-CRP — ask a doctor to confirm nothing acute is driving it.",
                      "based_on": f"hs-CRP {crp['value']:g} {crp['unit']}"})
    # 3) the prolactin + low-testosterone pairing (needs a doctor together)
    prl = markers.get("prolactin")
    tes = markers.get("total_testosterone")
    prl_high = prl and prl["status"] in ("warning", "critical")
    tes_low = tes and tes["status"] in ("warning", "critical")
    if prl_high and tes_low:
        items.append({"priority": 1, "area": "Hormones",
                      "text": "High prolactin with low testosterone — this pairing warrants a doctor's review.",
                      "based_on": f"prolactin {prl['value']:g}, testosterone {tes['value']:g}"})
    elif prl_high:
        items.append({"priority": 2, "area": "Hormones",
                      "text": "Elevated prolactin — worth a doctor's review.",
                      "based_on": f"prolactin {prl['value']:g} {prl['unit']}"})
    # 4) any other critical lab not already covered
    covered = {"hs_crp", "prolactin", "total_testosterone", "hba1c"}
    for m in (labs_panel or {}).get("critical", []):
        if m["key"] not in covered:
            items.append({"priority": 2, "area": m["group"],
                          "text": f"{m['name']} is out of range — review at your next visit.",
                          "based_on": f"{m['name']} {m['value']:g} {m['unit']} (ref {m['ref']})"})
    items.sort(key=lambda it: it["priority"])
    return items


def reconcile(metrics=None, labs_panel=None) -> dict:
    """The glycation gap: CGM-estimated A1c (GMI) vs the entered lab HbA1c.

    Bergenstal 2018 is explicit that GMI is an *estimate* and often differs from measured A1c;
    a discordance ≥0.5% is common and clinically meaningful (hemoglobinopathy, iron deficiency,
    altered RBC turnover — all plausible in a high-inflammation male). Surfacing the gap keeps
    the cockpit honest about which number is which.
    """
    g = ((metrics or {}).get("gmi_pct") or {}).get("value")
    a1c = (((labs_panel or {}).get("markers", {}) or {}).get("hba1c") or {}).get("value")
    if not isinstance(g, (int, float)) or not isinstance(a1c, (int, float)):
        return {"ok": False}
    gap = float(g) - float(a1c)
    if abs(gap) < 0.5:
        note = "Your CGM estimate agrees with your lab A1c."
    else:
        note = (f"Your CGM estimate reads {'higher' if gap > 0 else 'lower'} than your lab A1c "
                f"by {abs(gap):.1f}% — worth mentioning to your doctor.")
    return {"ok": True, "gmi_pct": round(float(g), 2), "lab_hba1c_pct": round(float(a1c), 2),
            "gap_pct": round(gap, 2), "discordant": abs(gap) >= 0.5, "note": note}


# DiRECT trial remission likelihood by weight lost from baseline (Lean 2018)
_DIRECT_BANDS = ((15.0, 86), (10.0, 57), (5.0, 34), (0.0, 7))


def weight_view(frame, *, labs_panel=None) -> dict:
    """Weight loss from baseline + a DiRECT-anchored remission-likelihood band.

    Weight is the strongest reversal lever for this profile, so it sits beside the GMI ladder as
    a co-primary. Baseline = the earliest logged weight in the window; the band is the DiRECT
    trial's observed remission rate at that amount of loss (observational, framed as odds).
    """
    ws = [(r["date"], r["weight_kg"]) for r in (frame or []) if r.get("weight_kg") is not None]
    if len(ws) < 2:
        return {"ok": False}
    baseline = ws[0][1]
    current = ws[-1][1]
    kg_lost = baseline - current
    pct = (kg_lost / baseline * 100.0) if baseline else 0.0
    band_pct = next((p for thr, p in _DIRECT_BANDS if kg_lost >= thr), 7)
    bmi = (((labs_panel or {}).get("markers", {}) or {}).get("bmi") or {}).get("value")
    return {"ok": True, "baseline_kg": round(baseline, 1), "current_kg": round(current, 1),
            "kg_lost": round(kg_lost, 1), "pct_lost": round(pct, 1),
            "remission_band_pct": band_pct, "bmi": bmi,
            "note": (f"At {kg_lost:.1f} kg lost, the DiRECT trial saw ~{band_pct}% reach remission."
                     if kg_lost >= 0.5 else "Logging weight regularly powers this estimate.")}


def build(frame, *, metrics=None, labs_panel=None, horizon_days: int = 90) -> dict:
    """Assemble the full reversal view: ladder + projection + reconciliation + doctor list."""
    current_mean = None
    entry = (metrics or {}).get("mean_mgdl") or {}
    if isinstance(entry.get("value"), (int, float)):
        current_mean = entry["value"]
    if current_mean is None:  # fall back to the most recent day in the frame
        for r in reversed(frame or []):
            if r.get("mean_mgdl") is not None:
                current_mean = r["mean_mgdl"]
                break
    return {
        "ladder": ladder(current_mean),
        "projection": project(frame or [], horizon_days=horizon_days),
        "reconcile": reconcile(metrics=metrics, labs_panel=labs_panel),
        "weight": weight_view(frame or [], labs_panel=labs_panel),
        "doctor_list": doctor_list(labs_panel=labs_panel, metrics=metrics),
    }


def self_check(view: dict) -> list[str]:
    violations = []
    lad = view.get("ladder", {})
    if lad.get("ok"):
        p = lad.get("overall_progress")
        if p is not None and not (0.0 <= p <= 1.0):
            violations.append(f"ladder progress out of [0,1]: {p}")
    proj = view.get("projection", {})
    if proj.get("ok"):
        if not (0.0 <= proj.get("r2", 0) <= 1.0):
            violations.append(f"projection r2 out of [0,1]: {proj.get('r2')}")
        if not (40.0 <= proj.get("projected_mean", 100) <= 400.0):
            violations.append("projected mean implausible")
    for it in view.get("doctor_list", []):
        if it.get("priority") not in (1, 2, 3):
            violations.append(f"doctor item bad priority: {it.get('priority')}")
    return violations
