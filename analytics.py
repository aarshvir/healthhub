"""analytics.py — the TREND ENGINE.

Given ``(metric_set, window_days, group_by)`` it reads the 500-day glucose store, computes a
grouped time series (per day / week / hour, Asia/Dubai local) and an AGP (modal-day
percentile bands) for that window. Switching the window re-reads the store and recomputes.
Supported windows: 7 / 14 / 30 / 90 / 180 / 365 / 500 / any custom positive integer.

All clinical math is reused from clinical.py (which routes through integrity); timestamps are
turned into numpy arrays up front so pandas never reorders a datetimelike column.
"""

from __future__ import annotations

from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

import clinical
import integrity

STANDARD_WINDOWS = (7, 14, 30, 90, 180, 365, 500)
DEFAULT_METRICS = ("mean_mgdl", "gmi_pct", "cv_pct", "tir_pct", "titr_pct",
                   "tbr_pct", "tar_pct", "gri", "mage_mgdl")
_GROUP_FMT = {"day": "%Y-%m-%d", "week": "%G-W%V", "hour": "%Y-%m-%d %H:00"}


def _clean(x):
    if isinstance(x, float) and np.isnan(x):
        return None
    return x


def _metrics_for(values: np.ndarray) -> dict:
    """All trend metrics for a time-ordered glucose array (mg/dL)."""
    cats = clinical.category_percentages(values)
    mean = float(np.mean(values)) if values.size else float("nan")
    sd = float(np.std(values, ddof=1)) if values.size > 1 else float("nan")
    cv = (sd / mean * 100.0) if (values.size > 1 and mean) else float("nan")
    gri, _hypo, _hyper = clinical.gri_components(cats)
    return {
        "mean_mgdl": mean,
        "gmi_pct": clinical.gmi(mean) if not np.isnan(mean) else float("nan"),
        "sd_mgdl": sd,
        "cv_pct": cv,
        "tir_pct": cats["tir"], "titr_pct": cats["titr"],
        "tbr_pct": cats["tbr"], "tar_pct": cats["tar"],
        "vlow_pct": cats["vlow"], "vhigh_pct": cats["vhigh"],
        "gri": gri,
        "mage_mgdl": clinical.mage(values),
    }


def _epoch_ns(measured_series) -> np.ndarray:
    epoch0 = pd.Timestamp("1970-01-01", tz="UTC")
    return ((measured_series - epoch0) // pd.Timedelta(1, "ns")).to_numpy()


def compute(store, *, window_days: int, now=None, group_by: str = "day",
            metric_set=None, subject=None) -> dict:
    """Compute the windowed trend + AGP from the store. Returns a JSON-friendly dict."""
    now = integrity.now_utc() if now is None else now
    if not isinstance(window_days, int) or window_days <= 0:
        raise ValueError("window_days must be a positive integer")
    if group_by not in _GROUP_FMT:
        raise ValueError(f"group_by must be one of {tuple(_GROUP_FMT)}")
    metrics = tuple(metric_set) if metric_set else DEFAULT_METRICS

    df = store.glucose_last_days(window_days, now=now)
    empty_agp = clinical.agp_percentiles(np.array([], dtype=int), np.array([], dtype=float))
    if df.empty:
        return {"window_days": window_days, "group_by": group_by, "n_readings": 0,
                "series": [], "agp": empty_agp, "summary": {}, "generated_at": now.isoformat()}

    measured = pd.to_datetime(df["measured_at"], utc=True)
    local = measured.dt.tz_convert(ZoneInfo(clinical.DISPLAY_TZ))
    values = df["glucose_mgdl"].to_numpy(dtype=float)
    epoch = _epoch_ns(measured)
    hours = local.dt.hour.to_numpy()
    minutes = local.dt.minute.to_numpy()
    slots = (hours.astype(np.int64) * 60 + minutes.astype(np.int64)) // 15
    labels = local.dt.strftime(_GROUP_FMT[group_by]).to_numpy()

    series = []
    for label in sorted(set(labels.tolist())):
        mask = labels == label
        gv = values[mask][np.argsort(epoch[mask], kind="stable")]  # time-order within group
        full = _metrics_for(gv)
        row = {"group": label, "n": int(gv.size)}
        row.update({k: _clean(full.get(k)) for k in metrics})
        series.append(row)

    ordered_all = values[np.argsort(epoch, kind="stable")]
    summary_full = _metrics_for(ordered_all)
    summary = {"n": int(values.size)}
    summary.update({k: _clean(summary_full.get(k)) for k in metrics})

    return {
        "window_days": window_days, "group_by": group_by,
        "n_readings": int(values.size),
        "series": series,
        "agp": clinical.agp_percentiles(slots, values),
        "summary": summary,
        "generated_at": now.isoformat(),
    }


def compute_standard_windows(store, *, now=None, group_by="day", metric_set=None) -> dict:
    """Compute every standard window in one pass (only those the store can cover)."""
    now = integrity.now_utc() if now is None else now
    return {w: compute(store, window_days=w, now=now, group_by=group_by, metric_set=metric_set)
            for w in STANDARD_WINDOWS}


def self_check(result: dict) -> list[str]:
    """Assert trend invariants; return human-readable violations (empty == ok)."""
    violations: list[str] = []
    for row in result.get("series", []) + ([result["summary"]] if result.get("summary") else []):
        tir, tbr, tar = row.get("tir_pct"), row.get("tbr_pct"), row.get("tar_pct")
        if None not in (tir, tbr, tar):
            total = tir + tbr + tar
            if not (99.0 <= total <= 101.0):  # TIR + TBR + TAR must partition ~100%
                violations.append(f"{row.get('group', 'summary')}: TIR+TBR+TAR={total:.1f}")
        for pct_key in ("tir_pct", "titr_pct", "tbr_pct", "tar_pct", "cv_pct"):
            v = row.get(pct_key)
            if v is not None and v < 0:
                violations.append(f"{row.get('group', 'summary')}: negative {pct_key}={v}")
    for band in result.get("agp", []):
        if band is not None:
            ps = [band[k] for k in ("p10", "p25", "p50", "p75", "p90")]
            if ps != sorted(ps):
                violations.append(f"AGP percentile bands not monotonic: {ps}")
    return violations
