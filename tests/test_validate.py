"""Tests for integrity.validate_readings — physiological-range validation."""

from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

import integrity
from integrity import Severity, ValidationError, validate_readings

NOW = datetime(2026, 6, 28, 12, 0, 0, tzinfo=timezone.utc)


def _row(subject="p1", metric="heart_rate", value=72, measured_at=NOW):
    return {"subject": subject, "metric": metric, "value": value, "measured_at": measured_at}


def _df(rows):
    return pd.DataFrame(rows)


# --- the REQUIRED case: out-of-range rejected ---------------------------------------
def test_required_out_of_range_rejected_inspect():
    df = _df([_row(value=72), _row(value=999)])
    rep = validate_readings(df, raise_on_error=False)
    assert rep.n_in == 2 and rep.n_clean == 1 and rep.n_rejected == 1
    err = rep.errors[0]
    assert err.severity is Severity.OUT_OF_RANGE
    assert err.metric == "heart_rate"
    assert 999.0 not in rep.clean["value"].tolist()


def test_required_out_of_range_raises_by_default():
    df = _df([_row(value=999)])
    with pytest.raises(ValidationError) as exc:
        validate_readings(df)
    assert exc.value.report.n_rejected == 1


# --- inclusive boundaries ------------------------------------------------------------
def test_inclusive_low_high_boundaries_pass():
    # distinct measured_at so none collide on the (subject, metric, measured_at) dedup key
    df = _df([
        _row(metric="heart_rate", value=20, measured_at=NOW),
        _row(metric="heart_rate", value=250, measured_at=NOW - timedelta(minutes=1)),
        _row(metric="temperature_c", value=25.0, measured_at=NOW),
        _row(metric="temperature_c", value=45.0, measured_at=NOW - timedelta(minutes=1)),
    ])
    rep = validate_readings(df, raise_on_error=False)
    assert rep.n_clean == 4 and rep.n_rejected == 0


def test_just_outside_boundary_rejected():
    df = _df([_row(metric="temperature_c", value=24.9)])
    rep = validate_readings(df, raise_on_error=False)
    assert rep.errors[0].severity is Severity.OUT_OF_RANGE


# --- value problems: missing / non-numeric / infinite --------------------------------
def test_null_value_is_missing():
    rep = validate_readings(_df([_row(value=None)]), raise_on_error=False)
    assert rep.errors[0].severity is Severity.MISSING


def test_non_numeric_value():
    rep = validate_readings(_df([_row(value="abc")]), raise_on_error=False)
    assert rep.errors[0].severity is Severity.NON_NUMERIC


def test_infinite_value_is_its_own_severity():
    rep = validate_readings(_df([_row(metric="glucose_mgdl", value=np.inf),
                                 _row(metric="glucose_mgdl", value=-np.inf)]),
                            raise_on_error=False)
    assert {e.severity for e in rep.errors} == {Severity.INFINITE}


# --- metric / subject ----------------------------------------------------------------
def test_unknown_metric():
    rep = validate_readings(_df([_row(metric="moon_phase", value=1)]), raise_on_error=False)
    assert rep.errors[0].severity is Severity.UNKNOWN_METRIC


def test_null_subject():
    rep = validate_readings(_df([_row(subject=None)]), raise_on_error=False)
    assert rep.errors[0].severity is Severity.MISSING


# --- duplicates ----------------------------------------------------------------------
def test_duplicate_first_kept_later_flagged():
    df = _df([_row(value=72), _row(value=80)])  # same subject/metric/measured_at
    rep = validate_readings(df, raise_on_error=False)
    assert rep.n_clean == 1
    assert rep.clean["value"].tolist() == [72.0]      # first wins
    assert rep.errors[0].severity is Severity.DUPLICATE


def test_duplicates_kept_when_disabled():
    df = _df([_row(value=72), _row(value=80)])
    rep = validate_readings(df, raise_on_error=False, drop_duplicates=False)
    assert rep.n_clean == 2 and rep.n_rejected == 0


# --- timestamps ----------------------------------------------------------------------
def test_naive_measured_at_is_bad_timestamp():
    rep = validate_readings(_df([_row(measured_at=datetime(2026, 6, 28, 12, 0, 0))]),
                            raise_on_error=False)
    assert rep.errors[0].severity is Severity.BAD_TIMESTAMP


def test_unparseable_measured_at():
    rep = validate_readings(_df([_row(measured_at="not-a-time")]), raise_on_error=False)
    assert rep.errors[0].severity is Severity.BAD_TIMESTAMP


# --- structural ----------------------------------------------------------------------
def test_missing_column_always_raises():
    df = pd.DataFrame({"subject": ["p1"], "metric": ["heart_rate"], "value": [72]})
    with pytest.raises(ValidationError):
        validate_readings(df, raise_on_error=False)  # structural error ignores the flag


# --- no mutation / dtype / index -----------------------------------------------------
def test_input_not_mutated():
    df = _df([_row(value=72), _row(value=999)])
    snapshot = df.copy(deep=True)
    validate_readings(df, raise_on_error=False)
    assert df.equals(snapshot)


def test_clean_index_reset_and_float_dtype():
    df = _df([_row(value=72), _row(value=999), _row(subject="p2", value=80)])
    rep = validate_readings(df, raise_on_error=False)
    assert list(rep.clean.index) == list(range(rep.n_clean))
    assert rep.clean["value"].dtype == np.float64


# --- idempotency + tripwire ----------------------------------------------------------
def test_idempotent_on_clean():
    df = _df([_row(value=72), _row(value=999), _row(subject="p2", value=80)])
    rep = validate_readings(df, raise_on_error=False)
    again = validate_readings(rep.clean, raise_on_error=False)
    assert again.n_rejected == 0


def test_assert_validated_roundtrip():
    df = _df([_row(value=72)])
    rep = validate_readings(df, raise_on_error=False)
    assert integrity.assert_validated(rep.clean) is rep.clean
    with pytest.raises(integrity.IntegrityError):
        integrity.assert_validated(df)


def test_all_errors_reported_in_one_pass():
    df = _df([_row(value=999), _row(metric="moon_phase", value=1),
              _row(value="abc"), _row(subject=None)])
    rep = validate_readings(df, raise_on_error=False)
    assert rep.n_rejected == 4
    assert {e.severity for e in rep.errors} == {
        Severity.OUT_OF_RANGE, Severity.UNKNOWN_METRIC,
        Severity.NON_NUMERIC, Severity.MISSING,
    }


def test_non_unique_index_does_not_drop_siblings():
    # a rejected row must not take out other rows that happen to share its index label
    df = pd.DataFrame(
        [_row(value=72), _row(value=999, measured_at=NOW - timedelta(minutes=1)),
         _row(subject="p2", value=80)],
        index=[0, 0, 1],
    )
    rep = validate_readings(df, raise_on_error=False)
    assert rep.n_clean == 2
    assert sorted(rep.clean["value"].tolist()) == [72.0, 80.0]
    assert rep.errors[0].severity is Severity.OUT_OF_RANGE
    assert rep.errors[0].row_index == 0  # original label preserved in the error


def test_non_default_index_preserved_in_errors():
    df = pd.DataFrame([_row(value=999)], index=["row-A"])
    rep = validate_readings(df, raise_on_error=False)
    assert rep.errors[0].row_index == "row-A"


def test_extra_columns_preserved():
    df = _df([{**_row(value=72), "unit": "bpm", "device": "x"}])
    rep = validate_readings(df, raise_on_error=False)
    assert "device" in rep.clean.columns and rep.clean["device"].tolist() == ["x"]
