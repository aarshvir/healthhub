"""journal.py — read the manual Health Log into typed records, routed through integrity.

Schema (matches the user's "Health Log … (data)" sheet):
  entry_id,date,time,day,type,item,food,calories_kcal,net_carbs_g,protein_g,fat_g,fiber_g,
  mood_1to5,energy_1to5,symptom,symptom_sev_1to5,supplements,glucose_mgdl,sleep_h,tags,note

``date``+``time`` are Asia/Dubai local; they are combined and normalized to a tz-aware UTC
timestamp. Sources are pluggable (``CsvLogSource`` for files/fixtures; ``GSheetLogSource`` as
a lazy gspread adapter). Records are appended to the store's ``log`` event stream idempotently
by ``entry_id``; future-dated/garbage timestamps are quarantined by the store.
"""

from __future__ import annotations

import csv
import io
from datetime import datetime
from zoneinfo import ZoneInfo

import integrity

LOCAL_TZ = "Asia/Dubai"
NUMERIC_FIELDS = ("calories_kcal", "net_carbs_g", "protein_g", "fat_g", "fiber_g",
                  "mood_1to5", "energy_1to5", "symptom_sev_1to5", "glucose_mgdl", "sleep_h")
COLUMNS = ("entry_id", "date", "time", "day", "type", "item", "food", "calories_kcal",
           "net_carbs_g", "protein_g", "fat_g", "fiber_g", "mood_1to5", "energy_1to5",
           "symptom", "symptom_sev_1to5", "supplements", "glucose_mgdl", "sleep_h",
           "tags", "note")


def _num(value):
    if value is None:
        return None
    s = str(value).strip().lstrip("~").replace(",", "")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _split_tags(value) -> list[str]:
    if not value:
        return []
    return [t.strip().lower() for t in str(value).replace(",", ";").split(";") if t.strip()]


def _combine_ts(date_str, time_str):
    """Combine local date + time (Asia/Dubai) into a tz-aware datetime; None on failure."""
    if not date_str or not time_str:
        return None
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            naive = datetime.strptime(f"{str(date_str).strip()} {str(time_str).strip()}", fmt)
            return naive.replace(tzinfo=ZoneInfo(LOCAL_TZ))
        except ValueError:
            continue
    return None


def parse_rows(rows: list[dict]) -> list[dict]:
    """Normalize raw log dict-rows into typed records. Rows with no usable ts are dropped."""
    out = []
    for raw in rows:
        ts = _combine_ts(raw.get("date"), raw.get("time"))
        if ts is None:
            continue
        rec = {
            "entry_id": str(raw.get("entry_id") or "").strip() or None,
            "measured_at": ts.isoformat(),
            "type": (str(raw.get("type") or "").strip().lower() or None),
            "item": raw.get("item") or raw.get("food") or None,
            "tags": _split_tags(raw.get("tags")),
            "symptom": (str(raw.get("symptom")).strip() or None) if raw.get("symptom") else None,
            "supplements": raw.get("supplements") or None,
            "note": raw.get("note") or None,
        }
        for f in NUMERIC_FIELDS:
            rec[f] = _num(raw.get(f))
        rec["key"] = rec["entry_id"] or rec["measured_at"]
        out.append(rec)
    return out


def parse_csv(text: str) -> list[dict]:
    reader = csv.DictReader(io.StringIO(text))
    return parse_rows(list(reader))


def local_date(ts_iso: str, tz: str = LOCAL_TZ) -> str | None:
    """Local (Asia/Dubai) calendar date 'YYYY-MM-DD' for an ISO timestamp, else None."""
    if not ts_iso:
        return None
    try:
        dt = datetime.fromisoformat(str(ts_iso))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo(tz))
    return dt.astimezone(ZoneInfo(tz)).date().isoformat()


# ======================================================================================
# Sources
# ======================================================================================
class CsvLogSource:
    """Read the log from CSV text or a file path."""

    name = "csv-log"

    def __init__(self, *, text: str | None = None, path: str | None = None):
        if text is None and path is None:
            raise ValueError("provide text= or path=")
        self._text, self._path = text, path

    def read(self) -> list[dict]:
        if self._text is not None:
            return parse_csv(self._text)
        with open(self._path, encoding="utf-8") as fh:
            return parse_csv(fh.read())


class GSheetLogSource:
    """Read the log from a Google Sheet via gspread + a service account (lazy import)."""

    name = "gsheet-log"

    def __init__(self, sheet_id: str, *, worksheet: str = "data",
                 service_account_path: str | None = None):
        self.sheet_id, self.worksheet = sheet_id, worksheet
        self.service_account_path = service_account_path

    def read(self) -> list[dict]:
        import gspread  # lazy: only needed for live reads

        gc = (gspread.service_account(filename=self.service_account_path)
              if self.service_account_path else gspread.service_account())
        ws = gc.open_by_key(self.sheet_id).worksheet(self.worksheet)
        return parse_rows(ws.get_all_records())


# ======================================================================================
# Ingest + accessors
# ======================================================================================
def ingest(store, source, *, now=None):
    """Read *source* and append its records to the store's ``log`` stream (idempotent)."""
    now = integrity.now_utc() if now is None else now
    records = source.read()
    return store.append_events("log", records, key_field="key",
                               ts_field="measured_at", now=now)


def records(store) -> list[dict]:
    return store.events("log")


def meals(store) -> list[dict]:
    return [r for r in store.events("log")
            if r.get("type") == "meal" or (r.get("net_carbs_g") or 0) > 0]
