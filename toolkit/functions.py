"""
functions.py — the fixed set of analysis primitives the agent may run: summary stats, correlation
(optionally stratified by a subgroup), outlier detection, group comparison and value counts.
Exists because the agent picks functions instead of writing code, so this file is its entire action
space; every result is JSON-safe and size-bounded, since results are fed straight back into a prompt.
"""

from __future__ import annotations

import math
from typing import Any

import pandas as pd
from scipy import stats

# --- output bounds --------------------------------------------------------------------------
# Every function truncates. Two reasons: results go back into the model's context, where unbounded
# output blows the window and the token budget; and a "finding" list of 400 rows is not a finding.
# Truncation is never silent — each result carries a `truncated` flag and the true total.
MAX_CATEGORIES_RETURNED = 20
MAX_GROUPS_RETURNED = 20
MAX_OUTLIER_EXAMPLES = 10

# Correlation on a handful of rows is noise. Groups below this are reported as skipped rather than
# handed back with a meaningless r that the agent might quote.
MIN_ROWS_FOR_CORRELATION = 10

# The two ways a pooled correlation fails to survive stratification. Both are arithmetic tests, not
# judgments — the agent still decides what they mean, but it should not have to notice them itself.
SIGN_REVERSAL_MIN_ABS_R = 0.10   # ignore sign flips that are indistinguishable from noise
ATTENUATION_RATIO = 0.50         # "vanishes" = strongest subgroup |r| is under half the pooled |r|

ZSCORE_THRESHOLD = 3.0
IQR_MULTIPLIER = 1.5


def _j(value: Any, digits: int = 4) -> Any:
    """Make a numpy/pandas scalar JSON-safe. NaN and inf become None so json.dumps stays valid."""
    if value is None or value is pd.NaT:
        return None
    item = value.item() if hasattr(value, "item") else value
    if isinstance(item, float):
        return None if (math.isnan(item) or math.isinf(item)) else round(item, digits)
    return item


def _strength(r: float | None) -> str:
    """Plain-language bucket for |r|, so every report describes the same number the same way."""
    if r is None:
        return "undefined"
    magnitude = abs(r)
    if magnitude >= 0.70:
        return "strong"
    if magnitude >= 0.40:
        return "moderate"
    if magnitude >= 0.20:
        return "weak"
    return "negligible"


def _numeric(df: pd.DataFrame, column: str) -> pd.Series:
    """Coerce a column to float. Values that cannot be parsed become NaN rather than raising."""
    return pd.to_numeric(df[column], errors="coerce").astype(float)


# ----------------------------------------------------------------------------------------------
# 1. summary stats
# ----------------------------------------------------------------------------------------------

def get_summary_stats(df: pd.DataFrame, column: str) -> dict[str, Any]:
    """Distribution summary for one numeric column: centre, spread, quartiles and missingness."""
    values = _numeric(df, column).dropna()
    n_rows = int(len(df))
    n_missing = n_rows - int(len(values))

    result: dict[str, Any] = {
        "column": column,
        "n_rows": n_rows,
        "n_present": int(len(values)),
        "n_missing": n_missing,
        "missing_pct": _j(100.0 * n_missing / n_rows, 2) if n_rows else 0.0,
        "n_distinct": int(values.nunique()),
    }
    if values.empty:
        result["note"] = "no non-null numeric values in this column"
        return result

    q1 = float(values.quantile(0.25))
    q3 = float(values.quantile(0.75))
    median = float(values.median())
    result.update(
        {
            "mean": _j(values.mean()),
            "std": _j(values.std()),
            "min": _j(values.min()),
            "p25": _j(q1),
            "median": _j(median),
            "p75": _j(q3),
            "max": _j(values.max()),
            "iqr": _j(q3 - q1),
            # A wide mean/median gap is the cheapest tell that one segment is dragging the average,
            # which is the exact mistake this project's store dataset is built to provoke.
            "mean_median_gap_ratio": (
                _j(abs(float(values.mean()) - median) / abs(median)) if median != 0 else None
            ),
        }
    )
    return result


# ----------------------------------------------------------------------------------------------
# 2. correlation, optionally stratified
# ----------------------------------------------------------------------------------------------

