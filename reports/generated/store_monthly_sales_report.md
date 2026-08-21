# store_monthly_sales.csv — findings report

*288 rows x 8 columns. 11 analyses run under a 11-call budget. Generated 2026-08-21 16:56.*

---

## Findings

> **This run did not finish.** It ended before the agent wrote its conclusions, so there are no findings to report. The evidence below is what it had gathered by then and is complete and trustworthy as far as it goes — it is simply not a conclusion.

---

## Supporting charts

**foot_traffic vs units_sold, split by region**

![foot_traffic vs units_sold, split by region](store_monthly_sales_stratification.png)

*pooled r is +0.997 and all 4 subgroups agree in direction (subgroup range +0.882 to +0.997)*

**Mean units_sold by month**

![Mean units_sold by month](store_monthly_sales_group_means.png)

*2024-11 is 1.58x the lowest group (2024-05); overall median 1,520.*

---

## Evidence

Every analysis this report rests on. Reproduced by replaying the run's audit log through the toolkit, so nothing here is a claim the agent did not actually check.

| # | analysis | result |
|---|---|---|
| 1 | `compute_correlation(col_a=units_sold, col_b=revenue_usd)` | r = +0.999 (strong), n = 288 |
| 2 | `compute_correlation(col_a=units_sold, col_b=revenue_usd, group_by=region)` | r = +0.999 (strong), n = 288; by `region` subgroup r +0.893..+0.999 — holds within subgroups |
| 3 | `group_compare(group_col=promo_flag, value_col=units_sold)` | 2 groups; highest 1 (mean 2,488), highest/lowest ratio 1.02x; overall median 1,520 |
| 4 | `compute_correlation(col_a=foot_traffic, col_b=units_sold)` | r = +0.997 (strong), n = 276 |
| 5 | `compute_correlation(col_a=units_sold, col_b=revenue_usd, group_by=promo_flag)` | r = +0.999 (strong), n = 288; by `promo_flag` subgroup r +0.997..+0.999 — holds within subgroups |
| 6 | `group_compare(group_col=promo_flag, value_col=revenue_usd)` | 2 groups; highest 1 (mean 80,344), highest/lowest ratio 1.04x; overall median 48,540 |
| 7 | `compute_correlation(col_a=foot_traffic, col_b=units_sold, group_by=region)` | r = +0.997 (strong), n = 276; by `region` subgroup r +0.882..+0.997 — holds within subgroups |
| 8 | `compute_correlation(col_a=returns_count, col_b=units_sold)` | r = +0.985 (strong), n = 288 |
| 9 | `detect_outliers(column=revenue_usd, method=iqr)` | iqr: 34 of 288 rows outside [36,040, 61,836] |
| 10 | `detect_outliers(column=units_sold, method=iqr)` | iqr: 34 of 288 rows outside [1,048, 2,013] |
| 11 | `group_compare(group_col=month, value_col=units_sold)` | 24 groups; highest 2024-11 (mean 3,539), highest/lowest ratio 1.58x; overall median 1,520 |

---

## How this was produced

- **Dataset:** `store_monthly_sales.csv` — 288 rows, 8 columns, 0 duplicates
- **Agent:** chose and ran 11 of a possible 11 analyses from a fixed five-function toolkit. It never wrote or executed code.
- **Guardrails:** 11 calls attempted, 11 allowed.
- **Full reasoning trace:** `reports\traces\graded\store_monthly_sales_trace.md`

