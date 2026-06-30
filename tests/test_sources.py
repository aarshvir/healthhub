"""Tests for Part 4 input sources (bearable, healthifyme, oura) + config leak gate."""

from datetime import datetime, timezone

import pytest
from freezegun import freeze_time

import bearable
import config
import healthifyme
import integrity
import journal
import oura
import store as store_mod
import wearables

NOW = datetime(2026, 6, 28, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture()
def st():
    with freeze_time(NOW):
        integrity.cache_clear()
        s = store_mod.Store(":memory:")
        yield s
        s.close()
        integrity.cache_clear()


# ---- Bearable ----------------------------------------------------------------------
BEARABLE_CSV = (
    "date,time of day,category,detail,rating,notes\n"
    "2026-06-27,Morning,Mood,Okay,Okay (3),woke groggy\n"
    "2026-06-27,Morning,Energy,Low,Low (2),\n"
    "2026-06-27,Afternoon,Symptom,Brain fog,(4),after lunch\n"
    "2026-06-27,Night,Supplement,Magnesium,,bedtime\n"
)


def test_bearable_parses_and_ingests(st):
    recs = bearable.parse_csv(BEARABLE_CSV)
    assert bearable.self_check(recs) == []
    bymood = next(r for r in recs if r["type"] == "mood")
    assert bymood["mood_1to5"] == 3
    assert next(r for r in recs if r["type"] == "energy")["energy_1to5"] == 2
    sym = next(r for r in recs if r["type"] == "symptom")
    assert sym["symptom"] == "Brain fog" and sym["symptom_sev_1to5"] == 4
    assert any(r["type"] == "supplement" for r in recs)
    # ingest -> appears in the journal log stream
    with freeze_time(NOW):
        bearable.ingest(st, bearable.CsvBearableSource(text=BEARABLE_CSV), now=NOW)
    logged = journal.records(st)
    assert any(r.get("mood_1to5") == 3 for r in logged)
    assert any(r.get("symptom") == "Brain fog" for r in logged)


# ---- HealthifyMe -------------------------------------------------------------------
HME_CSV = (
    "date,meal,food,calories,carbs,protein,fat\n"
    "2026-06-27,Lunch,Chilli Paneer,482,25,26,32\n"
    "2026-06-27,Dinner,Dal Rice,520,70,18,12\n"
)


def test_healthifyme_meals_feed_food_impact(st):
    recs = healthifyme.parse_csv(HME_CSV)
    assert healthifyme.self_check(recs) == []
    assert recs[0]["type"] == "meal" and recs[0]["net_carbs_g"] == 25
    with freeze_time(NOW):
        healthifyme.ingest(st, healthifyme.CsvHealthifyMeSource(text=HME_CSV), now=NOW)
    meals = journal.meals(st)
    assert {m["item"] for m in meals} == {"Chilli Paneer", "Dal Rice"}


# ---- Oura --------------------------------------------------------------------------
SLEEP_DOCS = [{"day": "2026-06-27", "light_sleep_duration": 86 * 60, "deep_sleep_duration": 56 * 60,
               "rem_sleep_duration": 60 * 60, "awake_time": 10 * 60, "average_heart_rate": 58}]
ACT_DOCS = [{"day": "2026-06-27", "steps": 8200, "total_calories": 2400, "active_calories": 600}]


class _FakeResp:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return {"data": self._data}


class _FakeSession:
    def get(self, url, headers=None, params=None, timeout=None):
        return _FakeResp(SLEEP_DOCS if url.endswith("/sleep") else ACT_DOCS)


def test_oura_to_rows_parsed_by_wearables():
    rows = oura.to_rows(SLEEP_DOCS, ACT_DOCS)
    with freeze_time(NOW):
        data = wearables.parse(rows, now=NOW)
    day = data["days"]["2026-06-27"]
    assert day["sleep_total_min"] == pytest.approx(202.0)   # 86+56+60
    assert day["steps"] == 8200.0
    assert day["hr_avg"] == 58.0
    assert oura.self_check(data, now=NOW) == []


def test_oura_fetch_days_with_injected_session():
    with freeze_time(NOW):
        data = oura.fetch_days("tok", days=7, now=NOW, session=_FakeSession())
    assert "2026-06-27" in data["days"]


# ---- config leak gate --------------------------------------------------------------
def test_scan_paths_blocks_planted_secret(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-PLANTEDSECRET")
    good = tmp_path / "dashboard.html"
    good.write_text("<div>no secrets</div>")
    config.scan_paths([str(good)])  # clean -> ok
    bad = tmp_path / "leak.json"
    bad.write_text('{"key": "sk-ant-PLANTEDSECRET"}')
    with pytest.raises(RuntimeError):
        config.scan_paths([str(bad)], where="publish")


def test_presence_report_demo_mode(monkeypatch):
    for k in ("DEXCOM_USERNAME", "DEXCOM_PASSWORD", "NS_URL"):
        monkeypatch.delenv(k, raising=False)
    rep = config.presence_report()
    assert rep["demo_mode"] is True and rep["dexcom"] is False
