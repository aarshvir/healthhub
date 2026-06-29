"""Tests for integrity.sanitize_narration — the narration grounding guard."""

import math

import pytest
from hypothesis import given, strategies as st

import integrity
from integrity import NarrationIntegrityError, sanitize_narration


# --- the REQUIRED case: an invented number must be blocked --------------------------
def test_required_invented_number_is_blocked():
    with pytest.raises(NarrationIntegrityError) as exc:
        sanitize_narration("HR was 72, BP 120/80, but glucose 999", [72, 120, 80])
    assert exc.value.token == "999"
    assert exc.value.value == 999.0
    assert exc.value.closest in (120.0, 80.0, 72.0)


def test_grounded_text_returned_unchanged():
    text = "HR was 72, BP 120/80, temp 98.6"
    assert sanitize_narration(text, [72, 120, 80, 98.61]) == text


# --- rounding tolerance is keyed to the TOKEN's precision ----------------------------
def test_honest_display_rounding_allowed():
    # |98.6 - 98.61| = 0.01 <= half-ULP(0.05)
    assert sanitize_narration("Temp 98.6F", [98.61]) == "Temp 98.6F"


def test_off_by_rounding_blocked():
    # |98.7 - 98.61| = 0.09 > 0.05
    with pytest.raises(NarrationIntegrityError):
        sanitize_narration("Temp 98.7F", [98.61])


def test_extra_digit_leak_blocked():
    # token 98.64 has d=2 -> band +-0.005; |98.64-98.6|=0.04 > 0.005 (token-keyed, not max-keyed)
    with pytest.raises(NarrationIntegrityError) as exc:
        sanitize_narration("reads 98.64", [98.6])
    assert exc.value.token == "98.64"


def test_distinct_precision_zero_vs_one_decimal():
    assert sanitize_narration("95.0", [95]) == "95.0"          # |95.0-95|=0 within 0.05
    with pytest.raises(NarrationIntegrityError):
        sanitize_narration("95.4", [95])                        # 0.4 > 0.05


def test_abs_tolerance_widens_band():
    with pytest.raises(NarrationIntegrityError):
        sanitize_narration("70.6", [70])
    assert sanitize_narration("70.6", [70], abs_tolerance=0.6) == "70.6"


# --- thousands separators ------------------------------------------------------------
def test_thousands_separator_grounded():
    assert sanitize_narration("count 1,234", [1234]) == "count 1,234"


def test_thousands_separator_off_by_one_blocked():
    with pytest.raises(NarrationIntegrityError):
        sanitize_narration("count 1,235", [1234])


def test_malformed_thousands_splits_into_two_tokens():
    # "1,23" -> tokens 1 and 23, both must be grounded; never merged into 123
    with pytest.raises(NarrationIntegrityError) as exc:
        sanitize_narration("1,23", [123])
    assert exc.value.token == "1"
    assert sanitize_narration("1,23", [1, 23]) == "1,23"


# --- percentages ---------------------------------------------------------------------
def test_percent_uses_displayed_scale():
    assert sanitize_narration("SpO2 95%", [95]) == "SpO2 95%"   # 95%, not 0.95
    with pytest.raises(NarrationIntegrityError):
        sanitize_narration("SpO2 94%", [95])


def test_percent_with_space():
    assert sanitize_narration("SpO2 95 %", [95]) == "SpO2 95 %"


# --- blood pressure: both halves validated independently -----------------------------
def test_bp_both_halves_grounded():
    assert sanitize_narration("BP 120/80", [120, 80]) == "BP 120/80"


def test_bp_bad_half_blocks_whole_token():
    with pytest.raises(NarrationIntegrityError) as exc:
        sanitize_narration("BP 120/80", [120])
    assert exc.value.token == "80"


# --- empty / trivial -----------------------------------------------------------------
def test_empty_allowed_blocks_any_number():
    with pytest.raises(NarrationIntegrityError):
        sanitize_narration("value 5", [])


def test_no_numbers_returned_unchanged():
    assert sanitize_narration("All vitals nominal.", []) == "All vitals nominal."


def test_empty_text():
    assert sanitize_narration("", []) == ""


# --- allowed_numbers normalization ---------------------------------------------------
def test_non_finite_allowed_rejected():
    with pytest.raises(ValueError):
        sanitize_narration("5", [float("nan")])
    with pytest.raises(ValueError):
        sanitize_narration("5", [float("inf")])


def test_mapping_form_uses_values():
    assert sanitize_narration("120/80", {"sys": 120, "dia": 80}) == "120/80"


def test_bad_abs_tolerance_rejected():
    with pytest.raises(ValueError):
        sanitize_narration("5", [5], abs_tolerance=-1.0)


def test_non_str_text_rejected():
    with pytest.raises(integrity.IntegrityError):
        sanitize_narration(123, [123])


# --- redaction -----------------------------------------------------------------------
def test_redact_replaces_offending_tokens():
    out = sanitize_narration("HR 72 and glucose 999", [72], redact=True)
    assert out == "HR 72 and glucose [UNVERIFIED]"


def test_redact_is_idempotent():
    once = sanitize_narration("glucose 999 and 888", [72], redact=True)
    twice = sanitize_narration(once, [72], redact=True)
    assert once == twice


def test_redact_grounded_unchanged():
    assert sanitize_narration("HR 72", [72], redact=True) == "HR 72"


# --- dates ---------------------------------------------------------------------------
def test_dates_strict_by_default():
    with pytest.raises(NarrationIntegrityError) as exc:
        sanitize_narration("on 2026 BP 120", [120])
    assert exc.value.token == "2026"


def test_allow_dates_skips_year_but_still_guards_vitals():
    assert sanitize_narration("on 2026 BP 120", [120], allow_dates=True) == "on 2026 BP 120"
    # a vitals-slot fabrication that is not date-shaped is still blocked
    with pytest.raises(NarrationIntegrityError):
        sanitize_narration("on 2026 glucose 250", [120], allow_dates=True)


# --- identifier / version safety -----------------------------------------------------
def test_identifiers_and_versions_produce_no_phantom_tokens():
    # digits embedded in identifiers / dotted versions are not tokenized
    for s in ["v1.2.3", "COVID19", "SpO2 reading", "section 4.2.1"]:
        assert sanitize_narration(s, []) == s


def test_clock_time_not_tokenized():
    # colon-separated clock times are not numeric tokens at all
    assert sanitize_narration("at 14:30 the patient rested", []) == "at 14:30 the patient rested"


# --- property: rounding band boundary is monotone ------------------------------------
@given(
    a=st.floats(min_value=1.0, max_value=500.0, allow_nan=False, allow_infinity=False),
    d=st.integers(min_value=0, max_value=3),
)
def test_property_rounding_band(a, d):
    half = 0.5 * 10 ** (-d)
    inside = round(a, d)                     # nearest value at d decimals -> within half-ULP
    token_in = f"{inside:.{d}f}"
    # a token printed at d decimals equal to round(a,d) is within the band -> accepted
    assert sanitize_narration(token_in, [a]) == token_in
    # a value well outside the band is rejected
    outside = a + 3 * half + 1.0
    token_out = f"{outside:.{d}f}"
    if abs(float(token_out) - a) > half + 1e-9:
        with pytest.raises(NarrationIntegrityError):
            sanitize_narration(token_out, [a])
