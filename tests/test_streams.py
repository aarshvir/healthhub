"""Tests for the log-driven analysis modules: journal, food_impact, experiments,
symptoms, mood_energy, statutil. Uses synthetic data matching the real sheet schema."""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest
from freezegun import freeze_time

import experiments
import food_impact
import integrity
import journal
import mood_energy
import statutil
import store as store_mod
import symptoms

NOW = datetime(2026, 6, 28, 12, 0, 0, tzinfo=timezone.utc)
DUBAI = ZoneInfo("Asia/Dubai")

# hand-checkable response: baseline 100, peak 160 @ +45m, back down
RESP_OFFSETS = [-15, 0, 15, 30, 45, 60, 75, 90, 105, 120]
RESP_VALUES = [100, 100, 120, 140, 160, 150, 130, 120, 110, 105]
EXPECTED_IAUC = float(np.trapezoid(np.clip(np.array(RESP_VALUES) - 100, 0, None)[1:],
                                   np.array(RESP_OFFSETS)[1:]))  # post-meal only (minutes>=0)


@pytest.fixture()
def st():
    with freeze_time(NOW):
        integrity.cache_clear()
        s = store_mod.Store(":memory:")
        yield s
        s.close()
        integrity.cache_clear()


def _seed_meal(st, *, day_offset, item, carbs, entry_id, tags=""):
    meal_utc = NOW - timedelta(days=day_offset)
    recs = [{"measured_at": meal_utc + timedelta(minutes=o), "glucose_mgdl": v}
            for o, v in zip(RESP_OFFSETS, RESP_VALUES)]
    st.append_glucose(pd.DataFrame(recs), now=NOW)
    dub = meal_utc.astimezone(DUBAI)
    raw = {"entry_id": entry_id, "date": dub.strftime("%Y-%m-%d"), "time": dub.strftime("%H:%M"),
           "type": "meal", "item": item, "net_carbs_g": str(carbs), "tags": tags}
    st.append_events("log", journal.parse_rows([raw]), key_field="key", now=NOW)


# ---- journal -----------------------------------------------------------------------
def test_journal_parses_real_schema():
    csv_text = (
        "entry_id,date,time,type,item,net_carbs_g,mood_1to5,energy_1to5,glucose_mgdl,tags,note\n"
        "1,2026-06-28,14:20,meal,Chilli Paneer,37.6,4,3,150,meal; high-fat; +walk,first meal\n"
    )
    recs = journal.parse_rows(list(__import__("csv").DictReader(__import__("io").StringIO(csv_text))))
    assert len(recs) == 1
    r = recs[0]
    assert r["type"] == "meal" and r["net_carbs_g"] == pytest.approx(37.6)
    assert r["mood_1to5"] == 4 and r["tags"] == ["meal", "high-fat", "+walk"]
    assert r["measured_at"].startswith("2026-06-28T14:20")


def test_journal_local_date_converts_to_dubai():
    # 00:30 UTC -> 04:30 Asia/Dubai (same calendar day)
    assert journal.local_date("2026-06-28T00:30:00+00:00") == "2026-06-28"
    # 22:00 UTC -> 02:00 next day Dubai
    assert journal.local_date("2026-06-28T22:00:00+00:00") == "2026-06-29"


# ---- statutil ----------------------------------------------------------------------
def test_statutil_spearman_monotonic():
    sp = statutil.spearman([1, 2, 3, 4, 5], [10, 20, 30, 40, 50])
    assert sp["rho"] == pytest.approx(1.0) and sp["n"] == 5


def test_statutil_lift_and_d():
    flag = [True, True, False, False]
    cond = [True, True, False, False]
    out = statutil.lift(flag, cond)
    assert out["p_treated"] == 1.0 and out["p_control"] == 0.0
    assert statutil.strength_label(statutil.cohens_d([5, 6, 7], [1, 2, 3])) == "strong"


