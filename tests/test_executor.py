"""Tests that the three guardrails compose in the right order.

Asserts the cap is charged before validation, that a rejected call never reaches pandas, that a
crash is contained and logged, and that a completed call is refused rather than re-run.
"""

from __future__ import annotations

import pytest

from guardrails.audit import OUTCOME_ALLOWED, OUTCOME_ERROR, OUTCOME_REJECTED
from guardrails.call_cap import CallCapExceeded
from guardrails.executor import EXECUTION_ERROR, GuardedToolkit, ToolResult
from guardrails.validator import UNKNOWN_FUNCTION, WRONG_COLUMN_ROLE
from toolkit.registry import ToolSpec


def test_a_valid_call_returns_data_and_is_logged_as_allowed(make_toolkit):
    toolkit = make_toolkit("marketing")
    result = toolkit.call("compute_correlation", {"col_a": "ad_spend_usd", "col_b": "conversions"})

    assert isinstance(result, ToolResult)
    assert result.ok is True
    assert result.error is None
    assert result.data["overall"]["pearson_r"] == pytest.approx(0.800, abs=0.005)

    entry = toolkit.audit.entries[-1]
    assert entry.outcome == OUTCOME_ALLOWED
    assert entry.duration_ms is not None
    assert "pearson_r" in entry.result_summary


def test_a_rejected_call_returns_the_reason_instead_of_raising(make_toolkit):
    """A validation failure returns a ToolResult carrying the reason rather than raising."""
    toolkit = make_toolkit("training")
    result = toolkit.call("group_compare", {"group_col": "employee_id", "value_col": "output_points"})

    assert result.ok is False
    assert result.data is None
    assert result.error_code == WRONG_COLUMN_ROLE
    assert "identifier" in result.error


def test_an_unknown_function_never_reaches_the_toolkit(make_toolkit):
    toolkit = make_toolkit("marketing")
    result = toolkit.call("exec_python", {"code": "import os; os.system('rm -rf /')"})

    assert result.ok is False
    assert result.error_code == UNKNOWN_FUNCTION
    assert toolkit.audit.entries[-1].outcome == OUTCOME_REJECTED


def test_the_cap_is_charged_before_validation(make_toolkit):
    """The cap is charged first, so a rejected call still consumes budget."""
    toolkit = make_toolkit("marketing", max_calls=2)
    toolkit.call("nonsense_tool", {})
    assert toolkit.cap.used == 1
    assert toolkit.cap.remaining == 1


def test_the_cap_raises_through_the_executor_to_the_caller(make_toolkit):
    toolkit = make_toolkit("marketing", max_calls=1)
    toolkit.call("value_counts", {"column": "region"})

    with pytest.raises(CallCapExceeded):
        toolkit.call("value_counts", {"column": "region"})


def test_a_crash_inside_a_tool_is_contained_and_logged(make_toolkit, monkeypatch):
    """A toolkit exception ends one call rather than the run, and is still logged."""
    toolkit = make_toolkit("marketing")

    def exploding(df, column):
        raise RuntimeError("pandas said no")

    broken = ToolSpec(
        name="get_summary_stats",
        fn=exploding,
        description="stand-in that always raises",
        params=toolkit_spec_params(),
    )
    monkeypatch.setattr("guardrails.executor.get_tool", lambda name: broken)

    result = toolkit.call("get_summary_stats", {"column": "ad_spend_usd"})

    assert result.ok is False
    assert result.error_code == EXECUTION_ERROR
    assert "pandas said no" in result.error

    entry = toolkit.audit.entries[-1]
    assert entry.outcome == OUTCOME_ERROR
    assert "RuntimeError" in entry.detail


def toolkit_spec_params():
    """Reuse the real parameter spec, so the stub tool is validated like a genuine one."""
    from toolkit.registry import get_tool

    return get_tool("get_summary_stats").params


def test_every_call_path_writes_exactly_one_audit_entry(make_toolkit):
    toolkit = make_toolkit("stores", max_calls=4)

    toolkit.call("group_compare", {"group_col": "store_id", "value_col": "revenue_usd"})  # allowed
    toolkit.call("value_counts", {"column": "revenue_usd"})                                # too wide
    toolkit.call("no_such_tool", {})                                                       # unknown
    with pytest.raises(CallCapExceeded):
        toolkit.call("get_summary_stats", {"column": "revenue_usd"})
        toolkit.call("get_summary_stats", {"column": "revenue_usd"})

    assert len(toolkit.audit) == toolkit.cap.used + 1  # +1: the capped attempt is logged, not charged
    assert [e.seq for e in toolkit.audit.entries] == [1, 2, 3, 4, 5]


def test_from_csv_profiles_the_file_it_loads(tmp_path):
    csv = tmp_path / "tiny.csv"
    csv.write_text("label,amount\na,1\nb,2\na,3\n", encoding="utf-8")

    toolkit = GuardedToolkit.from_csv(csv)
    assert toolkit.profile["source"] == "tiny.csv"
    assert toolkit.profile["row_count"] == 3

    result = toolkit.call("value_counts", {"column": "label"})
    assert result.ok is True
    assert result.data["n_distinct"] == 2


def test_the_audit_log_can_be_mirrored_to_disk(make_toolkit, tmp_path):
    path = tmp_path / "run.jsonl"
    toolkit = make_toolkit("marketing", audit_path=path)

    toolkit.call("value_counts", {"column": "region"})
    toolkit.call("value_counts", {"column": "does_not_exist"})

    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    assert '"outcome": "allowed"' in lines[0]
    assert '"outcome": "rejected"' in lines[1]


