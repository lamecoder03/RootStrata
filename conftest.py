"""
conftest.py — shared pytest fixtures: the three Day 1 datasets, loaded and profiled once per session.
Exists so the guardrail tests assert against the real committed fixtures and the values recorded in
eval/ground_truth.md, rather than toy frames that could drift from what the agent will actually see.
Sitting at the repo root also puts the root on sys.path, so tests import the packages directly.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from guardrails.audit import AuditLog
from guardrails.call_cap import CallCap
from guardrails.executor import GuardedToolkit
from profiling.profiler import load_csv, profile_dataframe

DATA_DIR = Path(__file__).parent / "data" / "test_datasets"
DATASETS = {
    "marketing": "marketing_weekly.csv",
    "stores": "store_monthly_sales.csv",
    "training": "training_productivity.csv",
}


@pytest.fixture(scope="session")
def loaded() -> dict[str, tuple]:
    """Every eval dataset as (DataFrame, profile). Session-scoped: profiling is pure, so share it."""
    result = {}
    for key, filename in DATASETS.items():
        df = load_csv(DATA_DIR / filename)
        result[key] = (df, profile_dataframe(df, name=filename))
    return result


@pytest.fixture(scope="session")
def profiles(loaded) -> dict[str, dict]:
    """Just the profiles — what the validator actually consumes."""
    return {key: profile for key, (_, profile) in loaded.items()}


@pytest.fixture
def make_toolkit(loaded):
    """Build a fresh GuardedToolkit per test: cap and audit log are mutable run state, not fixtures."""

    def _make(name: str, max_calls: int = 50, audit_path: Path | None = None) -> GuardedToolkit:
        df, profile = loaded[name]
        return GuardedToolkit(
            df, profile, call_cap=CallCap(max_calls), audit_log=AuditLog(audit_path)
        )

    return _make
