"""Tests for forecast.py (grounded meal-spike model) and digest.py (weekly deltas)."""

from datetime import date, timedelta

import digest
import forecast


def test_forecast_predict_with_and_without_walk():
    model = forecast.build(
        food_ranking=[{"item": "rice", "mean_per_gram": 1.5, "mean_delta_peak_mgdl": 60, "n": 5}],
        experiments=[{"tag": "+walk", "effect_abs": -14.0}],
        metrics={"mean_mgdl": {"value": 110}})
    assert forecast.self_check(model) == []
    p = forecast.predict(model, "rice", 40)          # 1.5 * 40 = 60 rise
    assert p["delta_mgdl"] == 60 and p["peak_mgdl"] == 170 and p["in_range"] is True
    pw = forecast.predict(model, "rice", 40, walk=True)   # 60 - 14 = 46
    assert pw["delta_mgdl"] == 46 and pw["peak_mgdl"] == 156
    assert forecast.predict(model, "unknown", 40)["ok"] is False


def test_forecast_walk_cannot_push_rise_negative():
    model = forecast.build(
        food_ranking=[{"item": "oats", "mean_per_gram": 0.2, "mean_delta_peak_mgdl": 6, "n": 4}],
        experiments=[{"tag": "+walk", "effect_abs": -30.0}], metrics={"mean_mgdl": {"value": 100}})
    p = forecast.predict(model, "oats", 10, walk=True)   # 2 - 30 -> clamp 0
    assert p["delta_mgdl"] == 0 and p["peak_mgdl"] == 100


def test_digest_week_over_week():
    base = date(2026, 6, 30)
    frame = []
    # prior week mean ~150, this week ~120 (improving), weight down
    for i in range(14):
        d = base - timedelta(days=13 - i)
        recent = i >= 7
        frame.append({"date": d.isoformat(),
                      "mean_mgdl": 120 if recent else 150,
                      "tir_pct": 80 if recent else 60,
                      "weight_kg": 100 - (0.5 if recent else 0)})
    dig = digest.build(frame)
    assert dig["ok"] and dig["n_this"] == 7 and dig["n_prior"] == 7
    g = next(r for r in dig["rows"] if r["key"] == "mean_mgdl")
    assert g["delta"] == -30.0 and g["better"] is True
    assert "down 30" in digest.headline(dig)
    assert digest.self_check(dig) == []


def test_digest_needs_two_weeks():
    frame = [{"date": f"2026-06-0{i}", "mean_mgdl": 120} for i in range(1, 6)]
    assert digest.build(frame)["ok"] is False
