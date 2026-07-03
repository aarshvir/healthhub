"""Tests for coach.py — grounded, ranked daily actions."""

import coach


def test_moves_rank_and_ground():
    ms = coach.moves(
        metrics={"titr_pct": {"value": 42.0}},
        experiments=[{"tag": "+walk", "effect_abs": -14.0, "n_treated": 60, "n_control": 53}],
        food_ranking=[{"item": "Chilli Paneer", "mean_delta_peak_mgdl": 62.0, "n": 5}],
        patterns={"time_of_day": {"worst": {"window": "Midday", "hours": "11:00–15:00",
                                            "median_mgdl": 190.0}}},
        reversal={"ladder": {"next": {"label": "Strong control", "mean_gap": 7.0, "gmi": 6.0}}},
        top=3)
    assert coach.self_check(ms) == []
    assert len(ms) == 3
    keys = [m["key"] for m in ms]
    assert keys[0] in ("gmi", "walk")            # priority-1 leads
    walk = next(m for m in ms if m["key"] == "walk")
    assert "14" in walk["detail"] and "n=53" in walk["detail"]   # grounded in the user's data


def test_walk_move_dropped_when_no_benefit():
    ms = coach.moves(experiments=[{"tag": "+walk", "effect_abs": 2.0,
                                   "n_treated": 5, "n_control": 5}])
    assert not any(m["key"] == "walk" for m in ms)


def test_empty_inputs_safe():
    assert coach.moves() == []
    assert coach.self_check([]) == []
