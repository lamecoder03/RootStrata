"""
generate_test_datasets.py — builds the three synthetic eval CSVs in data/test_datasets/, each with
a precisely planted finding: a genuine correlation, an outlier segment, and a Simpson's-paradox trap.
Exists so the eval fixtures are reproducible and their ground truth is exact rather than eyeballed —
it constructs correlations by projection (so sample r hits its target exactly), then re-measures and
prints what actually landed in the files, which is what eval/ground_truth.md records.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

SEED = 20240817
OUT_DIR = Path(__file__).resolve().parent / "test_datasets"


# --------------------------------------------------------------------------------------
# helpers: build variables whose *sample* correlation equals the target, not just its
# expectation. Everything is standardised, projected orthogonal, then recombined.
# --------------------------------------------------------------------------------------

def _standardise(v: np.ndarray) -> np.ndarray:
    """Zero mean, unit (population) sd — so dot products are sample correlations."""
    v = v - v.mean()
    return v / v.std()


def _orthogonalise(v: np.ndarray, basis: list[np.ndarray]) -> np.ndarray:
    """Remove the component of v lying along each already-standardised basis vector."""
    v = v - v.mean()
    for b in basis:
        v = v - (v @ b) / len(b) * b
    return _standardise(v)


def _combine(rng: np.random.Generator, basis: list[np.ndarray], weights: list[float]) -> np.ndarray:
    """Standardised variable correlating exactly `weights[i]` with each orthonormal basis[i]."""
    residual_weight = 1.0 - sum(w * w for w in weights)
    if residual_weight <= 0:
        raise ValueError(f"weights {weights} imply a correlation matrix that is not valid")
    noise = _orthogonalise(rng.standard_normal(len(basis[0])), basis)
    combined = sum(w * b for w, b in zip(weights, basis)) + np.sqrt(residual_weight) * noise
    return _standardise(combined)


def _blank_out(rng: np.random.Generator, values: np.ndarray, fraction: float) -> np.ndarray:
    """Punch a realistic amount of missingness into a column so the profiler has work to do."""
    out = values.astype(float).copy()
    n_missing = int(round(len(out) * fraction))
    out[rng.choice(len(out), size=n_missing, replace=False)] = np.nan
    return out


# --------------------------------------------------------------------------------------
# dataset 1 — a genuine, strong, honest correlation (plus decoys)
# --------------------------------------------------------------------------------------

def build_marketing_weekly() -> pd.DataFrame:
    """52 weeks x 5 regions of marketing data. Planted: r(ad_spend, conversions) = 0.80 exactly."""
    rng = np.random.default_rng(SEED)
    regions = ["North", "South", "East", "West", "Central"]
    weeks = pd.date_range("2024-01-01", periods=52, freq="W-MON")
    n = len(weeks) * len(regions)

    spend_z = _standardise(rng.standard_normal(n))
    conversions_z = _combine(rng, [spend_z], [0.80])     # THE planted finding
    visits_z = _combine(rng, [spend_z], [0.55])          # weaker, still genuine
    aov_z = _combine(rng, [spend_z], [0.00])             # deliberately uncorrelated

    ad_spend = 5000 + 1200 * spend_z
    df = pd.DataFrame(
        {
            "week_start": np.tile(weeks.strftime("%Y-%m-%d"), len(regions)),
            "region": np.repeat(regions, len(weeks)),
            "ad_spend_usd": ad_spend.round(2),
            # impressions are ~mechanically ad_spend / CPM: a very high r that means nothing.
            "impressions": (ad_spend * 210 + rng.normal(0, 25_000, n)).round().astype(int),
            "website_visits": (12_000 + 2_600 * visits_z).round().astype(int),
            "conversions": (180 + 45 * conversions_z).round().astype(int),
            "avg_order_value_usd": _blank_out(rng, (68 + 9 * aov_z).round(2), 0.07),
            # pure noise, correlated with nothing: the decoy a pattern-matcher may still report.
            "support_tickets": rng.poisson(40, n),
        }
    )
    return df


# --------------------------------------------------------------------------------------
# dataset 2 — one segment far outside the rest (plus a boring seasonal bump)
# --------------------------------------------------------------------------------------

OUTLIER_STORE = "STORE_07"
SEASONAL_MONTH = "2024-11"
SEASONAL_UPLIFT = 1.40


def build_store_monthly_sales() -> pd.DataFrame:
    """12 stores x 24 months. Planted: STORE_07 sits ~8x above every other store, every month."""
    rng = np.random.default_rng(SEED + 1)
    stores = [f"STORE_{i:02d}" for i in range(1, 13)]
    months = pd.date_range("2023-01-01", periods=24, freq="MS").strftime("%Y-%m")
    region_of = {s: ["North", "South", "East", "West"][i % 4] for i, s in enumerate(stores)}

    rows = []
    for store in stores:
        is_outlier = store == OUTLIER_STORE
        base, spread = (392_000, 24_000) if is_outlier else (48_000, 4_000)
        for month in months:
            revenue = rng.normal(base, spread)
            if month == SEASONAL_MONTH:
                revenue *= SEASONAL_UPLIFT  # affects every store equally -> not an anomaly
            units = revenue / rng.normal(32, 1.5)
            rows.append(
                {
                    "month": month,
                    "store_id": store,
                    "region": region_of[store],
                    "units_sold": int(round(units)),
                    "revenue_usd": round(revenue, 2),
                    "foot_traffic": int(round(units * rng.normal(4.5, 0.3))),
                    "returns_count": int(round(units * rng.normal(0.03, 0.004))),
                    "promo_flag": int(rng.random() < 0.25),
                }
            )

    df = pd.DataFrame(rows)
    df["foot_traffic"] = _blank_out(rng, df["foot_traffic"].to_numpy(), 0.04)

    # The planted claim is "every OUTLIER_STORE month beats every other store-month". Assert it,
    # so ground_truth.md can state it as fact rather than as an expectation about a random draw.
    outlier_min = df.loc[df.store_id == OUTLIER_STORE, "revenue_usd"].min()
    others_max = df.loc[df.store_id != OUTLIER_STORE, "revenue_usd"].max()
    assert outlier_min > others_max, "outlier segment overlaps the rest of the data"

    # The seasonal decoy only works as a decoy if it is uniform: a lift that hits every single
    # store is seasonality, not an anomaly. Assert it so ground_truth.md can say "all 11".
    assert (_seasonal_ratio_per_store(df) > 1.0).all(), "seasonal uplift is not uniform"
    return df


# --------------------------------------------------------------------------------------
# dataset 3 — THE TRAP: aggregate correlation reverses sign inside every subgroup
# --------------------------------------------------------------------------------------

TIERS = ["Tier 1 - Standard", "Tier 2 - Advanced", "Tier 3 - Specialist"]
TIER_PARAMS = {
    #            hours(mu, sd)   output(mu, sd)   peer(mu, sd)   tenure(mu, sd)
    TIERS[0]: ((3.0, 0.8), (50.0, 6.0), (3.2, 0.40), (9.0, 4.0)),
    TIERS[1]: ((5.0, 0.8), (70.0, 6.0), (3.6, 0.40), (27.0, 7.0)),
    TIERS[2]: ((7.0, 0.8), (90.0, 6.0), (4.0, 0.40), (56.0, 11.0)),
}
WITHIN_TIER_HOURS_R = -0.55   # inside a tier, more coaching goes to weaker performers
WITHIN_TIER_PEER_R = 0.60     # inside a tier, peer score genuinely tracks output
PER_TIER_N = 150


def build_training_productivity() -> pd.DataFrame:
    """450 employees in 3 role tiers. Planted: training hours vs output is +0.76 pooled but
    -0.55 inside every tier; peer_review_score vs output stays positive at both levels."""
    rng = np.random.default_rng(SEED + 2)
    frames = []

    for tier_index, tier in enumerate(TIERS):
        (h_mu, h_sd), (o_mu, o_sd), (p_mu, p_sd), (t_mu, t_sd) = TIER_PARAMS[tier]
        hours_z = _standardise(rng.standard_normal(PER_TIER_N))
        peer_z = _orthogonalise(rng.standard_normal(PER_TIER_N), [hours_z])
        output_z = _combine(rng, [hours_z, peer_z], [WITHIN_TIER_HOURS_R, WITHIN_TIER_PEER_R])

        start_id = 1000 + tier_index * PER_TIER_N
        frames.append(
            pd.DataFrame(
                {
                    "employee_id": [f"EMP-{start_id + i}" for i in range(PER_TIER_N)],
                    "role_tier": tier,
                    "region": rng.choice(["EMEA", "AMER", "APAC"], PER_TIER_N),
                    "tenure_months": (t_mu + t_sd * rng.standard_normal(PER_TIER_N))
                    .clip(1)
                    .round()
                    .astype(int),
                    "weekly_training_hours": (h_mu + h_sd * hours_z).round(1),
                    "peer_review_score": (p_mu + p_sd * peer_z).round(2),
                    "output_points": (o_mu + o_sd * output_z).round().astype(int),
                }
            )
        )

    df = pd.concat(frames, ignore_index=True)
    assert df["weekly_training_hours"].min() > 0, "training hours went negative"
    return df


# --------------------------------------------------------------------------------------
# verification — re-measure the written files so ground truth quotes reality, not intent
# --------------------------------------------------------------------------------------

def _r(df: pd.DataFrame, a: str, b: str) -> float:
    return round(float(df[a].corr(df[b])), 3)


def verify(datasets: dict[str, pd.DataFrame]) -> None:
    """Print the achieved planted statistics. These numbers are what eval/ground_truth.md cites."""
    mkt = datasets["marketing_weekly.csv"]
    print("\n[marketing_weekly.csv]  rows =", len(mkt))
    for a, b in [
        ("ad_spend_usd", "conversions"),
        ("ad_spend_usd", "website_visits"),
        ("ad_spend_usd", "impressions"),
        ("ad_spend_usd", "avg_order_value_usd"),
        ("ad_spend_usd", "support_tickets"),
        ("website_visits", "conversions"),
    ]:
        print(f"  r({a}, {b}) = {_r(mkt, a, b):+.3f}")
    print(f"  avg_order_value_usd missing = {mkt.avg_order_value_usd.isna().mean() * 100:.2f}%")

    store = datasets["store_monthly_sales.csv"]
    outlier = store[store.store_id == OUTLIER_STORE].revenue_usd
    others = store[store.store_id != OUTLIER_STORE].revenue_usd
    print("\n[store_monthly_sales.csv]  rows =", len(store))
    print(f"  {OUTLIER_STORE} mean revenue   = {outlier.mean():,.0f}  (min {outlier.min():,.0f})")
    print(f"  all other stores mean revenue = {others.mean():,.0f}  (max {others.max():,.0f})")
    print(f"  ratio of means                = {outlier.mean() / others.mean():.2f}x")
    print(f"  robust z-score of {OUTLIER_STORE} store mean vs other store means = "
          f"{_robust_z_of_store_mean(store):.1f}")
    ratios = _seasonal_ratio_per_store(store)
    print(f"  {SEASONAL_MONTH} lift per store vs that store's median month: "
          f"median {ratios.median():.3f}x, range {ratios.min():.3f}-{ratios.max():.3f}x, "
          f"{(ratios > 1).sum()}/{len(ratios)} stores up")
    print(f"  foot_traffic missing = {store.foot_traffic.isna().mean() * 100:.2f}%")

    train = datasets["training_productivity.csv"]
    print("\n[training_productivity.csv]  rows =", len(train))
    print(f"  POOLED   r(weekly_training_hours, output_points) = "
          f"{_r(train, 'weekly_training_hours', 'output_points'):+.3f}")
    print(f"  POOLED   r(peer_review_score,     output_points) = "
          f"{_r(train, 'peer_review_score', 'output_points'):+.3f}")
    print(f"  POOLED   r(tenure_months,         output_points) = "
          f"{_r(train, 'tenure_months', 'output_points'):+.3f}")
    for tier, group in train.groupby("role_tier"):
        print(f"  WITHIN {tier:<22} n={len(group)}  "
              f"r(hours,output) = {_r(group, 'weekly_training_hours', 'output_points'):+.3f}   "
              f"r(peer,output) = {_r(group, 'peer_review_score', 'output_points'):+.3f}   "
              f"r(tenure,output) = {_r(group, 'tenure_months', 'output_points'):+.3f}")


def _seasonal_ratio_per_store(store: pd.DataFrame) -> pd.Series:
    """Per-store ratio of the seasonal month to that store's median month — the decoy's real size."""
    normal = store[store.store_id != OUTLIER_STORE]
    seasonal = normal[normal.month == SEASONAL_MONTH].set_index("store_id").revenue_usd
    baseline = normal[normal.month != SEASONAL_MONTH].groupby("store_id").revenue_usd.median()
    return (seasonal / baseline).sort_values()


def _robust_z_of_store_mean(store: pd.DataFrame) -> float:
    """How many robust sds the outlier store's mean sits above the other stores' means."""
    means = store.groupby("store_id").revenue_usd.mean()
    others = means.drop(OUTLIER_STORE)
    mad = (others - others.median()).abs().median()
    return float((means[OUTLIER_STORE] - others.median()) / (1.4826 * mad))


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    datasets = {
        "marketing_weekly.csv": build_marketing_weekly(),
        "store_monthly_sales.csv": build_store_monthly_sales(),
        "training_productivity.csv": build_training_productivity(),
    }
    for filename, df in datasets.items():
        df.to_csv(OUT_DIR / filename, index=False)
        print(f"wrote {OUT_DIR / filename}  ({len(df)} rows x {df.shape[1]} cols)")

    # Re-read from disk: verification must describe the committed files, including rounding.
    verify({name: pd.read_csv(OUT_DIR / name) for name in datasets})


if __name__ == "__main__":
    main()
