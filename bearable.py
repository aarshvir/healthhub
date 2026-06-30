"""bearable.py — parse a Bearable nightly CSV export into journal records (Part 4).

Bearable exports a "long" CSV: one row per logged item with a category (Mood / Energy /
Symptom / Supplement / Medication / Sleep / Note), a detail, an optional rating, and a
time-of-day. We map each row into the journal record shape (the same dicts
``journal.parse_rows`` emits) and append them to the store's ``log`` stream — so mood/energy
feed mood_energy.py and symptoms feed symptoms.py with no further wiring.

Clock only via ``integrity.now_utc``. Ratings are parsed with a hand-rolled regex (no
pandas coercion). Asia/Dubai local times.
"""

from __future__ import annotations

import csv
import io
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import integrity

LOCAL_TZ = "Asia/Dubai"
DEFAULT_TIME = "12:00"
TIME_OF_DAY = {"morning": "09:00", "afternoon": "14:00", "evening": "19:00",
               "night": "22:00", "before bed": "22:30", "midday": "12:00"}
SUPPLEMENT_CATEGORIES = {"supplement", "supplements", "medication", "vitamin", "vitamins"}
# per-source freshness (Bearable is a nightly manual export)
FRESH = (timedelta(hours=36), timedelta(hours=120))

_RATING_RE = re.compile(r"\((\d+)\)")


def _rating(text):
    if text is None:
        return None
    m = _RATING_RE.search(str(text))
    if m:
        return max(1.0, min(5.0, float(m.group(1))))
    s = str(text).strip()
    if s and s[0].isdigit():
        try:
            return max(1.0, min(5.0, float(re.split(r"[^\d.]", s)[0])))
        except ValueError:
            return None
    return None


def _sleep_hours(text):
    if not text:
        return None
    s = str(text).lower()
    h = re.search(r"(\d+(?:\.\d+)?)\s*h", s)
    m = re.search(r"(\d+)\s*m", s)
    if h or m:
        return round((float(h.group(1)) if h else 0) + (float(m.group(1)) / 60 if m else 0), 2)
    try:
        return float(s)
    except ValueError:
        return None


def _combine_ts(date_str, time_hint):
    if not date_str:
        return None
    t = None
    hint = str(time_hint or "").strip().lower()
    if re.match(r"^\d{1,2}:\d{2}", hint):
        t = hint[:5]
    elif hint in TIME_OF_DAY:
        t = TIME_OF_DAY[hint]
    t = t or DEFAULT_TIME
    for fmt in ("%Y-%m-%d %H:%M", "%d/%m/%Y %H:%M", "%m/%d/%Y %H:%M"):
        try:
            return datetime.strptime(f"{str(date_str).strip()} {t}", fmt).replace(
                tzinfo=ZoneInfo(LOCAL_TZ))
        except ValueError:
            continue
    return None


def _ci(row: dict, *names):
    low = {k.strip().lower(): v for k, v in row.items()}
    for n in names:
        if n in low and str(low[n]).strip():
            return low[n]
    return None


def _base(ts, idx):
    return {"entry_id": None, "measured_at": ts.isoformat(), "type": "note", "item": None,
            "tags": ["bearable"], "symptom": None, "supplements": None, "note": None,
            "calories_kcal": None, "net_carbs_g": None, "protein_g": None, "fat_g": None,
            "fiber_g": None, "mood_1to5": None, "energy_1to5": None, "symptom_sev_1to5": None,
            "glucose_mgdl": None, "sleep_h": None, "key": f"bearable-{idx}"}


def parse_rows(rows: list[dict]) -> list[dict]:
    out = []
    for idx, raw in enumerate(rows):
        ts = _combine_ts(_ci(raw, "date"), _ci(raw, "time of day", "time"))
        if ts is None:
            continue
        category = str(_ci(raw, "category", "type") or "").strip().lower()
        detail = _ci(raw, "detail", "sub-category", "name", "item")
        rating = _ci(raw, "rating", "amount", "value", "severity")
        note = _ci(raw, "notes", "note")
        rec = _base(ts, idx)
        rec["note"] = note
        if category == "mood":
            rec["type"], rec["mood_1to5"] = "mood", _rating(rating or detail)
        elif category == "energy":
            rec["type"], rec["energy_1to5"] = "energy", _rating(rating or detail)
        elif category == "symptom":
            rec["type"], rec["symptom"] = "symptom", detail
            rec["symptom_sev_1to5"] = _rating(rating)
        elif category in SUPPLEMENT_CATEGORIES:
            rec["type"], rec["supplements"], rec["item"] = "supplement", detail, detail
        elif category == "sleep":
            rec["type"], rec["sleep_h"] = "sleep", _sleep_hours(rating or detail)
        else:
            rec["item"] = detail
        if category:
            rec["tags"].append(category)
        out.append(rec)
    return out


def parse_csv(text: str) -> list[dict]:
    return parse_rows(list(csv.DictReader(io.StringIO(text))))


class CsvBearableSource:
    """Drop-in log source: ``.read()`` returns journal records ready for the log stream."""

    name = "bearable"

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
        for k in ("mood_1to5", "energy_1to5", "symptom_sev_1to5"):
            v = r.get(k)
            if v is not None and not (1 <= v <= 5):
                violations.append(f"{k}={v} out of 1..5")
        if r.get("sleep_h") is not None and not (0 <= r["sleep_h"] <= 24):
            violations.append(f"sleep_h={r['sleep_h']} out of 0..24")
        if not r.get("measured_at"):
            violations.append("record missing measured_at")
    return violations
