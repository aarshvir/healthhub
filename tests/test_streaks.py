"""Tests for streaks.py — honest habit streaks over the daily frame."""

import streaks


def _row(d, **kw):
    r = {"date": d}
    r.update(kw)
    return r


def test_consecutive_run_and_best():
    # remission = mean < 133.4; a gap and an unmet day both reset the run
    frame = [
        _row("2026-06-01", mean_mgdl=120),   # ok
        _row("2026-06-02", mean_mgdl=125),   # ok  (run 2)
        _row("2026-06-03", mean_mgdl=140),   # unmet -> reset
        _row("2026-06-04", mean_mgdl=110),   # ok  (run 1)
        _row("2026-06-06", mean_mgdl=115),   # gap (05 missing) -> new run 1
        _row("2026-06-07", mean_mgdl=118),   # ok  (run 2, ends at latest day)
    ]
    s = {x["key"]: x for x in streaks.compute(frame)}["remission"]
    assert s["current"] == 2       # 06-06, 06-07
    assert s["best"] == 2          # 06-01..06-02 and 06-06..06-07 both length 2
    assert s["met_today"] is True
    assert streaks.self_check(streaks.compute(frame)) == []


def test_missing_day_breaks_current():
    frame = [_row("2026-06-01", mean_mgdl=120), _row("2026-06-02", mean_mgdl=None)]
    s = {x["key"]: x for x in streaks.compute(frame)}["remission"]
    assert s["current"] == 0 and s["met_today"] is False and s["best"] == 1


def test_dead_streaks_are_hidden():
    # never in tight range -> no titr streak entry at all
    frame = [_row(f"2026-06-0{i}", titr_pct=10, mean_mgdl=200) for i in range(1, 5)]
    keys = {x["key"] for x in streaks.compute(frame)}
    assert "titr" not in keys and "remission" not in keys


def test_headline_prefers_active_remission():
    frame = [_row("2026-06-01", mean_mgdl=120, titr_pct=90),
             _row("2026-06-02", mean_mgdl=121, titr_pct=91)]
    h = streaks.headline(streaks.compute(frame))
    assert h["key"] == "remission" and h["current"] == 2


def test_empty_frame_safe():
    assert streaks.compute([]) == []
    assert streaks.headline([]) is None
    assert streaks.self_check([]) == []
