"""healthhub — the single trust boundary.

Every other module obtains *trusted* health data, timestamps, cached last-known-good
values and *validated* narration **only** through this module. Defaults are fail-closed
(this is health-safety software); each strict default has an explicit opt-in escape hatch
so the module stays usable.

Public surface (frozen in :data:`__all__`):

* ``validate_readings(df)``   — physiological-range validation of a readings DataFrame.
* ``freshness(ts)``           — age + state (fresh / stale / expired / future) of a timestamp.
* ``cache_set`` / ``cache_get`` / ``cache_get_value`` / ``cache_clear`` — last-known-good cache.
* ``sanitize_narration(text, allowed_numbers)`` — blocks any ungrounded number in narration.
* ``assert_validated`` / ``now_utc`` — runtime tripwire + the one sanctioned wall-clock.
* ``RANGES`` — read-only view of the single-source-of-truth range table.

Guiding principles: fail closed, never guess (a timezone, a unit), single source of truth,
immutable verdicts, never mutate caller input, and float-discipline (epsilon, explicit
NaN/inf routing).
"""

from __future__ import annotations

import dataclasses
import enum
import math
import threading
import uuid
from datetime import datetime, timedelta, timezone
from types import MappingProxyType
from typing import Hashable, Iterable, Mapping

import numpy as np
import pandas as pd
from dateutil import parser as _dateparser

from _rules import RANGE_LIMITS, YEAR_RE, iter_number_tokens

__all__ = (
    # enums
    "FreshnessState", "Severity",
    # value objects
    "MetricRange", "RowError", "ValidationReport", "Freshness", "CacheEntry",
    # exceptions
    "IntegrityError", "ValidationError", "NarrationIntegrityError", "CacheMiss",
    # required public functions
    "validate_readings", "freshness",
    "cache_get", "cache_set", "cache_get_value", "cache_clear",
    "sanitize_narration",
    # helpers
    "assert_validated", "now_utc",
    # single-source-of-truth table (read-only)
    "RANGES",
)

_EPS = 1e-9
_PROCESS_TOKEN = uuid.uuid4().hex  # stamped on validated frames; per-process, unforgeable
_USED = False  # tripwire: flips True once any public guard is exercised


def _mark_used() -> None:
    global _USED
    _USED = True


# ======================================================================================
# Enums
# ======================================================================================
class FreshnessState(enum.Enum):
    FRESH = "fresh"
    STALE = "stale"
    EXPIRED = "expired"
    FUTURE = "future"


# Ordering for the cache read-time gate: FRESH < STALE < EXPIRED (FUTURE is always rejected).
_STATE_ORDER = {FreshnessState.FRESH: 0, FreshnessState.STALE: 1, FreshnessState.EXPIRED: 2}


class Severity(enum.Enum):
    OK = "ok"
    OUT_OF_RANGE = "out_of_range"
    NON_NUMERIC = "non_numeric"
    INFINITE = "infinite"
    MISSING = "missing"
    UNKNOWN_METRIC = "unknown_metric"
    BAD_TIMESTAMP = "bad_timestamp"
    DUPLICATE = "duplicate"


# ======================================================================================
# Value objects (frozen — a trust verdict cannot be mutated after the fact)
# ======================================================================================
@dataclasses.dataclass(frozen=True, slots=True)
class MetricRange:
    metric: str
    low: float
    high: float
    unit: str
    decimals: int
    allow_future: bool = False


@dataclasses.dataclass(frozen=True, slots=True)
class RowError:
    row_index: object
    metric: object
    value: object
    severity: Severity
    detail: str


@dataclasses.dataclass(frozen=True, slots=True)
class ValidationReport:
    clean: pd.DataFrame
    errors: tuple[RowError, ...]
    n_in: int
    n_clean: int
    n_rejected: int

    @property
    def ok(self) -> bool:
        return not self.errors

    def raise_if_invalid(self) -> "ValidationReport":
        if self.errors:
            raise ValidationError(self)
        return self


