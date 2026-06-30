"""wearables.py — parse the Health Connect export into wearables.json, via integrity.

The export (Samsung Health / Health Connect) is one sheet holding several stacked tables,
each beginning with a ``Date | Source(s) | Timezone | ...`` header:

  * Activity  (Steps, Total/Active Calories, walking exercises)
  * Body composition (Weight kg, Body fat …)
  * Hydration (ml)
  * Sleep (Light/Deep/REM/Awake minutes)
  * Vitals (Heart rate / SpO2 / Resp / BP / temperature)

Multiple apps report the same day (android, com.sec.android.app.shealth, healthconnect,
life.simple), so Activity is **deduplicated** to one preferred source per day. Dates are
treated as Asia/Dubai local; a future-dated row is quarantined (never trusted). Sources are
pluggable (``FixtureRows`` for tests; lazy ``GSheetWearablesSource``).
"""

from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

import integrity

LOCAL_TZ = "Asia/Dubai"
# Highest-trust source first; matched by prefix.
SOURCE_PRIORITY = ("com.sec.android.app.shealth", "com.android.healthconnect",
                   "life.simple", "android")


def _num(x):
    if x is None:
        return None
    s = str(x).strip().replace(",", "")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _source_rank(source: str) -> int:
    s = str(source or "")
    for i, pref in enumerate(SOURCE_PRIORITY):
        if s.startswith(pref):
            return i
    return len(SOURCE_PRIORITY)


def _table_kind(header: list[str]) -> str | None:
    cols = {c.strip() for c in header}
    if "Steps" in cols:
        return "activity"
    if "Weight (kg)" in cols:
        return "body"
    if "Hydration (ml)" in cols:
        return "hydration"
    if any(c.startswith("Light Sleep") for c in cols):
        return "sleep"
    if any(c.startswith("Heart rate min") for c in cols):
        return "vitals"
    return None


def split_tables(rows: list[list[str]]) -> list[tuple[str, list[str], list[list[str]]]]:
    """Split flat sheet rows into (kind, header, data_rows) blocks by their header rows."""
    tables, header, kind, body = [], None, None, []
    for row in rows:
        cells = [str(c) for c in row]
        if any(str(c).strip() == "Source(s)" for c in cells):  # a header row
            if header is not None and kind:
                tables.append((kind, header, body))
            header, kind, body = cells, _table_kind(cells), []
        elif header is not None and any(str(c).strip() for c in cells):
            body.append(cells)
    if header is not None and kind:
        tables.append((kind, header, body))
    return tables


def _row_dict(header, row):
    return {header[i].strip(): (row[i] if i < len(row) else "") for i in range(len(header))}


