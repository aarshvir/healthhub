"""Tests for the saved custom-analysis registry: spec → registry → deterministic execute."""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
from freezegun import freeze_time

import custom
import integrity
import journal
import store as store_mod

NOW = datetime(2026, 6, 28, 12, 0, 0, tzinfo=timezone.utc)
DUBAI = ZoneInfo("Asia/Dubai")


@pytest.fixture()
def st():
    with freeze_time(NOW):
        integrity.cache_clear()
        s = store_mod.Store(":memory:")
        yield s
        s.close()
        integrity.cache_clear()


def _seed_dinner(st, *, day_offset, walked, peak, entry_id):
    """A dinner of rice at ~19:00 Dubai with a controlled glucose response (baseline 100)."""
    meal_utc = (NOW - timedelta(days=day_offset)).astimezone(DUBAI).replace(
        hour=19, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    offs = [-15, 0, 30, 45, 60, 90, 120]
    vals = [100, 100, 100 + peak * 0.6, 100 + peak, 100 + peak * 0.7, 100 + peak * 0.3, 100]
    st.append_glucose(pd.DataFrame(
        [{"measured_at": meal_utc + timedelta(minutes=o), "glucose_mgdl": v}
         for o, v in zip(offs, vals)]), now=NOW)
    dub = meal_utc.astimezone(DUBAI)
    raw = {"entry_id": entry_id, "date": dub.strftime("%Y-%m-%d"), "time": dub.strftime("%H:%M"),
           "type": "meal", "item": "Rice bowl", "net_carbs_g": "60",
           "tags": "dinner; +walk" if walked else "dinner"}
    st.append_events("log", journal.parse_rows([raw]), key_field="key", now=NOW)


SPEC = {
    "name": "dinner_rice_by_walk",
    "title": "Dinner spike with rice, by walk (90d)",
    "source": "meals", "metric": "delta_peak_mgdl", "window": 90, "agg": "mean",
    "group_by": "walked",
    "filters": [{"field": "item", "op": "contains", "value": "rice"},
                {"field": "meal_time", "op": "eq", "value": "dinner"}],
    "viz": "bar",
}


def test_validate_spec_rejects_bad():
    with pytest.raises(ValueError):
        custom.validate_spec({"name": "x", "source": "meals", "metric": "tir_pct", "window": 7})
    with pytest.raises(ValueError):
        custom.validate_spec({"name": "x", "source": "meals", "metric": "delta_peak_mgdl",
                              "window": 0})


def test_registry_add_update_delete(tmp_path):
    path = str(tmp_path / "analyses.json")
    custom.upsert_analysis(SPEC, path=path)
    assert len(custom.load_registry(path)) == 1
    custom.upsert_analysis({**SPEC, "window": 30}, path=path)   # same name -> replace
    regs = custom.load_registry(path)
    assert len(regs) == 1 and regs[0]["window"] == 30
    custom.delete_analysis("dinner_rice_by_walk", path=path)
    assert custom.load_registry(path) == []


def test_execute_dinner_rice_by_walk_deterministic(st):
    # walked dinners blunt the peak (Δ40) vs not-walked (Δ80), 3 each
    for i in range(3):
        _seed_dinner(st, day_offset=1 + i, walked=True, peak=40, entry_id=f"w{i}")
    for i in range(3):
        _seed_dinner(st, day_offset=10 + i, walked=False, peak=80, entry_id=f"n{i}")

    r1 = custom.execute(st, SPEC, now=NOW)
    r2 = custom.execute(st, SPEC, now=NOW)
    assert r1["groups"] == r2["groups"]                       # deterministic
    by_group = {g["group"]: g for g in r1["groups"]}
    assert by_group["walked"]["value"] == pytest.approx(40.0)
    assert by_group["walked"]["n"] == 3
    assert by_group["not_walked"]["value"] == pytest.approx(80.0)
    assert by_group["not_walked"]["n"] == 3
    # auto-assessment: lower Δpeak is better -> walked wins; n shown; exact filter present
    assert "walked is best" in r1["assessment"]
    assert "item contains rice" in r1["filter"] and "meal_time eq dinner" in r1["filter"]
    assert custom.result_self_check(r1) == []


def test_execute_glucose_spec(st):
    base = NOW.astimezone(DUBAI).replace(minute=0, second=0, microsecond=0)
    recs = []
    for d in range(1, 8):
        for h in range(24):
            recs.append({"measured_at": (base - timedelta(days=d)).replace(hour=h),
                         "glucose_mgdl": 100 if h < 6 else 185})
    st.append_glucose(pd.DataFrame(recs), now=NOW)
    spec = {"name": "overnight_tir", "source": "glucose", "metric": "tir_pct", "window": 14,
            "agg": "value", "group_by": None,
            "filters": [{"field": "time_of_day", "op": "between", "value": [0, 6]}], "viz": "number"}
    res = custom.execute(st, spec, now=NOW)
    assert res["groups"][0]["value"] == pytest.approx(100.0)   # overnight all in-range
    assert res["groups"][0]["n"] > 0


def test_run_registry_from_file(st, tmp_path):
    path = str(tmp_path / "analyses.json")
    custom.upsert_analysis(SPEC, path=path)
    for i in range(3):
        _seed_dinner(st, day_offset=1 + i, walked=True, peak=40, entry_id=f"w{i}")
    cards = custom.run_registry(st, path=path, now=NOW)
    assert len(cards) == 1 and cards[0]["name"] == "dinner_rice_by_walk"
