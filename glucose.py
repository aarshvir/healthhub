"""glucose.py — pull CGM readings and append them to the 500-day store.

Sources are pluggable:
  * ``FixtureSource``    — in-memory readings (offline / tests / acceptance cycle).
  * ``NightscoutSource`` — a Nightscout site's REST API (lazy ``requests``).
  * ``PydexcomSource``   — Dexcom Share via ``pydexcom`` (region="ous" by default; lazy import).

``sync()`` pulls with exponential backoff and appends through ``store.append_glucose`` (which
screens every row via integrity and quarantines bad/future rows). If the pull fails after all
retries, it degrades to the last-known-good already held in the store / integrity cache rather
than crashing the cycle. The clock is read only via ``integrity.now_utc``.
"""

from __future__ import annotations

import time

import pandas as pd

import integrity


class GlucosePullError(RuntimeError):
    """Raised when a source cannot be read after all retries."""


# ======================================================================================
# Sources
# ======================================================================================
class FixtureSource:
    """A fixed list of readings (dicts with measured_at + glucose_mgdl) or a DataFrame."""

    name = "fixture"

    def __init__(self, readings):
        self._readings = readings

    def fetch(self, *, days, now):
        if isinstance(self._readings, pd.DataFrame):
            return self._readings.to_dict("records")
        return list(self._readings)


class _FlakySource:
    """Test helper: fails the first ``fail_times`` fetches, then returns ``inner``."""

    name = "flaky"

    def __init__(self, inner, fail_times):
        self._inner, self._fail, self._calls = inner, fail_times, 0

    def fetch(self, *, days, now):
        self._calls += 1
        if self._calls <= self._fail:
            raise ConnectionError(f"transient failure {self._calls}")
        return self._inner.fetch(days=days, now=now)


class NightscoutSource:
    """Read SGV entries from a Nightscout site (``/api/v1/entries/sgv.json``)."""

    name = "nightscout"

    def __init__(self, base_url: str, token: str | None = None):
        self.base_url = base_url.rstrip("/")
        self.token = token

    def fetch(self, *, days, now):
        import requests  # lazy: only needed for live pulls

        params = {"count": int(days) * 288}
        if self.token:
            params["token"] = self.token
        resp = requests.get(f"{self.base_url}/api/v1/entries/sgv.json",
                            params=params, timeout=30)
        resp.raise_for_status()
        out = []
        for entry in resp.json():
            sgv = entry.get("sgv")
            epoch_ms = entry.get("date")
            if sgv is None or epoch_ms is None:
                continue
            measured = pd.to_datetime(int(epoch_ms), unit="ms", utc=True)
            out.append({"measured_at": measured, "glucose_mgdl": float(sgv)})
        return out


class PydexcomSource:
    """Read recent readings from Dexcom Share via pydexcom (region default 'ous')."""

    name = "pydexcom"

    def __init__(self, username: str, password: str, region: str = "ous",
                 tz: str = "Asia/Dubai"):
        self.username, self.password, self.region, self.tz = username, password, region, tz

    def fetch(self, *, days, now):
        from pydexcom import Dexcom  # lazy import
        from zoneinfo import ZoneInfo

        dex = Dexcom(username=self.username, password=self.password, region=self.region)
        minutes = min(1440, int(days) * 1440)
        readings = dex.get_glucose_readings(minutes=minutes, max_count=288 * int(days))
        tzinfo = ZoneInfo(self.tz)
        out = []
        for r in readings:
            # pydexcom returns a naive local datetime; attach the configured tz
            measured = r.datetime.replace(tzinfo=tzinfo)
            out.append({"measured_at": measured, "glucose_mgdl": float(r.value)})
        return out


# ======================================================================================
# Pull + sync
# ======================================================================================
def _to_frame(raw_records, subject: str) -> pd.DataFrame:
    if isinstance(raw_records, pd.DataFrame):
        df = raw_records.copy()
    else:
        df = pd.DataFrame(list(raw_records))
    if "subject" not in df.columns:
        df["subject"] = subject
    return df


def pull(source, *, days: int = 1, subject: str = "patient", now=None,
         attempts: int = 4, base_delay: float = 2.0, sleep=None) -> pd.DataFrame:
    """Fetch readings from *source* with exponential backoff (2s, 4s, 8s, 16s ...)."""
    now = integrity.now_utc() if now is None else now
    sleep = sleep or time.sleep
    last_exc: Exception | None = None
    for attempt in range(attempts):
        try:
            raw = source.fetch(days=days, now=now)
            return _to_frame(raw, subject)
        except Exception as exc:  # noqa: BLE001 - any source error is retryable
            last_exc = exc
            if attempt < attempts - 1:
                sleep(base_delay * (2 ** attempt))
    raise GlucosePullError(
        f"failed to pull from {getattr(source, 'name', 'source')} after {attempts} attempts"
    ) from last_exc


def last_known_good(subject: str = "patient", *, now=None):
    """The last-known-good reading from the integrity cache, or None if unavailable."""
    try:
        return integrity.cache_get("glucose_mgdl", subject, now=now)
    except integrity.CacheMiss:
        return None


def sync(store, source, *, days: int = 1, subject: str = "patient", now=None,
         attempts: int = 4, base_delay: float = 2.0, sleep=None):
    """Pull from *source* and append to *store*. Degrades to last-known-good on failure.

    Returns the store ``AppendResult`` (zero stored if the pull failed; existing history and
    the cached last-known-good remain intact).
    """
    now = integrity.now_utc() if now is None else now
    try:
        df = pull(source, days=days, subject=subject, now=now,
                  attempts=attempts, base_delay=base_delay, sleep=sleep)
    except GlucosePullError:
        return store.append_glucose(pd.DataFrame(), source=getattr(source, "name", "cgm"),
                                    subject=subject, now=now)
    return store.append_glucose(df, source=getattr(source, "name", "cgm"),
                                subject=subject, now=now)
