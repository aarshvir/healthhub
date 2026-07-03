"""labs.py — hand-entered lab panels: reference ranges, status, trends.

Bloodwork is the other half of a diabetes-reversal loop: glucose is the engine, but liver
(ALT/AST/GGT), inflammation (hs-CRP/ESR), hormones (testosterone/prolactin/SHBG), body
composition and micronutrients are what say whether the whole metabolic picture is turning.
They move on a *re-test* cadence (months), not minutes — so they live in the ``labs`` events
stream and render with their true test date + age (§A rule 6: never pretend fresh).

This module holds a GENERIC clinical reference catalog (adult ranges, direction-of-good, and
grouping) — no personal values live here. It reads the user's own entered panels from the
store, attaches each marker's reference range + in/out-of-range status, and builds a per-marker
trend so the dashboard can show movement over successive panels. Status uses the reserved
status palette semantics (good / warning / critical) so a flagged marker is never colour-alone.

Everything that touches the clock goes through :mod:`integrity` (the architecture test enforces
it); numbers are the user's own entered figures, never invented.
"""

from __future__ import annotations

import math

import integrity

# direction-of-good: how to read a value against its reference range
LOW_GOOD = "low_good"     # lower is healthier; flagged when it climbs above ref_high
HIGH_GOOD = "high_good"   # higher is healthier; flagged when it falls below ref_low
IN_RANGE = "in_range"     # both bounds matter; flagged outside [ref_low, ref_high]

# severity band multipliers past the exceeded bound (warning below, critical beyond)
_WARN_FACTOR = 1.5        # low_good: value <= 1.5*ref_high is a warning, beyond is critical
_HIGH_GOOD_WARN = 0.66    # high_good: value >= 0.66*ref_low is a warning, below is critical
_IN_RANGE_WARN = 1.25     # in_range: within 1.25x of the breached bound is a warning

# A generic adult reference catalog (male-oriented where sex-specific), grouped by system.
# key -> (display, unit, ref_low, ref_high, direction, group, optimal). optimal = the
# reversal-target the dashboard nudges toward (may be None). ref_low/ref_high may be None.
CATALOG = {
    # --- glycemic ---
    "hba1c": ("HbA1c", "%", None, 5.6, LOW_GOOD, "Glycemic", 5.4),
    "fasting_glucose": ("Fasting glucose", "mg/dL", 70.0, 99.0, LOW_GOOD, "Glycemic", 90.0),
    "fasting_insulin": ("Fasting insulin", "µIU/mL", 2.0, 10.0, IN_RANGE, "Glycemic", 6.0),
    "homa_ir": ("HOMA-IR", "", None, 2.0, LOW_GOOD, "Glycemic", 1.2),
    # --- liver (fatty-liver signature) ---
    "alt": ("ALT", "U/L", None, 40.0, LOW_GOOD, "Liver", 25.0),
    "ast": ("AST", "U/L", None, 40.0, LOW_GOOD, "Liver", 25.0),
    "ggt": ("GGT", "U/L", None, 55.0, LOW_GOOD, "Liver", 30.0),
    "platelets": ("Platelets", "10⁹/L", 150.0, 400.0, IN_RANGE, "Liver", 250.0),
    # --- inflammation ---
    "hs_crp": ("hs-CRP", "mg/L", None, 3.0, LOW_GOOD, "Inflammation", 1.0),
    "esr": ("ESR", "mm/hr", None, 20.0, LOW_GOOD, "Inflammation", 10.0),
    # --- hormones ---
    "total_testosterone": ("Total testosterone", "ng/dL", 264.0, 916.0, HIGH_GOOD, "Hormones", 500.0),
    "free_testosterone": ("Free testosterone", "pg/mL", 8.7, 25.1, HIGH_GOOD, "Hormones", 18.0),
    "shbg": ("SHBG", "nmol/L", 16.5, 55.9, IN_RANGE, "Hormones", 35.0),
    "prolactin": ("Prolactin", "ng/mL", None, 18.0, LOW_GOOD, "Hormones", 10.0),
    "tsh": ("TSH", "µIU/mL", 0.4, 4.0, IN_RANGE, "Hormones", 1.8),
    # --- micronutrient / other ---
    "vitamin_d": ("Vitamin D", "ng/mL", 30.0, 100.0, HIGH_GOOD, "Micronutrient", 50.0),
    "homocysteine": ("Homocysteine", "µmol/L", None, 15.0, LOW_GOOD, "Micronutrient", 8.0),
    "vitamin_b12": ("Vitamin B12", "pg/mL", 200.0, 900.0, HIGH_GOOD, "Micronutrient", 500.0),
    "ferritin": ("Ferritin", "ng/mL", 30.0, 400.0, IN_RANGE, "Micronutrient", 150.0),
    "hemoglobin": ("Hemoglobin", "g/dL", 13.0, 17.0, HIGH_GOOD, "Blood", 15.0),
    # --- body ---
    "bmi": ("BMI", "kg/m²", None, 25.0, LOW_GOOD, "Body", 23.0),
    # --- lipids (this user's are already good — shown for completeness) ---
    "apob": ("ApoB", "mg/dL", None, 90.0, LOW_GOOD, "Lipids", 70.0),
    "ldl": ("LDL", "mg/dL", None, 100.0, LOW_GOOD, "Lipids", 80.0),
    "hdl": ("HDL", "mg/dL", 40.0, None, HIGH_GOOD, "Lipids", 55.0),
    "triglycerides": ("Triglycerides", "mg/dL", None, 150.0, LOW_GOOD, "Lipids", 90.0),
}
GROUP_ORDER = ("Glycemic", "Liver", "Inflammation", "Hormones", "Micronutrient",
               "Blood", "Body", "Lipids")

