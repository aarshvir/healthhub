"""Raw, *governed* primitives for :mod:`integrity` — the ONLY module that may import this.

This file holds the "dangerous" literals: the physiological range table, the numeric
tokenizer regex and the date/year shapes. They live here (private, underscore-prefixed)
so that the single sanctioned path to use them is through :mod:`integrity`. The
architecture test (``tests/test_architecture.py``) fails the build if any other module
imports ``_rules`` or re-defines these names.

Nothing in here makes a trust decision; it only provides the inert building blocks.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Iterator

# --------------------------------------------------------------------------------------
# Physiological range table — the SINGLE SOURCE OF TRUTH for plausible reading bounds.
# Bounds are *physiologically possible* (including critical values), inclusive [low, high].
# Columns: low, high, unit, decimals (canonical display precision).
# --------------------------------------------------------------------------------------
RANGE_LIMITS: dict[str, tuple[float, float, str, int]] = {
    "heart_rate":       (20.0, 250.0, "bpm",   0),
    "systolic_bp":      (50.0, 300.0, "mmHg",  0),
    "diastolic_bp":     (20.0, 200.0, "mmHg",  0),
    "spo2":             (50.0, 100.0, "%",     0),
    "temperature_c":    (25.0,  45.0, "C",     1),
    "respiratory_rate": ( 4.0,  80.0, "1/min", 0),
    "glucose_mgdl":     (20.0, 800.0, "mg/dL", 0),
    "weight_kg":        ( 1.0, 500.0, "kg",    1),
}

# --------------------------------------------------------------------------------------
# Numeric tokenizer.
#
# Scanned left-to-right; alternatives ordered most-specific first (clock-time, blood-
# pressure, percent, then plain number).
#
# Boundary design is deliberately FAIL-CLOSED:
#   * Leading ``(?<![\w.])`` refuses to *start* a number mid-identifier or mid-version, so
#     the digits in ``v1.2.3``, ``COVID19`` and ``SpO2`` are never tokenized.
#   * Trailing ``(?!\.\d)`` only refuses a *decimal/version continuation* (``1.2.3`` ->
#     no token). It intentionally PERMITS a number glued to letters or sentence
#     punctuation, so ``98.6F``, ``999mg`` and ``glucose 250.`` ARE tokenized and
#     therefore challenged — an invented number wearing a unit suffix cannot slip through.
#   * A bare digit after a hyphen (the ``7`` in ``Patient-7``) IS tokenized — again,
#     challenge-by-default rather than silently ignore.
#
# Clock times ``HH:MM[:SS]`` match the ``time`` group and yield NO sub-numbers: a colon-
# formatted token reads as a time, not a vital, so it cannot smuggle a measurement value.
#
# Thousands groups must be exactly three digits, so ``1,23`` tokenizes as TWO numbers
# (``1`` and ``23``) and is never silently merged into ``123``.
# --------------------------------------------------------------------------------------
NUMBER_RE = re.compile(
    r"""
      (?P<time> (?<![\w.]) \d{1,2} : \d{2} (?: : \d{2})? (?!\d) )                  # 14:30  09:05:30
    | (?P<bp>   (?<![\w.]) [+-]? \d{2,3} \s*/\s* \d{2,3} (?!\.\d) )                # 120/80
    | (?P<pct>  (?<![\w.]) [+-]? (?:\d{1,3}(?:,\d{3})+|\d+) (?:\.\d+)? \s*% )      # 95%  12.5 %
    | (?P<num>  (?<![\w.]) [+-]? (?:\d{1,3}(?:,\d{3})+|\d+) (?:\.\d+)? (?!\.\d) )  # 1,234  -3.5  98.6F
    """,
    re.VERBOSE,
)

# A bare 4-digit year (1900-2099). Used only when ``allow_dates=True`` to skip year tokens.
YEAR_RE = re.compile(r"(?:19|20)\d{2}")


def _decimals(cleaned: str) -> int:
    """Displayed fractional digits of a comma-stripped numeric literal.

    Uses :class:`~decimal.Decimal` so ``"95"`` (0) is distinguished from ``"95.0"`` (1) —
    the rounding tolerance is keyed to exactly this.
    """
    try:
        exp = Decimal(cleaned).as_tuple().exponent
    except InvalidOperation:  # pragma: no cover - tokenizer never yields this
        return 0
    return max(0, -exp) if isinstance(exp, int) else 0


def number_tokens(match: re.Match) -> list[tuple[str, float, int]]:
    """Decompose one regex match into ``(token_text, value, decimals)`` sub-numbers.

    A blood-pressure match yields *two* independent sub-numbers (systolic, diastolic);
    a clock-time match yields *none* (it is not a vital); every other match yields one.
    ``95%`` yields value ``95.0`` (displayed scale, not 0.95).
    """
    if match.group("time") is not None:
        return []

    bp = match.group("bp")
    pct = match.group("pct")
    num = match.group("num")

    if bp is not None:
        out: list[tuple[str, float, int]] = []
        for part in bp.split("/"):
            part = part.strip()
            cleaned = part.replace(",", "")
            out.append((part, float(cleaned), _decimals(cleaned)))
        return out
    if pct is not None:
        literal = pct.rstrip().rstrip("%").strip()
        cleaned = literal.replace(",", "")
        return [(pct.strip(), float(cleaned), _decimals(cleaned))]

    cleaned = num.replace(",", "")
    return [(num.strip(), float(cleaned), _decimals(cleaned))]


def iter_number_tokens(text: str) -> Iterator[tuple[re.Match, list[tuple[str, float, int]]]]:
    """Yield ``(match, sub_numbers)`` for every numeric token in *text*, left to right."""
    for match in NUMBER_RE.finditer(text):
        yield match, number_tokens(match)