@dataclasses.dataclass(frozen=True, slots=True)
class Freshness:
    age: timedelta
    state: FreshnessState
    measured_at: datetime
    evaluated_at: datetime


@dataclasses.dataclass(frozen=True, slots=True)
class CacheEntry:
    value: float
    metric: str
    measured_at: datetime
    stored_at: datetime


# ======================================================================================
# Exceptions
# ======================================================================================
class IntegrityError(Exception):
    """Base for every integrity violation."""


class ValidationError(IntegrityError):
    def __init__(self, report: "ValidationReport"):
        self.report = report
        super().__init__(
            f"validation failed: {report.n_rejected}/{report.n_in} rows rejected"
        )


class NarrationIntegrityError(IntegrityError):
    def __init__(self, token: str, value: float, closest: float | None):
        self.token = token
        self.value = value
        self.closest = closest
        super().__init__(
            f"ungrounded number {token!r} (={value}); closest allowed={closest}"
        )


class CacheMiss(IntegrityError):
    """No trusted last-known-good value available for the requested key."""


# ======================================================================================
# Single-source-of-truth range table
# ======================================================================================
_RANGES: dict[str, MetricRange] = {
    metric: MetricRange(metric, low, high, unit, decimals)
    for metric, (low, high, unit, decimals) in RANGE_LIMITS.items()
}
RANGES: Mapping[str, MetricRange] = MappingProxyType(_RANGES)


# ======================================================================================
# Wall-clock — the ONE sanctioned accessor (so "now" also routes through integrity)
# ======================================================================================
def now_utc() -> datetime:
    """The single sanctioned wall-clock: a timezone-aware UTC ``datetime``."""
    return datetime.now(timezone.utc)


# ======================================================================================
# Timestamp normalization
# ======================================================================================
def _to_utc(ts, assume_tz: timezone | None) -> datetime:
    """Normalize *ts* to a tz-aware UTC datetime, failing closed on ambiguity."""
    if isinstance(ts, str):
        try:
            dt = _dateparser.isoparse(ts)
        except (ValueError, OverflowError) as exc:
            raise IntegrityError(f"unparseable timestamp: {ts!r}") from exc
    elif isinstance(ts, pd.Timestamp):
        if pd.isna(ts):
            raise IntegrityError("timestamp is NaT")
        dt = ts.to_pydatetime()
    elif isinstance(ts, datetime):
        dt = ts
    else:
        raise IntegrityError(f"unsupported timestamp type: {type(ts).__name__}")

    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        if assume_tz is None:
            raise IntegrityError("naive timestamp; pass assume_tz to attach a timezone")
        dt = dt.replace(tzinfo=assume_tz)
    return dt.astimezone(timezone.utc)


def _normalize_now(now) -> datetime:
    if now is None:
        return now_utc()
    if not isinstance(now, datetime) or now.tzinfo is None or now.tzinfo.utcoffset(now) is None:
        raise IntegrityError("now must be a tz-aware datetime")
    return now.astimezone(timezone.utc)


# ======================================================================================
# freshness
# ======================================================================================
def freshness(
    ts,
    *,
    now: datetime | None = None,
    assume_tz: timezone | None = None,
    fresh_within: timedelta = timedelta(hours=6),
    stale_within: timedelta = timedelta(hours=72),
    future_skew: timedelta = timedelta(minutes=5),
) -> Freshness:
    """Return the signed age and freshness :class:`FreshnessState` of *ts*.

    A timestamp meaningfully ahead of ``now`` (beyond ``future_skew``) is ``FUTURE`` — a
    distinct *fail* state (clock bug / tampering), never treated as "freshest".
    """
    _mark_used()
    if future_skew < timedelta(0):
        raise IntegrityError("future_skew must be >= 0")
    if not (fresh_within < stale_within):
        raise IntegrityError("require fresh_within < stale_within")

    measured = _to_utc(ts, assume_tz)
    evaluated = _normalize_now(now)
    age = evaluated - measured

    if age < -future_skew:
        state = FreshnessState.FUTURE
    elif age <= fresh_within:
        state = FreshnessState.FRESH
    elif age <= stale_within:
        state = FreshnessState.STALE
    else:
        state = FreshnessState.EXPIRED
    return Freshness(age=age, state=state, measured_at=measured, evaluated_at=evaluated)


