"""Acceptance test: run the full cycle on a fixture and verify the contract.

  1. metrics.json matches hand-computed values,
  2. switching the window recomputes correctly,
  3. a deliberately corrupted row is quarantined and flagged, not plotted.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest
from freezegun import freeze_time

import analytics
import engine
import glucose
import integrity
import store as store_mod

NOW = datetime(2026, 6, 28, 12, 0, 0, tzinfo=timezone.utc)
# the hand-checked distribution fixture (mean 150, GMI 6.898, TIR 50, TITR 30, GRI 86)
CORE = [50, 60, 70, 100, 140, 150, 180, 200, 250, 300]


@pytest.fixture()
def env(tmp_path):
    with freeze_time(NOW):
        integrity.cache_clear()
        s = store_mod.Store(":memory:")
        yield s, tmp_path
        s.close()
        integrity.cache_clear()


def _core_readings():
    return [{"measured_at": NOW - timedelta(minutes=i), "glucose_mgdl": v}
            for i, v in enumerate(CORE)]


def test_full_cycle_metrics_match_hand_computed(env):
    st, out = env
    src = glucose.FixtureSource(_core_readings())
    result = engine.run_cycle(store=st, glucose_source=src, now=NOW, window_days=7,
                              walk_adherence=0.8, out_dir=str(out))
    m = result.metrics
    assert m["mean_mgdl"]["value"] == pytest.approx(150.0)
    assert m["gmi_pct"]["value"] == pytest.approx(6.898)
    assert m["tir_pct"]["value"] == pytest.approx(50.0)
    assert m["titr_pct"]["value"] == pytest.approx(30.0)
    assert m["gri"]["value"] == pytest.approx(86.0)
    # written to disk and reloads identically
    on_disk = json.loads((out / "metrics.json").read_text())
    assert on_disk["gri"]["value"] == pytest.approx(86.0)
    # cycle self-checks pass
    assert result.ok, result.self_check_violations


def test_corrupted_row_is_quarantined_not_plotted(env):
    st, out = env
    readings = _core_readings() + [
        {"measured_at": NOW - timedelta(minutes=3, seconds=30), "glucose_mgdl": 9999},  # corrupt
    ]
    result = engine.run_cycle(store=st, glucose_source=glucose.FixtureSource(readings),
                              now=NOW, window_days=7, out_dir=str(out))
    # quarantined and flagged with a reason
    assert result.n_quarantined == 1
    assert "out_of_range" in result.quarantined[0]["reason"]
    # NOT plotted: clean store + metrics unchanged from the 10 good readings
    assert st.glucose_all()["glucose_mgdl"].max() == 300.0
    assert result.metrics["mean_mgdl"]["value"] == pytest.approx(150.0)
    assert result.metrics["n_readings"]["value"] == 10
    # and the corrupted value never appears in the trend series
    for row in result.trend["series"]:
        assert (row.get("mean_mgdl") or 0) < 1000


def test_switching_window_recomputes(env):
    st, _ = env
    # 30 days of data, two regimes: older days high, last 5 days lower
    recs = []
    for d in range(30):
        day = NOW - timedelta(days=d)
        base = 110 if d < 5 else 200
        for h in range(0, 24, 2):
            recs.append({"measured_at": day.replace(hour=h), "glucose_mgdl": base + (h % 5)})
    glucose.sync(st, glucose.FixtureSource(recs), now=NOW)

    r7 = analytics.compute(st, window_days=7, now=NOW)
    r30 = analytics.compute(st, window_days=30, now=NOW)
    assert r7["n_readings"] < r30["n_readings"]
    # the recent 7-day window is much lower than the 30-day blend -> different TIR
    assert r7["summary"]["tir_pct"] != r30["summary"]["tir_pct"]
    assert r7["summary"]["mean_mgdl"] < r30["summary"]["mean_mgdl"]
    assert analytics.self_check(r7) == [] and analytics.self_check(r30) == []


def test_full_cycle_writes_all_artifacts(env):
    st, out = env
    import journal
    import wearables
    dub_dates = [(NOW - timedelta(days=k)).astimezone(timezone.utc) for k in (1,)]
    log_csv = (
        "entry_id,date,time,type,item,net_carbs_g,mood_1to5,energy_1to5,tags,note\n"
        "1,2026-06-27,14:00,meal,Paneer,38,4,3,meal; +walk,lunch\n"
        "2,2026-06-27,21:00,mood,evening,,3,2,,checkin\n"
    )
    wear_rows = [
        ["Date", "Source(s)", "Timezone", "Steps", "Distance (m)", "Elevation (m)",
         "Floors climbed", "Total Calories (kcal)", "Active Calories (kcal)",
         "Power min (W)", "Power max (W)", "Power avg (W)", "Speed min (m/s)",
         "Speed max (m/s)", "Speed avg (m/s)", "VO2 max min", "VO2 max max",
         "VO2 max avg", "Wheelchair pushes", "Start Date/Time", "Exercise Name",
         "Duration (min)"],
        ["2026-06-27", "com.sec.android.app.shealth", "Asia/Dubai", "7000", "", "", "",
         "1600", "", "", "", "", "", "", "", "", "", "", "", "", "", ""],
    ]
    result = engine.run_cycle(
        store=st, glucose_source=glucose.FixtureSource(_core_readings()),
        log_source=journal.CsvLogSource(text=log_csv),
        wearables_source=wearables.FixtureRows(wear_rows),
        now=NOW, window_days=7, walk_adherence=0.8, dashboard=True,
        dashboard_windows=(7, 14, 30), out_dir=str(out))
    assert result.ok, result.self_check_violations
    assert (out / "metrics.json").exists()
    assert (out / "trend.json").exists()
    assert (out / "wearables.json").exists()
    assert (out / "dashboard.html").exists()
    html = (out / "dashboard.html").read_text()
    assert "last-known-good, not live" in html and "Analytics" in html
    assert result.metrics["mean_mgdl"]["value"] == pytest.approx(150.0)
    assert isinstance(result.insights_text, str)


def test_cycle_runs_without_source_on_prepopulated_store(env):
    st, out = env
    glucose.sync(st, glucose.FixtureSource(_core_readings()), now=NOW)
    result = engine.run_cycle(store=st, now=NOW, window_days=7, out_dir=str(out))
    assert result.n_stored == 0  # no source this cycle
    assert result.metrics["mean_mgdl"]["value"] == pytest.approx(150.0)
