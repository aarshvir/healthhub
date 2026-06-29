"""clinical.py — glucose analytics over a chosen window, computed from the glucose store.

Everything trustworthy here is obtained THROUGH :mod:`integrity` (the single trust
boundary): the raw store is validated with ``integrity.validate_readings``, recency is
judged with ``integrity.freshness``, the last-known-good value flows through the integrity
cache, and the wall clock is read only via ``integrity.now_utc``. This module never
coerces values, parses timestamps, or reads the clock on its own — the architecture test
(``tests/test_architecture.py``) fails the build if it tries.

Metrics produced (each serialized as ``{value, source, ts, valid}`` to ``metrics.json``):

* ``gmi_pct``      — Glucose Management Indicator = 3.31 + 0.02392*mean_mgdl  (Bergenstal 2018)
* ``mean_mgdl``, ``sd_mgdl`` (sample SD, ddof=1), ``cv_pct`` = SD/mean*100, ``cv_flag``
  (excellent <30, good <36) (Battelino 2019 consensus)
* ``tir_pct``  — Time In Range,  70-180 mg/dL
* ``titr_pct`` — Time In TIGHT Range, 70-140 mg/dL  (the reversal / tight-control metric)
* ``tbr_pct`` (<70), ``vlow_pct`` (<54), ``low_pct`` (54-<70)
* ``tar_pct`` (>180), ``vhigh_pct`` (>250), ``high_pct`` (180-250)
* ``mage_mgdl`` — Mean Amplitude of Glycemic Excursions >1 SD (Service 1970; algorithm
  per Baghurst 2011 — see :func:`mage`)
* ``gri``, ``gri_hypo_component``, ``gri_hyper_component`` — Glycemia Risk Index (Klonoff 2022)
* ``dawn_delta_mgdl`` — mean(04:00-08:00) - mean(00:00-04:00), Asia/Dubai local time
* ``agp_percentiles`` — 10/25/50/75/90 modal-day bands over 96 fifteen-minute slots-of-day
* ``daily_metabolic_score`` — transparent 0-100 weighted blend (see :func:`metabolic_score`)
* ``spike_count`` — upward crossings of 180 mg/dL
"""

from __future__ import annotations

import dataclasses
import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

import integrity

# Display/clinical timezone for time-of-day metrics (dawn window, AGP slots).
DISPLAY_TZ = "Asia/Dubai"
METRIC = "glucose_mgdl"

# --- glycemic category cutpoints (mg/dL) -------------------------------------------
# Clinical category bounds (distinct from integrity's physiological-plausibility range).
# Partition (mutually exclusive, exhaustive): VLOW < 54 <= LOW < 70 <= TIR <= 180
#                                             < HIGH <= 250 < VHIGH.
CUT_VLOW = 54.0     # below -> very low
CUT_LOW = 70.0      # [54,70) low ; >=70 enters range
CUT_TIR_HI = 180.0  # (70..180] time-in-range
CUT_TITR_HI = 140.0 # (70..140] tight range
CUT_VHIGH = 250.0   # (180..250] high ; >250 very high

SPIKE_THRESHOLD = 180.0

# --- daily metabolic score weights (documented, reproducible — NOT a black box) ----
# score = W_TITR*titr_score + W_CV*cv_score + W_SPIKE*spike_score + W_WALK*walk_score
# Each component is mapped to a 0-100 "goodness" sub-score (higher is healthier).
W_TITR, W_CV, W_SPIKE, W_WALK = 0.40, 0.25, 0.20, 0.15
CV_GOOD_CEILING = 60.0   # cv >= 60% -> 0 goodness
CV_EXCELLENT = 30.0      # cv <= 30% -> 100 goodness
SPIKE_PENALTY = 10.0     # goodness points lost per spike (0 at >= 10 spikes)


@dataclasses.dataclass(frozen=True, slots=True)
class Metric:
    """One reported metric, serialized as ``{value, source, ts, valid}``."""
    value: object
    source: str
    ts: str | None
    valid: bool

    def as_dict(self) -> dict:
        return {"value": self.value, "source": self.source, "ts": self.ts, "valid": self.valid}