# ======================================================================================
# validate_readings
# ======================================================================================
_REQUIRED_COLS = ("subject", "metric", "value", "measured_at")


def _empty_clean(df: pd.DataFrame) -> pd.DataFrame:
    out = df.iloc[0:0].copy()
    out.attrs["_integrity_validated"] = _PROCESS_TOKEN
    return out


def validate_readings(
    df: pd.DataFrame,
    *,
    raise_on_error: bool = True,
    drop_duplicates: bool = True,
) -> ValidationReport:
    """Validate a tidy DataFrame of readings against the physiological range table.

    Schema (one reading per row): ``subject``, ``metric``, ``value``, ``measured_at``
    (extra columns are preserved on clean rows). The caller's frame is never mutated.

    With ``raise_on_error=True`` (default, fail-closed) any rejected row raises
    :class:`ValidationError`. With ``raise_on_error=False`` the full report — clean rows
    plus *every* per-row error — is returned for inspection.
    """
    _mark_used()
    if not isinstance(df, pd.DataFrame):
        raise IntegrityError("validate_readings requires a pandas DataFrame")

    missing_cols = [c for c in _REQUIRED_COLS if c not in df.columns]
    if missing_cols:
        report = ValidationReport(
            clean=_empty_clean(df),
            errors=(RowError(None, None, None, Severity.MISSING,
                             f"missing column: {missing_cols[0]}"),),
            n_in=len(df), n_clean=0, n_rejected=len(df),
        )
        raise ValidationError(report)  # malformed frame: never partially trusted

    n_in = len(df)
    original_labels = list(df.index)            # preserved for RowError.row_index reporting
    # Work positionally on a 0..n-1 indexed copy so non-unique / MultiIndex labels cannot
    # cause one rejected row to silently take out its index-siblings.
    work = df.copy().reset_index(drop=True)
    errors: list[RowError] = []
    reject: set[int] = set()                    # POSITIONS rejected (one error recorded per row)

    def fail(pos, metric, value, severity, detail):
        if pos not in reject:
            reject.add(pos)
            errors.append(RowError(original_labels[pos], metric, value, severity, detail))

    subject_col = work["subject"]
    metric_col = work["metric"]
    value_col = work["value"]
    measured_col = work["measured_at"]
    coerced = pd.to_numeric(value_col, errors="coerce")
    orig_null = value_col.isna()
    arr = coerced.to_numpy(dtype="float64", na_value=np.nan)
    is_inf = np.isinf(arr)
    known_metric = metric_col.isin(list(_RANGES.keys()))
    normalized_ts: dict[int, datetime] = {}

    for pos in range(n_in):
        # 1) subject present
        if pd.isna(subject_col.iloc[pos]):
            fail(pos, metric_col.iloc[pos], value_col.iloc[pos], Severity.MISSING, "null subject")
            continue
        # 2) known metric
        metric = metric_col.iloc[pos]
        if not known_metric.iloc[pos]:
            fail(pos, metric, value_col.iloc[pos], Severity.UNKNOWN_METRIC,
                 f"unknown metric: {metric!r}")
            continue
        # 3) value: missing / non-numeric / infinite / out-of-range
        if orig_null.iloc[pos]:
            fail(pos, metric, value_col.iloc[pos], Severity.MISSING, "null value")
            continue
        if np.isnan(arr[pos]):
            fail(pos, metric, value_col.iloc[pos], Severity.NON_NUMERIC,
                 f"non-numeric value: {value_col.iloc[pos]!r}")
            continue
        if is_inf[pos]:
            fail(pos, metric, value_col.iloc[pos], Severity.INFINITE, "infinite value")
            continue
        mr = _RANGES[metric]
        v = float(arr[pos])
        if v < mr.low - _EPS or v > mr.high + _EPS:
            fail(pos, metric, v, Severity.OUT_OF_RANGE,
                 f"{v} outside [{mr.low}, {mr.high}] {mr.unit}")
            continue
        # 4) measured_at: tz-aware & parseable
        try:
            normalized_ts[pos] = _to_utc(measured_col.iloc[pos], assume_tz=None)
        except IntegrityError as exc:
            fail(pos, metric, value_col.iloc[pos], Severity.BAD_TIMESTAMP, str(exc))
            continue

    # 5) duplicates among rows that passed every check above (first wins)
    if drop_duplicates:
        seen: set = set()
        for pos in range(n_in):
            if pos in reject:
                continue
            key = (subject_col.iloc[pos], metric_col.iloc[pos], normalized_ts[pos])
            if key in seen:
                fail(pos, metric_col.iloc[pos], value_col.iloc[pos],
                     Severity.DUPLICATE, "duplicate (subject, metric, measured_at)")
            else:
                seen.add(key)

    keep = [pos for pos in range(n_in) if pos not in reject]
    clean = work.iloc[keep].copy()
    clean["value"] = pd.to_numeric(clean["value"], errors="coerce").astype("float64")
    clean = clean.reset_index(drop=True)
    clean.attrs["_integrity_validated"] = _PROCESS_TOKEN

    report = ValidationReport(
        clean=clean,
        errors=tuple(errors),
        n_in=n_in,
        n_clean=len(clean),
        n_rejected=len(errors),
    )
    if raise_on_error and report.errors:
        raise ValidationError(report)
    return report


