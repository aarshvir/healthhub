"""Tests for patterns.py — time-of-day, dawn, weekday, and trend-change mining."""

import patterns


def _agp(level_by_slot):
    """Build a 96-slot AGP where level_by_slot(i)->p50 (or None to leave a slot empty)."""
    out = []
    for i in range(96):
        v = level_by_slot(i)
        out.append(None if v is None else {"p10": v - 10, "p25": v - 5, "p50": v,
                                            "p75": v + 5, "p90": v + 10})
    return out


def test_time_of_day_and_dawn():
    # overnight low (100), a dawn rise 04-08 (150), midday spike (190)
    def lvl(i):
        h = i // 4
        if h < 4:
            return 100.0
        if h < 8:
            return 150.0
        if 11 <= h < 15:
            return 190.0
        return 120.0
    agp = _agp(lvl)
    tod = patterns.time_of_day(agp)
    assert tod["worst"]["window"] == "Midday" and tod["worst"]["median_mgdl"] == 190.0
    assert tod["best"]["window"] == "Overnight"
    d = patterns.dawn(agp)
    assert d["delta_mgdl"] == 50.0 and d["present"] is True
    assert patterns.self_check(patterns.compute(agp, [])) == []


def test_weekday_effect_weekend_higher():
    # Sat/Sun (weekend) higher than weekdays
    frame = [
        {"date": "2026-06-01", "mean_mgdl": 110},  # Mon
        {"date": "2026-06-02", "mean_mgdl": 112},  # Tue
        {"date": "2026-06-06", "mean_mgdl": 150},  # Sat
        {"date": "2026-06-07", "mean_mgdl": 148},  # Sun
    ]
    w = patterns.weekday_effect(frame)
    assert w["ok"] and w["weekend_minus_weekday"] > 0
    assert w["highest"]["day"] in ("Sat", "Sun")


def test_trend_change_detects_improvement():
    # four weeks trending down
    frame = []
    starts = ["2026-05-04", "2026-05-11", "2026-05-18", "2026-05-25"]
    for wk, base in zip(starts, (170, 155, 140, 120)):
        frame.append({"date": wk, "mean_mgdl": base})
    t = patterns.trend_change(frame)
    assert t["ok"] and t["direction"] == "improving"
    assert t["overall_change_mgdl"] < 0 and t["n_weeks"] == 4


def test_empty_inputs_safe():
    assert patterns.time_of_day([])["parts"] == []
    assert patterns.dawn([])["present"] is False
    assert patterns.weekday_effect([])["ok"] is False
    assert patterns.trend_change([])["ok"] is False
    assert patterns.self_check(patterns.compute([], [])) == []
