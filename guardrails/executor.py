"""The single entry point for tool calls: charge the cap, validate, run, log.

CallCapExceeded propagates to the caller and ends the run. A validation failure comes back as a
ToolResult carrying the reason.
"""

from __future__ import annotations

import time
from pathlib import Path
from dataclasses import dataclass
from typing import Any

import pandas as pd

from guardrails.audit import (
    AuditLog,
    OUTCOME_ALLOWED,
    OUTCOME_CAPPED,
    OUTCOME_ERROR,
    OUTCOME_REJECTED,
)
from guardrails.call_cap import CallCap, CallCapExceeded, DEFAULT_MAX_CALLS
from guardrails.validator import ValidationError, validate_call
from profiling.profiler import load_csv, profile_dataframe
from toolkit.registry import get_tool

EXECUTION_ERROR = "EXECUTION_ERROR"
DUPLICATE_CALL = "DUPLICATE_CALL"


def call_signature(function_name: str, arguments: dict[str, Any] | None = None) -> str:
    """The canonical identity of a call: its name and arguments, order-independent.

    Unset optional arguments are dropped, so `compute_correlation(a, b)` and the same call with an
    explicit `group_by=None` produce one signature. Uses the validator's resolved arguments where
    available.
    """
    rendered = ", ".join(
        f"{key}={value!r}" for key, value in sorted((arguments or {}).items()) if value is not None
    )
    return f"{function_name}({rendered})"


@dataclass(frozen=True)
class ToolResult:
    """What a call produced: either data or the reason it was refused, never both."""

    ok: bool
    function: str
    data: dict[str, Any] | None = None
    error: str | None = None
    error_code: str | None = None
    # The validator's normalised arguments and the canonical signature built from them, so the
    # caller records what ran without re-deriving it.
    resolved: dict[str, Any] | None = None
    signature: str = ""


class GuardedToolkit:
    """Runs allowlisted analysis functions against one loaded CSV, under cap, validation and audit.

    Call order: the cap is charged first, so rejected calls still consume budget; then validation,
    so a rejected call never reaches pandas; then the duplicate check, which needs the validator's
    normalised arguments. Every outcome, including refusals, is written to the audit log.

    A call whose signature matches an already-completed call is refused with the result it already
    produced.
    """

    def __init__(
        self,
        df: pd.DataFrame,
        profile: dict[str, Any],
        call_cap: CallCap | None = None,
        audit_log: AuditLog | None = None,
        max_calls: int = DEFAULT_MAX_CALLS,
    ) -> None:
        self._df = df
        self._profile = profile
        self._cap = call_cap if call_cap is not None else CallCap(max_calls)
        self._audit = audit_log if audit_log is not None else AuditLog()
        # signature -> headline of the result it produced. Only successful calls are recorded: a
        # repeated invalid call must keep getting its own rejection, which names the columns that
        # would have worked.
        self._completed: dict[str, str] = {}

    @classmethod
    def from_csv(
        cls,
        path: str | Path,
        call_cap: CallCap | None = None,
        audit_log: AuditLog | None = None,
        max_calls: int = DEFAULT_MAX_CALLS,
    ) -> "GuardedToolkit":
        """Load and profile a CSV, then wrap it. The profile is the schema validation runs against."""
        csv_path = Path(path)
        df = load_csv(csv_path)
        profile = profile_dataframe(df, name=csv_path.name)
        return cls(df, profile, call_cap=call_cap, audit_log=audit_log, max_calls=max_calls)

    @property
    def profile(self) -> dict[str, Any]:
        return self._profile

    @property
    def audit(self) -> AuditLog:
        return self._audit

    @property
    def cap(self) -> CallCap:
        return self._cap

    def call(self, function_name: str, arguments: dict[str, Any] | None = None) -> ToolResult:
        """Attempt one tool call. Raises CallCapExceeded; returns a failed ToolResult otherwise."""
        attempted = dict(arguments or {})

        try:
            self._cap.spend()
        except CallCapExceeded as exc:
            # Log before re-raising, so the capped call still appears in the record.
            self._audit.record(function_name, attempted, OUTCOME_CAPPED, detail=str(exc))
            raise

        try:
            resolved = validate_call(self._profile, function_name, attempted)
        except ValidationError as exc:
            self._audit.record(function_name, attempted, OUTCOME_REJECTED, detail=str(exc))
            return ToolResult(ok=False, function=function_name, error=str(exc), error_code=exc.code,
                              signature=call_signature(function_name, attempted))

        signature = call_signature(function_name, resolved)
        if signature in self._completed:
            message = (
                f"This exact call has already run in this session and returned: "
                f"{self._completed[signature]}. Running it again cannot produce a different answer — "
                f"the data does not change between calls — so it is refused rather than executed. "
                f"Use the result you already have, or ask a different question: change an argument, "
                f"add a group_by, or call one of the other functions."
            )
            self._audit.record(function_name, resolved, OUTCOME_REJECTED, detail=message)
            return ToolResult(ok=False, function=function_name, error=message,
                              error_code=DUPLICATE_CALL, resolved=resolved, signature=signature)

        tool = get_tool(function_name)
        started = time.perf_counter()
        try:
            data = tool.fn(self._df, **resolved)
        except Exception as exc:  # a toolkit bug must not take the whole run down silently
            elapsed = (time.perf_counter() - started) * 1000
            detail = f"{type(exc).__name__}: {exc}"
            self._audit.record(function_name, resolved, OUTCOME_ERROR, detail=detail,
                               duration_ms=elapsed)
            # Not remembered: a crashed call produced no answer to reuse, and a fixed toolkit bug
            # should not leave the call permanently refused.
            return ToolResult(ok=False, function=function_name, error=detail,
                              error_code=EXECUTION_ERROR, resolved=resolved, signature=signature)

        elapsed = (time.perf_counter() - started) * 1000
        summary = _summarise(data)
        self._audit.record(function_name, resolved, OUTCOME_ALLOWED, duration_ms=elapsed,
                           result_summary=summary)
        self._completed[signature] = summary
        return ToolResult(ok=True, function=function_name, data=data, resolved=resolved,
                          signature=signature)


def _summarise(data: dict[str, Any]) -> str:
    """A short, fixed-size note about a result, for the audit log."""
    if not isinstance(data, dict):
        return type(data).__name__
    headline = []
    # Both stratification flags are reported: a reversal and an attenuation are different results.
    for key in ("n", "n_rows", "n_outliers", "n_groups_total", "n_distinct",
                "sign_reversal", "attenuated"):
        if key in data:
            headline.append(f"{key}={data[key]}")
    if "overall" in data and isinstance(data["overall"], dict):
        headline.append(f"pearson_r={data['overall'].get('pearson_r')}")
    return ", ".join(headline) if headline else f"{len(data)} fields"
