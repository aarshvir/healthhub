"""custom.py — CUSTOM ANALYTICS (§C): saved, parameterized analyses over the 500-day store.

Two layers:

* **Primitives** (``query`` / ``compare`` / ``by_hour``) — ad-hoc glucose queries: filter by
  local time-of-day (wrap-around) and weekday, pick a clinical metric, report ``n``.

* **Saved analyses** — a plain-English request is translated ONCE (by Claude, in chat) into a
  structured spec ``{name, title, source, metric, window, agg, group_by, filters, viz}`` and
  appended to an ``analyses.json`` registry. The engine then ``execute``s each spec
  *deterministically every cycle* and renders it as a permanent card with its own
  auto-assessment — no LLM math at run time, so results are reproducible and hallucination-free.
  Every card shows ``n`` and the exact filter. Analyses can be added / updated / deleted.

  Example spec for "average dinner spike with rice, split by whether I walked, last 90 days":
      {"name": "dinner_rice_by_walk", "source": "meals", "metric": "delta_peak_mgdl",
       "window": 90, "agg": "mean", "group_by": "walked",
       "filters": [{"field": "item", "op": "contains", "value": "rice"},
                   {"field": "meal_time", "op": "eq", "value": "dinner"}], "viz": "bar"}
"""

from __future__ import annotations

import json
from datetime import timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

import clinical
import food_impact
import integrity
import journal

METRICS = ("tir_pct", "titr_pct", "tbr_pct", "tar_pct", "mean_mgdl", "cv_pct",
           "gmi_pct", "gri", "mage_mgdl")
MEAL_METRICS = ("delta_peak_mgdl", "iauc_120", "per_gram", "peak_mgdl",
                "baseline_mgdl", "time_to_peak_min")
# metrics where a smaller value is healthier (drives auto-assessment direction)
LOWER_IS_BETTER = {"tbr_pct", "tar_pct", "cv_pct", "gmi_pct", "gri", "mage_mgdl",
                   "delta_peak_mgdl", "iauc_120", "per_gram", "peak_mgdl", "time_to_peak_min"}
_AGGS = {"mean": np.mean, "median": np.median, "count": np.size}
MEAL_TIME_BUCKETS = (("breakfast", 5, 11), ("lunch", 11, 16),
                     ("dinner", 16, 23), ("overnight", 23, 5))


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


# ======================================================================================
# Saved analyses: spec validation + registry + deterministic executor + auto-assessment
# ======================================================================================
REQUIRED_SPEC = ("name", "source", "metric", "window")
GROUP_BYS = (None, "walked", "weekday", "meal_time", "hour")  # plus "tag:<t>"
_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def validate_spec(spec: dict) -> dict:
    for k in REQUIRED_SPEC:
        if k not in spec:
            raise ValueError(f"spec missing required field: {k}")
    if spec["source"] not in ("glucose", "meals"):
        raise ValueError("source must be 'glucose' or 'meals'")
    valid_metrics = MEAL_METRICS if spec["source"] == "meals" else METRICS
    if spec["metric"] not in valid_metrics:
        raise ValueError(f"metric {spec['metric']!r} invalid for source {spec['source']}")
    if not isinstance(spec["window"], int) or spec["window"] <= 0:
        raise ValueError("window must be a positive integer (days)")
    gb = spec.get("group_by")
    if gb is not None and gb not in GROUP_BYS and not str(gb).startswith("tag:"):
        raise ValueError(f"group_by {gb!r} not supported")
    if spec.get("agg", "mean") not in (set(_AGGS) | {"value"}):
        raise ValueError("agg must be mean/median/count/value")
    for f in spec.get("filters", []):
        if not {"field", "op", "value"} <= set(f):
            raise ValueError("each filter needs field/op/value")
    return spec


def load_registry(path: str = "analyses.json") -> list[dict]:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return []
    return data if isinstance(data, list) else data.get("analyses", [])


def save_registry(specs: list[dict], path: str = "analyses.json") -> str:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(specs, fh, indent=2)
    return path


