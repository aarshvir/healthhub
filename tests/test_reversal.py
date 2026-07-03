"""Tests for labs.py (reference ranges/status/trends), reversal.py (remission ladder, GMI
projection, doctor list), and the dashboard Reversal tab. Plus a regression test for the
self_check false-alarm on quarantined values colliding with SVG coordinates."""

from datetime import datetime, timedelta, timezone

import pytest
from freezegun import freeze_time

import build_dashboard
import integrity
import labs
import reversal
import store as store_mod

NOW = datetime(2026, 7, 2, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture()
def st():
    with freeze_time(NOW):
        integrity.cache_clear()
        s = store_mod.Store(":memory:")
        yield s
        s.close()
        integrity.cache_clear()


def _lab(key_marker, value, *, days_ago, unit="", ref_high=None, ref_low=None, marker=None):
    return {"key": f"{key_marker}-{days_ago}", "marker": marker or key_marker,
            "measured_at": (NOW - timedelta(days=days_ago)).isoformat(),
            "value": value, "unit": unit, "ref_high": ref_high, "ref_low": ref_low}


# ---- labs.normalize / status -------------------------------------------------------------
def test_normalize_aliases():
    assert labs.normalize("CRP") == "hs_crp"
    assert labs.normalize("hs-CRP") == "hs_crp"
    assert labs.normalize("ALT (SGPT)") == "alt"
    assert labs.normalize("Testosterone") == "total_testosterone"
    assert labs.normalize("Vit D") == "vitamin_d"
    assert labs.normalize("wibble") is None


def test_status_low_good_high_good_in_range():
    # low_good ALT (ref_high 40): 25 good, 50 warning (<=60), 85 critical (>60)
    assert labs.status("alt", 25) == "good"
    assert labs.status("alt", 50) == "warning"
    assert labs.status("alt", 85) == "critical"
    # high_good testosterone (ref_low 264): 500 good, 200 warning, 162 critical (<174)
    assert labs.status("total_testosterone", 500) == "good"
    assert labs.status("total_testosterone", 200) == "warning"
    assert labs.status("total_testosterone", 162) == "critical"
    # in_range fasting_insulin (2-10): 6 good, 12 warning, 25 critical
    assert labs.status("fasting_insulin", 6) == "good"
    assert labs.status("fasting_insulin", 12) == "warning"
    assert labs.status("fasting_insulin", 25) == "critical"
    # inflammation
    assert labs.status("hs_crp", 1.0) == "good"
    assert labs.status("hs_crp", 11.5) == "critical"


# ---- labs.panel --------------------------------------------------------------------------
def test_panel_groups_status_and_trend(st):
    with freeze_time(NOW):
        st.append_events("labs", [
            _lab("ALT", 92, days_ago=60, unit="U/L"),
            _lab("ALT", 85, days_ago=1, unit="U/L"),          # improving (▼)
            _lab("hs-CRP", 11.5, days_ago=1, unit="mg/L"),
            _lab("Testosterone", 162, days_ago=1, unit="ng/dL"),
            _lab("Vitamin D", 27.8, days_ago=1, unit="ng/mL"),
        ], key_field="key", now=NOW)
        p = labs.panel(st, now=NOW)
    assert labs.self_check(p) == []
    alt = p["markers"]["alt"]
    assert alt["value"] == 85 and alt["status"] == "critical"
    assert alt["group"] == "Liver"
    assert alt["n"] == 2 and len(alt["trend"]) == 2      # two panels tracked over time
    assert alt["delta"] == pytest.approx(-7.0)           # 85 - 92, improving
    assert "Liver" in p["groups"] and "Inflammation" in p["groups"]
    # vitamin D 27.8 is insufficient (warning), not deficient (critical)
    assert p["markers"]["vitamin_d"]["status"] == "warning"
    # the genuinely-critical markers surface for the doctor list
    crit_keys = {m["key"] for m in p["critical"]}
    assert {"alt", "hs_crp", "total_testosterone"} <= crit_keys


# ---- reversal.ladder / projection / doctor_list ------------------------------------------
def test_mean_for_gmi_inverse():
    assert reversal.mean_for_gmi(6.5) == pytest.approx(133.36, abs=0.1)


def test_ladder_positions_and_next_rung():
    lad = reversal.ladder(157.0)          # his baseline avg glucose -> GMI ~7.07
    assert lad["current_gmi"] == pytest.approx(7.07, abs=0.02)
    assert lad["next"]["gmi"] == 6.5
    assert lad["next"]["mean_needed"] == pytest.approx(133.4, abs=0.2)
    assert lad["next"]["mean_gap"] == pytest.approx(23.6, abs=0.3)
    assert 0.0 <= lad["overall_progress"] <= 1.0
    assert reversal.self_check({"ladder": lad}) == []


def test_ladder_goal_reached():
    lad = reversal.ladder(95.0)           # GMI ~5.58 -> below 5.7
    assert lad["goal_reached"] is True and "next" not in lad


def test_projection_trend_and_min_days():
    # a clear downward mean trend over 30 days -> improving, projected < current
    frame = [{"date": (NOW - timedelta(days=30 - i)).date().isoformat(),
              "mean_mgdl": 160.0 - i} for i in range(30)]
    proj = reversal.project(frame, horizon_days=90)
    assert proj["ok"] and proj["direction"] == "improving"
    assert proj["projected_gmi"] < proj["current_gmi"]
    assert 0.0 <= proj["r2"] <= 1.0
    assert reversal.self_check({"projection": proj}) == []
    # too few days -> not projected
    assert reversal.project(frame[:5])["ok"] is False


def test_doctor_list_from_data(st):
    with freeze_time(NOW):
        st.append_events("labs", [
            _lab("hs-CRP", 11.5, days_ago=1, unit="mg/L"),
            _lab("Prolactin", 31.4, days_ago=1, unit="ng/mL"),
            _lab("Testosterone", 162, days_ago=1, unit="ng/dL"),
        ], key_field="key", now=NOW)
        p = labs.panel(st, now=NOW)
    metrics = {"gmi_pct": {"value": 7.1}}
    items = reversal.doctor_list(labs_panel=p, metrics=metrics)
    areas = {it["area"] for it in items}
    assert "Glycemia" in areas and "Inflammation" in areas and "Hormones" in areas
    # the prolactin+testosterone pairing is called out together
    assert any("prolactin" in it["text"].lower() and "testosterone" in it["text"].lower()
               for it in items)
    assert all(it["priority"] in (1, 2) for it in items)


# ---- dashboard Reversal tab --------------------------------------------------------------
def test_reversal_tab_renders(st):
    with freeze_time(NOW):
        st.append_events("labs", [
            _lab("ALT", 85, days_ago=1, unit="U/L"),
            _lab("hs-CRP", 11.5, days_ago=1, unit="mg/L"),
        ], key_field="key", now=NOW)
        p = labs.panel(st, now=NOW)
        frame = [{"date": (NOW - timedelta(days=20 - i)).date().isoformat(),
                  "mean_mgdl": 150.0 - i} for i in range(20)]
        rev = reversal.build(frame, metrics={"gmi_pct": {"value": 7.0},
                                             "mean_mgdl": {"value": 150.0}}, labs_panel=p)
        cockpit = build_dashboard.build_cockpit(metrics={}, trend_by_window={},
                                                labs=p, reversal=rev, now=NOW)
        html = build_dashboard.render(cockpit)
    assert ">Reversal<" in html
    assert "Remission ladder" in html and "current GMI" in html
    assert "90-day GMI projection" in html
    assert "For your doctor" in html
    assert "ALT" in html and "hs-CRP" in html
    assert build_dashboard.self_check(cockpit) == []


# ---- regression: self_check must not false-alarm on quarantined value vs SVG coords -------
def test_self_check_no_false_alarm_on_svg_coordinate_collision():
    frame = [{"date": "2026-06-01", "mean_mgdl": 120.0},
             {"date": "2026-06-02", "mean_mgdl": 130.0}]   # -> a Trends sparkline with x=6.0 pad
    cockpit = build_dashboard.build_cockpit(
        metrics={}, trend_by_window={}, daily_frame=frame,
        latest_glucose={"ts": (NOW - timedelta(minutes=5)).isoformat(), "value": 150},
        quarantine=[{"payload": {"glucose_mgdl": 6.0}}], now=NOW)
    # 6.0 is a quarantined value AND appears as an SVG pad coordinate — must NOT be flagged
    assert build_dashboard.self_check(cockpit) == []


def test_labs_ingest_from_csv_source(st):
    csv_text = ("date,marker,value,unit,ref_high\n"
                "2026-05-01,ALT,92,U/L,40\n"
                "2026-06-30,ALT,78,U/L,40\n"
                "2026-06-30,hs-CRP,6.2,mg/L,3\n")
    with freeze_time(NOW):
        res = labs.ingest(st, labs.CsvLabsSource(text=csv_text), now=NOW)
        p = labs.panel(st, now=NOW)
    assert res.n_stored == 3
    assert p["markers"]["alt"]["value"] == 78 and p["markers"]["alt"]["n"] == 2
    assert p["markers"]["alt"]["delta"] == pytest.approx(-14.0)   # 78 - 92
    assert p["markers"]["hs_crp"]["status"] == "critical"
    assert labs.self_check(p) == []


def test_labs_parse_skips_incomplete_rows():
    rows = [{"date": "2026-06-30", "marker": "ALT", "value": ""},   # no value -> skip
            {"date": "", "marker": "AST", "value": "40"},           # no date -> skip
            {"date": "2026-06-30", "marker": "GGT", "value": "61", "unit": "U/L"}]
    out = labs.parse_rows(rows)
    assert len(out) == 1 and out[0]["marker"] == "GGT"
    assert out[0]["measured_at"] == "2026-06-30T09:00:00+04:00"


def test_self_check_flags_quarantined_value_in_header():
    cockpit = build_dashboard.build_cockpit(
        metrics={}, trend_by_window={},
        latest_glucose={"ts": (NOW - timedelta(minutes=5)).isoformat(), "value": 9999},
        quarantine=[{"payload": {"glucose_mgdl": 9999}}], now=NOW)
    assert build_dashboard.self_check(cockpit) != []

def test_doctor_list_cites_gmi_when_only_gmi_crossed():
    # GMI 7.0 (diabetic) but lab HbA1c 6.2 (below 6.5) -> must cite GMI, not HbA1c
    import store as store_mod
    with freeze_time(NOW):
        integrity.cache_clear()
        s = store_mod.Store(":memory:")
        s.append_events("labs", [_lab("HbA1c", 6.2, days_ago=1, unit="%")], key_field="key", now=NOW)
        p = labs.panel(s, now=NOW)
        items = reversal.doctor_list(labs_panel=p, metrics={"gmi_pct": {"value": 7.0}})
        s.close(); integrity.cache_clear()
    gly = next(it for it in items if it["area"] == "Glycemia")
    assert gly["based_on"].startswith("GMI")   # not "HbA1c 6.2%", which didn't cross 6.5


def test_reconcile_glycation_gap():
    r = reversal.reconcile(metrics={"gmi_pct": {"value": 6.9}},
                           labs_panel={"markers": {"hba1c": {"value": 6.2}}})
    assert r["ok"] and r["gap_pct"] == pytest.approx(0.7) and r["discordant"] is True
    assert reversal.reconcile(metrics={}, labs_panel={})["ok"] is False


def test_weight_view_direct_bands():
    frame = [{"date": f"2026-05-{d:02d}", "weight_kg": w}
             for d, w in [(1, 126.0), (10, 122.0), (20, 118.0), (30, 114.0)]]  # 12 kg lost
    w = reversal.weight_view(frame)
    assert w["ok"] and w["kg_lost"] == pytest.approx(12.0)
    assert w["remission_band_pct"] == 57      # 10–15 kg band
    assert w["pct_lost"] == pytest.approx(9.5, abs=0.1)
    assert reversal.weight_view([{"date": "2026-05-01", "weight_kg": 100}])["ok"] is False


def test_hepatic_scores_de_ritis_and_fib4():
    markers = {"alt": {"value": 85}, "ast": {"value": 47}, "platelets": {"value": 220}}
    d0 = labs.hepatic_scores(markers)                     # no age -> De Ritis only
    assert [x["key"] for x in d0] == ["de_ritis"]
    assert d0[0]["value"] == pytest.approx(0.55, abs=0.01)
    d1 = labs.hepatic_scores(markers, age=35)             # + age + platelets -> FIB-4
    keys = {x["key"] for x in d1}
    assert keys == {"de_ritis", "fib4"}
    fib4 = next(x for x in d1 if x["key"] == "fib4")
    assert fib4["value"] == pytest.approx((35 * 47) / (220 * (85 ** 0.5)), abs=0.01)