def _pair_correlation(a: pd.Series, b: pd.Series) -> dict[str, Any]:
    """Pearson and Spearman for one aligned pair.

    Spearman rides along because it is robust to the single-segment outliers this same toolkit is
    asked to find: a large Pearson/Spearman gap means one cluster of rows is driving the result.
    """
    paired = pd.DataFrame(
        {"a": pd.to_numeric(a, errors="coerce"), "b": pd.to_numeric(b, errors="coerce")}
    ).dropna()
    n = int(len(paired))

    blank = {"n": n, "pearson_r": None, "pearson_p": None, "spearman_r": None,
             "strength": "undefined", "direction": None}
    if n < MIN_ROWS_FOR_CORRELATION:
        return {**blank, "note": f"fewer than {MIN_ROWS_FOR_CORRELATION} complete pairs"}
    if paired["a"].nunique() < 2 or paired["b"].nunique() < 2:
        return {**blank, "note": "one of the columns is constant, so correlation is undefined"}

    pearson = stats.pearsonr(paired["a"], paired["b"])
    spearman = stats.spearmanr(paired["a"], paired["b"])
    r = float(pearson.statistic)
    return {
        "n": n,
        "pearson_r": _j(r),
        "pearson_p": _j(pearson.pvalue, 6),
        "spearman_r": _j(spearman.statistic),
        "strength": _strength(r),
        "direction": "positive" if r > 0 else "negative",
    }


def compute_correlation(
    df: pd.DataFrame, col_a: str, col_b: str, group_by: str | None = None
) -> dict[str, Any]:
    """Correlate two numeric columns, and — if group_by is given — inside each subgroup as well.

    The stratified path is the point: a pooled r that reverses sign or collapses within subgroups
    is a confound, not a finding, so the result reports both outcomes as explicit flags.
    """
    overall = _pair_correlation(df[col_a], df[col_b])
    result: dict[str, Any] = {
        "col_a": col_a,
        "col_b": col_b,
        "group_by": group_by,
        "overall": overall,
    }
    if group_by is None:
        return result

    analysed: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for value, chunk in df.groupby(group_by, dropna=True, observed=True):
        entry = {"group": str(value), **_pair_correlation(chunk[col_a], chunk[col_b])}
        (analysed if entry["pearson_r"] is not None else skipped).append(entry)

    # When there are too many groups to return, keep the most informative ones (largest |r|) rather
    # than an alphabetical head — but sort what survives by name so the output reads consistently.
    by_magnitude = sorted(analysed, key=lambda g: -abs(g["pearson_r"]))
    shown = sorted(by_magnitude[:MAX_GROUPS_RETURNED], key=lambda g: g["group"])

    overall_r = overall["pearson_r"]
    subgroup_rs = [g["pearson_r"] for g in analysed]
    sign_reversal = False
    attenuated = False
    if overall_r is not None and subgroup_rs:
        sign_reversal = any(
            r * overall_r < 0 and abs(r) >= SIGN_REVERSAL_MIN_ABS_R for r in subgroup_rs
        )
        attenuated = max(abs(r) for r in subgroup_rs) < ATTENUATION_RATIO * abs(overall_r)

    result.update(
        {
            "n_groups_total": len(analysed) + len(skipped),
            "n_groups_analysed": len(analysed),
            "n_groups_returned": len(shown),
            "truncated": len(analysed) > MAX_GROUPS_RETURNED,
            "groups": shown,
            "n_groups_skipped": len(skipped),
            "groups_skipped": [g["group"] for g in skipped[:MAX_GROUPS_RETURNED]],
            "subgroup_r_min": _j(min(subgroup_rs)) if subgroup_rs else None,
            "subgroup_r_max": _j(max(subgroup_rs)) if subgroup_rs else None,
            "sign_reversal": sign_reversal,
            "attenuated": attenuated,
            "stratification_summary": _stratification_summary(
                overall_r, subgroup_rs, sign_reversal, attenuated
            ),
        }
    )
    return result