# marker-name aliases (normalized) -> catalog key
_ALIASES = {
    "hba1c": "hba1c", "a1c": "hba1c", "glycatedhemoglobin": "hba1c",
    "fastingglucose": "fasting_glucose", "glucosefasting": "fasting_glucose", "fbs": "fasting_glucose",
    "fastinginsulin": "fasting_insulin", "insulin": "fasting_insulin",
    "homair": "homa_ir", "homa": "homa_ir",
    "alt": "alt", "sgpt": "alt", "altsgpt": "alt",
    "ast": "ast", "sgot": "ast", "astsgot": "ast",
    "ggt": "ggt", "gammagt": "ggt",
    "hscrp": "hs_crp", "crp": "hs_crp", "creactiveprotein": "hs_crp",
    "esr": "esr",
    "totaltestosterone": "total_testosterone", "testosterone": "total_testosterone",
    "freetestosterone": "free_testosterone", "freet": "free_testosterone",
    "shbg": "shbg", "prolactin": "prolactin", "tsh": "tsh",
    "platelets": "platelets", "platelet": "platelets", "plt": "platelets", "plateletcount": "platelets",
    "vitamind": "vitamin_d", "vitd": "vitamin_d", "25ohd": "vitamin_d",
    "homocysteine": "homocysteine",
    "vitaminb12": "vitamin_b12", "b12": "vitamin_b12",
    "ferritin": "ferritin", "hemoglobin": "hemoglobin", "hb": "hemoglobin",
    "bmi": "bmi", "apob": "apob", "ldl": "ldl", "hdl": "hdl",
    "triglycerides": "triglycerides", "tg": "triglycerides",
}


def normalize(marker) -> str | None:
    """Map a free-text marker name to a catalog key (best effort), else None."""
    if not marker:
        return None
    slug = "".join(ch for ch in str(marker).lower() if ch.isalnum())
    if slug in _ALIASES:
        return _ALIASES[slug]
    return slug if slug in CATALOG else None


def status(key: str, value, *, ref_low=None, ref_high=None) -> str:
    """'good' / 'warning' / 'critical' / 'unknown' for *value* of marker *key*.

    Reference bounds come from the catalog; a record may override them (``ref_low``/``ref_high``).
    """
    if value is None or key not in CATALOG:
        return "unknown"
    disp = CATALOG[key]
    lo = ref_low if ref_low is not None else disp[2]
    hi = ref_high if ref_high is not None else disp[3]
    direction = disp[4]
    v = float(value)
    if direction == LOW_GOOD:
        if hi is None:
            return "unknown"
        if v <= hi:
            return "good"
        return "warning" if v <= _WARN_FACTOR * hi else "critical"
    if direction == HIGH_GOOD:
        if lo is None:
            return "unknown"
        if v >= lo:
            return "good"
        return "warning" if v >= _HIGH_GOOD_WARN * lo else "critical"
    # IN_RANGE
    if lo is not None and v < lo:
        return "warning" if v >= lo / _IN_RANGE_WARN else "critical"
    if hi is not None and v > hi:
        return "warning" if v <= hi * _IN_RANGE_WARN else "critical"
    if lo is None and hi is None:
        return "unknown"
    return "good"