# --- the audit summary must carry both stratification flags -------------------------------------

def test_the_audit_summary_records_both_stratification_flags(make_toolkit):
    """The audit summary records both sign_reversal and attenuated, which are different results."""
    toolkit = make_toolkit("training")

    reversed_call = toolkit.call("compute_correlation", {
        "col_a": "weekly_training_hours", "col_b": "output_points", "group_by": "role_tier"})
    assert reversed_call.data["sign_reversal"] is True
    summary = toolkit.audit.entries[-1].result_summary
    assert "sign_reversal=True" in summary
    assert "attenuated=False" in summary

    attenuated_call = toolkit.call("compute_correlation", {
        "col_a": "tenure_months", "col_b": "output_points", "group_by": "role_tier"})
    assert attenuated_call.data["attenuated"] is True
    summary = toolkit.audit.entries[-1].result_summary
    assert "attenuated=True" in summary
    assert "sign_reversal=False" in summary


def test_an_unstratified_correlation_logs_neither_flag(make_toolkit):
    """The flags exist only when group_by was passed; the summary does not invent them."""
    toolkit = make_toolkit("training")
    toolkit.call("compute_correlation",
                 {"col_a": "weekly_training_hours", "col_b": "output_points"})

    summary = toolkit.audit.entries[-1].result_summary
    assert "sign_reversal" not in summary
    assert "attenuated" not in summary
    assert "pearson_r" in summary


# --- a completed call is refused, not re-run -----------------------------------------------------

def test_an_identical_completed_call_is_refused_before_it_reaches_pandas(make_toolkit):
    """A call whose signature matches a completed call is refused before it reaches pandas."""
    from guardrails.executor import DUPLICATE_CALL

    kit = make_toolkit("training", max_calls=10)
    arguments = {"col_a": "weekly_training_hours", "col_b": "output_points"}

    first = kit.call("compute_correlation", arguments)
    second = kit.call("compute_correlation", arguments)

    assert first.ok is True
    assert second.ok is False
    assert second.error_code == DUPLICATE_CALL
    assert second.data is None                       # it never ran
    assert "already run" in second.error


def test_the_refusal_hands_back_the_answer_it_already_has(make_toolkit):
    """The duplicate refusal quotes the result the earlier call already produced."""
    kit = make_toolkit("training", max_calls=10)
    kit.call("get_summary_stats", {"column": "output_points"})

    repeat = kit.call("get_summary_stats", {"column": "output_points"})
    assert "n=" in repeat.error or "n_rows=" in repeat.error
    assert "Use the result you already have" in repeat.error


def test_a_duplicate_still_costs_a_call(make_toolkit):
    """A duplicate is charged a call, consistent with rejected calls consuming budget."""
    kit = make_toolkit("training", max_calls=10)
    kit.call("get_summary_stats", {"column": "output_points"})
    kit.call("get_summary_stats", {"column": "output_points"})

    assert kit.cap.used == 2
    assert kit.audit.counts_by_outcome()["allowed"] == 1
    assert kit.audit.counts_by_outcome()["rejected"] == 1


def test_two_spellings_of_one_call_are_the_same_call(make_toolkit):
    """The signature comes from the validator's normalised arguments, so an explicitly-null
    optional argument and an omitted one are one call."""
    kit = make_toolkit("training", max_calls=10)

    first = kit.call("compute_correlation",
                     {"col_a": "tenure_months", "col_b": "output_points"})
    second = kit.call("compute_correlation",
                      {"col_a": "tenure_months", "col_b": "output_points", "group_by": None})

    assert first.ok is True
    assert second.ok is False
    assert first.signature == second.signature


def test_a_different_grouping_is_a_different_call(make_toolkit):
    """Adding group_by is a different question, so it is not blocked as a duplicate."""
    kit = make_toolkit("training", max_calls=10)
    pair = {"col_a": "weekly_training_hours", "col_b": "output_points"}

    assert kit.call("compute_correlation", pair).ok is True
    assert kit.call("compute_correlation", {**pair, "group_by": "role_tier"}).ok is True
    assert kit.call("compute_correlation", {**pair, "group_by": "region"}).ok is True


def test_a_repeated_invalid_call_keeps_its_own_rejection(make_toolkit):
    """A rejected call never completed, so it is not a duplicate and keeps its own error, which
    names the columns that would have worked."""
    kit = make_toolkit("training", max_calls=10)
    bad = {"group_col": "employee_id", "value_col": "output_points"}

    first = kit.call("group_compare", bad)
    second = kit.call("group_compare", bad)

    assert first.error_code == second.error_code == "WRONG_COLUMN_ROLE"
    assert "role_tier" in second.error          # still the useful message, not "duplicate"


def test_the_duplicate_refusal_is_written_to_the_audit_log(make_toolkit):
    """The duplicate refusal is written to the audit log, like every other attempt."""
    kit = make_toolkit("training", max_calls=10)
    kit.call("value_counts", {"column": "role_tier"})
    kit.call("value_counts", {"column": "role_tier"})

    entries = kit.audit.entries
    assert len(entries) == 2
    assert entries[0].outcome == "allowed"
    assert entries[1].outcome == "rejected"
    assert "already run" in entries[1].detail
