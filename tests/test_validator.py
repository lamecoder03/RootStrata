"""Tests that the allowlist is enforced against each file's real schema rather than a fixed list.

Covers the calls that must pass, one semantically invalid call per toolkit function, structural
rejections, and the cardinality bounds that role checks alone do not catch.
"""

from __future__ import annotations

import pandas as pd
import pytest

from guardrails.validator import (
    COLUMN_TOO_WIDE,
    DUPLICATE_COLUMN,
    EMPTY_COLUMN,
    INVALID_ARGUMENT_TYPE,
    INVALID_ENUM_VALUE,
    MISSING_ARGUMENT,
    UNKNOWN_ARGUMENT,
    UNKNOWN_COLUMN,
    UNKNOWN_FUNCTION,
    WRONG_COLUMN_ROLE,
    ValidationError,
    validate_call,
)
from profiling.profiler import profile_dataframe
from toolkit.registry import MAX_GROUP_KEY_DISTINCT, tool_names


def _reject(profile, function, arguments) -> ValidationError:
    """Assert a call is refused and return the error, so a test can check the code it expects."""
    with pytest.raises(ValidationError) as excinfo:
        validate_call(profile, function, arguments)
    return excinfo.value


# --- the calls that must be allowed --------------------------------------------------------------

def test_every_tool_has_a_legitimate_call_that_passes(profiles):
    """Positive controls: a validator that rejected everything would pass the tests below."""
    marketing, training, stores = profiles["marketing"], profiles["training"], profiles["stores"]

    assert validate_call(marketing, "get_summary_stats", {"column": "ad_spend_usd"}) == {
        "column": "ad_spend_usd"
    }
    assert validate_call(
        marketing, "compute_correlation", {"col_a": "ad_spend_usd", "col_b": "conversions"}
    ) == {"col_a": "ad_spend_usd", "col_b": "conversions", "group_by": None}
    assert validate_call(
        training,
        "compute_correlation",
        {"col_a": "weekly_training_hours", "col_b": "output_points", "group_by": "role_tier"},
    )["group_by"] == "role_tier"
    assert validate_call(stores, "detect_outliers", {"column": "revenue_usd", "method": "iqr"})[
        "method"
    ] == "iqr"
    assert validate_call(
        stores, "group_compare", {"group_col": "store_id", "value_col": "revenue_usd"}
    )["group_col"] == "store_id"
    assert validate_call(training, "value_counts", {"column": "role_tier"}) == {
        "column": "role_tier"
    }


def test_optional_arguments_are_filled_from_their_declared_default(profiles):
    resolved = validate_call(profiles["stores"], "detect_outliers", {"column": "revenue_usd"})
    assert resolved == {"column": "revenue_usd", "method": "zscore"}


# --- one semantically-invalid call per toolkit function ------------------------------------------

def test_get_summary_stats_refuses_a_categorical_column(profiles):
    error = _reject(profiles["marketing"], "get_summary_stats", {"column": "region"})
    assert error.code == WRONG_COLUMN_ROLE
    assert "categorical" in str(error)
    # the message must name a usable alternative, so the agent can correct itself next turn
    assert "ad_spend_usd" in str(error)


def test_compute_correlation_refuses_two_categorical_columns(profiles):
    error = _reject(
        profiles["training"], "compute_correlation", {"col_a": "region", "col_b": "role_tier"}
    )
    assert error.code == WRONG_COLUMN_ROLE
    assert "output_points" in str(error)


def test_compute_correlation_refuses_grouping_by_an_identifier(profiles):
    error = _reject(
        profiles["training"],
        "compute_correlation",
        {"col_a": "weekly_training_hours", "col_b": "output_points", "group_by": "employee_id"},
    )
    assert error.code == WRONG_COLUMN_ROLE
    assert "identifier" in str(error)


def test_detect_outliers_refuses_a_categorical_column(profiles):
    error = _reject(profiles["training"], "detect_outliers", {"column": "role_tier"})
    assert error.code == WRONG_COLUMN_ROLE


def test_detect_outliers_refuses_an_unlisted_method(profiles):
    error = _reject(
        profiles["stores"], "detect_outliers", {"column": "revenue_usd", "method": "isolation_forest"}
    )
    assert error.code == INVALID_ENUM_VALUE
    assert "zscore" in str(error) and "iqr" in str(error)


def test_group_compare_refuses_grouping_by_an_identifier(profiles):
    error = _reject(
        profiles["training"], "group_compare", {"group_col": "employee_id", "value_col": "output_points"}
    )
    assert error.code == WRONG_COLUMN_ROLE
    assert "identifier" in str(error)


def test_group_compare_refuses_a_categorical_value_column(profiles):
    error = _reject(
        profiles["stores"], "group_compare", {"group_col": "region", "value_col": "store_id"}
    )
    assert error.code == WRONG_COLUMN_ROLE


def test_value_counts_refuses_a_column_with_too_many_distinct_values(profiles):
    """revenue_usd has the right role and is still invalid: 288 rows, 288 distinct values."""
    error = _reject(profiles["stores"], "value_counts", {"column": "revenue_usd"})
    assert error.code == COLUMN_TOO_WIDE
    assert "288" in str(error)


