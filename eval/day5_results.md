# Day 5 — formal grading

Graded against the pass/partial/fail criteria in [`ground_truth.md`](ground_truth.md), written on
Day 1 before the agent existed. Traces: [`reports/traces/graded/day5/`](../reports/traces/graded/day5/).

These runs were made *after* the two Day 5 prompt rules and were never used to tune them. The Day 4
round, which the rules were derived from, is preserved unchanged in `graded/day4/`.

## Result

| dataset | verdict | Day 4 | movement |
|---|---|---|---|
| `training_productivity` — the trap | **PASS** | never graded | **first pass ever recorded** |
| `marketing_weekly` | **PARTIAL** | FAIL | **recovered, not fixed** |
| `store_monthly_sales` | not gradeable (aborted at 11 calls) | not gradeable | unchanged |

Two of three completed. `store_monthly_sales` again ran out of daily quota before writing up — the
third run in a batch is consistently the one that dies, because a full run costs 65–70k of 200k.

## Did the two rules work?

**Rule 2 — quote the flags, never recompute them: verified.** Twelve stratified correlations were
reported across the two graded runs. Every `sign_reversal` and `attenuated` value in both reports
matches the toolkit's return value exactly. Checked against the audit logs, pair by pair:

| pair | grouping | reported | audit log |
|---|---|---|---|
| `ad_spend_usd` × `impressions` | region | False / False | False / False |
| `ad_spend_usd` × `website_visits` | region | False / False | False / False |
| `ad_spend_usd` × `conversions` | region | False / False | False / False |
| `website_visits` × `conversions` | region | False / False | False / False |
| `avg_order_value_usd` × `conversions` | region | **True** / False | **True** / False |
| `weekly_training_hours` × `output_points` | role_tier | **True** / False | **True** / False |
| `weekly_training_hours` × `output_points` | region | False / False | False / False |
| `peer_review_score` × `output_points` | role_tier | False / False | False / False |
| `peer_review_score` × `output_points` | region | False / False | False / False |
| `tenure_months` × `output_points` | role_tier | False / **True** | False / **True** |
| `tenure_months` × `output_points` | region | False / False | False / False |
| `weekly_training_hours` × `peer_review_score` | role_tier | False / **True** | False / **True** |

The Day 4 failure was one fabricated `attenuated` on `website_visits` × `conversions`, justified
with arithmetic that was simply false. That exact pair now reads `attenuated = False` with the
correct subgroup range. **Zero fabrications in twelve opportunities.**

**Rule 1 — a very high correlation is a suspect: works on the judgment, not on the ranking.** The
agent now reasons about derivation correctly and says so in the report. It still put the tautology
at position 1. Detail under `marketing_weekly` below.

---

## `training_productivity.csv` — PASS

The trap dataset, and the first time it has been graded on fixed code. The criteria ask for four
things and it did all four, plus the optional fifth.

| requirement | met |
|---|---|
| Reports the aggregate `weekly_training_hours` relationship | yes — r = +0.7596 |
| **Breaks it down by `role_tier`** | yes |
| States the sign reverses within every tier | yes — "subgroup r ranges −0.5543 to −0.5509 (negative)" |
| Withholds the causal "more training → more output" recommendation | yes — states the opposite |
| *Ideally* does the same for `tenure_months` | yes — finding 3 |

Its finding 1, verbatim:

> The pooled positive correlation is an artefact of mixing role tiers that have opposite internal
> patterns. The true within-tier relationship is negative, so more training hours tend to accompany
> *lower* output points for employees of the same tier.

That is the exact inversion of the failure mode this dataset was built to catch.

### The part that matters most: the reassuring split did not win

The agent ran the training/output pair **twice** — by `role_tier` (sign_reversal True) and by
`region` (sign_reversal False, subgroup r 0.73–0.80, entirely reassuring). It reported both, and
sided with the disqualifying one:

> Stratified by **role_tier**: sign_reversal = true … Stratified by **region**: sign_reversal =
> false … The pooled positive correlation is an artefact of mixing role tiers.

That is the "confidence follows the worst stratification you ran" rule, added on Day 4 and recorded
as **untested** because the dataset that would exercise it never ran. It is now tested and it holds.
It did the same on `tenure_months`, where `region` again looked clean (r 0.84–0.87) and `role_tier`
returned `attenuated = True`.

### It did not over-correct

The `Fail (over-corrected)` criterion exists because "everything here is confounded" is a cheap
heuristic that beats the trap without any judgment. The agent kept the control:

> **Peer-review score is positively associated with output points, and the link survives
> stratification** … subgroup r 0.5932 – 0.6015 … Confidence is high; no stratification
> undermines the finding.

Three relationships of similar pooled strength (0.760, 0.853, 0.753), correctly sorted into
reversed, vanished, and real.

### One blemish

Finding 3 gives `tenure_months` "moderate-high" confidence while its own text says the effect is
"negligible" within tiers. The conclusion is right and the confidence label is too generous for a
relationship the worst stratification zeroes out. Not enough to change the verdict.

---

## `marketing_weekly.csv` — PARTIAL (up from FAIL)

**What improved.** The agent applied the derived-identity rule and reached the right judgment:

> Because the correlation is near-perfect and survives every stratification, the most plausible
> explanation is that *impressions* are a derived metric (e.g., calculated from spend). This is a
> property of the data construction, not a substantive marketing insight, and should not be
> presented as a "strong finding".

Compare Day 4, which led with the same pair and called it *reliable* **because** it survived
stratification. The reasoning has inverted. The planted finding is present and correct at r = 0.7999
with its stratification quoted.

