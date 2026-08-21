# Day 4 — formal grading

Graded against the pass/partial/fail criteria in [`ground_truth.md`](ground_truth.md), which were
written on Day 1, before the agent existed.

## Status: 1 of 3 datasets graded

Groq's free tier allows 200,000 tokens/day per organisation and a full run costs 50–95k. Both
available accounts were exhausted before the second and third fresh runs could finish.

| dataset | fresh run | gradeable |
|---|---|---|
| `marketing_weekly` | completed, 12/12 calls, 61,824 tokens | **yes** |
| `store_monthly_sales` | aborted at turn 12 after 11 calls (daily token cap) | no — it never wrote conclusions |
| `training_productivity` | aborted at turn 0, 0 calls | no — never started |

The two aborted runs still produced traces, which is itself the Day 3 crash-safety fix working:
before it, an API failure destroyed the whole run. `store_monthly_sales` has 11 calls of complete,
trustworthy evidence and no conclusion drawn from it.

**This document does not grade the Day 3 diagnostic traces.** Those runs are what the Day 4 fixes
were derived from, so scoring the fixes against them would be marking my own homework. They are
kept in `reports/traces/diagnostics/` as the debugging record; `reports/traces/graded/day4/` holds the
fresh runs, and only those are scored here.

---

## Result

| dataset | criterion | verdict |
|---|---|---|
| `marketing_weekly` | Leads the report with `impressions` (r = 0.994) as the top insight | **FAIL** |
| `store_monthly_sales` | — | not gradeable |
| `training_productivity` | — | not run |

One dataset graded, and it failed. It also failed **worse than the diagnostic run it was meant to
improve on**. The detail is below, because a fix that made something worse is the most useful thing
in this document.

---

## `marketing_weekly.csv` — FAIL

**Ground truth criteria.** Pass: names `ad_spend_usd` → `conversions` at roughly r = 0.8 *and* does
not present `impressions` as a substantive insight. Partial: finds it but also reports
`support_tickets` or `avg_order_value_usd` as findings. **Fail: misses it, or leads the report with
`impressions` (r = 0.994) as the top insight.**

**What it did.** The planted finding is present and correctly handled — but at position 3:

> **3. Ad spend is strongly associated with conversions, and the link survives regional
> stratification** — r = 0.7999 … subgroup correlations 0.6967 to 0.8576, no sign reversal and no
> attenuation.

That entry is exactly right. The problem is what sits above it:

> **1. Ad spend drives impressions uniformly across regions** — r = 0.9945 … subgroup correlations
> 0.9918 to 0.9951 with no sign reversal and no attenuation. This shows the relationship holds in
> every region, **so the pooled number is reliable**.

`impressions` is generated as `ad_spend_usd × 210` — a fixed CPM. The relationship is an identity.
The agent led with it, and justified leading with it on the grounds that it *survived
stratification*. That is the fail criterion, verbatim.

### A second, new error: it overrode a flag the toolkit set

Finding 2 claims `website_visits` ↔ `conversions` is attenuated:

> none reverse sign, but the correlation is **attenuated** in several regions (the strongest
> subgroup r ≈ 0.624 is less than half the pooled r)

The tool returned the opposite, and said so in plain language:

```
pooled r      : 0.4495
subgroup r    : Central 0.6242, East 0.4709, North 0.2761, South 0.1762, West 0.4984
sign_reversal : False      attenuated : False
summary       : "pooled r is +0.450 and all 5 subgroups agree in direction"
```

The stated justification is arithmetically false: 0.624 is not less than half of 0.4495 — it is
larger than the pooled value outright. The agent asserted a flag against the computed result and
invented a reason. The chart in
[`reports/generated/marketing_weekly_report.md`](../reports/generated/marketing_weekly_report.md)
shows every subgroup bar clearly above zero, with Central beyond the pooled line.

### Compared with the diagnostic run

| | Day 3 diagnostic (18 calls) | Day 4 fresh (12 calls) |
|---|---|---|
| Planted r = 0.800 found and stratified | yes, as finding #1 | yes, as finding #3 |
| `impressions` tautology | reported at #2, explicitly called *"mechanically derived from spend… adds little new insight"* | **reported at #1 as reliable** |
| `support_tickets` noise | invented a "threshold effect" | correctly reported as negligible |
| Flag contradicted | no | **yes** (fabricated attenuation) |
| Verdict | partial | **fail** |