def upsert_analysis(spec: dict, *, path: str = "analyses.json") -> list[dict]:
    """Validate and add/replace a saved analysis (keyed by name)."""
    validate_spec(spec)
    specs = [s for s in load_registry(path) if s.get("name") != spec["name"]]
    specs.append(spec)
    save_registry(specs, path)
    return specs


def delete_analysis(name: str, *, path: str = "analyses.json") -> list[dict]:
    specs = [s for s in load_registry(path) if s.get("name") != name]
    save_registry(specs, path)
    return specs


def _meal_time_bucket(hour: int) -> str:
    for name, lo, hi in MEAL_TIME_BUCKETS:
        if lo <= hi:
            if lo <= hour < hi:
                return name
        elif hour >= lo or hour < hi:  # overnight wrap
            return name
    return "overnight"


def _meal_records(store, *, window, now):
    cutoff = now - timedelta(days=window)
    out = []
    for meal in journal.meals(store):
        ts = meal.get("measured_at") or meal.get("ts")
        try:
            measured = integrity.freshness(ts, now=now).measured_at
        except integrity.IntegrityError:
            continue
        if measured < cutoff:
            continue
        resp = food_impact.meal_response(store, meal, now=now)
        if not resp["valid"]:
            continue
        local = measured.astimezone(ZoneInfo(clinical.DISPLAY_TZ))
        rec = dict(resp)
        rec["tags"] = [t.lower() for t in (meal.get("tags") or [])]
        rec["item_l"] = str(meal.get("item") or "").lower()
        rec["hour"], rec["weekday"] = local.hour, _WEEKDAYS[local.weekday()]
        rec["meal_time"] = _meal_time_bucket(local.hour)
        out.append(rec)
    return out


def _passes(rec, filters) -> bool:
    for f in filters:
        field, op, val = f["field"], f["op"], f["value"]
        if field == "item" and op == "contains" and str(val).lower() not in rec["item_l"]:
            return False
        if field == "tag" and op == "contains" and str(val).lower() not in rec["tags"]:
            return False
        if field in ("meal_time", "weekday") and op == "eq" and rec.get(field) != val:
            return False
    return True


def _group_key(rec, group_by):
    if group_by is None:
        return "all"
    if group_by == "walked":
        return "walked" if "+walk" in rec["tags"] else "not_walked"
    if group_by in ("weekday", "meal_time"):
        return rec.get(group_by)
    if group_by == "hour":
        return f"{rec['hour']:02d}:00"
    if str(group_by).startswith("tag:"):
        t = group_by.split(":", 1)[1].lower()
        return t if t in rec["tags"] else f"not_{t}"
    return "all"


def _agg_values(values, agg):
    arr = np.array([v for v in values if v is not None], dtype=float)
    if arr.size == 0:
        return None
    if agg == "count":
        return float(arr.size)
    return float(_AGGS.get(agg, np.mean)(arr))


def _exec_meals(store, spec, now):
    recs = [r for r in _meal_records(store, window=spec["window"], now=now)
            if _passes(r, spec.get("filters", []))]
    metric, agg, gb = spec["metric"], spec.get("agg", "mean"), spec.get("group_by")
    groups: dict[str, list] = {}
    for r in recs:
        groups.setdefault(_group_key(r, gb), []).append(r.get(metric))
    rows = [{"group": k, "value": _agg_values(v, agg),
             "n": len([x for x in v if x is not None])} for k, v in sorted(groups.items())]
    return rows, {"value": _agg_values([r.get(metric) for r in recs], agg), "n": len(recs)}


