"""Tests for the audit log's append-only and completeness properties.

Asserts entries are frozen, handed out as copies and never renumbered, that the JSONL file only
grows, and that rejected and capped calls appear in it alongside successful ones.
"""

from __future__ import annotations

import dataclasses
import json

import pytest

from guardrails.audit import (
    AuditEntry,
    AuditLog,
    OUTCOME_ALLOWED,
    OUTCOME_CAPPED,
    OUTCOME_ERROR,
    OUTCOME_REJECTED,
)
from guardrails.call_cap import CallCapExceeded


# --- the append-only property ------------------------------------------------------------------

def test_entries_are_handed_out_as_an_immutable_tuple():
    log = AuditLog()
    log.record("value_counts", {"column": "region"}, OUTCOME_ALLOWED)
    entries = log.entries
    assert isinstance(entries, tuple)
    with pytest.raises(AttributeError):
        entries.append("forged")           # type: ignore[attr-defined]
    assert len(log) == 1


def test_an_entry_cannot_be_edited_after_it_is_written():
    log = AuditLog()
    entry = log.record("value_counts", {"column": "region"}, OUTCOME_ALLOWED)
    for field in ("seq", "function", "outcome", "detail"):
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(entry, field, "forged")
    assert log.entries[0].outcome == OUTCOME_ALLOWED


def test_reading_arguments_yields_a_fresh_copy_each_time():
    """Arguments are stored as JSON text, so a frozen entry cannot hand out a mutable dict."""
    log = AuditLog()
    entry = log.record("get_summary_stats", {"column": "ad_spend_usd"}, OUTCOME_ALLOWED)

    borrowed = entry.arguments
    borrowed["column"] = "hacked"
    borrowed["injected"] = True

    assert entry.arguments == {"column": "ad_spend_usd"}
    assert log.entries[0].arguments == {"column": "ad_spend_usd"}


def test_sequence_numbers_are_dense_and_strictly_increasing():
    log = AuditLog()
    for i in range(6):
        log.record(f"tool_{i}", {}, OUTCOME_ALLOWED)
    assert [e.seq for e in log.entries] == [1, 2, 3, 4, 5, 6]


def test_log_exposes_no_deletion_or_rewrite_api():
    """The log exposes no method to delete, clear, reorder or overwrite an entry."""
    for forbidden in ("clear", "pop", "remove", "delete", "truncate", "rewrite", "__delitem__",
                      "__setitem__", "insert", "sort"):
        assert not hasattr(AuditLog, forbidden), f"AuditLog should not expose {forbidden}"


def test_jsonl_file_only_ever_grows(tmp_path):
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path)

    log.record("value_counts", {"column": "region"}, OUTCOME_ALLOWED)
    after_first = path.read_bytes()
    log.record("get_summary_stats", {"column": "nope"}, OUTCOME_REJECTED, detail="unknown column")
    after_second = path.read_bytes()

    assert after_second.startswith(after_first), "existing bytes were rewritten, not appended to"
    assert len(after_second) > len(after_first)


def test_a_new_log_on_the_same_path_appends_rather_than_truncating(tmp_path):
    """The file is opened in append mode, so a second run does not erase the first."""
    path = tmp_path / "audit.jsonl"
    AuditLog(path).record("run_one", {}, OUTCOME_ALLOWED)
    first_run = path.read_bytes()

    AuditLog(path).record("run_two", {}, OUTCOME_ALLOWED)
    both_runs = path.read_bytes()

    assert both_runs.startswith(first_run)
    assert path.read_text(encoding="utf-8").count("\n") == 2


def test_every_written_line_is_valid_json(tmp_path):
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path)
    log.record("compute_correlation", {"col_a": "a", "col_b": "b"}, OUTCOME_ALLOWED, duration_ms=1.234)
    log.record("group_compare", {"group_col": "id"}, OUTCOME_REJECTED, detail="identifier column")

    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    parsed = [json.loads(line) for line in lines]
    assert [p["seq"] for p in parsed] == [1, 2]
    assert parsed[1]["outcome"] == OUTCOME_REJECTED
    assert parsed[0]["arguments"] == {"col_a": "a", "col_b": "b"}


def test_unserialisable_arguments_are_recorded_rather_than_crashing_the_log():
    """json.dumps falls back to str() rather than raising and losing the entry."""
    log = AuditLog()
    entry = log.record("group_compare", {"group_col": object()}, OUTCOME_REJECTED)
    assert isinstance(entry.arguments["group_col"], str)
    assert len(log) == 1


def test_unknown_outcomes_are_refused():
    log = AuditLog()
    with pytest.raises(ValueError):
        log.record("value_counts", {}, "sort_of_allowed")
    assert len(log) == 0


def test_long_detail_is_truncated_and_marked():
    log = AuditLog()
    entry = log.record("x", {}, OUTCOME_ERROR, detail="E" * 5000)
    assert len(entry.detail) < 5000
    assert entry.detail.endswith("...")


# --- completeness: refusals must show up too ----------------------------------------------------

def test_allowed_rejected_and_capped_all_reach_the_log(make_toolkit):
    toolkit = make_toolkit("marketing", max_calls=3)

    toolkit.call("get_summary_stats", {"column": "ad_spend_usd"})   # allowed
    toolkit.call("get_summary_stats", {"column": "region"})         # rejected: categorical
    toolkit.call("not_a_real_tool", {})                             # rejected: unknown function
    with pytest.raises(CallCapExceeded):
        toolkit.call("value_counts", {"column": "region"})          # capped

    counts = toolkit.audit.counts_by_outcome()
    assert counts[OUTCOME_ALLOWED] == 1
    assert counts[OUTCOME_REJECTED] == 2
    assert counts[OUTCOME_CAPPED] == 1
    assert len(toolkit.audit) == 4


def test_the_capped_call_records_what_the_agent_wanted_to_do(make_toolkit):
    """A capped call is recorded with the arguments it was attempted with."""
    toolkit = make_toolkit("marketing", max_calls=1)
    toolkit.call("value_counts", {"column": "region"})
    with pytest.raises(CallCapExceeded):
        toolkit.call("detect_outliers", {"column": "ad_spend_usd", "method": "iqr"})

    last = toolkit.audit.entries[-1]
    assert last.outcome == OUTCOME_CAPPED
    assert last.function == "detect_outliers"
    assert last.arguments == {"column": "ad_spend_usd", "method": "iqr"}


def test_rejected_entries_keep_the_raw_attempt_not_a_cleaned_version(make_toolkit):
    toolkit = make_toolkit("training")
    toolkit.call("group_compare", {"group_col": "employee_id", "value_col": "output_points"})

    entry = toolkit.audit.entries[-1]
    assert entry.outcome == OUTCOME_REJECTED
    assert entry.arguments["group_col"] == "employee_id"
    assert "identifier" in entry.detail


def test_format_table_reports_every_outcome_bucket(make_toolkit):
    toolkit = make_toolkit("marketing")
    toolkit.call("value_counts", {"column": "region"})
    toolkit.call("value_counts", {"column": "week_start_typo"})

    table = toolkit.audit.format_table()
    assert "allowed=1" in table
    assert "rejected=1" in table
    assert "total=2" in table


def test_entry_round_trips_through_json():
    entry = AuditEntry(
        seq=1, timestamp="2026-01-01T00:00:00+00:00", function="value_counts",
        arguments_json='{"column": "region"}', outcome=OUTCOME_ALLOWED,
    )
    restored = json.loads(entry.to_json())
    assert restored["function"] == "value_counts"
    assert restored["arguments"] == {"column": "region"}
