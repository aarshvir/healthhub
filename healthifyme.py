"""healthifyme.py — parse a HealthifyMe nutrition CSV into meal journal records (Part 4).

Carbs normally arrive via the Telegram logger marker (``/meal … carbs=…``). This adds a
periodic CSV-export path for depth: each food row becomes a ``type="meal"`` journal record
(with ``net_carbs_g`` set) on the ``log`` stream, so food_impact.py picks it up. If
HealthifyMe writes Nutrition to Health Connect instead, wearables.py carries it automatically.

Asia/Dubai local; clock only via ``integrity.now_utc``; numbers parsed by hand (no coercion).
"""

from __future__ import annotations

import csv
import io
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import integrity

LOCAL_TZ = "Asia/Dubai"
MEAL_SLOT_TIME = {"breakfast": "08:00", "morning snack": "10:30", "lunch": "13:00",
                  "snack": "16:00", "evening snack": "17:30", "dinner": "20:00",
                  "late snack": "22:30"}
DEFAULT_TIME = "13:00"
FRESH = (timedelta(hours=24), timedelta(hours=96))


def _num(x):
    if x is None:
        return None
    s = re.sub(r"[^\d.\-]", "", str(x))
    if not s or s in ("-", "."):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _ci(row, *names):
    low = {k.strip().lower(): v for k, v in row.items()}
    for n in names:
        if n in low and str(low[n]).strip():
            return low[n]
    return None


def _combine_ts(date_str, time_or_slot):
    if not date_str:
        return None
    hint = str(time_or_slot or "").strip().lower()
    if re.match(r"^\d{1,2}:\d{2}", hint):
        t = hint[:5]
    else:
        t = MEAL_SLOT_TIME.get(hint, DEFAULT_TIME)
    for fmt in ("%Y-%m-%d %H:%M", "%d/%m/%Y %H:%M", "%m/%d/%Y %H:%M"):
        try:
            return datetime.strptime(f"{str(date_str).strip()} {t}", fmt).replace(
                tzinfo=ZoneInfo(LOCAL_TZ))
        except ValueError:
            continue
    return None


def parse_rows(rows: list[dict]) -> list[dict]:
    out = []
    for idx, raw in enumerate(rows):
        ts = _combine_ts(_ci(raw, "date"), _ci(raw, "time", "meal", "slot", "meal type"))
        if ts is None:
            continue
        food = _ci(raw, "food", "item", "name") or _ci(raw, "meal", "slot")
        carbs = _num(_ci(raw, "net carbs", "net_carbs", "carbs", "carbohydrates"))
        out.append({
            "entry_id": None, "measured_at": ts.isoformat(), "type": "meal",
            "item": food, "tags": ["meal", "healthifyme"], "symptom": None,
            "supplements": None, "note": None,
            "calories_kcal": _num(_ci(raw, "calories", "kcal", "energy")),
            "net_carbs_g": carbs, "protein_g": _num(_ci(raw, "protein")),
            "fat_g": _num(_ci(raw, "fat")), "fiber_g": _num(_ci(raw, "fiber", "fibre")),
            "mood_1to5": None, "energy_1to5": None, "symptom_sev_1to5": None,
            "glucose_mgdl": None, "sleep_h": None,
            "key": f"hme-{ts.date().isoformat()}-{str(food or idx).strip().lower()[:24]}",
        })
    return out


def parse_csv(text: str) -> list[dict]:
    return parse_rows(list(csv.DictReader(io.StringIO(text))))


class CsvHealthifyMeSource:
    name = "healthifyme"

    def __init__(self, *, text: str | None = None, path: str | None = None):
        if text is None and path is None:
            raise ValueError("provide text= or path=")
        self._text, self._path = text, path

    def read(self) -> list[dict]:
        if self._text is not None:
            return parse_csv(self._text)
        with open(self._path, encoding="utf-8") as fh:
            return parse_csv(fh.read())


def ingest(store, source, *, now=None):
    now = integrity.now_utc() if now is None else now
    return store.append_events("log", source.read(), key_field="key",
                               ts_field="measured_at", now=now)


def self_check(records: list[dict]) -> list[str]:
    violations = []
    for r in records:
        if r.get("type") != "meal":
            violations.append("non-meal record from healthifyme")
        for k in ("net_carbs_g", "protein_g", "fat_g", "fiber_g", "calories_kcal"):
            v = r.get(k)
            if v is not None and v < 0:
                violations.append(f"{k}={v} negative")
    return violations
