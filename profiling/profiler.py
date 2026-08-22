"""Reduces any CSV to a compact, JSON-serialisable profile.

Covers row count, dtypes, missing percentage per column, an inferred role per column, numeric
summary stats and categorical cardinality. The guardrail allowlist validates tool arguments
against this profile.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import warnings
from pathlib import Path
from typing import Any

import pandas as pd

# --- tuning constants ---

# A text column counts as a datetime only if this fraction of sampled values parses as one.
DATETIME_PARSE_THRESHOLD = 0.90
# ...and only if the values look date-ish, so IDs like "10432" are not read as the year 10432.
DATETIME_SHAPE_RE = re.compile(r"\d[-/:. ]\d")
# Number of values sampled when sniffing for datetimes. Bounded so profiling stays cheap.
DATETIME_SAMPLE_SIZE = 200
# A text column with more distinct values than this fraction of rows is treated as an identifier
# rather than a grouping key.
IDENTIFIER_UNIQUENESS_THRESHOLD = 0.95
# Below this row count, uniqueness is too noisy to call something an identifier.
IDENTIFIER_MIN_ROWS = 20
# How many of the most frequent values to show for a categorical column.
TOP_VALUES_KEPT = 5

# Roles the profiler can assign. The toolkit uses these to decide which functions apply to a
# column (e.g. correlation needs two NUMERIC columns).
ROLE_NUMERIC = "numeric"
ROLE_BOOLEAN = "boolean"
ROLE_DATETIME = "datetime"
ROLE_CATEGORICAL = "categorical"
ROLE_IDENTIFIER = "identifier"
ROLE_EMPTY = "empty"


def load_csv(path: str | Path) -> pd.DataFrame:
    """Read a CSV into a DataFrame, raising a specific error on failure."""
    csv_path = Path(path)
    if not csv_path.is_file():
        raise FileNotFoundError(f"No CSV at {csv_path}")
    try:
        return pd.read_csv(csv_path)
    except pd.errors.EmptyDataError as exc:
        raise ValueError(f"{csv_path} is empty or has no parseable header") from exc


def _py(value: Any) -> Any:
    """Convert a numpy/pandas scalar to a plain JSON-safe Python value (NaN/NaT -> None)."""
    if value is None or value is pd.NaT:
        return None
    item = value.item() if hasattr(value, "item") else value
    if isinstance(item, float):
        if math.isnan(item) or math.isinf(item):
            return None
        return round(item, 4)
    return item


def _looks_like_datetime(series: pd.Series) -> bool:
    """Sniff whether a text column holds dates, using a shape guard then a real parse."""
    sample = series.dropna().astype(str).head(DATETIME_SAMPLE_SIZE)
    if sample.empty:
        return False
    if sample.str.contains(DATETIME_SHAPE_RE).mean() < DATETIME_PARSE_THRESHOLD:
        return False
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # pandas warns when inferring per-element formats
        parsed = pd.to_datetime(sample, errors="coerce")
    return bool(parsed.notna().mean() >= DATETIME_PARSE_THRESHOLD)


def _infer_role(series: pd.Series) -> str:
    """Map a column to the coarse role the toolkit dispatches on."""
    non_null = series.dropna()
    if non_null.empty:
        return ROLE_EMPTY
    if pd.api.types.is_bool_dtype(series):
        return ROLE_BOOLEAN
    if pd.api.types.is_numeric_dtype(series):
        return ROLE_NUMERIC
    if pd.api.types.is_datetime64_any_dtype(series):
        return ROLE_DATETIME
    if _looks_like_datetime(series):
        return ROLE_DATETIME
    if len(non_null) >= IDENTIFIER_MIN_ROWS:
        if non_null.nunique() / len(non_null) > IDENTIFIER_UNIQUENESS_THRESHOLD:
            return ROLE_IDENTIFIER
    return ROLE_CATEGORICAL


def _numeric_stats(series: pd.Series) -> dict[str, Any]:
    """Summary stats for one numeric column. All-null columns return Nones."""
    # astype(float) is required: bool columns survive to_numeric as bools, and quantile() raises
    # on a bool dtype.
    values = pd.to_numeric(series, errors="coerce").dropna().astype(float)
    if values.empty:
        return {k: None for k in ("mean", "std", "min", "p25", "median", "p75", "max")}
    return {
        "mean": _py(values.mean()),
        "std": _py(values.std()),
        "min": _py(values.min()),
        "p25": _py(values.quantile(0.25)),
        "median": _py(values.median()),
        "p75": _py(values.quantile(0.75)),
        "max": _py(values.max()),
    }


def _categorical_stats(series: pd.Series) -> dict[str, Any]:
    """Cardinality detail and the most common values for one categorical column."""
    counts = series.dropna().astype(str).value_counts()
    return {
        "top_values": [
            {"value": str(value), "count": int(count)}
            for value, count in counts.head(TOP_VALUES_KEPT).items()
        ]
    }


def profile_dataframe(df: pd.DataFrame, name: str = "<dataframe>") -> dict[str, Any]:
    """Build the full profile dict for an already-loaded DataFrame."""
    row_count = int(len(df))
    columns: list[dict[str, Any]] = []

    for column in df.columns:
        series = df[column]
        missing = int(series.isna().sum())
        role = _infer_role(series)
        info: dict[str, Any] = {
            "name": str(column),
            "dtype": str(series.dtype),
            "role": role,
            "non_null_count": row_count - missing,
            "missing_count": missing,
            "missing_pct": round(100.0 * missing / row_count, 2) if row_count else 0.0,
            "distinct_count": int(series.nunique(dropna=True)),
        }
        if role in (ROLE_NUMERIC, ROLE_BOOLEAN):
            info.update(_numeric_stats(series))
        elif role in (ROLE_CATEGORICAL, ROLE_DATETIME):
            info.update(_categorical_stats(series))
        columns.append(info)

    return {
        "source": str(name),
        "row_count": row_count,
        "column_count": int(df.shape[1]),
        "duplicate_row_count": int(df.duplicated().sum()),
        "columns": columns,
        "numeric_columns": [c["name"] for c in columns if c["role"] == ROLE_NUMERIC],
        "categorical_columns": [c["name"] for c in columns if c["role"] == ROLE_CATEGORICAL],
        "datetime_columns": [c["name"] for c in columns if c["role"] == ROLE_DATETIME],
    }


def profile_csv(path: str | Path) -> dict[str, Any]:
    """Load a CSV and profile it. The entry point for other modules."""
    csv_path = Path(path)
    return profile_dataframe(load_csv(csv_path), name=csv_path.name)


def format_profile(profile: dict[str, Any]) -> str:
    """Render a profile as compact text for a prompt or console."""
    lines = [
        f"DATASET: {profile['source']}",
        "rows={row_count}  columns={column_count}  duplicate_rows={duplicate_row_count}".format(
            **profile
        ),
        "",
        f"{'column':<26}{'role':<13}{'dtype':<12}{'miss%':>7}{'distinct':>10}",
        "-" * 68,
    ]
    for col in profile["columns"]:
        lines.append(
            f"{col['name'][:25]:<26}{col['role']:<13}{col['dtype'][:11]:<12}"
            f"{col['missing_pct']:>7}{col['distinct_count']:>10}"
        )

    numeric = [c for c in profile["columns"] if c["role"] in (ROLE_NUMERIC, ROLE_BOOLEAN)]
    if numeric:
        header = f"{'column':<26}{'mean':>13}{'std':>13}{'min':>13}{'median':>13}{'max':>13}"
        lines += ["", "NUMERIC SUMMARY", header, "-" * len(header)]
        for col in numeric:
            cells = "".join(
                f"{('-' if col[k] is None else col[k]):>13}"
                for k in ("mean", "std", "min", "median", "max")
            )
            lines.append(f"{col['name'][:25]:<26}{cells}")

    categorical = [c for c in profile["columns"] if c.get("top_values")]
    if categorical:
        lines += ["", "MOST COMMON VALUES"]
        for col in categorical:
            preview = ", ".join(f"{t['value']} ({t['count']})" for t in col["top_values"])
            lines.append(f"  {col['name']}: {preview}")

    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Profile a CSV for the AutoSight agent.")
    parser.add_argument("csv_path", help="path to the CSV to profile")
    parser.add_argument("--json", action="store_true", help="emit the raw profile dict as JSON")
    args = parser.parse_args()

    profile = profile_csv(args.csv_path)
    print(json.dumps(profile, indent=2) if args.json else format_profile(profile))


if __name__ == "__main__":
    main()