def _stratification_summary(
    overall_r: float | None, subgroup_rs: list[float], sign_reversal: bool, attenuated: bool
) -> str:
    """One factual sentence restating the flags — no interpretation, just what the numbers did."""
    if overall_r is None or not subgroup_rs:
        return "not enough data to compare pooled and subgroup correlations"
    n = len(subgroup_rs)
    if sign_reversal:
        opposed = sum(1 for r in subgroup_rs if r * overall_r < 0)
        return (
            f"pooled r is {overall_r:+.3f}, but {opposed} of {n} subgroups have the opposite sign "
            f"(subgroup range {min(subgroup_rs):+.3f} to {max(subgroup_rs):+.3f})"
        )
    if attenuated:
        strongest = max(subgroup_rs, key=abs)
        return (
            f"pooled r is {overall_r:+.3f}, but the strongest subgroup r is only {strongest:+.3f} "
            f"- the relationship largely disappears within subgroups"
        )
    return (
        f"pooled r is {overall_r:+.3f} and all {n} subgroups agree in direction "
        f"(subgroup range {min(subgroup_rs):+.3f} to {max(subgroup_rs):+.3f})"
    )


# ----------------------------------------------------------------------------------------------
# 3. outliers
# ----------------------------------------------------------------------------------------------

def detect_outliers(df: pd.DataFrame, column: str, method: str = "zscore") -> dict[str, Any]:
    """Flag unusual values in one numeric column, by z-score or by the IQR fence.

    Both methods are offered because z-score is *masked* by the thing it is looking for: a large
    cluster of extreme rows inflates the standard deviation and can hide itself. IQR does not move.
    """
    values = _numeric(df, column).dropna()
    n_rows = int(len(df))
    result: dict[str, Any] = {
        "column": column,
        "method": method,
        "n_rows": n_rows,
        "n_present": int(len(values)),
        "n_missing": n_rows - int(len(values)),
    }
    if len(values) < 3 or values.nunique() < 2:
        return {**result, "n_outliers": 0, "examples": [],
                "note": "too few distinct values to look for outliers"}

    if method == "zscore":
        mean = float(values.mean())
        std = float(values.std())
        if std == 0:
            return {**result, "n_outliers": 0, "examples": [], "note": "zero variance"}
        scores = (values - mean) / std
        lower, upper = mean - ZSCORE_THRESHOLD * std, mean + ZSCORE_THRESHOLD * std
        result["threshold"] = ZSCORE_THRESHOLD
    elif method == "iqr":
        q1, q3 = float(values.quantile(0.25)), float(values.quantile(0.75))
        iqr = q3 - q1
        if iqr == 0:
            return {**result, "n_outliers": 0, "examples": [], "note": "zero interquartile range"}
        lower, upper = q1 - IQR_MULTIPLIER * iqr, q3 + IQR_MULTIPLIER * iqr
        # Score = how many IQRs past the nearer fence, so it is comparable across columns.
        scores = ((values - upper).clip(lower=0) + (values - lower).clip(upper=0)) / iqr
        result["threshold"] = IQR_MULTIPLIER
    else:  # unreachable via the validator, which enforces the enum; kept so direct calls fail loudly
        raise ValueError(f"unknown outlier method {method!r}; expected 'zscore' or 'iqr'")

    flagged = values[(values < lower) | (values > upper)]
    ranked = scores.loc[flagged.index].abs().sort_values(ascending=False)
    examples = [
        {"row_index": int(idx), "value": _j(values.loc[idx]), "score": _j(scores.loc[idx], 2)}
        for idx in ranked.head(MAX_OUTLIER_EXAMPLES).index
    ]

    result.update(
        {
            "lower_bound": _j(lower),
            "upper_bound": _j(upper),
            "n_outliers": int(len(flagged)),
            "outlier_pct": _j(100.0 * len(flagged) / len(values), 2),
            "outlier_value_min": _j(flagged.min()) if len(flagged) else None,
            "outlier_value_max": _j(flagged.max()) if len(flagged) else None,
            "examples": examples,
            "n_examples_returned": len(examples),
            "truncated": len(flagged) > len(examples),
        }
    )
    return result


# ----------------------------------------------------------------------------------------------
# 4. group comparison
# ----------------------------------------------------------------------------------------------

