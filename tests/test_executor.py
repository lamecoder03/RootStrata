"""
test_executor.py — proves the three guardrails actually compose, and in the right order.
Exists because each guard passing on its own says nothing about the door they are bolted to: these
tests assert the cap is charged before validation, that a rejected call never reaches pandas, that a
crash inside a tool is contained and logged, and that no call path skips the audit log.
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
    """Validation failure is recoverable, so it comes back as data the agent can read and retry from."""
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
    """Ordering matters: if rejection were free, an agent emitting garbage would loop forever."""
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
    """A toolkit bug should end one call, not the run — and it must still leave a record."""
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
    """Reuse the real parameter spec so the stub above is validated exactly like the genuine tool."""
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