One decoy handled better, one handled worse, and a new class of error. Net: a regression.

---

## What the Day 4 fixes did, and did not, achieve

Four fixes went in before these runs. Their status, honestly:

| fix | verified | evidence |
|---|---|---|
| Ratio denominators labelled at both layers | **yes** | Pinned by regression tests; the ledger now reads `highest/lowest=8.32; overall mean=…, median=…`, and no denominator error appears in the fresh run |
| `attenuated` given equal weight to `sign_reversal` | **backfired** | The agent now applies "attenuated" where the flag is False, with fabricated arithmetic |
| Confidence follows the worst stratification | **untested** | Requires the trap dataset, which did not run |
| One signal is one finding | **untested** | Requires the store dataset, which did not finish |
| `attenuated` logged in the audit summary | **yes** | Two regression tests; the flag now appears in every graded audit line |

### The likely mechanism behind the regression

Two of the prompt changes plausibly interact badly, and the trace supports it:

1. `attenuated` was raised to the same rhetorical weight as `sign_reversal`, with strong language
   ("exactly as disqualifying", "do not call it strong, robust, or consistent"). The agent began
   reaching for the label rather than reading the flag.
2. "Both false — the relationship survived that particular split. That is a materially stronger
   finding than an unstratified correlation of the same size, **and worth saying out loud**" gives
   a stratified-and-survived pair a promotion. A tautology survives every split perfectly, so this
   rule promotes it hardest. The agent's own justification for leading with `impressions` is a
   near-restatement of that sentence.

The second is the more serious design error, and it is mine, not the model's: I wrote a rule that
rewards surviving stratification without any counterweight for whether the relationship was
*interesting* to begin with. The Day 3 prompt had that counterweight closer to the surface.

**Not fixed in this session, deliberately.** Both diagnoses come from a single graded run. Changing
the prompt again on that basis, then grading against the same run, is the exact loop this document
exists to avoid. The next session should re-run all three on fresh quota and see whether the
regression reproduces before touching the prompt.

---

## `store_monthly_sales.csv` — not gradeable

Aborted after 11 calls. No conclusions were written, so there is nothing to score against the
criteria. What the calls show is still worth recording, because it suggests the run was heading for
a partial at best:

| # | call | note |
|---|---|---|
| 1–2 | `units_sold` ↔ `revenue_usd`, then by `region` | r = 0.9986 — mechanically an identity (revenue = units × price) |
| 4, 7 | `foot_traffic` ↔ `units_sold`, then by `region` | r = 0.9974 — also near-mechanical |
| 5 | `units_sold` ↔ `revenue_usd` by `promo_flag` | a third stratification of the same identity |
| 9–10 | `detect_outliers` on `revenue_usd` and `units_sold` | 34 outliers each — the same 34 rows seen twice |
| 11 | `group_compare(month, units_sold)` | 24 groups |

It had spent 11 of 12 calls without once running `group_compare(store_id, …)` — the call that
localises STORE_07, the dataset's entire planted finding. Calls 9 and 10 are the double-counting
pattern the "one signal is one finding" rule was written to prevent, appearing at the *investigation*
stage rather than the reporting stage: the rule tells it how to write up, not what to stop
re-measuring.

## `training_productivity.csv` — not run

The daily cap was reached before turn 1. This is the dataset that matters most and the one where
the "confidence follows the worst stratification" rule was aimed, so the central question of Day 4
is still open.

---

## What is owed

1. Fresh runs of `store_monthly_sales` and `training_productivity` on new quota, graded here.
2. A second `marketing_weekly` run to establish whether the `impressions` regression is stable or
   a single sample. One run is not a measurement.
3. Only then, a prompt change addressing the "survived stratification" over-promotion — with the
   next grading done on runs made after that change, not these.

## Reproducing this

```bash
python -m agent.run data/test_datasets/<file>.csv --max-calls 12 --trace-dir reports/traces/graded/day4
python reports/generate_report.py data/test_datasets/<file>.csv \
    --trace-dir reports/traces/graded/day4 --out-dir reports/generated
```

Every number quoted above is checkable: `reports/traces/graded/day4/` holds the full transcripts and
audit logs, and `generate_report.py` rebuilds its evidence table by replaying the audit log through
the toolkit, so a report can only ever cite a call that was actually made.
