"""Shared pytest fixtures: the three eval datasets, loaded and profiled once per session.

Tests assert against the real committed fixtures and the values recorded in eval/ground_truth.md.
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
    """Every eval dataset as (DataFrame, profile). Session-scoped, since profiling is pure."""
    result = {}
    for key, filename in DATASETS.items():
        df = load_csv(DATA_DIR / filename)
        result[key] = (df, profile_dataframe(df, name=filename))
    return result


@pytest.fixture(scope="session")
def profiles(loaded) -> dict[str, dict]:
    """Just the profiles, which is what the validator consumes."""
    return {key: profile for key, (_, profile) in loaded.items()}


@pytest.fixture
def make_toolkit(loaded):
    """Build a fresh GuardedToolkit per test: the cap and audit log are mutable run state."""

    def _make(name: str, max_calls: int = 50, audit_path: Path | None = None) -> GuardedToolkit:
        df, profile = loaded[name]
        return GuardedToolkit(
            df, profile, call_cap=CallCap(max_calls), audit_log=AuditLog(audit_path)
        )

    return _make
