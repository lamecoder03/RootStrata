# Ground Truth — planted findings in the eval datasets

Every number below was **measured from the committed CSVs**, not from the generator's intent.
Regenerate with `python data/generate_test_datasets.py` (seed `20240817`); output is
byte-identical, so these figures stay valid.

| file | md5 |
|---|---|
| `marketing_weekly.csv` | `1c27255a27bb053d78d4fe80943d5888` |
| `store_monthly_sales.csv` | `3570018a5ed968a745334083770537a9` |
| `training_productivity.csv` | `29d28b72338c9b34bbf89103ed8d214d` |

Correlations are built by orthogonal projection rather than by sampling, so the *sample*
correlation hits its target to 3 decimal places — `r = 0.800` is the actual value in the file,
not an expectation.

Each dataset below lists: what was planted, what was planted **as a decoy**, and how to grade.

---

## 1. `marketing_weekly.csv` — a genuine, strong correlation

260 rows (52 weeks x 5 regions), 8 columns. Tests the base case: can the agent find a real
relationship and describe its strength honestly?

### Planted — should be found

| relationship | measured r | note |
|---|---|---|
| `ad_spend_usd` -> `conversions` | **+0.800** | **THE planted finding.** The headline the report should lead with. |
| `ad_spend_usd` -> `website_visits` | +0.550 | Genuine but clearly weaker. A secondary finding at most. |

### Planted as decoys — should NOT be headline findings

| column / pair | measured r | why it is a decoy |
|---|---|---|
| `ad_spend_usd` -> `impressions` | **+0.994** | The **strongest** correlation in the file and completely uninteresting: impressions are generated as `ad_spend x 210` (a fixed CPM), so this is a definitional identity, not a discovery. An agent that ranks by \|r\| alone leads with this. |
| `ad_spend_usd` -> `support_tickets` | -0.109 | Pure Poisson noise, correlated with nothing. Reporting this as a finding is a false positive. |
| `ad_spend_usd` -> `avg_order_value_usd` | +0.006 | Deliberately zero. Its only notable property is **6.92% missing** values. |
| `website_visits` -> `conversions` | +0.449 | Not planted directly — it appears only because both are driven by `ad_spend_usd`. Should not be presented as an independent effect. |

### Grading

- **Pass** — names `ad_spend_usd` -> `conversions` with a strength of roughly 0.8, and does not
  present `impressions` as a substantive insight.
- **Partial** — finds it, but also reports `support_tickets` or `avg_order_value_usd` as findings.
- **Fail** — misses it, or leads the report with `impressions` (r = 0.994) as the top insight.

---

## 2. `store_monthly_sales.csv` — one segment far outside the rest

288 rows (12 stores x 24 months), 8 columns. Tests outlier detection *and* whether the agent
can tell an anomaly apart from a plausible seasonal effect.

### Planted — should be found

**`STORE_07` is the outlier segment.**

| statistic | value |
|---|---|
| `STORE_07` mean monthly revenue | **398,295** |
| All other 11 stores, mean monthly revenue | **48,737** |
| Ratio | **8.17x** |
| `STORE_07` *lowest* month | 343,564 |
| All other stores' *highest* month | 79,097 |
| Overlap between the two ranges | **none — separation is total** |
| Robust z-score of `STORE_07`'s store mean vs the other store means | **436** |

The lift also appears in `units_sold` and `foot_traffic`, because both are derived from
revenue. That is **one** underlying anomaly, not three — a report listing it three times has
padded its findings.

Distortion this causes (a secondary check): pooled `revenue_usd` mean is **77,867** but the
median is **48,540**. An agent that quotes the mean as "typical store revenue" has been fooled
by a single segment.

### Planted as a decoy — should NOT be reported as an anomaly

**The `2024-11` bump.** Every store's revenue was multiplied by 1.40 in that one month.

| statistic | value |
|---|---|
| Non-outlier stores up in `2024-11` vs their own median month | **11 of 11** |
| Median lift | 1.461x |
| Range across stores | 1.247x – 1.737x |

It is large enough that a naive month-level anomaly detector will flag it. But it is **uniform
across every store and confined to one calendar month** — that is the signature of seasonality
(a November promotion), not of an incident. Correct handling is to mention it as seasonal
context, or to omit it; calling it an anomaly requiring investigation is the trap.

Also present: `foot_traffic` is **4.17% missing**.

### Grading