# ======================================================================================
# Pure metric kernels (operate on plain numpy arrays — easy to unit-test in isolation)
# ======================================================================================
def gmi(mean_mgdl: float) -> float:
    """Glucose Management Indicator (%) — Bergenstal et al., Diabetes Care 2018."""
    return 3.31 + 0.02392 * mean_mgdl


def category_percentages(values: np.ndarray) -> dict[str, float]:
    """Return the glycemic-category time percentages for *values* (mg/dL)."""
    g = np.asarray(values, dtype=float)
    g = g[~np.isnan(g)]
    n = g.size
    if n == 0:
        return {k: float("nan") for k in
                ("vlow", "low", "tir", "high", "vhigh", "tbr", "tar", "titr")}

    def pct(mask) -> float:
        return 100.0 * float(np.count_nonzero(mask)) / n

    vlow = g < CUT_VLOW
    low = (g >= CUT_VLOW) & (g < CUT_LOW)
    tir = (g >= CUT_LOW) & (g <= CUT_TIR_HI)
    high = (g > CUT_TIR_HI) & (g <= CUT_VHIGH)
    vhigh = g > CUT_VHIGH
    titr = (g >= CUT_LOW) & (g <= CUT_TITR_HI)
    return {
        "vlow": pct(vlow), "low": pct(low), "tir": pct(tir),
        "high": pct(high), "vhigh": pct(vhigh),
        "tbr": pct(g < CUT_LOW), "tar": pct(g > CUT_TIR_HI), "titr": pct(titr),
    }


def cv_flag(cv_pct: float) -> str:
    """Variability quality flag: 'excellent' (<30), 'good' (<36), else 'high'."""
    if np.isnan(cv_pct):
        return "unknown"
    if cv_pct < 30.0:
        return "excellent"
    if cv_pct < 36.0:
        return "good"
    return "high"


def gri_components(cats: dict[str, float]) -> tuple[float, float, float]:
    """Glycemia Risk Index (Klonoff et al., J Diabetes Sci Technol 2022).

    Hypo  = VLow + 0.8*Low ;  Hyper = VHigh + 0.5*High
    GRI   = min(100, 3.0*Hypo + 1.6*Hyper)

    Constant check vs the published index: 3.0*(VLow+0.8*Low) + 1.6*(VHigh+0.5*High)
    == 3.0*VLow + 2.4*Low + 1.6*VHigh + 0.8*High, which is exactly the GRI of Klonoff 2022.
    """
    hypo = cats["vlow"] + 0.8 * cats["low"]
    hyper = cats["vhigh"] + 0.5 * cats["high"]
    gri = min(100.0, 3.0 * hypo + 1.6 * hyper)
    return gri, hypo, hyper


def mage(values: np.ndarray) -> float:
    """Mean Amplitude of Glycemic Excursions exceeding 1 SD.

    Original definition: Service FJ et al., *Diabetes* 1970;19:644-655. The reproducible
    turning-point algorithm follows Baghurst PA, *Diabetes Technol Ther* 2011;13:296-302:

    1. SD = sample standard deviation (ddof=1) of the series.
    2. Collapse consecutive-equal runs (so a flat-topped plateau is still recognised as a
       single turning point — plateaus are common in CGM traces), then identify turning
       points: the two endpoints plus every interior local extremum (slope sign change).
    3. Excursions are the absolute differences between consecutive turning points.
    4. MAGE = mean amplitude of the excursions whose amplitude exceeds 1 SD.

    Returns ``nan`` if fewer than 3 readings; ``0.0`` if no excursion exceeds 1 SD.
    """
    g = np.asarray(values, dtype=float)
    g = g[~np.isnan(g)]
    if g.size < 3:
        return float("nan")
    sd = float(g.std(ddof=1))
    if sd == 0.0:
        return 0.0

    # Collapse runs of equal values; otherwise a plateau (e.g. 100,150,150,150,100) yields
    # a zero slope-product at the plateau and the excursion is missed entirely.
    gd = g[np.concatenate(([True], np.diff(g) != 0))]

    turning = [0]
    for i in range(1, gd.size - 1):
        if (gd[i] - gd[i - 1]) * (gd[i + 1] - gd[i]) < 0:  # slope sign change -> extremum
            turning.append(i)
    turning.append(gd.size - 1)

    extrema = gd[turning]
    amplitudes = np.abs(np.diff(extrema))
    qualifying = amplitudes[amplitudes > sd]
    if qualifying.size == 0:
        return 0.0
    return float(qualifying.mean())