def assert_validated(df: pd.DataFrame) -> pd.DataFrame:
    """Runtime tripwire: raise unless *df* was produced by :func:`validate_readings`.

    Note: ``DataFrame.attrs`` can be dropped by some pandas operations, so the static
    architecture test remains the primary guarantee; this is a defence-in-depth backstop.
    """
    if not isinstance(df, pd.DataFrame):
        raise IntegrityError("assert_validated requires a pandas DataFrame")
    if df.attrs.get("_integrity_validated") != _PROCESS_TOKEN:
        raise IntegrityError("DataFrame was not produced by validate_readings")
    return df


# ======================================================================================
# sanitize_narration
# ======================================================================================
def _normalize_allowed(allowed_numbers) -> frozenset:
    values = allowed_numbers.values() if isinstance(allowed_numbers, Mapping) else allowed_numbers
    out: set[float] = set()
    for x in values:
        f = float(x)
        if not math.isfinite(f):
            raise ValueError("allowed_numbers must contain only finite numbers")
        out.add(f)
    return frozenset(out)


def sanitize_narration(
    text: str,
    allowed_numbers: Iterable[float] | Mapping[object, float],
    *,
    redact: bool = False,
    redaction: str = "[UNVERIFIED]",
    allow_dates: bool = False,
    abs_tolerance: float = 0.0,
) -> str:
    """Guarantee every numeric token in *text* is grounded in *allowed_numbers*.

    A token is accepted iff it lies within ``±(half-ULP-of-its-own-displayed-precision)``
    of some allowed value (honest display rounding), plus optional ``abs_tolerance``. The
    tolerance is keyed to the *token's* precision only — never the allowed value's — so a
    fabricated extra digit (``98.64`` vs grounded ``98.6``) is still blocked.

    Default (``redact=False``) raises :class:`NarrationIntegrityError` on the first
    ungrounded token (fail-closed: refuse to emit a narration carrying an invented number).
    With ``redact=True`` each offending token span is replaced with *redaction* instead.
    """
    _mark_used()
    if not isinstance(text, str):
        raise IntegrityError("sanitize_narration requires str text")
    if not math.isfinite(abs_tolerance) or abs_tolerance < 0:
        raise ValueError("abs_tolerance must be a finite, non-negative number")
    allowed = _normalize_allowed(allowed_numbers)

    redactions: list[tuple[int, int]] = []
    for match, subtokens in iter_number_tokens(text):
        for token, value, decimals in subtokens:
            if allow_dates and YEAR_RE.fullmatch(token.replace(",", "")):
                continue
            tol = 0.5 * (10 ** (-decimals)) + abs_tolerance + _EPS
            if allowed:
                closest = min(allowed, key=lambda a: abs(a - value))
                distance = abs(closest - value)
            else:
                closest, distance = None, math.inf
            if distance > tol:
                if not redact:
                    raise NarrationIntegrityError(token, value, closest)
                redactions.append((match.start(), match.end()))
                break  # redact the whole match span once

    if not redact or not redactions:
        return text

    pieces: list[str] = []
    cursor = 0
    for start, end in redactions:
        pieces.append(text[cursor:start])
        pieces.append(redaction)
        cursor = end
    pieces.append(text[cursor:])
    return "".join(pieces)