# ---- food_impact -------------------------------------------------------------------
def test_meal_response_hand_checked(st):
    _seed_meal(st, day_offset=1, item="TestMeal", carbs=60, entry_id="m1")
    meal = journal.meals(st)[0]
    r = food_impact.meal_response(st, meal, now=NOW)
    assert r["valid"]
    assert r["baseline_mgdl"] == pytest.approx(100.0)
    assert r["delta_peak_mgdl"] == pytest.approx(60.0)
    assert r["time_to_peak_min"] == pytest.approx(45.0)
    assert r["per_gram"] == pytest.approx(1.0)
    assert r["iauc_120"] == pytest.approx(EXPECTED_IAUC)
    assert food_impact.self_check([r]) == []


def test_rank_foods_requires_min_n(st):
    _seed_meal(st, day_offset=1, item="Rice", carbs=60, entry_id="r1")
    _seed_meal(st, day_offset=2, item="Rice", carbs=60, entry_id="r2")
    assert food_impact.rank_foods(st, now=NOW) == []          # n=2 < 3 -> not ranked
    _seed_meal(st, day_offset=3, item="Rice", carbs=60, entry_id="r3")
    ranked = food_impact.rank_foods(st, now=NOW)
    assert len(ranked) == 1 and ranked[0]["item"] == "rice" and ranked[0]["n"] == 3
    assert ranked[0]["mean_delta_peak_mgdl"] == pytest.approx(60.0)


# ---- experiments -------------------------------------------------------------------
def test_experiments_never_causal_below_min_n(st):
    for i in range(3):
        _seed_meal(st, day_offset=1 + i, item="Meal", carbs=60, entry_id=f"t{i}", tags="+walk")
    for i in range(3):
        _seed_meal(st, day_offset=10 + i, item="Meal", carbs=60, entry_id=f"c{i}", tags="")
    res = experiments.compare_tag(st, "+walk", now=NOW)
    assert res["n_treated"] == 3 and res["n_control"] == 3
    assert res["causal"] is False
    assert res["causal_label"] == "insufficient_n (not causal)"
    assert experiments.self_check([res]) == []


def test_experiments_effect_computed(st):
    # treated walks blunt the peak (lower delta); control higher
    for i in range(2):
        _seed_meal(st, day_offset=1 + i, item="M", carbs=60, entry_id=f"t{i}", tags="+walk")
    res = experiments.compare_tag(st, "+walk", now=NOW)
    # with no control meals, effect undefined but call is safe
    assert res["n_treated"] == 2
    assert res["mean_treated"] == pytest.approx(60.0)


# ---- symptoms + mood_energy --------------------------------------------------------
def _seed_days_with_logs(st):
    # 12 days of glucose + a mood/energy/symptom log each day
    raws = []
    for d in range(12):
        day = NOW - timedelta(days=d + 1)
        for h in range(0, 24, 3):
            st.append_glucose(pd.DataFrame([{"measured_at": day.replace(hour=h),
                                             "glucose_mgdl": 110 + (d * 8) % 90}]), now=NOW)
        dub = day.astimezone(DUBAI)
        raws.append({"entry_id": f"d{d}", "date": dub.strftime("%Y-%m-%d"), "time": "21:00",
                     "type": "mood", "mood_1to5": str(5 - d % 5), "energy_1to5": str(4 - d % 4),
                     "symptom": ("fatigue" if d % 2 == 0 else ""),
                     "symptom_sev_1to5": ("3" if d % 2 == 0 else ""), "tags": ""})
    st.append_events("log", journal.parse_rows(raws), key_field="key", now=NOW)


def test_symptoms_analyze(st):
    _seed_days_with_logs(st)
    res = symptoms.analyze(st, window_days=30, now=NOW)
    assert res["n_days"] >= 10
    assert res["lift"]["n_treated"] + res["lift"]["n_control"] == res["n_days"]
    assert symptoms.self_check(res) == []


def test_mood_energy_analyze(st):
    _seed_days_with_logs(st)
    res = mood_energy.analyze(st, window_days=30, now=NOW)
    assert len(res["correlations"]) == 6  # mood/energy x mean/tir/cv
    assert all(c["n"] >= 0 for c in res["correlations"])
    assert mood_energy.self_check(res) == []