- **Pass** — identifies `STORE_07` as an outlier segment with a magnitude (~8x), and either
  omits the November bump or labels it seasonality.
- **Partial** — finds an anomaly but misattributes it (e.g. blames the `North` region rather
  than the store), lists the same anomaly three times, or reports `2024-11` as an anomaly.
- **Fail** — misses `STORE_07` entirely, or reports the pooled mean (77,867) as typical.

---

## 3. `training_productivity.csv` — THE TRAP (Simpson's paradox)

450 rows (3 role tiers x 150 employees), 7 columns. This dataset is not about detection — the
correlations are trivially easy to find. It is about **judgment**: does the agent check whether
a strong aggregate relationship survives being broken down by an obvious subgroup?

The dataset deliberately contains **two confounded relationships and one genuine one**, all of
similar aggregate strength, so they cannot be told apart by `r` alone.

### Trap A — sign reversal (the headline trap)

| level | measured r (`weekly_training_hours` vs `output_points`) |
|---|---|
| **Pooled (all 450 rows)** | **+0.760** |
| Within `Tier 1 - Standard` (n=150) | **-0.551** |
| Within `Tier 2 - Advanced` (n=150) | **-0.551** |
| Within `Tier 3 - Specialist` (n=150) | **-0.554** |

**Mechanism:** `role_tier` drives both variables. Tier means are 3 / 5 / 7 training hours and
50 / 70 / 90 output points, so pooling the tiers manufactures a strong positive slope. Inside
any single tier the true relationship is **negative** — coaching hours are allocated to the
weaker performers.

**The failure mode this is designed to catch:** a report that says *"training hours strongly
predict output (r = 0.76); increase training to raise output."* That recommendation is exactly
backwards relative to the within-tier data.

### Trap B — disappearance (the strongest correlation in the file)

| level | measured r (`tenure_months` vs `output_points`) |
|---|---|
| **Pooled** | **+0.853** |
| Within `Tier 1 - Standard` | -0.024 |
| Within `Tier 2 - Advanced` | -0.078 |
| Within `Tier 3 - Specialist` | +0.012 |

This is the **highest \|r\| in the dataset**, so a rank-by-strength agent will lead with it.
`tenure_months` is a proxy for `role_tier` (tier means: 9 / 27 / 56 months). The effect does
not reverse here — it **vanishes to zero**. Both failure shapes are therefore represented.

### Control — a genuine relationship that survives stratification

| level | measured r (`peer_review_score` vs `output_points`) |
|---|---|
| **Pooled** | **+0.753** |
| Within `Tier 1 - Standard` | **+0.593** |
| Within `Tier 2 - Advanced` | **+0.602** |
| Within `Tier 3 - Specialist` | **+0.597** |

This one is real: positive at both levels, and its within-tier strength is stable. The control
exists so the trap cannot be beaten by a cheap heuristic. An agent that learns to say *"all
correlations here are confounded"* is not being careful, it is being useless — and it fails
this dataset just as surely as the naive one does.

### Other columns

- `region` (EMEA / AMER / APAC) — pure noise, roughly balanced, related to nothing.
- `employee_id` — a unique identifier for all 450 rows. Must never be used as a grouping key or
  correlated against anything. (The profiler labels it `identifier` for exactly this reason.)

### Grading

- **Pass** — reports the aggregate `weekly_training_hours` relationship, **breaks it down by
  `role_tier`**, states that the sign reverses within every tier, and withholds the causal
  "more training -> more output" recommendation. Ideally does the same for `tenure_months`.
- **Partial** — mentions confounding as a generic caveat without actually computing the
  stratified numbers. The right instinct, no evidence.
- **Fail (naive)** — reports r = 0.76 or r = 0.85 as an actionable causal finding.
- **Fail (over-corrected)** — dismisses `peer_review_score` as confounded too, or refuses to
  state any finding at all.

---

## Summary — what the three datasets test

| dataset | capability under test | the way to fail it |
|---|---|---|
| `marketing_weekly` | Find a real signal; rank findings by importance, not by \|r\| | Leading with the tautological `impressions` correlation |
| `store_monthly_sales` | Localise an anomaly to a segment; separate anomaly from seasonality | Flagging the uniform November bump as an incident |
| `training_productivity` | Judgment: stratify before claiming an effect — and know when *not* to | Recommending more training; or dismissing every correlation |

The first two are answerable by pattern-matching. **The third is not**, which is why it is the
dataset that actually measures whether this agent is worth anything.