# ======================================================================================
# Last-known-good cache
# ======================================================================================
_CACHE: dict[tuple[str, Hashable], CacheEntry] = {}
_LOCK = threading.RLock()


def cache_set(
    metric: str,
    subject: Hashable,
    value: float,
    measured_at,
    *,
    assume_tz: timezone | None = None,
) -> CacheEntry:
    """Store a last-known-good value. Monotonic by measurement time: an older-or-equal
    ``measured_at`` is refused (the existing entry is kept and returned). A future-dated
    measurement is rejected outright. Only governed metrics may be cached.
    """
    _mark_used()
    if metric not in _RANGES:
        raise IntegrityError(f"refusing to cache ungoverned metric: {metric!r}")
    measured = _to_utc(measured_at, assume_tz)
    if freshness(measured).state is FreshnessState.FUTURE:
        raise IntegrityError("refusing to cache a future-dated measurement")

    fvalue = float(value)
    if not math.isfinite(fvalue):
        raise IntegrityError("refusing to cache a non-finite value")

    key = (metric, subject)
    with _LOCK:
        existing = _CACHE.get(key)
        if existing is not None and existing.measured_at >= measured:
            return existing  # monotonic: never regress to an older/equal reading
        entry = CacheEntry(value=fvalue, metric=metric,
                           measured_at=measured, stored_at=now_utc())
        _CACHE[key] = entry
        return entry


def cache_get(
    metric: str,
    subject: Hashable,
    *,
    max_state: FreshnessState = FreshnessState.STALE,
    now: datetime | None = None,
) -> CacheEntry:
    """Return the last-known-good :class:`CacheEntry`, or raise :class:`CacheMiss`.

    An ``EXPIRED`` entry self-evicts and misses (expired LKG is worse than none). An entry
    fresher-than-or-equal-to ``max_state`` is returned; a too-stale (but not expired) entry
    misses without eviction (a later, looser call may still want it).
    """
    _mark_used()
    with _LOCK:
        entry = _CACHE.get((metric, subject))
        if entry is None:
            raise CacheMiss(f"no cached value for ({metric!r}, {subject!r})")
        state = freshness(entry.measured_at, now=now).state
        if state is FreshnessState.EXPIRED:
            _CACHE.pop((metric, subject), None)
            raise CacheMiss(f"cached value for ({metric!r}, {subject!r}) is expired")
        if state is FreshnessState.FUTURE:  # defence in depth; set() already blocks this
            raise CacheMiss(f"cached value for ({metric!r}, {subject!r}) is future-dated")
        if _STATE_ORDER[state] > _STATE_ORDER[max_state]:
            raise CacheMiss(
                f"cached value for ({metric!r}, {subject!r}) is {state.value}, "
                f"worse than required {max_state.value}"
            )
        return entry


def cache_get_value(
    metric: str,
    subject: Hashable,
    *,
    max_state: FreshnessState = FreshnessState.STALE,
    now: datetime | None = None,
) -> float:
    """Convenience wrapper returning just the cached float."""
    return cache_get(metric, subject, max_state=max_state, now=now).value


def cache_clear() -> None:
    """Empty the cache atomically (test/reset helper)."""
    with _LOCK:
        _CACHE.clear()
