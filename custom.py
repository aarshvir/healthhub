"""custom.py — CUSTOM ANALYTICS (§C): ad-hoc glucose queries over the 500-day store.

A small composable surface for questions the fixed dashboards don't answer, e.g.
"my TIR overnight (00:00-06:00)", "mean glucose on weekends vs weekdays", "GRI by hour".
Filters are by local time-of-day (Asia/Dubai, with wrap-around) and weekday; the chosen
metric reuses the clinical kernels. Everything is integrity-routed and reports ``n``.
"""

from __future__ import annotations

from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

import clinical
import integrity

METRICS = ("tir_pct", "titr_pct", "tbr_pct", "tar_pct", "mean_mgdl", "cv_pct",
           "gmi_pct", "gri", "mage_mgdl")


def _metric(values: np.ndarray, name: str):
    if name not in METRICS:
        raise ValueError(f"metric must be one of {METRICS}")
    if values.size == 0:
        return None
    cats = clinical.category_percentages(values)
    if name in ("tir_pct", "titr_pct", "tbr_pct", "tar_pct"):
        return cats[name[:-4]]
    if name == "mean_mgdl":
        return float(np.mean(values))
    if name == "gmi_pct":
        return clinical.gmi(float(np.mean(values)))
    if name == "cv_pct":
        if values.size < 2:
            return None
        m = float(np.mean(values))
        return float(np.std(values, ddof=1) / m * 100.0) if m else None
    if name == "gri":
        return clinical.gri_components(cats)[0]
    if name == "mage_mgdl":
        return clinical.mage(values)
    return None


def _filtered_values(store, *, window_days, time_of_day=None, weekday=None, now):
    df = store.glucose_last_days(window_days, now=now)
    if df.empty:
        return np.array([], dtype=float)
    local = pd.to_datetime(df["measured_at"], utc=True).dt.tz_convert(ZoneInfo(clinical.DISPLAY_TZ))
    hours = local.dt.hour.to_numpy()
    wdays = local.dt.weekday.to_numpy()
    values = df["glucose_mgdl"].to_numpy(dtype=float)
    mask = np.ones(values.shape, dtype=bool)
    if time_of_day is not None:
        start_h, end_h = time_of_day
        if start_h <= end_h:
            mask &= (hours >= start_h) & (hours < end_h)
        else:  # wrap past midnight, e.g. (22, 6)
            mask &= (hours >= start_h) | (hours < end_h)
    if weekday is not None:
        wd_set = {weekday} if isinstance(weekday, int) else set(weekday)
        mask &= np.isin(wdays, list(wd_set))
    return values[mask]


def query(store, *, window_days: int, metric: str = "tir_pct",
          time_of_day=None, weekday=None, now=None) -> dict:
    """Compute *metric* over the filtered subset of the window."""
    now = integrity.now_utc() if now is None else now
    vals = _filtered_values(store, window_days=window_days, time_of_day=time_of_day,
                            weekday=weekday, now=now)
    return {"metric": metric, "value": _metric(vals, metric), "n": int(vals.size),
            "window_days": window_days,
            "filter": {"time_of_day": time_of_day, "weekday": weekday}}


def compare(store, *, window_days: int, metric: str, filter_a: dict, filter_b: dict,
            now=None) -> dict:
    """Compare *metric* between two custom filters (A − B)."""
    now = integrity.now_utc() if now is None else now
    a = query(store, window_days=window_days, metric=metric, now=now, **filter_a)
    b = query(store, window_days=window_days, metric=metric, now=now, **filter_b)
    effect = (a["value"] - b["value"]) if (a["value"] is not None and b["value"] is not None) else None
    return {"metric": metric, "a": a, "b": b, "effect_abs": effect}


def by_hour(store, *, window_days: int, metric: str = "mean_mgdl", now=None) -> list[dict]:
    """The metric computed within each hour-of-day slot (modal daily pattern)."""
    now = integrity.now_utc() if now is None else now
    out = []
    for h in range(24):
        vals = _filtered_values(store, window_days=window_days, time_of_day=(h, h + 1), now=now)
        out.append({"hour": h, "value": _metric(vals, metric), "n": int(vals.size)})
    return out


def self_check(result: dict) -> list[str]:
    violations = []
    v = result.get("value")
    if result.get("metric", "").endswith("_pct") and v is not None and not (0 <= v <= 100):
        violations.append(f"{result['metric']}={v} out of [0,100]")
    if result.get("n", 0) < 0:
        violations.append("negative n")
    return violations