def spike_count(values: np.ndarray, threshold: float = SPIKE_THRESHOLD) -> int:
    """Number of upward crossings of *threshold* (rising edges <=thr -> >thr)."""
    g = np.asarray(values, dtype=float)
    g = g[~np.isnan(g)]
    if g.size < 2:
        return 0
    above = g > threshold
    return int(np.count_nonzero(~above[:-1] & above[1:]))


def metabolic_score(titr_pct: float, cv_pct: float, spikes: int,
                    walk_adherence: float) -> float:
    """Daily Metabolic Score (0-100, higher = healthier) — transparent weighted blend.

    Components, each normalized to a 0-100 "goodness":
      * titr_score  = titr_pct                                       (in range already)
      * cv_score    = clip(100*(CEIL-cv)/(CEIL-EXC), 0, 100)         (30%->100, 60%->0)
      * spike_score = clip(100 - SPIKE_PENALTY*spikes, 0, 100)       (10 pts/spike)
      * walk_score  = 100*walk_adherence                            (post-meal-walk fraction)
    Final = W_TITR*titr + W_CV*cv + W_SPIKE*spike + W_WALK*walk, weights summing to 1.0.
    Fully reproducible from the four inputs and the module-level weight constants.
    """
    titr_score = float(np.clip(titr_pct, 0.0, 100.0))
    cv_score = float(np.clip(100.0 * (CV_GOOD_CEILING - cv_pct)
                             / (CV_GOOD_CEILING - CV_EXCELLENT), 0.0, 100.0))
    spike_score = float(np.clip(100.0 - SPIKE_PENALTY * spikes, 0.0, 100.0))
    walk_score = float(np.clip(100.0 * walk_adherence, 0.0, 100.0))
    return (W_TITR * titr_score + W_CV * cv_score
            + W_SPIKE * spike_score + W_WALK * walk_score)


def dawn_delta(local_hours: np.ndarray, values: np.ndarray) -> float:
    """mean(04:00-08:00) - mean(00:00-04:00) using *local_hours* (Asia/Dubai)."""
    h = np.asarray(local_hours, dtype=float)
    g = np.asarray(values, dtype=float)
    night = g[(h >= 0) & (h < 4)]
    dawn = g[(h >= 4) & (h < 8)]
    if night.size == 0 or dawn.size == 0:
        return float("nan")
    return float(dawn.mean() - night.mean())


def agp_percentiles(slots: np.ndarray, values: np.ndarray) -> list:
    """Modal-day bands: for each of 96 fifteen-minute slots-of-day, the 10/25/50/75/90
    percentiles of all readings falling in that slot. Empty slots -> ``None``.
    """
    s = np.asarray(slots, dtype=int)
    g = np.asarray(values, dtype=float)
    bands: list = []
    for slot in range(96):
        vals = g[s == slot]
        if vals.size == 0:
            bands.append(None)
        else:
            bands.append({f"p{p}": float(np.percentile(vals, p))
                          for p in (10, 25, 50, 75, 90)})
    return bands


# ======================================================================================
# Orchestration: validate the store through integrity, compute, stamp validity, persist
# ======================================================================================
def _to_tidy(store: pd.DataFrame, subject) -> pd.DataFrame:
    """Project a glucose store into integrity's tidy reading schema.

    Accepts either the tidy schema already (``subject, metric, value, measured_at``) or a
    glucose-store shape with a ``glucose_mgdl`` column and a ``measured_at``/``timestamp``
    column. The result is handed to ``integrity.validate_readings`` — we never coerce here.
    """
    cols = set(store.columns)
    if {"metric", "value"} <= cols:
        tidy = store.copy()
    else:
        ts_col = "measured_at" if "measured_at" in cols else "timestamp"
        if ts_col not in cols or METRIC not in cols:
            raise integrity.IntegrityError(
                "glucose store needs a glucose_mgdl column and a measured_at/timestamp column"
            )
        tidy = pd.DataFrame({
            "subject": store["subject"] if "subject" in cols else (subject if subject is not None else "patient"),
            "metric": METRIC,
            "value": store[METRIC],
            "measured_at": store[ts_col],
        })
    if "subject" not in tidy.columns:
        tidy["subject"] = subject if subject is not None else "patient"
    return tidy


