"""store.py — the 500-day idempotent multi-stream store, routed through integrity.

The glucose store is the backbone of the analysis engine: a SQLite table keyed by
timestamp (so re-ingesting the same reading is a no-op — idempotent) with a bounded
500-day retention window. Every write is screened by ``integrity.validate_readings`` and
``integrity.freshness``; anything that fails (bad range, non-numeric, naive/garbage or
*future* timestamp) is **quarantined** with a reason and never enters the clean table —
so a corrupted row is flagged, not plotted.

A generic ``events`` table holds the other streams (food/symptom/mood/experiment log
rows) keyed by (stream, idempotency-key). The store reads the clock only via
``integrity.now_utc`` and never coerces values itself (the architecture test enforces this).
"""

from __future__ import annotations

import dataclasses
import json
import sqlite3
from datetime import timedelta

import pandas as pd

import integrity

RETENTION_DAYS = 500
GLUCOSE_METRIC = "glucose_mgdl"


@dataclasses.dataclass(frozen=True, slots=True)
class AppendResult:
    n_in: int
    n_stored: int
    n_quarantined: int
    reasons: tuple[str, ...]


def _iso(dt) -> str:
    return dt.isoformat()


class Store:
    """SQLite-backed store. Use ``:memory:`` (default) for tests, a path for persistence."""

    def __init__(self, path: str = ":memory:"):
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    # -- schema ----------------------------------------------------------------------
    def _init_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS glucose (
                ts           TEXT PRIMARY KEY,   -- canonical UTC ISO; idempotency key
                subject      TEXT NOT NULL,
                glucose_mgdl REAL NOT NULL,
                source       TEXT,
                ingested_at  TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events (
                stream      TEXT NOT NULL,
                key         TEXT NOT NULL,        -- idempotency key within the stream
                ts          TEXT NOT NULL,        -- canonical UTC ISO
                payload     TEXT NOT NULL,        -- json
                ingested_at TEXT NOT NULL,
                PRIMARY KEY (stream, key)
            );
            CREATE TABLE IF NOT EXISTS quarantine (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                stream     TEXT NOT NULL,
                ts         TEXT,
                payload    TEXT NOT NULL,
                reason     TEXT NOT NULL,
                flagged_at TEXT NOT NULL
            );
            """
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # -- glucose (the 500-day backbone) ----------------------------------------------
    def append_glucose(self, df: pd.DataFrame, *, source: str = "cgm",
                       subject: str = "patient", now=None) -> AppendResult:
        """Validate *df* through integrity and idempotently upsert clean readings.

        *df* needs ``glucose_mgdl`` plus ``measured_at`` (tz-aware) or ``timestamp``;
        ``subject`` defaults to the argument. Rejected or future-dated rows are quarantined.
        """
        now = integrity.now_utc() if now is None else now
        if df is None or len(df) == 0:
            return AppendResult(0, 0, 0, ())

        ts_col = "measured_at" if "measured_at" in df.columns else "timestamp"
        if ts_col not in df.columns or GLUCOSE_METRIC not in df.columns:
            raise integrity.IntegrityError(
                "glucose frame needs glucose_mgdl and measured_at/timestamp columns")

        subj = df["subject"] if "subject" in df.columns else subject
        tidy = pd.DataFrame({
            "subject": subj,
            "metric": GLUCOSE_METRIC,
            "value": df[GLUCOSE_METRIC],
            "measured_at": df[ts_col],
        })
        report = integrity.validate_readings(tidy, raise_on_error=False, drop_duplicates=False)

        n_in = len(df)
        reasons: list[str] = []
        flagged_at = _iso(now)

        # quarantine integrity-rejected rows
        for err in report.errors:
            raw = self._row_json(df, err.row_index)
            self._quarantine("glucose", raw.get("ts"), raw, f"{err.severity.value}: {err.detail}",
                             flagged_at)
            reasons.append(err.severity.value)

        # upsert clean rows; future-dated rows are quarantined (no future timestamps invariant)
        clean = report.clean
        stored = 0
        for i in range(len(clean)):
            measured = clean["measured_at"].iloc[i]
            value = float(clean["value"].iloc[i])
            subj_val = clean["subject"].iloc[i]
            fresh = integrity.freshness(measured, now=now)
            if fresh.state is integrity.FreshnessState.FUTURE:
                self._quarantine("glucose", _iso(fresh.measured_at),
                                 {"glucose_mgdl": value, "subject": subj_val,
                                  "measured_at": _iso(fresh.measured_at)},
                                 "future timestamp", flagged_at)
                reasons.append("future_timestamp")
                continue
            ts_key = _iso(fresh.measured_at)  # canonical UTC -> idempotency key
            self.conn.execute(
                "INSERT INTO glucose(ts, subject, glucose_mgdl, source, ingested_at) "
                "VALUES(?,?,?,?,?) ON CONFLICT(ts) DO UPDATE SET "
                "glucose_mgdl=excluded.glucose_mgdl, subject=excluded.subject, "
                "source=excluded.source, ingested_at=excluded.ingested_at",
                (ts_key, str(subj_val), value, source, flagged_at),
            )
            stored += 1

        self.conn.commit()

        # cache last-known-good (most recent stored reading) through integrity
        if stored:
            latest = self.conn.execute(
                "SELECT ts, subject, glucose_mgdl FROM glucose ORDER BY ts DESC LIMIT 1"
            ).fetchone()
            try:
                integrity.cache_set(GLUCOSE_METRIC, latest["subject"],
                                    float(latest["glucose_mgdl"]), latest["ts"])
            except integrity.IntegrityError:
                pass  # e.g. latest happens to be future relative to a mocked clock

        return AppendResult(n_in, stored, n_in - stored, tuple(reasons))

    def glucose_window(self, start, end, *, now=None) -> pd.DataFrame:
        """Clean glucose readings with start <= measured_at <= end (tz-aware bounds)."""
        now = integrity.now_utc() if now is None else now
        s = _iso(integrity.freshness(start, now=now).measured_at)
        e = _iso(integrity.freshness(end, now=now).measured_at)
        rows = self.conn.execute(
            "SELECT ts, subject, glucose_mgdl, source FROM glucose "
            "WHERE ts >= ? AND ts <= ? ORDER BY ts", (s, e)
        ).fetchall()
        return self._glucose_frame(rows)

    def glucose_last_days(self, days: int, *, now=None) -> pd.DataFrame:
        now = integrity.now_utc() if now is None else now
        return self.glucose_window(now - timedelta(days=days), now, now=now)

    def glucose_all(self) -> pd.DataFrame:
        rows = self.conn.execute(
            "SELECT ts, subject, glucose_mgdl, source FROM glucose ORDER BY ts").fetchall()
        return self._glucose_frame(rows)

    def latest_glucose(self) -> dict | None:
        """The most recent stored reading as {ts, value, subject, source}, or None."""
        r = self.conn.execute(
            "SELECT ts, subject, glucose_mgdl, source FROM glucose ORDER BY ts DESC LIMIT 1"
        ).fetchone()
        if r is None:
            return None
        return {"ts": r["ts"], "value": float(r["glucose_mgdl"]),
                "subject": r["subject"], "source": r["source"]}

    def prune(self, *, now=None, retention_days: int = RETENTION_DAYS) -> int:
        """Delete readings older than the retention window. Returns rows deleted."""
        now = integrity.now_utc() if now is None else now
        cutoff = _iso(now - timedelta(days=retention_days))
        cur = self.conn.execute("DELETE FROM glucose WHERE ts < ?", (cutoff,))
        self.conn.commit()
        return cur.rowcount

    # -- generic events (food/symptom/mood/experiment log) ---------------------------
    def append_events(self, stream: str, records: list[dict], *, key_field: str = "key",
                      ts_field: str = "measured_at", now=None) -> AppendResult:
        """Idempotently upsert *records* into the events table for *stream*.

        Each record needs a timestamp (``ts_field``); the idempotency key is
        ``record[key_field]`` if present else the timestamp. Future-dated or unparseable
        timestamps are quarantined.
        """
        now = integrity.now_utc() if now is None else now
        flagged_at = _iso(now)
        stored, reasons = 0, []
        for rec in records or []:
            raw_ts = rec.get(ts_field)
            try:
                fresh = integrity.freshness(raw_ts, now=now)
            except integrity.IntegrityError as exc:
                self._quarantine(stream, str(raw_ts), rec, f"bad_timestamp: {exc}", flagged_at)
                reasons.append("bad_timestamp")
                continue
            if fresh.state is integrity.FreshnessState.FUTURE:
                self._quarantine(stream, _iso(fresh.measured_at), rec, "future timestamp", flagged_at)
                reasons.append("future_timestamp")
                continue
            ts_iso = _iso(fresh.measured_at)
            key = str(rec.get(key_field, ts_iso))
            self.conn.execute(
                "INSERT INTO events(stream, key, ts, payload, ingested_at) VALUES(?,?,?,?,?) "
                "ON CONFLICT(stream, key) DO UPDATE SET ts=excluded.ts, "
                "payload=excluded.payload, ingested_at=excluded.ingested_at",
                (stream, key, ts_iso, json.dumps(rec, default=str), flagged_at),
            )
            stored += 1
        self.conn.commit()
        n_in = len(records or [])
        return AppendResult(n_in, stored, n_in - stored, tuple(reasons))

    def events(self, stream: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT ts, payload FROM events WHERE stream = ? ORDER BY ts", (stream,)).fetchall()
        out = []
        for r in rows:
            rec = json.loads(r["payload"])
            rec.setdefault("ts", r["ts"])
            out.append(rec)
        return out

    # -- quarantine ------------------------------------------------------------------
    def quarantined(self, stream: str | None = None) -> list[dict]:
        if stream is None:
            rows = self.conn.execute(
                "SELECT stream, ts, payload, reason, flagged_at FROM quarantine ORDER BY id"
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT stream, ts, payload, reason, flagged_at FROM quarantine "
                "WHERE stream = ? ORDER BY id", (stream,)).fetchall()
        return [{"stream": r["stream"], "ts": r["ts"], "reason": r["reason"],
                 "flagged_at": r["flagged_at"], "payload": json.loads(r["payload"])}
                for r in rows]

    # -- self-check (in-cycle invariants) --------------------------------------------
    def self_check(self, *, now=None) -> list[str]:
        """Assert store invariants; return a list of human-readable violations (empty == ok)."""
        now = integrity.now_utc() if now is None else now
        violations: list[str] = []
        low, high = integrity.RANGES[GLUCOSE_METRIC].low, integrity.RANGES[GLUCOSE_METRIC].high
        for r in self.conn.execute("SELECT ts, glucose_mgdl FROM glucose").fetchall():
            if integrity.freshness(r["ts"], now=now).state is integrity.FreshnessState.FUTURE:
                violations.append(f"future timestamp in clean store: {r['ts']}")
            if not (low <= r["glucose_mgdl"] <= high):
                violations.append(f"out-of-range value in clean store: {r['glucose_mgdl']}")
        dupes = self.conn.execute(
            "SELECT ts, COUNT(*) c FROM glucose GROUP BY ts HAVING c > 1").fetchall()
        if dupes:
            violations.append(f"duplicate ts keys: {len(dupes)}")
        return violations

    # -- internals -------------------------------------------------------------------
    def _quarantine(self, stream, ts, payload, reason, flagged_at) -> None:
        self.conn.execute(
            "INSERT INTO quarantine(stream, ts, payload, reason, flagged_at) VALUES(?,?,?,?,?)",
            (stream, ts, json.dumps(payload, default=str), reason, flagged_at))

    @staticmethod
    def _row_json(df: pd.DataFrame, row_index) -> dict:
        try:
            row = df.loc[row_index]
            if isinstance(row, pd.DataFrame):
                row = row.iloc[0]
            out = {k: (str(v) if not isinstance(v, (int, float, bool, type(None))) else v)
                   for k, v in row.to_dict().items()}
        except Exception:
            return {"ts": None, "row_index": str(row_index)}
        for cand in ("measured_at", "timestamp", "ts"):
            if cand in out:
                out["ts"] = str(out[cand])
                break
        return out

    @staticmethod
    def _glucose_frame(rows) -> pd.DataFrame:
        if not rows:
            return pd.DataFrame({
                "subject": pd.Series([], dtype="object"),
                "measured_at": pd.Series([], dtype="datetime64[ns, UTC]"),
                "glucose_mgdl": pd.Series([], dtype="float64"),
                "source": pd.Series([], dtype="object"),
            })
        return pd.DataFrame({
            "subject": [r["subject"] for r in rows],
            "measured_at": pd.to_datetime([r["ts"] for r in rows], utc=True),
            "glucose_mgdl": [float(r["glucose_mgdl"]) for r in rows],
            "source": [r["source"] for r in rows],
        })