def test_value_counts_refuses_an_identifier_column(profiles):
    error = _reject(profiles["training"], "value_counts", {"column": "employee_id"})
    assert error.code == WRONG_COLUMN_ROLE


# --- structural rejections -----------------------------------------------------------------------

def test_a_function_outside_the_allowlist_is_refused(profiles):
    error = _reject(profiles["marketing"], "run_sql", {"query": "SELECT 1"})
    assert error.code == UNKNOWN_FUNCTION
    for name in tool_names():
        assert name in str(error)   # the refusal lists what *is* allowed


def test_a_column_that_does_not_exist_is_refused(profiles):
    error = _reject(profiles["marketing"], "get_summary_stats", {"column": "revenue_usd"})
    assert error.code == UNKNOWN_COLUMN


def test_a_near_miss_column_name_gets_a_suggestion(profiles):
    error = _reject(profiles["marketing"], "get_summary_stats", {"column": "ad_spend"})
    assert error.code == UNKNOWN_COLUMN
    assert "ad_spend_usd" in str(error)


def test_undeclared_arguments_are_refused(profiles):
    """An argument the spec never declared is a rejected call."""
    error = _reject(
        profiles["marketing"], "get_summary_stats", {"column": "ad_spend_usd", "limit": 10_000}
    )
    assert error.code == UNKNOWN_ARGUMENT
    assert "limit" in str(error)


def test_missing_required_arguments_are_refused(profiles):
    error = _reject(profiles["marketing"], "compute_correlation", {"col_a": "ad_spend_usd"})
    assert error.code == MISSING_ARGUMENT
    assert "col_b" in str(error)


def test_a_column_argument_must_be_a_string(profiles):
    error = _reject(profiles["marketing"], "get_summary_stats", {"column": ["ad_spend_usd"]})
    assert error.code == INVALID_ARGUMENT_TYPE


def test_correlating_a_column_with_itself_is_refused(profiles):
    error = _reject(
        profiles["marketing"],
        "compute_correlation",
        {"col_a": "conversions", "col_b": "conversions"},
    )
    assert error.code == DUPLICATE_COLUMN


def test_stratifying_a_column_by_itself_is_refused(profiles):
    error = _reject(
        profiles["stores"],
        "compute_correlation",
        {"col_a": "promo_flag", "col_b": "revenue_usd", "group_by": "promo_flag"},
    )
    assert error.code == DUPLICATE_COLUMN


# --- role is not enough: cardinality does real work -----------------------------------------------

def test_a_wide_categorical_is_refused_as_a_group_key():
    """200 distinct values in 260 rows is under the identifier threshold, so only the cardinality
    bound catches it."""
    df = pd.DataFrame(
        {
            "ticket_ref": [f"T-{i % 200}" for i in range(260)],
            "amount": [float(i % 97) for i in range(260)],
        }
    )
    profile = profile_dataframe(df, "wide.csv")
    assert next(c for c in profile["columns"] if c["name"] == "ticket_ref")["role"] == "categorical"

    error = _reject(profile, "group_compare", {"group_col": "ticket_ref", "value_col": "amount"})
    assert error.code == COLUMN_TOO_WIDE
    assert str(MAX_GROUP_KEY_DISTINCT) in str(error)


def test_a_low_cardinality_numeric_flag_is_accepted_as_a_group_key(profiles):
    """promo_flag is numeric, but with 2 distinct values it is a valid grouping key."""
    resolved = validate_call(
        profiles["stores"], "group_compare", {"group_col": "promo_flag", "value_col": "revenue_usd"}
    )
    assert resolved["group_col"] == "promo_flag"


def test_an_all_null_column_is_refused_with_its_own_error():
    df = pd.DataFrame({"notes": [None] * 40, "value": [float(i) for i in range(40)]})
    profile = profile_dataframe(df, "empty.csv")
    error = _reject(profile, "value_counts", {"column": "notes"})
    assert error.code == EMPTY_COLUMN


# --- the point: validation is relative to the file that is actually loaded ------------------------

def test_the_same_argument_is_valid_in_one_file_and_unknown_in_another(profiles):
    validate_call(profiles["stores"], "group_compare", {"group_col": "store_id", "value_col": "revenue_usd"})

    error = _reject(
        profiles["marketing"], "group_compare", {"group_col": "store_id", "value_col": "ad_spend_usd"}
    )
    assert error.code == UNKNOWN_COLUMN


def test_the_same_column_name_can_pass_in_one_file_and_fail_on_role_in_another():
    """The same column name passes in one file and fails on role in another."""
    numeric = profile_dataframe(
        pd.DataFrame({"score": [float(i % 50) for i in range(60)]}), "numeric.csv"
    )
    textual = profile_dataframe(
        pd.DataFrame({"score": ["low", "medium", "high"] * 20}), "textual.csv"
    )

    assert validate_call(numeric, "get_summary_stats", {"column": "score"}) == {"column": "score"}

    error = _reject(textual, "get_summary_stats", {"column": "score"})
    assert error.code == WRONG_COLUMN_ROLE


def test_validate_call_returns_arguments_and_never_a_boolean(profiles):
    """validate_call returns normalised arguments and signals failure by raising."""
    resolved = validate_call(profiles["marketing"], "value_counts", {"column": "region"})
    assert isinstance(resolved, dict)
    assert not isinstance(resolved, bool)
