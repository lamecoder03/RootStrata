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
| `store_monthly_sales` | **PARTIAL** (re-run alone, see below) | not gradeable | **first verdict in three rounds** |

All three now have verdicts. `store_monthly_sales` needed a second attempt: its first, as the third
run of a batch, died on quota at 11 calls. Re-run alone on a fresh key it completed 12/12 and wrote
up. A full run costs 65–70k of the 200k daily budget, so three per day is the ceiling and the third
is always at risk.

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

## `store_monthly_sales.csv` — PARTIAL

Its first attempt, as the third run of a batch, died on quota at 11 calls. Re-run **alone** on a
fresh key it completed 12/12 and wrote up. This is the dataset's first verdict in three rounds.

**The rubric's partial clause describes what happened almost word for word.** *"Partial — finds an
anomaly but misattributes it (e.g. blames the `North` region rather than the store)."* Finding 4:

> **Revenue varies dramatically across regions.** East mean = $165,528 (highest), South mean =
> $48,482 (lowest), highest/lowest ratio = 3.41 … indicating a strong geographic performance
> disparity that warrants further business investigation (e.g. market size, product mix, pricing).

STORE_07 sits in the East region. Its 24 extreme months are what lifts East's mean to 3.4× the other
three, which sit within a few hundred dollars of each other — a tell the agent had in front of it and
read as a regional story. It never ran `group_compare(store_id, revenue_usd)` and never ran
`detect_outliers`, so STORE_07 is not named anywhere in the report. The anomaly was found, one level
too coarse.

It avoided the other two partial triggers and both fail triggers: the November bump is not reported
at all, the same anomaly is not listed three times, and the pooled mean (77,867) is never presented
as typical.

**Rule 1 fired correctly here too**, on two different pairs:

> The correlation is far above the 0.95 threshold where a near-perfect fit usually signals a derived
> relationship (e.g. revenue = units × price) … this is a property of how the file was constructed,
> not a substantive business insight.

Both near-identities (`units_sold` × `revenue_usd` at 0.9986, `foot_traffic` × `units_sold` at
0.9974) are correctly labelled construction artefacts rather than findings.

### What actually cost it the pass: four calls spent on one question

| # | call | |
|---|---|---|
| 1 | `compute_correlation(units_sold, revenue_usd)` | |
| **2, 4, 6, 8** | **`compute_correlation(foot_traffic, units_sold)`** | **the identical call, four times** |
| 3 | `compute_correlation(units_sold, revenue_usd, group_by=region)` | |
| 5 | `compute_correlation(units_sold, revenue_usd, group_by=promo_flag)` | |
| 7 | `compute_correlation(foot_traffic, units_sold, group_by=promo_flag)` | |
| 9 | `group_compare(promo_flag, units_sold)` | reached group_compare at last |
| 10 | `compute_correlation(promo_flag, revenue_usd)` | |
| 11 | `group_compare(promo_flag, revenue_usd)` | |
| 12 | `group_compare(region, revenue_usd)` | the call that produced finding 4 |

Four of twelve calls — a third of the budget — went to re-asking one question whose answer had not
changed. `group_compare(store_id, revenue_usd)` costs one call and is the entire planted finding.

**Progress against the previous attempt:** it did reach `group_compare` this time (3 calls, versus
zero last round when all 11 were `compute_correlation`). It grouped by `promo_flag` twice and
`region` once, and never by `store_id`.

### The ledger fix works, and it did not change the behaviour

The dedupe shipped before this run, and it did exactly what it was built to do — 12 calls collapsed
to 9 ledger entries, with the repeat counted and named:

```
2. compute_correlation(col_a='foot_traffic', col_b='units_sold') -> r=0.9974; n=276
   [ALREADY REQUESTED 4 TIMES - the answer did not change, and each attempt cost a call]
```

That text was in front of the model, at the top of the conversation, on every turn. It repeated the
call anyway — a fourth time after being told it had asked three times. The previous round's failure
was blamed partly on the ledger's presentation; this round removes that explanation. **Making the
repetition visible is not sufficient.** The remaining lever is enforcement rather than display: the
executor already sees every call before it runs, and could return the stored result for an exact
repeat, or refuse it the way it refuses an invalid column. That is a guardrail change, not a display
change, and it is not made here.

### A second defect this run exposed

The audit log is opened in append mode by design — nothing written may be edited or dropped — but
its **filename is keyed on the dataset, not the run**. Re-running the same CSV into the same
directory silently concatenated both runs into one file: 11 entries from the aborted attempt
followed by 12 from this one, 23 in total, with nothing marking the boundary. `generate_report.py`
replays that file to rebuild its evidence table, so it would have reported a 23-call investigation
that never happened. The two runs were separated by their 23-minute timestamp gap, and
`_fresh_audit_path` now rotates an existing log aside — renamed by the timestamp of its last entry,
never deleted — so one file is always one run.

---

## Status of every fix, across Day 4 and Day 5

| fix | origin | status |
|---|---|---|
| Ratio denominators labelled at both layers | Day 4 | verified (Day 4) |
| `attenuated` logged in the audit summary | Day 4 | verified (Day 4) |
| `attenuated` given equal weight to `sign_reversal` | Day 4 | backfired, then superseded by rule 2 |
| Confidence follows the worst stratification | Day 4 | **verified here** — trap dataset, both pairs |
| One signal is one finding | Day 4 | **untested still** — the store run reported four distinct signals, none duplicated, so nothing exercised it |
| Rule 1: high r is a suspect, check for derivation | Day 5 | **partly verified** — correct on all four identities across two datasets; ranking still wrong on marketing |
| Rule 2: quote the flags, never recompute | Day 5 | **verified** — 12 of 12 exact |
| Ledger deduplicates by call signature | Day 5 | **works, and insufficient** — repeat collapsed and counted; the model repeated it anyway |
| One audit log is one run (`_fresh_audit_path`) | Day 5 | new — found when two runs concatenated into one file |
| `context_floor` / `check_context_headroom` | Day 5 | held: three runs, no over-budget request |
| `DailyQuotaExhausted`, `preflight()` | Day 5 | held: the third run refused to start on 11 tokens |

## What is owed

1. **Enforcement, not display, for repeated calls.** The ledger now collapses and counts a repeat and
   says so in capitals; the model still spent a third of its budget re-asking one question. The
   executor is the only place that can actually stop it — return the stored result for an exact
   repeat, or refuse it the way an invalid column is refused. Either changes what a guardrail does,
   so it needs its own graded round.
2. **The ranking problem on `marketing_weekly`.** The tautology is judged correctly and printed
   first anyway. Stating "never as your leading finding" was not enough.
3. **`store_monthly_sales` needs `group_compare(store_id, …)` to be reachable.** It is one call and
   it is the whole planted finding. Two rounds running, the budget was gone before it was tried.
4. **One signal is one finding** has still never been exercised, three rounds in.

## Reproducing this

```bash
python -m agent.run data/test_datasets/<file>.csv --max-calls 12 --trace-dir reports/traces/graded/day5
```

Every number above is checkable: `reports/traces/graded/day5/` holds the transcripts and the
append-only audit logs, and the flag table was built by reading the audit logs, not the reports.
