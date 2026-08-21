"""
executor.py — the single door every tool call passes through: charge the cap, validate, run, log.
Exists because guardrails are only worth as much as the guarantee that nothing bypasses them, and one
enforcement point is far easier to audit than three call sites. The cap raises through to the caller
(the run is over); a validation failure comes back as a ToolResult carrying the reason, to retry from.
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


@dataclass(frozen=True)
class ToolResult:
    """What a call produced: either data, or the reason it was refused — never both."""

    ok: bool
    function: str
    data: dict[str, Any] | None = None
    error: str | None = None
    error_code: str | None = None


class GuardedToolkit:
    """Runs allowlisted analysis functions against one loaded CSV, under cap, validation and audit.

    Call order is deliberate. The cap is charged *first*, before validation, so an agent that spams
    malformed calls still terminates — otherwise "rejected" would be a free action and a broken agent
    could loop forever. Validation comes next, so a rejected call never reaches pandas. Everything,
    including the refusals, is written to the audit log.
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

    @classmethod
    def from_csv(
        cls,
        path: str | Path,
        call_cap: CallCap | None = None,
        audit_log: AuditLog | None = None,
        max_calls: int = DEFAULT_MAX_CALLS,
    ) -> "GuardedToolkit":
        """Load and profile a CSV, then wrap it. The profile *is* the schema validation runs against."""
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
            # Log before re-raising: a run that ends at the cap should still show what it wanted next.
            self._audit.record(function_name, attempted, OUTCOME_CAPPED, detail=str(exc))
            raise

        try:
            resolved = validate_call(self._profile, function_name, attempted)
        except ValidationError as exc:
            self._audit.record(function_name, attempted, OUTCOME_REJECTED, detail=str(exc))
            return ToolResult(ok=False, function=function_name, error=str(exc), error_code=exc.code)

        tool = get_tool(function_name)
        started = time.perf_counter()
        try:
            data = tool.fn(self._df, **resolved)
        except Exception as exc:  # a toolkit bug must not take the whole run down silently
            elapsed = (time.perf_counter() - started) * 1000
            detail = f"{type(exc).__name__}: {exc}"
            self._audit.record(function_name, resolved, OUTCOME_ERROR, detail=detail,
                               duration_ms=elapsed)
            return ToolResult(ok=False, function=function_name, error=detail,
                              error_code=EXECUTION_ERROR)

        elapsed = (time.perf_counter() - started) * 1000
        self._audit.record(function_name, resolved, OUTCOME_ALLOWED, duration_ms=elapsed,
                           result_summary=_summarise(data))
        return ToolResult(ok=True, function=function_name, data=data)


def _summarise(data: dict[str, Any]) -> str:
    """A short, fixed-size note about a result — enough to read the log, not enough to bloat it."""
    if not isinstance(data, dict):
        return type(data).__name__
    headline = []
    # Both stratification flags, not just one: a reversal and an attenuation are different failures,
    # and grading a run later means being able to see which one the agent was shown.
    for key in ("n", "n_rows", "n_outliers", "n_groups_total", "n_distinct",
                "sign_reversal", "attenuated"):
        if key in data:
            headline.append(f"{key}={data[key]}")
    if "overall" in data and isinstance(data["overall"], dict):
        headline.append(f"pearson_r={data['overall'].get('pearson_r')}")
    return ", ".join(headline) if headline else f"{len(data)} fields"
