"""Tests the analysis primitives against eval/ground_truth.md and their output bounds.

Pins each planted finding to the numbers the toolkit reports, and checks that every result stays
bounded and JSON-serialisable.
"""

from __future__ import annotations

import json

import pandas as pd
import pytest

from toolkit.functions import (
    MAX_CATEGORIES_RETURNED,
    MAX_GROUPS_RETURNED,
    MAX_OUTLIER_EXAMPLES,
    compute_correlation,
    detect_outliers,
    get_summary_stats,
    group_compare,
    value_counts,
)


# --- dataset 1: the genuine correlation ----------------------------------------------------------

def test_planted_correlation_matches_ground_truth(loaded):
    df, _ = loaded["marketing"]
    result = compute_correlation(df, "ad_spend_usd", "conversions")["overall"]
    assert result["pearson_r"] == pytest.approx(0.800, abs=0.005)
    assert result["strength"] == "strong"
    assert result["direction"] == "positive"
    assert result["n"] == 260


def test_the_decoy_noise_column_reads_as_negligible(loaded):
    df, _ = loaded["marketing"]
    result = compute_correlation(df, "ad_spend_usd", "support_tickets")["overall"]
    assert abs(result["pearson_r"]) < 0.2
    assert result["strength"] == "negligible"


def test_missingness_is_reported_for_the_column_that_has_it(loaded):
    df, _ = loaded["marketing"]
    stats = get_summary_stats(df, "avg_order_value_usd")
    assert stats["missing_pct"] == pytest.approx(6.92, abs=0.01)
    assert stats["n_present"] == 260 - stats["n_missing"]


# --- dataset 2: the outlier segment --------------------------------------------------------------

def test_zscore_is_masked_by_the_very_cluster_it_should_find(loaded):
    """STORE_07 has 24 extreme months, and their contribution to the standard deviation hides 6 of
    them from the z-score method. IQR is offered for this reason."""
    df, _ = loaded["stores"]
    result = detect_outliers(df, "revenue_usd", method="zscore")
    assert result["n_outliers"] == 18
    assert result["n_outliers"] < 24

    flagged = df[(df.revenue_usd < result["lower_bound"]) | (df.revenue_usd > result["upper_bound"])]
    assert set(flagged.store_id) == {"STORE_07"}


def test_iqr_catches_the_whole_segment_but_also_the_seasonal_decoy(loaded):
    """IQR finds all 24 STORE_07 months and additionally flags November rows from other stores."""
    df, _ = loaded["stores"]
    result = detect_outliers(df, "revenue_usd", method="iqr")
    flagged = df[(df.revenue_usd < result["lower_bound"]) | (df.revenue_usd > result["upper_bound"])]

    assert (flagged.store_id == "STORE_07").sum() == 24
    others = flagged[flagged.store_id != "STORE_07"]
    assert len(others) > 0
    assert (others.month == "2024-11").sum() >= len(others) - 1


def test_group_compare_localises_the_outlier_to_its_segment(loaded):
    df, _ = loaded["stores"]
    result = group_compare(df, "store_id", "revenue_usd")
    assert result["highest_group"]["group"] == "STORE_07"
    assert result["highest_over_lowest_ratio"] == pytest.approx(8.3, abs=0.3)
    assert result["n_groups_total"] == 12


def test_the_mean_median_gap_flags_a_distorted_average(loaded):
    """Pooled mean 77.9k against median 48.5k indicates one segment dragging the average."""
    df, _ = loaded["stores"]
    stats = get_summary_stats(df, "revenue_usd")
    assert stats["mean"] > stats["median"] * 1.5
    assert stats["mean_median_gap_ratio"] == pytest.approx(0.60, abs=0.02)


# --- dataset 3: the trap -------------------------------------------------------------------------

def test_sign_reversal_is_detected_on_the_trap_pair(loaded):
    df, _ = loaded["training"]
    result = compute_correlation(df, "weekly_training_hours", "output_points", group_by="role_tier")

    assert result["overall"]["pearson_r"] == pytest.approx(0.760, abs=0.005)
    assert result["sign_reversal"] is True
    assert result["attenuated"] is False
    assert result["n_groups_analysed"] == 3
    assert all(group["pearson_r"] < 0 for group in result["groups"])
    assert "opposite sign" in result["stratification_summary"]


def test_attenuation_is_detected_on_the_confounded_pair(loaded):
    """tenure_months has the highest pooled r in the file and collapses within tiers."""
    df, _ = loaded["training"]
    result = compute_correlation(df, "tenure_months", "output_points", group_by="role_tier")

    assert result["overall"]["pearson_r"] == pytest.approx(0.853, abs=0.005)
    assert result["attenuated"] is True
    assert result["sign_reversal"] is False
    assert max(abs(g["pearson_r"]) for g in result["groups"]) < 0.1


def test_the_genuine_relationship_survives_stratification(loaded):
    """Control case: a genuine relationship survives stratification with both flags false."""
    df, _ = loaded["training"]
    result = compute_correlation(df, "peer_review_score", "output_points", group_by="role_tier")

    assert result["overall"]["pearson_r"] == pytest.approx(0.753, abs=0.005)
    assert result["sign_reversal"] is False
    assert result["attenuated"] is False
    assert all(group["pearson_r"] > 0.5 for group in result["groups"])
    assert "agree in direction" in result["stratification_summary"]


def test_ungrouped_calls_carry_no_stratification_verdict(loaded):
    df, _ = loaded["training"]
    result = compute_correlation(df, "weekly_training_hours", "output_points")
    assert result["group_by"] is None
    assert "sign_reversal" not in result