def _ref_text(key: str, ref_low, ref_high) -> str:
    disp = CATALOG.get(key)
    lo = ref_low if ref_low is not None else (disp[2] if disp else None)
    hi = ref_high if ref_high is not None else (disp[3] if disp else None)
    if lo is not None and hi is not None:
        return f"{lo:g}–{hi:g}"
    if hi is not None:
        return f"≤{hi:g}"
    if lo is not None:
        return f"≥{lo:g}"
    return "—"


def _num(x):
    try:
        return None if x is None or x == "" else float(x)
    except (TypeError, ValueError):
        return None


def _age_str(delta) -> str:
    secs = abs(delta.total_seconds())
    if secs < 86400:
        return f"{secs / 3600:.0f}h"
    days = secs / 86400
    if days < 90:
        return f"{days:.0f}d"
    return f"{days / 30:.0f}mo"


def parse_rows(rows: list[dict]) -> list[dict]:
    """Normalize hand-entered lab rows into events. Accepts columns: date (or measured_at),
    marker (or test), value (or result), unit, ref_high, ref_low, [time], [key]. Rows without a
    date/marker/value are skipped. Timestamps are made tz-aware in Asia/Dubai (labs are dated,
    not timed) so they pass the integrity freshness gate."""
    out = []
    for raw in rows:
        d = (raw.get("date") or raw.get("measured_at") or "").strip() if isinstance(
            raw.get("date") or raw.get("measured_at"), str) else (raw.get("date") or raw.get("measured_at"))
        marker = raw.get("marker") or raw.get("test") or raw.get("name")
        val = _num(raw.get("value") if raw.get("value") not in (None, "") else raw.get("result"))
        if not d or not marker or val is None:
            continue
        ds = str(d)[:10]
        t = (str(raw.get("time")).strip() if raw.get("time") else "") or "09:00"
        if len(t) == 5:          # HH:MM -> add seconds
            t = t + ":00"
        elif len(t) != 8:        # anything unexpected -> a stable default
            t = "09:00:00"
        ts = f"{ds}T{t}+04:00"   # labs are dated, not timed; Asia/Dubai so freshness passes
        out.append({"key": str(raw.get("key") or f"{marker}-{ds}"), "measured_at": ts,
                    "marker": str(marker), "value": val, "unit": raw.get("unit") or "",
                    "ref_high": _num(raw.get("ref_high")), "ref_low": _num(raw.get("ref_low"))})
    return out


class CsvLabsSource:
    """Read labs from CSV text or a file path (columns per :func:`parse_rows`)."""

    name = "csv-labs"

    def __init__(self, *, text: str | None = None, path: str | None = None):
        if text is None and path is None:
            raise ValueError("provide text= or path=")
        self._text, self._path = text, path

    def read(self) -> list[dict]:
        import csv
        import io
        if self._text is not None:
            return parse_rows(list(csv.DictReader(io.StringIO(self._text))))
        with open(self._path, encoding="utf-8") as fh:
            return parse_rows(list(csv.DictReader(fh)))


class GSheetLabsSource:
    """Read a labs tab from a Google Sheet via gspread + a service account (lazy import)."""

    name = "gsheet-labs"

    def __init__(self, sheet_id: str, *, worksheet: str = "labs",
                 service_account_path: str | None = None):
        self.sheet_id, self.worksheet = sheet_id, worksheet
        self.service_account_path = service_account_path

    def read(self) -> list[dict]:
        import gspread  # lazy: only needed for live reads
        gc = (gspread.service_account(filename=self.service_account_path)
              if self.service_account_path else gspread.service_account())
        sh = gc.open_by_key(self.sheet_id)
        try:
            ws = sh.worksheet(self.worksheet)
        except gspread.WorksheetNotFound:
            # dedicated labs spreadsheet whose tab isn't named 'labs' — use the first tab.
            # Safe even if pointed at the wrong sheet: parse_rows only accepts rows that
            # actually have date+marker+value columns, so anything else yields [].
            ws = sh.sheet1
        return parse_rows(ws.get_all_records())


def ingest(store, source, *, now=None):
    """Read *source* and upsert its lab records into the store's ``labs`` stream (idempotent)."""
    now = integrity.now_utc() if now is None else now
    return store.append_events("labs", source.read(), key_field="key",
                               ts_field="measured_at", now=now)


