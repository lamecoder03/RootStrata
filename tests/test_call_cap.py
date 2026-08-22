"""Tests for the call cap.

Asserts that it raises at exactly its ceiling, that its counter cannot be assigned or rewound,
and that a call the validator rejects still consumes budget.
"""

from __future__ import annotations

import pytest

from guardrails.call_cap import CallCap, CallCapExceeded, DEFAULT_MAX_CALLS


def test_cap_allows_exactly_its_limit_then_raises():
    cap = CallCap(3)
    assert [cap.spend() for _ in range(3)] == [1, 2, 3]
    assert cap.remaining == 0
    with pytest.raises(CallCapExceeded):
        cap.spend()


def test_spend_returns_an_int_and_never_a_boolean():
    """spend() signals failure by raising, not by returning a falsy value."""
    cap = CallCap(2)
    for _ in range(2):
        value = cap.spend()
        assert isinstance(value, int)
        assert not isinstance(value, bool)

    with pytest.raises(CallCapExceeded):
        cap.spend()


def test_exceeding_stays_exceeded_on_every_later_attempt():
    cap = CallCap(1)
    cap.spend()
    for _ in range(3):
        with pytest.raises(CallCapExceeded):
            cap.spend()
    assert cap.used == 1  # refused attempts are not charged twice


def test_exception_carries_the_limit_and_the_refused_attempt_number():
    cap = CallCap(2)
    cap.spend()
    cap.spend()
    with pytest.raises(CallCapExceeded) as excinfo:
        cap.spend()
    assert excinfo.value.limit == 2
    assert excinfo.value.attempted == 3
    assert "2" in str(excinfo.value) and "3" in str(excinfo.value)


def test_used_counter_is_read_only():
    cap = CallCap(5)
    cap.spend()
    with pytest.raises(AttributeError):
        cap.used = 0
    with pytest.raises(AttributeError):
        cap.limit = 999
    assert cap.used == 1
    assert cap.limit == 5


def test_cap_exposes_no_way_to_rewind_itself():
    """The cap exposes no reset; a new run constructs a new CallCap."""
    for forbidden in ("reset", "clear", "refund", "set_used", "extend", "increase", "release"):
        assert not hasattr(CallCap, forbidden), f"CallCap should not expose {forbidden}()"


def test_invalid_limits_are_refused_at_construction():
    for bad in (0, -1, -100):
        with pytest.raises(ValueError):
            CallCap(bad)
    for wrong_type in (1.5, "10", None, True):
        with pytest.raises(TypeError):
            CallCap(wrong_type)


def test_default_limit_is_used_when_none_given():
    assert CallCap().limit == DEFAULT_MAX_CALLS


def test_rejected_calls_still_consume_budget(make_toolkit):
    """An agent emitting only invalid calls still reaches the ceiling and stops."""
    toolkit = make_toolkit("marketing", max_calls=4)
    for _ in range(4):
        result = toolkit.call("get_summary_stats", {"column": "region"})  # categorical: always rejected
        assert result.ok is False

    assert toolkit.cap.used == 4
    with pytest.raises(CallCapExceeded):
        toolkit.call("get_summary_stats", {"column": "ad_spend_usd"})  # a valid call, refused anyway


def test_unknown_functions_also_consume_budget(make_toolkit):
    toolkit = make_toolkit("marketing", max_calls=2)
    toolkit.call("definitely_not_a_tool", {})
    toolkit.call("also_not_a_tool", {})
    with pytest.raises(CallCapExceeded):
        toolkit.call("value_counts", {"column": "region"})