def test_spearman_reports_alongside_pearson(loaded):
    df, _ = loaded["stores"]
    result = compute_correlation(df, "units_sold", "revenue_usd")["overall"]
    assert result["spearman_r"] is not None
    assert result["pearson_r"] is not None


# --- output bounds -------------------------------------------------------------------------------

def test_value_counts_truncates_and_says_so():
    df = pd.DataFrame({"code": [f"C{i:03d}" for i in range(60)] * 3})
    result = value_counts(df, "code")

    assert result["n_distinct"] == 60
    assert result["n_returned"] == MAX_CATEGORIES_RETURNED
    assert result["truncated"] is True
    assert result["other_distinct"] == 60 - MAX_CATEGORIES_RETURNED
    assert result["other_count"] == 180 - MAX_CATEGORIES_RETURNED * 3


def test_group_compare_truncates_to_both_extremes():
    df = pd.DataFrame({"g": [f"G{i:02d}" for i in range(40)] * 5,
                       "v": [float(i) for i in range(40)] * 5})
    result = group_compare(df, "g", "v")

    assert result["n_groups_total"] == 40
    assert result["n_groups_returned"] == MAX_GROUPS_RETURNED
    assert result["truncated"] is True
    assert "highest" in result["truncation_note"] and "lowest" in result["truncation_note"]
    returned = [g["group"] for g in result["groups"]]
    assert "G39" in returned and "G00" in returned   # both ends survived the cut


def test_stratified_correlation_truncates_its_group_list():
    rows = []
    for group in range(25):
        for i in range(20):
            rows.append({"g": f"G{group:02d}", "x": float(i), "y": float(i * 2 + group)})
    result = compute_correlation(pd.DataFrame(rows), "x", "y", group_by="g")

    assert result["n_groups_total"] == 25
    assert result["n_groups_returned"] == MAX_GROUPS_RETURNED
    assert result["truncated"] is True


def test_outlier_examples_are_capped():
    # The bulk needs real spread: an IQR of zero is a degenerate column, not an outlier test.
    values = [float(i % 50) for i in range(200)] + [10_000.0 + i for i in range(40)]
    result = detect_outliers(pd.DataFrame({"v": values}), "v", method="iqr")
    assert result["n_outliers"] == 40
    assert result["n_examples_returned"] == MAX_OUTLIER_EXAMPLES
    assert result["truncated"] is True


def test_a_column_with_no_spread_reports_zero_variance_rather_than_outliers():
    """83% identical values means no usable IQR fence, reported as zero variance."""
    result = detect_outliers(pd.DataFrame({"v": [1.0] * 200 + [9_999.0] * 40}), "v", method="iqr")
    assert result["n_outliers"] == 0
    assert "interquartile" in result["note"]


def test_groups_too_small_to_correlate_are_skipped_not_guessed():
    """Groups below the minimum size are reported as skipped rather than correlated."""
    rows = []
    for group in ("big", "tiny"):
        n = 40 if group == "big" else 3
        for i in range(n):
            rows.append({"g": group, "x": float(i), "y": float(i % 7)})
    result = compute_correlation(pd.DataFrame(rows), "x", "y", group_by="g")

    assert result["n_groups_analysed"] == 1
    assert result["n_groups_skipped"] == 1
    assert result["groups_skipped"] == ["tiny"]


# --- results must survive the trip back into a prompt ---------------------------------------------

def test_every_function_returns_json_serialisable_output(loaded):
    df, _ = loaded["stores"]
    results = [
        get_summary_stats(df, "revenue_usd"),
        compute_correlation(df, "units_sold", "revenue_usd", group_by="store_id"),
        detect_outliers(df, "revenue_usd", method="iqr"),
        group_compare(df, "store_id", "revenue_usd"),
        value_counts(df, "region"),
    ]
    for result in results:
        encoded = json.dumps(result)          # raises on NaN-free violations of the JSON spec
        assert "NaN" not in encoded
        assert "Infinity" not in encoded


def test_all_null_and_constant_columns_return_a_note_rather_than_raising():
    df = pd.DataFrame({"blank": [None] * 30, "flat": [7.0] * 30, "x": [float(i) for i in range(30)]})

    assert "note" in get_summary_stats(df, "blank")
    assert "note" in compute_correlation(df, "flat", "x")["overall"]
    assert detect_outliers(df, "flat")["n_outliers"] == 0


def test_direct_calls_with_an_unknown_method_fail_loudly():
    """The function refuses an unknown method itself, independent of the validator."""
    df = pd.DataFrame({"v": [float(i) for i in range(50)]})
    with pytest.raises(ValueError, match="unknown outlier method"):
        detect_outliers(df, "v", method="isolation_forest")


# --- output labelling: a ratio must say what it is a ratio OF -------------------------------------

def test_group_ratios_name_their_denominator(loaded):
    """Ratio keys name their denominator, so a median ratio is not read against overall_mean."""
    df, _ = loaded["stores"]
    result = group_compare(df, "store_id", "revenue_usd")

    group = result["groups"][0]
    assert "median_ratio_to_overall_median" in group
    assert "median_ratio_to_overall" not in group

    # and the value really is against the median, not the mean
    expected = group["median"] / result["overall_median"]
    assert group["median_ratio_to_overall_median"] == pytest.approx(expected, abs=0.001)
    assert result["overall_mean"] != result["overall_median"]     # the two differ here, so it matters

    # the headline ratio is highest-over-lowest, which is a different number again
    assert result["highest_over_lowest_ratio"] == pytest.approx(
        result["highest_group"]["mean"] / result["lowest_group"]["mean"], abs=0.01)
    over_overall_mean = result["highest_group"]["mean"] / result["overall_mean"]
    assert abs(result["highest_over_lowest_ratio"] - over_overall_mean) > 3   # 8.32 vs 5.11