def hepatic_scores(markers: dict, *, age=None) -> list[dict]:
    """Derived liver-fibrosis scores from an entered panel (fatty liver is a core target).

    De Ritis (AST/ALT) needs only the transaminases; FIB-4 additionally needs age + platelets.
    """
    out = []
    alt = _num((markers.get("alt") or {}).get("value"))
    ast = _num((markers.get("ast") or {}).get("value"))
    plt = _num((markers.get("platelets") or {}).get("value"))
    age = _num(age)
    if alt and ast and alt > 0:
        ratio = ast / alt
        out.append({"key": "de_ritis", "name": "AST/ALT (De Ritis)", "value": round(ratio, 2),
                    "unit": "", "status": "warning" if ratio >= 1.0 else "good",
                    "note": "De Ritis ratio — typically <1 in fatty liver; ≥1 can signal fibrosis."})
        if age and plt and plt > 0:
            fib4 = (age * ast) / (plt * math.sqrt(alt))
            st = "good" if fib4 < 1.3 else ("warning" if fib4 <= 2.67 else "critical")
            out.append({"key": "fib4", "name": "FIB-4", "value": round(fib4, 2), "unit": "",
                        "status": st,
                        "note": "Fibrosis-4 index — <1.3 low risk, >2.67 advanced-fibrosis risk."})
    return out


def panel(store, *, now=None, age=None) -> dict:
    """Latest value + status + trend for every entered lab marker, grouped by system.

    Returns ``{groups: {group: [marker_dict,...]}, markers: {key: marker_dict}, n_markers,
    critical: [...], generated_at}``. Each marker_dict carries value, unit, ref, status,
    direction, optimal, latest ts + age, delta vs previous panel, and the full trend series.
    """
    now = integrity.now_utc() if now is None else now
    by_key: dict[str, list] = {}
    unknown: dict[str, list] = {}
    for ev in store.events("labs"):
        key = normalize(ev.get("marker"))
        ts = ev.get("measured_at") or ev.get("ts")
        val = _num(ev.get("value"))
        if val is None or ts is None:
            continue
        rec = {"ts": ts, "value": val, "unit": ev.get("unit"),
               "ref_low": _num(ev.get("ref_low")), "ref_high": _num(ev.get("ref_high")),
               "marker": ev.get("marker")}
        (by_key if key else unknown).setdefault(key or str(ev.get("marker")), []).append(rec)

    markers: dict[str, dict] = {}
    groups: dict[str, list] = {}
    for key, recs in by_key.items():
        recs.sort(key=lambda r: r["ts"])              # chronological
        last = recs[-1]
        prev = recs[-2] if len(recs) > 1 else None
        disp = CATALOG[key]
        st = status(key, last["value"], ref_low=last["ref_low"], ref_high=last["ref_high"])
        try:
            age = _age_str(integrity.freshness(last["ts"], now=now).age)
        except integrity.IntegrityError:
            age = None
        md = {
            "key": key, "name": disp[0], "unit": last.get("unit") or disp[1],
            "value": last["value"], "group": disp[5], "direction": disp[4],
            "optimal": disp[6], "status": st,
            "ref": _ref_text(key, last["ref_low"], last["ref_high"]),
            "ref_low": last["ref_low"] if last["ref_low"] is not None else disp[2],
            "ref_high": last["ref_high"] if last["ref_high"] is not None else disp[3],
            "ts": last["ts"], "age": age,
            "delta": (last["value"] - prev["value"]) if prev else None,
            "trend": [(r["ts"][:10], r["value"]) for r in recs],
            "n": len(recs),
        }
        markers[key] = md
        groups.setdefault(disp[5], []).append(md)

    ordered = {g: sorted(groups[g], key=lambda m: m["name"])
               for g in GROUP_ORDER if g in groups}
    critical = [m for m in markers.values() if m["status"] == "critical"]
    return {"groups": ordered, "markers": markers, "n_markers": len(markers),
            "critical": critical, "derived": hepatic_scores(markers, age=age),
            "generated_at": now.isoformat()}


def self_check(p: dict) -> list[str]:
    violations = []
    for key, m in p.get("markers", {}).items():
        if key not in CATALOG:
            violations.append(f"unknown marker leaked into panel: {key}")
        if m["status"] not in ("good", "warning", "critical", "unknown"):
            violations.append(f"{key}: bad status {m['status']}")
        dates = [d for d, _ in m["trend"]]
        if dates != sorted(dates):
            violations.append(f"{key}: trend not chronological")
    return violations