def compute_metrics(
    store: pd.DataFrame,
    *,
    window: tuple | None = None,
    subject=None,
    now: datetime | None = None,
    walk_adherence: float | None = None,
    source_label: str = "glucose_store",
) -> dict[str, dict]:
    """Compute all glucose metrics over *window* and return ``{name: {value, source, ts, valid}}``.

    The store is validated through ``integrity.validate_readings`` (bad/out-of-range rows
    are dropped and make the run ``valid=False``). Recency is judged with
    ``integrity.freshness`` against *now* (default ``integrity.now_utc()``); EXPIRED or
    FUTURE latest data makes every metric ``valid=False`` (values are still reported, but
    flagged untrusted). The latest good reading is written to the integrity last-known-good
    cache; when the window has no usable data we fall back to that cache if available.
    """
    evaluated_at = integrity.freshness(integrity.now_utc() if now is None else now,
                                       now=now).evaluated_at

    report = integrity.validate_readings(_to_tidy(store, subject), raise_on_error=False)
    clean = report.clean
    data_ok = report.ok

    # Normalize timestamps to numpy arrays (epoch-ns + local hour/minute) and do ALL masking
    # and sorting in numpy. We deliberately never reorder a pandas datetimelike column (via
    # .iloc[array] or sort_values): pandas' datetimelike `take` can segfault on some
    # numpy/pandas/CPython build combinations. Working in numpy is both robust and faster.
    if len(clean):
        measured_utc = pd.to_datetime(clean["measured_at"], utc=True)
        local = measured_utc.dt.tz_convert(ZoneInfo(DISPLAY_TZ))
        epoch0 = pd.Timestamp("1970-01-01", tz="UTC")
        epoch_ns = ((measured_utc - epoch0) // pd.Timedelta(1, "ns")).to_numpy()
        hour_arr = local.dt.hour.to_numpy()
        minute_arr = local.dt.minute.to_numpy()
        value_arr = clean["value"].to_numpy(dtype=float)

        if window is not None:
            start, end = window
            start_ns = (pd.Timestamp(integrity.freshness(start, now=evaluated_at).measured_at)
                        - epoch0) // pd.Timedelta(1, "ns")
            end_ns = (pd.Timestamp(integrity.freshness(end, now=evaluated_at).measured_at)
                      - epoch0) // pd.Timedelta(1, "ns")
            mask_arr = (epoch_ns >= start_ns) & (epoch_ns <= end_ns)
        else:
            mask_arr = np.ones(epoch_ns.shape, dtype=bool)

        order = np.argsort(epoch_ns[mask_arr], kind="stable")
        values = value_arr[mask_arr][order]
        local_hours = hour_arr[mask_arr][order].astype(np.int64)
        minutes_sorted = minute_arr[mask_arr][order].astype(np.int64)
        slots = (local_hours * 60 + minutes_sorted) // 15
        epochs_sorted = epoch_ns[mask_arr][order]
    else:
        values = np.array([], dtype=float)
        local_hours = np.array([], dtype=np.int64)
        slots = np.array([], dtype=np.int64)
        epochs_sorted = np.array([], dtype=np.int64)

    subj_key = subject if subject is not None else (
        clean["subject"].iloc[0] if len(clean) else "patient")

    # --- recency gate + last-known-good cache (both via integrity) ---------------------
    latest_ts = None
    fresh_state = None
    if values.size:
        latest_dt = pd.Timestamp(int(epochs_sorted[-1]), unit="ns", tz="UTC").to_pydatetime()
        fresh = integrity.freshness(latest_dt, now=evaluated_at)
        fresh_state = fresh.state
        latest_ts = fresh.measured_at.isoformat()
        if fresh_state not in (integrity.FreshnessState.EXPIRED, integrity.FreshnessState.FUTURE):
            integrity.cache_set(METRIC, subj_key, float(values[-1]), latest_dt)
    else:
        # No usable window data: degrade to last-known-good if integrity still trusts one.
        try:
            entry = integrity.cache_get(METRIC, subj_key, now=evaluated_at)
            latest_ts = entry.measured_at.isoformat()
        except integrity.CacheMiss:
            latest_ts = None

    data_fresh = fresh_state in (integrity.FreshnessState.FRESH, integrity.FreshnessState.STALE)
    base_valid = data_ok and data_fresh and values.size > 0

    # --- compute every metric ---------------------------------------------------------
    cats = category_percentages(values)
    mean_mgdl = float(np.mean(values)) if values.size else float("nan")
    sd_mgdl = float(np.std(values, ddof=1)) if values.size > 1 else float("nan")
    cv_pct = (sd_mgdl / mean_mgdl * 100.0) if (values.size > 1 and mean_mgdl) else float("nan")
    gri, hypo, hyper = gri_components(cats)
    mage_val = mage(values)
    spikes = spike_count(values)
    dawn = dawn_delta(local_hours, values)
    agp = agp_percentiles(slots, values)

    if walk_adherence is None:
        score_val: float | None = None
    else:
        score_val = metabolic_score(cats["titr"], cv_pct if not np.isnan(cv_pct) else 0.0,
                                     spikes, walk_adherence)

    src = f"{source_label}/{METRIC}"

    def finite(x) -> bool:
        return isinstance(x, (int, float)) and not (isinstance(x, float) and np.isnan(x))

    def m(value, *, valid=None, source=src) -> Metric:
        ok = base_valid if valid is None else (base_valid and valid)
        if not finite(value) and not isinstance(value, (list, str)):
            ok = False
        return Metric(value=value, source=source, ts=latest_ts, valid=ok)

    metrics: dict[str, Metric] = {
        "n_readings": Metric(int(values.size), source=src, ts=latest_ts, valid=data_ok),
        "gmi_pct": m(gmi(mean_mgdl) if not np.isnan(mean_mgdl) else None),
        "mean_mgdl": m(mean_mgdl if not np.isnan(mean_mgdl) else None),
        "sd_mgdl": m(sd_mgdl if not np.isnan(sd_mgdl) else None),
        "cv_pct": m(cv_pct if not np.isnan(cv_pct) else None),
        "cv_flag": Metric(cv_flag(cv_pct), source=src, ts=latest_ts,
                          valid=base_valid and not np.isnan(cv_pct)),
        "tir_pct": m(cats["tir"] if not np.isnan(cats["tir"]) else None),
        "titr_pct": m(cats["titr"] if not np.isnan(cats["titr"]) else None),
        "tbr_pct": m(cats["tbr"] if not np.isnan(cats["tbr"]) else None),
        "tar_pct": m(cats["tar"] if not np.isnan(cats["tar"]) else None),
        "vlow_pct": m(cats["vlow"] if not np.isnan(cats["vlow"]) else None),
        "low_pct": m(cats["low"] if not np.isnan(cats["low"]) else None),
        "high_pct": m(cats["high"] if not np.isnan(cats["high"]) else None),
        "vhigh_pct": m(cats["vhigh"] if not np.isnan(cats["vhigh"]) else None),
        "mage_mgdl": m(mage_val if not np.isnan(mage_val) else None),
        "gri": m(gri if not np.isnan(gri) else None),
        "gri_hypo_component": m(hypo if not np.isnan(hypo) else None),
        "gri_hyper_component": m(hyper if not np.isnan(hyper) else None),
        "dawn_delta_mgdl": m(dawn if not np.isnan(dawn) else None,
                             valid=not np.isnan(dawn)),
        "spike_count": Metric(spikes, source=src, ts=latest_ts, valid=base_valid),
        "agp_percentiles": Metric(agp, source=src, ts=latest_ts,
                                  valid=base_valid and any(b is not None for b in agp)),
        "daily_metabolic_score": Metric(
            score_val, source=(src if walk_adherence is not None
                               else f"{src} (walk_adherence not provided)"),
            ts=latest_ts, valid=base_valid and score_val is not None),
    }
    return {name: metric.as_dict() for name, metric in metrics.items()}


def write_metrics(metrics: dict[str, dict], path: str = "metrics.json") -> str:
    """Serialize the metrics mapping to *path* as JSON; returns the path written."""
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(metrics, fh, indent=2, sort_keys=True, allow_nan=False)
    return path