def group_compare(df: pd.DataFrame, group_col: str, value_col: str) -> dict[str, Any]:
    """Compare a numeric column across the levels of a grouping column.

    This is how an outlier gets localised: detect_outliers says *that* rows are extreme, this says
    *which segment* they belong to.
    """
    frame = pd.DataFrame(
        {"group": df[group_col].astype(str), "value": _numeric(df, value_col)}
    ).dropna(subset=["value"])

    result: dict[str, Any] = {"group_col": group_col, "value_col": value_col}
    if frame.empty:
        return {**result, "n_groups_total": 0, "groups": [],
                "note": "no rows with both a group and a numeric value"}

    agg = frame.groupby("group")["value"].agg(["count", "mean", "median", "std", "min", "max"])
    overall_median = float(frame["value"].median())
    rows = [
        {
            "group": str(name),
            "n": int(row["count"]),
            "mean": _j(row["mean"]),
            "median": _j(row["median"]),
            "std": _j(row["std"]),
            "min": _j(row["min"]),
            "max": _j(row["max"]),
            # Ratio to the pooled median, not the pooled mean: the mean is exactly what a runaway
            # segment distorts, so comparing against it would hide the segment.
            "median_ratio_to_overall": (
                _j(float(row["median"]) / overall_median) if overall_median != 0 else None
            ),
        }
        for name, row in agg.iterrows()
    ]
    rows.sort(key=lambda r: (r["mean"] is None, -(r["mean"] or 0)))

    n_total = len(rows)
    truncated = n_total > MAX_GROUPS_RETURNED
    if truncated:
        # Keep both ends: a comparison is about the extremes, so an alphabetical head is useless.
        half = MAX_GROUPS_RETURNED // 2
        shown = rows[:half] + rows[-half:]
    else:
        shown = rows

    highest, lowest = rows[0], rows[-1]
    spread_ratio = None
    if lowest["mean"] not in (None, 0) and highest["mean"] is not None:
        if lowest["mean"] > 0 and highest["mean"] > 0:
            spread_ratio = _j(highest["mean"] / lowest["mean"], 2)

    result.update(
        {
            "n_rows_used": int(len(frame)),
            "overall_mean": _j(frame["value"].mean()),
            "overall_median": _j(overall_median),
            "n_groups_total": n_total,
            "n_groups_returned": len(shown),
            "truncated": truncated,
            "truncation_note": (
                f"showing the {MAX_GROUPS_RETURNED // 2} highest and "
                f"{MAX_GROUPS_RETURNED // 2} lowest groups by mean"
            ) if truncated else "",
            "groups": shown,
            "highest_group": {"group": highest["group"], "mean": highest["mean"], "n": highest["n"]},
            "lowest_group": {"group": lowest["group"], "mean": lowest["mean"], "n": lowest["n"]},
            "highest_over_lowest_ratio": spread_ratio,
        }
    )
    return result


# ----------------------------------------------------------------------------------------------
# 5. value counts
# ----------------------------------------------------------------------------------------------

def value_counts(df: pd.DataFrame, column: str) -> dict[str, Any]:
    """Frequency of each value in a column, capped to the most common ones with a remainder bucket."""
    series = df[column]
    n_rows = int(len(df))
    n_missing = int(series.isna().sum())
    counts = series.dropna().astype(str).value_counts()

    total = int(counts.sum())
    top = counts.head(MAX_CATEGORIES_RETURNED)
    values = [
        {"value": str(value), "count": int(count),
         "pct": _j(100.0 * count / total, 2) if total else 0.0}
        for value, count in top.items()
    ]

    return {
        "column": column,
        "n_rows": n_rows,
        "n_missing": n_missing,
        "missing_pct": _j(100.0 * n_missing / n_rows, 2) if n_rows else 0.0,
        "n_distinct": int(len(counts)),
        "values": values,
        "n_returned": len(values),
        "truncated": len(counts) > len(top),
        "other_distinct": int(len(counts) - len(top)),
        "other_count": int(total - int(top.sum())),
        "top_value_share_pct": values[0]["pct"] if values else None,
    }