def _exec_glucose(store, spec, now):
    metric, gb = spec["metric"], spec.get("group_by")
    base = {}
    for f in spec.get("filters", []):
        if f["field"] == "time_of_day":
            base["time_of_day"] = tuple(f["value"])
        elif f["field"] == "weekday":
            base["weekday"] = f["value"]
    ov = query(store, window_days=spec["window"], metric=metric, now=now, **base)
    overall = {"value": ov["value"], "n": ov["n"]}
    if gb in (None, "all"):
        return [{"group": "all", "value": ov["value"], "n": ov["n"]}], overall
    if gb == "hour":
        hours = by_hour(store, window_days=spec["window"], metric=metric, now=now)
        return ([{"group": f"{h['hour']:02d}:00", "value": h["value"], "n": h["n"]}
                 for h in hours], overall)
    if gb == "weekday":
        rows = []
        for i, name in enumerate(_WEEKDAYS):
            q = query(store, window_days=spec["window"], metric=metric, now=now, weekday=i,
                      **{k: v for k, v in base.items() if k != "weekday"})
            rows.append({"group": name, "value": q["value"], "n": q["n"]})
        return rows, overall
    return [{"group": "all", "value": ov["value"], "n": ov["n"]}], overall


def filter_description(spec) -> str:
    parts = [f"source={spec['source']}", f"window={spec['window']}d", f"metric={spec['metric']}"]
    for f in spec.get("filters", []):
        parts.append(f"{f['field']} {f['op']} {f['value']}")
    if spec.get("group_by"):
        parts.append(f"by {spec['group_by']}")
    parts.append(f"agg={spec.get('agg', 'mean')}")
    return " · ".join(parts)


def execute(store, spec: dict, *, now=None) -> dict:
    """Run one saved analysis deterministically; returns a render-ready card dict."""
    now = integrity.now_utc() if now is None else now
    validate_spec(spec)
    runner = _exec_meals if spec["source"] == "meals" else _exec_glucose
    rows, overall = runner(store, spec, now)
    result = {"name": spec["name"], "title": spec.get("title", spec["name"]), "spec": spec,
              "filter": filter_description(spec), "groups": rows, "overall": overall,
              "viz": spec.get("viz", "bar"), "generated_at": now.isoformat()}
    result["assessment"] = auto_assess(spec, result)
    return result


def execute_all(store, specs: list[dict], *, now=None) -> list[dict]:
    now = integrity.now_utc() if now is None else now
    out = []
    for spec in specs:
        try:
            out.append(execute(store, spec, now=now))
        except (ValueError, integrity.IntegrityError) as exc:
            out.append({"name": spec.get("name"), "title": spec.get("name"), "spec": spec,
                        "error": str(exc), "groups": [], "overall": {"value": None, "n": 0},
                        "filter": "", "assessment": f"spec error: {exc}", "viz": "number"})
    return out


def run_registry(store, *, path: str = "analyses.json", now=None) -> list[dict]:
    return execute_all(store, load_registry(path), now=now)


def auto_assess(spec: dict, result: dict) -> str:
    """Plain-English verdict from the numbers only (no LLM); always reports n."""
    metric = spec["metric"]
    groups = [g for g in result["groups"] if g["value"] is not None]
    if not groups:
        return "No data matched this analysis."
    if len(groups) == 1:
        return f"{metric} = {groups[0]['value']:.1f} (n={groups[0]['n']})."
    lower_better = metric in LOWER_IS_BETTER
    best = (min if lower_better else max)(groups, key=lambda g: g["value"])
    worst = (max if lower_better else min)(groups, key=lambda g: g["value"])
    spread = abs(best["value"] - worst["value"])
    better = "lower" if lower_better else "higher"
    return (f"{best['group']} is best ({metric} {best['value']:.1f}, n={best['n']}; "
            f"{better} is better) vs {worst['group']} ({worst['value']:.1f}, n={worst['n']}) "
            f"— spread {spread:.1f}. Associational; n shown.")


def result_self_check(result: dict) -> list[str]:
    violations = []
    for g in result.get("groups", []):
        if g.get("n", 0) < 0:
            violations.append(f"{result.get('name')}: negative n")
        if result.get("spec", {}).get("metric", "").endswith("_pct") and g["value"] is not None \
                and not (-0.001 <= g["value"] <= 100.001):
            violations.append(f"{result.get('name')}: pct out of range")
    return violations