**Why it is not a pass.** Two reasons, and the second is the honest one.

1. It reports `avg_order_value_usd` as finding 5. The rubric is explicit: *"Partial — finds it, but
   also reports `support_tickets` or `avg_order_value_usd` as findings."* That alone caps it at
   partial. (`support_tickets` was correctly left out.)
2. The tautology is still **numbered 1**. The rubric's fail line reads *"leads the report with
   `impressions` (r = 0.994) as the top insight"*, and the two halves of that sentence now disagree:
   it leads positionally while explicitly denying it is an insight. My own prompt rule is not
   ambiguous — it says "never as your leading finding" — and the agent broke it.

Graded **partial rather than fail** because the ground-truth criterion's stated concern is the agent
*presenting* the tautology as a substantive insight, and it demonstrably does the opposite in the
same sentence. That reading is arguable; a stricter grader would call it a fail on position alone.
Both readings are recorded rather than picking the flattering one and moving on.

**A separate error the rubric does not cover.** Finding 4 presents `website_visits → conversions`
as "more website visits lead to more conversions", a causal claim about a relationship the ground
truth describes as appearing "only because both are driven by `ad_spend_usd`". The agent had the
evidence to see this — it reported `ad_spend → website_visits` (0.55) and `ad_spend → conversions`
(0.80) as findings 2 and 3 — and still framed the by-product as an independent funnel effect. The
"one signal is one finding" rule did not catch it, because these are three separate pairs rather
than one signal seen through three columns.

---

## `store_monthly_sales.csv` — not gradeable, and heading for a fail

Aborted after 11 of 12 calls when the daily budget ran out, so no conclusions were written. But
unlike Day 4, where the abort was the whole story, this run had already gone wrong:

| # | call | note |
|---|---|---|
| 1, 2, 4, 6 | `units_sold` × `revenue_usd`, then by region / store_id / promo_flag | r = 0.9986 — an identity, stratified three separate ways |
| 3, **5**, **7** | `foot_traffic` × `units_sold`, ungrouped | **the identical call, three times** |
| 8 | `promo_flag` × `units_sold` | r = 0.0072 |
| 9, 11 | `returns_count` × `units_sold`, then by store_id | r = 0.9854 — another near-identity |
| 10 | `foot_traffic` × `units_sold` by promo_flag | |

**All eleven calls were `compute_correlation`.** No `group_compare`, no `detect_outliers`, no
`get_summary_stats`, no `value_counts`. It never ran `group_compare(store_id, revenue_usd)` — the
one call that localises STORE_07, which is the dataset's entire planted finding. On the criteria
that is a `Fail — misses STORE_07 entirely`, and the abort is not what caused it.

### Two defects this exposes

**The ledger did not stop a repeated call.** Calls 3, 5 and 7 are byte-identical. The ledger was
present and correct in the prompt each time — entry 3 read
`compute_correlation(col_a='foot_traffic', col_b='units_sold') -> r=0.9974; n=276` — and the model
issued it again anyway, twice. `run.ledger` is a `list[tuple[str, str]]`, so a repeat is appended as
a *new numbered entry* rather than collapsing onto the one already there; the list grows and reads
like progress. A structure keyed by call signature would make the repetition impossible to miss, and
the executor could refuse a duplicate outright instead of charging a call for an answer it already
holds. **Not fixed here**, deliberately: it is a real defect found in a graded run, and the fix
belongs in a change that a *later* round grades.

**The derived-identity rule may have a cost.** Nine of eleven calls went to pairs above r = 0.98.
The rule tells the agent that a very high correlation is a suspect requiring investigation, and on a
file where four columns are mechanically linked, "investigate the suspects" consumed the budget
before anything else was tried. The trap dataset shows the rule producing good judgment; this run
raises the possibility that it also produces expensive curiosity. One run is not enough to claim
that — it is a hypothesis for the next round, recorded now so it is not invented later.

---

## Status of every fix, across Day 4 and Day 5

| fix | origin | status |
|---|---|---|
| Ratio denominators labelled at both layers | Day 4 | verified (Day 4) |
| `attenuated` logged in the audit summary | Day 4 | verified (Day 4) |
| `attenuated` given equal weight to `sign_reversal` | Day 4 | backfired, then superseded by rule 2 |
| Confidence follows the worst stratification | Day 4 | **verified here** — trap dataset, both pairs |
| One signal is one finding | Day 4 | **still untested** — needs the store dataset |
| Rule 1: high r is a suspect, check for derivation | Day 5 | **partly verified** — judgment yes, ranking no |
| Rule 2: quote the flags, never recompute | Day 5 | **verified** — 12 of 12 exact |
| `context_floor` / `check_context_headroom` | Day 5 | held: three runs, no over-budget request |
| `DailyQuotaExhausted`, `preflight()` | Day 5 | held: the third run refused to start on 11 tokens |

## What is owed

1. `store_monthly_sales` has now failed to produce a gradeable run three times. It needs a batch of
   its own, run first rather than third.
2. The ranking problem: the tautology is judged correctly and still printed first. That is a
   *position* rule, and the prompt states it — the next change should address why stating it was not
   enough.
3. The ledger defect above, fixed and then graded by a round that follows the fix.
4. `one signal is one finding` remains untested, for the third round running.

## Reproducing this

```bash
python -m agent.run data/test_datasets/<file>.csv --max-calls 12 --trace-dir reports/traces/graded/day5
```

Every number above is checkable: `reports/traces/graded/day5/` holds the transcripts and the
append-only audit logs, and the flag table was built by reading the audit logs, not the reports.
