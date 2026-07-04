"""Tests for assess, insights, custom, and build_dashboard."""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
from freezegun import freeze_time

import analytics
import assess
import build_dashboard
import custom
import engine
import glucose
import insights
import integrity
import store as store_mod

NOW = datetime(2026, 6, 28, 12, 0, 0, tzinfo=timezone.utc)
DUBAI = ZoneInfo("Asia/Dubai")
CORE = [50, 60, 70, 100, 140, 150, 180, 200, 250, 300]


@pytest.fixture()
def cycle(tmp_path):
    with freeze_time(NOW):
        integrity.cache_clear()
        s = store_mod.Store(":memory:")
        recs = [{"measured_at": NOW - timedelta(minutes=i), "glucose_mgdl": v}
                for i, v in enumerate(CORE)]
        result = engine.run_cycle(store=s, glucose_source=glucose.FixtureSource(recs),
                                  now=NOW, window_days=7, walk_adherence=0.8, out_dir=str(tmp_path))
        yield s, result
        s.close()
        integrity.cache_clear()


# ---- assess ------------------------------------------------------------------------
def test_assess_from_numbers(cycle):
    _, result = cycle
    a = assess.assess(result.metrics, window_days=7)
    assert a["grade"] in {"A", "B", "C", "D", "F"}
    assert 0 <= a["score_pct"] <= 100
    tir_line = next(l for l in a["lines"] if l["metric"] == "tir_pct")
    assert tir_line["value"] == pytest.approx(50.0)
    assert "Time in range" in tir_line["text"]
    assert assess.self_check(a) == []


# ---- insights ----------------------------------------------------------------------
def test_insights_rank_and_grounded_narration():
    food = [{"item": "rice", "n": 4, "mean_delta_peak_mgdl": 70.0, "mean_iauc_120": 3000.0},
            {"item": "oats", "n": 3, "mean_delta_peak_mgdl": 30.0, "mean_iauc_120": 1200.0}]
    exps = [{"tag": "+walk", "outcome": "delta_peak_mgdl", "effect_abs": -25.0,
             "effect_pct": -30.0, "n_treated": 6, "n_control": 6, "signal_strength": "moderate",
             "causal_label": "eligible_for_causal_review"}]
    findings = insights.rank(food_ranking=food, experiment_results=exps)
    assert findings[0]["score"] >= findings[-1]["score"]
    assert insights.self_check(findings) == []
    # narration is grounded: the real numbers pass through unchanged
    text = insights.narrate(findings)
    assert "rice" in text and "70" in text


def test_insights_blocks_invented_number():
    findings = [{"kind": "food", "label": "rice raises glucose ~70 mg/dL", "value": 70.0,
                 "n": 4, "detail": "n=4 meals", "score": 350.0}]
    # an LLM trying to smuggle an ungrounded number must be blocked by the guard
    with pytest.raises(integrity.NarrationIntegrityError):
        integrity.sanitize_narration("rice spiked to 999 mg/dL",
                                     insights.grounded_numbers(findings))


# ---- custom analytics --------------------------------------------------------------
@pytest.fixture()
def tod_store():
    with freeze_time(NOW):
        integrity.cache_clear()
        s = store_mod.Store(":memory:")
        recs = []
        base = NOW.astimezone(DUBAI).replace(minute=0, second=0, microsecond=0)
        for d in range(1, 8):
            for h in range(24):
                ts = (base - timedelta(days=d)).replace(hour=h)
                recs.append({"measured_at": ts, "glucose_mgdl": 100 if h < 6 else 185})
        s.append_glucose(pd.DataFrame(recs), now=NOW)
        yield s
        s.close()
        integrity.cache_clear()


def test_custom_time_of_day(tod_store):
    overnight = custom.query(tod_store, window_days=14, metric="mean_mgdl",
                             time_of_day=(0, 6), now=NOW)
    daytime = custom.query(tod_store, window_days=14, metric="mean_mgdl",
                           time_of_day=(6, 24), now=NOW)
    assert overnight["value"] == pytest.approx(100.0)
    assert daytime["value"] == pytest.approx(185.0)
    assert overnight["n"] > 0 and custom.self_check(overnight) == []


def test_custom_compare_and_by_hour(tod_store):
    cmp = custom.compare(tod_store, window_days=14, metric="tir_pct",
                         filter_a={"time_of_day": (0, 6)}, filter_b={"time_of_day": (6, 24)})
    assert cmp["effect_abs"] is not None
    hours = custom.by_hour(tod_store, window_days=14, metric="mean_mgdl", now=NOW)
    assert len(hours) == 24
    assert hours[0]["value"] == pytest.approx(100.0)   # 00:00 Dubai -> overnight low


# ---- dashboard ---------------------------------------------------------------------
def test_dashboard_renders_with_freshness_and_tabs(cycle):
    st, result = cycle
    trend_by_window = {w: analytics.compute(st, window_days=w, now=NOW)
                       for w in (7, 14, 30)}
    latest = {"ts": result.metrics["mean_mgdl"]["ts"], "value": 150}
    cockpit = build_dashboard.build_cockpit(
        metrics=result.metrics, trend_by_window=trend_by_window, latest_glucose=latest,
        assessment=assess.assess(result.metrics, window_days=7),
        quarantine=result.quarantined, now=NOW)
    html = build_dashboard.render(cockpit)
    for tab in build_dashboard.TABS:
        assert f">{tab}<" in html
    assert "last-known-good, not live" in html        # never pretends live (§A rule 6)
    assert "ago)" in html                             # shows age
    assert "<svg" in html and "AGP" in html           # AGP chart present
    assert 'onclick="showWindow(7)"' in html          # window selector wired
    assert build_dashboard.self_check(cockpit) == []


def test_dashboard_does_not_plot_quarantined_value():
    with freeze_time(NOW):
        integrity.cache_clear()
        s = store_mod.Store(":memory:")
        recs = [{"measured_at": NOW - timedelta(minutes=i), "glucose_mgdl": v}
                for i, v in enumerate(CORE)]
        recs.append({"measured_at": NOW - timedelta(minutes=2, seconds=30), "glucose_mgdl": 9999})
        result = engine.run_cycle(store=s, glucose_source=glucose.FixtureSource(recs),
                                  now=NOW, window_days=7, out_dir="")
        trend_by_window = {7: analytics.compute(s, window_days=7, now=NOW)}
        cockpit = build_dashboard.build_cockpit(
            metrics=result.metrics, trend_by_window=trend_by_window,
            latest_glucose={"ts": result.metrics["mean_mgdl"]["ts"], "value": 150},
            quarantine=result.quarantined, now=NOW)
        html = build_dashboard.render(cockpit)
        assert "9999" not in html
        assert build_dashboard.self_check(cockpit) == []
        s.close()
        integrity.cache_clear()


def test_signed_delta_helpers_kill_negative_zero():
    # deltas that round to zero must never render the misleading "-0"
    assert build_dashboard._pm(-0.03) == "+0"
    assert build_dashboard._pm(-0.0) == "+0"
    assert build_dashboard._pm(-0.04, 1) == "+0.0"
    assert build_dashboard._pm(-4.4) == "-4"
    assert build_dashboard._pm(12.0) == "+12"
    assert build_dashboard._pm(None) == "—"
    assert build_dashboard._pmg(-0.0) == "+0"
    assert build_dashboard._pmg(-30.0) == "-30"
    assert build_dashboard._pmg(2.5) == "+2.5"
    assert build_dashboard._pmg(None) == "—"
