"""oura.py — optional Oura Ring v2 adapter, drop-in to the wearables pipeline (Part 4).

``OuraSource`` emits rows in the Health Connect export shape that ``wearables.parse`` already
understands (Sleep / Vitals / Activity stacked tables, ``Source(s)="oura"``), so it plugs in
wherever a ``wearables_source`` is accepted — zero engine change. One consolidated sleep row
per day (durations summed) so accumulation can't double-count. Lazy ``requests``; clock only
via ``integrity.now_utc``; the token comes from config (never hard-coded).
"""

from __future__ import annotations

from datetime import timedelta

import integrity

OURA_BASE = "https://api.ouraring.com"
LOCAL_TZ = "Asia/Dubai"
FRESH = (timedelta(hours=18), timedelta(hours=48))


def _min(seconds):
    return round(float(seconds) / 60.0, 1) if seconds not in (None, "") else None


def to_rows(sleep_docs: list[dict], activity_docs: list[dict]) -> list[list]:
    """Build wearables-shaped stacked tables (Sleep + Vitals + Activity) from Oura docs."""
    # consolidate multiple sleep periods per day into one row
    by_day: dict[str, dict] = {}
    for s in sleep_docs or []:
        day = s.get("day")
        if not day:
            continue
        agg = by_day.setdefault(day, {"light": 0.0, "deep": 0.0, "rem": 0.0, "awake": 0.0,
                                      "hr": []})
        agg["light"] += _min(s.get("light_sleep_duration")) or 0
        agg["deep"] += _min(s.get("deep_sleep_duration")) or 0
        agg["rem"] += _min(s.get("rem_sleep_duration")) or 0
        agg["awake"] += _min(s.get("awake_time")) or 0
        if s.get("average_heart_rate"):
            agg["hr"].append(float(s["average_heart_rate"]))

    rows: list[list] = []
    rows.append(["Date", "Source(s)", "Timezone", "Start Time", "End Time",
                 "Light Sleep (min)", "Deep Sleep (min)", "REM Sleep (min)", "Awake (min)"])
    for day, a in sorted(by_day.items()):
        rows.append([day, "oura", LOCAL_TZ, "", "", a["light"], a["deep"], a["rem"], a["awake"]])

    rows.append([])
    rows.append(["Date", "Source(s)", "Timezone", "Heart rate min (bpm)",
                 "Heart rate max (bpm)", "Heart rate avg (bpm)", "Oxygen saturation avg (%)"])
    for day, a in sorted(by_day.items()):
        hr = round(sum(a["hr"]) / len(a["hr"]), 1) if a["hr"] else ""
        rows.append([day, "oura", LOCAL_TZ, "", "", hr, ""])

    rows.append([])
    rows.append(["Date", "Source(s)", "Timezone", "Steps", "Total Calories (kcal)",
                 "Active Calories (kcal)"])
    for d in sorted(activity_docs or [], key=lambda x: x.get("day", "")):
        if not d.get("day"):
            continue
        rows.append([d["day"], "oura", LOCAL_TZ, d.get("steps", ""),
                     d.get("total_calories", ""), d.get("active_calories", "")])
    return rows


class OuraSource:
    """A wearables source backed by the Oura v2 API (or an injected session for tests)."""

    name = "oura"

    def __init__(self, token: str, *, days: int = 14, base_url: str = OURA_BASE, session=None):
        self.token, self.days, self.base_url, self._session = token, days, base_url, session

    def _fetch(self, endpoint: str, params: dict) -> list[dict]:
        session = self._session
        if session is None:
            import requests  # lazy
            session = requests
        resp = session.get(f"{self.base_url}/v2/usercollection/{endpoint}",
                           headers={"Authorization": f"Bearer {self.token}"},
                           params=params, timeout=30)
        resp.raise_for_status()
        return resp.json().get("data", [])

    def _date_range(self, now):
        end = now.astimezone().date()
        return {"start_date": (end - timedelta(days=self.days)).isoformat(),
                "end_date": end.isoformat()}

    def read_values(self, *, now=None) -> list[list]:
        now = integrity.now_utc() if now is None else now
        params = self._date_range(now)
        return to_rows(self._fetch("sleep", params), self._fetch("daily_activity", params))


def fetch_days(token: str, *, days: int = 14, now=None, session=None) -> dict:
    now = integrity.now_utc() if now is None else now
    src = OuraSource(token, days=days, session=session)
    import wearables  # local import to avoid a heavy top-level dependency cycle
    return wearables.parse(src.read_values(now=now), now=now)


def self_check(data: dict, *, now=None) -> list[str]:
    import wearables
    violations = list(wearables.self_check(data, now=now))
    for d, rec in data.get("days", {}).items():
        if rec.get("spo2_avg") is not None and not (70 <= rec["spo2_avg"] <= 100):
            violations.append(f"{d}: spo2 out of range")
        if rec.get("sleep_total_min") is not None and rec["sleep_total_min"] > 1440:
            violations.append(f"{d}: sleep > 24h")
    return violations