def parse(rows: list[list[str]], *, now=None) -> dict:
    """Parse the export rows into a wearables structure (days + walk events + quarantine)."""
    now = integrity.now_utc() if now is None else now
    days: dict[str, dict] = {}
    walk_events: list[dict] = []
    quarantine: list[dict] = []

    def day_slot(date_str):
        d = str(date_str).strip()[:10]
        if not d:
            return None
        # future-date guard via integrity (noon local -> compare to now)
        try:
            local_noon = datetime.strptime(d, "%Y-%m-%d").replace(
                hour=12, tzinfo=ZoneInfo(LOCAL_TZ))
        except ValueError:
            quarantine.append({"date": d, "reason": "bad_date"})
            return None
        if integrity.freshness(local_noon, now=now).state is integrity.FreshnessState.FUTURE:
            quarantine.append({"date": d, "reason": "future_date"})
            return None
        return days.setdefault(d, {"date": d})

    # Activity AND sleep need source-preference dedup; collect candidates then pick best per day.
    # (Multiple sleep *periods* from the SAME source on one day are summed; different SOURCES
    # on the same day are NOT summed — the preferred source wins, like Activity.)
    activity_candidates: dict[str, list[tuple[int, dict]]] = {}
    sleep_candidates: dict[str, dict[str, dict]] = {}

    for kind, header, body in split_tables(rows):
        for raw in body:
            rec = _row_dict(header, raw)
            date = rec.get("Date") or rec.get("Date/Time") or ""
            slot = day_slot(date)
            if slot is None:
                continue
            d = slot["date"]
            if kind == "activity":
                activity_candidates.setdefault(d, []).append((_source_rank(rec.get("Source(s)")), rec))
                ex = (rec.get("Exercise Name") or "").lower()
                if "walk" in ex and rec.get("Start Date/Time"):
                    walk_events.append({
                        "start": rec.get("Start Date/Time"),
                        "exercise": rec.get("Exercise Name"),
                        "duration_min": _num(rec.get("Duration (min)")),
                        "distance_m": _num(rec.get("Distance (m)")),
                    })
            elif kind == "body":
                slot["weight_kg"] = _num(rec.get("Weight (kg)"))
                slot["body_fat_pct"] = _num(rec.get("Body Fat (%)"))
            elif kind == "hydration":
                slot["hydration_ml"] = _num(rec.get("Hydration (ml)"))
            elif kind == "sleep":
                src = rec.get("Source(s)")
                agg = sleep_candidates.setdefault(d, {}).setdefault(
                    src, {"light": 0.0, "deep": 0.0, "rem": 0.0, "awake": 0.0})
                agg["light"] += _num(rec.get("Light Sleep (min)")) or 0
                agg["deep"] += _num(rec.get("Deep Sleep (min)")) or 0
                agg["rem"] += _num(rec.get("REM Sleep (min)")) or 0
                agg["awake"] += _num(rec.get("Awake (min)")) or 0
            elif kind == "vitals":
                slot["hr_min"] = _num(rec.get("Heart rate min (bpm)"))
                slot["hr_max"] = _num(rec.get("Heart rate max (bpm)"))
                slot["hr_avg"] = _num(rec.get("Heart rate avg (bpm)"))
                slot["spo2_avg"] = _num(rec.get("Oxygen saturation avg (%)"))

    for d, cands in activity_candidates.items():
        cands.sort(key=lambda x: x[0])  # preferred source first
        best = cands[0][1]
        days[d]["steps"] = _num(best.get("Steps"))
        days[d]["total_calories"] = _num(best.get("Total Calories (kcal)"))
        days[d]["active_calories"] = _num(best.get("Active Calories (kcal)"))
        days[d]["activity_source"] = best.get("Source(s)")

    # sleep: pick the single preferred source per day (periods within it already summed)
    for d, by_src in sleep_candidates.items():
        best_src = min(by_src.keys(), key=_source_rank)
        a = by_src[best_src]
        days[d]["sleep_light_min"] = a["light"]
        days[d]["sleep_deep_min"] = a["deep"]
        days[d]["sleep_rem_min"] = a["rem"]
        days[d]["sleep_awake_min"] = a["awake"]
        days[d]["sleep_total_min"] = a["light"] + a["deep"] + a["rem"]
        days[d]["sleep_source"] = best_src

    return {
        "generated_at": now.isoformat(),
        "timezone": LOCAL_TZ,
        "days": days,
        "walk_events": walk_events,
        "quarantine": quarantine,
        "n_days": len(days),
    }


# ======================================================================================
# Sources
# ======================================================================================
class FixtureRows:
    """Wrap an in-memory list-of-rows (get_all_values shape)."""

    name = "fixture-wearables"

    def __init__(self, rows):
        self._rows = rows

    def read_values(self, *, now=None):
        return self._rows


class GSheetWearablesSource:
    """Read the Health Connect export sheet via gspread (lazy)."""

    name = "gsheet-wearables"

    def __init__(self, sheet_id: str, *, worksheet: str | None = None,
                 service_account_path: str | None = None):
        self.sheet_id, self.worksheet = sheet_id, worksheet
        self.service_account_path = service_account_path

    def read_values(self, *, now=None):
        import gspread  # lazy

        gc = (gspread.service_account(filename=self.service_account_path)
              if self.service_account_path else gspread.service_account())
        sh = gc.open_by_key(self.sheet_id)
        ws = sh.worksheet(self.worksheet) if self.worksheet else sh.sheet1
        return ws.get_all_values()


def build(source, *, now=None) -> dict:
    return parse(source.read_values(now=now), now=now)


def write_wearables(data: dict, path: str = "wearables.json") -> str:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True, default=str)
    return path


def self_check(data: dict, *, now=None) -> list[str]:
    """Invariants: no future days, non-negative steps/sleep, HR within physiological range."""
    now = integrity.now_utc() if now is None else now
    hr = integrity.RANGES["heart_rate"]
    violations: list[str] = []
    for d, rec in data.get("days", {}).items():
        if integrity.freshness(datetime.strptime(d, "%Y-%m-%d").replace(
                hour=12, tzinfo=ZoneInfo(LOCAL_TZ)), now=now).state is integrity.FreshnessState.FUTURE:
            violations.append(f"future day in wearables: {d}")
        if rec.get("steps") is not None and rec["steps"] < 0:
            violations.append(f"{d}: negative steps")
        if rec.get("sleep_total_min") is not None and rec["sleep_total_min"] < 0:
            violations.append(f"{d}: negative sleep")
        for k in ("hr_min", "hr_max", "hr_avg"):
            v = rec.get(k)
            if v is not None and not (hr.low <= v <= hr.high):
                violations.append(f"{d}: {k}={v} outside HR range")
    return violations
