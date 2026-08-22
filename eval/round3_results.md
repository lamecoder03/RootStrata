# Round 3 — `store_monthly_sales` only

One dataset, run alone on the Round 3 code (`776b865`). `marketing_weekly` and
`training_productivity` were deliberately **not** re-run: quota allows roughly one more full run and
it is being held in reserve. Their Round 2 verdicts stand and are not re-graded here.

Trace: [`reports/traces/graded/round3/`](../reports/traces/graded/round3/). 12/12 calls, 14 turns,
68,881 tokens, model finished and wrote up.

## Verdict: PARTIAL

**This was first graded PASS and the grade was wrong.** The correction, and the reasoning on both
sides, is in the next section — it belongs at the top because the initial verdict was committed and
should not stand uncontested.

Against [`ground_truth.md`](ground_truth.md), the pass condition is met exactly:

> **Pass** — identifies `STORE_07` as an outlier segment with a magnitude (~8x), and either omits
> the November bump or labels it seasonality.

Finding 1, verbatim:

> **Revenue varies dramatically by store** – STORE 07's average monthly revenue ($398,295) is
> **8.32×** higher than the lowest-earning store (STORE 05, $47,894).

Checked against the toolkit directly: `highest STORE_07 398295, lowest STORE_05 47894, ratio 8.32`.
Exact. The November bump is not mentioned anywhere, and the pooled mean (77,867) is never presented
as typical, so neither fail condition is triggered.

**The denominator is right, too.** It says "8.32× higher than the lowest-earning store", which is
what `highest_over_lowest_ratio` actually measures. The earlier bug — reading that same 8.32 as "8.3×
the overall mean" — does not recur.

But the report also contains a second numbered finding that is the same anomaly one level coarser,
carrying a claim its own evidence refutes. That caps it at partial.

| diagnostic | Round 1 | Round 2 | Round 3 |
|---|---|---|---|
| not graded (diagnostic) | aborted, not gradeable | PARTIAL — blamed the East region | **PARTIAL** — names STORE_07 at 8.32×, then blames East anyway |

---

## Reconciling the verdict with the narrative

Two statements were made about this run and they do not sit together: that it is a clean PASS, and
that it is the first failure of "one signal is one finding". Both cannot be true of a graded report.

**The case for PASS**, which is what the letter of the rubric supports:

- The pass condition is met explicitly, with the right store, the right magnitude, and the right
  denominator.
- The rubric's partial clause reads *"finds an anomaly but misattributes it (e.g. blames the `North`
  region rather than the store), **lists the same anomaly three times**, or reports `2024-11` as an
  anomaly."* Nothing in it fires literally. It did not *mis*attribute — the store is named first and
  correctly. It listed the anomaly **twice**, not three times. November is absent.
- The capability under test — localise an anomaly to a segment, separate anomaly from seasonality —
  was demonstrated.

**The case for PARTIAL**, which is what I now think is right:

1. **"Three times" is an observation, not a threshold.** That number came from a pre-grading diagnostic
   where STORE_07 was reported across `revenue_usd`, `units_sold` and `foot_traffic` — three
   columns. It describes the failure mode; it was never meant as a count that two occurrences slip
   under. Grading on the letter while the plain intent is violated is the flattering reading, and
   this document said in the Round 2 round that the flattering reading is not the one to take.
2. **Finding 2 is not merely redundant, it is wrong.** "The difference persists across all stores"
   was never tested — no within-region store comparison was run — and call 12 refutes it directly:
   one store sits at 8.32× the lowest. A reader acting on finding 2 investigates regional market
   dynamics, pricing and product mix. The actual answer is one store. That is a materially
   misleading numbered finding in a graded report, not a stylistic blemish.
3. **Consistency with `marketing_weekly` decides it.** That run named its planted finding correctly
   and was still capped at partial *because it also reported a decoy as a finding*. The structure
   here is identical: planted finding named correctly, plus an extra numbered finding that should
   not be there. Applying a stricter standard to one dataset than the other would make the two
   grades incomparable, which defeats the point of having a fixed rubric at all.

**Verdict: PARTIAL.** The localisation is genuinely right and that is real progress from Round 2 —
recorded plainly below — but a report that names the store and then tells the reader the region is
the story has not finished the job.

**What does not change.** Every factual observation in the three answers below stands: the call
sequence, the duplicate refusal, the one-call margin, and the attribution evidence. Only the grade
moved, and it moved because of evidence already written down here rather than anything new.

---

## The three questions

### 1. Did it call `group_compare(store_id, revenue_usd)`, and early enough?

**It called it. It was call 12 of 12 — the last one it had.**

| # | call | |
|---|---|---|
| 1 | `compute_correlation(revenue_usd, units_sold)` | |
| 2 | `compute_correlation(foot_traffic, units_sold)` | |
| 3 | `compute_correlation(revenue_usd, units_sold, group_by=store_id)` | |
| **4** | `compute_correlation(foot_traffic, units_sold)` | **REFUSED — DUPLICATE_CALL** |
| 5 | `group_compare(promo_flag, revenue_usd)` | |
| 6 | `compute_correlation(foot_traffic, units_sold, group_by=store_id)` | |
| 7 | `compute_correlation(revenue_usd, units_sold, group_by=region)` | |
| 8 | `compute_correlation(foot_traffic, units_sold, group_by=region)` | |
| 9 | `group_compare(region, revenue_usd)` | |
| 10 | `group_compare(promo_flag, units_sold)` | |
| 11 | `detect_outliers(revenue_usd, iqr)` | 34 outliers |
| **12** | **`group_compare(store_id, revenue_usd)`** | **the planted finding, on the last call** |

So the answer to "before the budget ran low" is **no**. It arrived with zero budget remaining. The
localisation rests on a one-call margin: had anything earlier cost one more call, the store is never
named and this is a fail rather than a partial.

Two things follow. `detect_outliers` ran for the first time on this dataset in any round (34 of 288
rows), and the run reached the plan's item 5 — *"detect_outliers on revenue_usd ... then group_compare
by store_id or region to locate which segment"* — which existed in Round 2's plan too and was never
executed.

### 2. STORE_07, or the East-region framing again?

**STORE_07, correctly, as finding 1.** But the East-region framing is *also* still there, as
finding 2:

> **Revenue differs strongly by region** – The East region's average revenue ($165,528) is **3.41×**
> the South region's average ($48,482) … the difference persists across all stores.

STORE_07 is in East. East is 3.41× *because* STORE_07 is in it. This is the same signal reported a
second time, one level coarser, as an independent finding — and the clause "the difference persists
across all stores" is **unsupported and contradicted by its own evidence**: it never compared stores
within a region, and its own call 12 shows one store at eight times the lowest.

So the Round 2 misattribution is not repeated — the store is named first and correctly — but the
region finding was not retired once the store explained it.

**This is the first real test of "one signal is one finding", and it failed** — and it is why the
grade is PARTIAL rather than PASS. That rule has been carried as *untested* since Round 1 because the
dataset that exercises it never completed. It has now run, and the run double-counted exactly the
way the rule was written to prevent.

Two smaller defects in the same report:

- Finding 3 claims "no reversal or attenuation was observed when stratifying by store or region" for
  the promotions result. No such stratification was run — promotions were examined with
  `group_compare` only. Nothing was fabricated about a specific flag value, but it asserts a
  stratification result it does not have.
- "Checked and not reported" says "All toolkit calls made … produced information that was
  incorporated into the findings above. No call was made that failed to yield a relevant
  observation." Call 4 was refused. The section that exists to record what did not make the report
  omits the one call that produced nothing.

### 3. Which fix caused the improvement?

**One run cannot answer this, and three changes shipped together.** What follows is mechanism
evidence for each — which is weaker than attribution, and is not a substitute for it.

**The dedup fix: directly observed firing, and the arithmetic fits.**

```
- compute_correlation(col_a='foot_traffic', col_b='units_sold') -> REFUSED  (budget left: 8)
  [DUPLICATE_CALL] This exact call has already run in this session and returned: pearson_r=0.9974 ...
```

The model attempted the same repeat that consumed four calls in Round 2. It was refused once, and the
next turn moved to a different call — that signature never reappears.

| | Round 2 | Round 3 |
|---|---|---|
| attempts at `compute_correlation(foot_traffic, units_sold)` | 4 | 2 |
| calls wasted on it | 3 | 1 |
| distinct calls achieved from a 12-call budget | 9 | 11 |

Two extra distinct calls, and the planted finding was reached on call 12. Without them the run ends
around the equivalent of call 10 — before `detect_outliers` and before `group_compare(store_id, …)`.
That is a coherent causal story and it is the strongest of the three. It is still one run.

**The schema fix: observably changed the planning input, unlikely to be the cause.** Round 2's planning
reasoning guessed at the toolkit — *"maybe others? Not listed but typical toolkit includes … But we
assume these."* Round 3's opens *"Use all functions"* and enumerates all five, `get_summary_stats` and
`value_counts` included. The guessing is gone.

But it then dropped both of those from the plan ("That's more than 6 … Choose top 5") and never
called either. More importantly, **the plan was never the bottleneck**: Round 2's plan already
contained the correct item 5. Better knowledge of a toolkit cannot explain reaching a step the
previous plan also specified and simply ran out of budget before.

**The contaminated `group_compare` rewrite: the evidence points away from it.** The added text says
an aggregate is moved by its extreme members and to prefer the most specific grouping key. If that
had driven behaviour, `group_compare(store_id, …)` should have come *before* `group_compare(region,
…)`. It came after — region at call 9, store at call 12. During the investigation the agent did not
prefer the specific key.

It may have influenced the **write-up**: the store leads and the region follows. That ordering is
equally explained by ranking on magnitude, 8.32 against 3.41. And it plainly did not cause the
region finding to be retired, which is what the paragraph's logic implies.

**What this does not establish.** The three changes are confounded in one run. The mechanism
evidence separates them further than nothing does — one fix was seen firing, one was seen changing
an input that was not the bottleneck, and one is contradicted by call ordering — but none of that is
a counterfactual. A clean answer needs the run repeated with the executor guard disabled and the
prompt changes in place, and again with the reverse. That is two runs, and quota is being held.

---

## Status of the fixes

| fix | status after this run |
|---|---|
| Executor-level duplicate refusal | **works, and the model obeyed it** — refused once, never re-attempted |
| Plan turn shown the toolkit | works; guessing gone; two functions still unused |
| Tool-coverage rebalance | `detect_outliers` and `group_compare` both used heavily (5 of 12 calls) |
| `group_compare` "most specific key" line | no effect visible on call ordering |
| Rule 1 (high r is a suspect) | held — both near-identities labelled construction artefacts, at low confidence |
| Rule 2 (quote the flags) | held — all four stratified results quoted correctly |
| **One signal is one finding** | **tested for the first time, and FAILED** — the region finding duplicates the store finding, and is why this run is PARTIAL |

## What is owed

1. **The one-signal rule needs work, and it is now measurable — it is what stands between this run
   and a pass.** This run is the reproduction case:
   `group_compare(region, …)` and `group_compare(store_id, …)` on the same column, reported as two
   findings, with the coarser one carrying an unsupported "persists across all stores".
2. **Attribution**, if it matters enough to spend two runs: same dataset, one fix disabled at a time.
3. `marketing_weekly` and `training_productivity` have not been run on Round 3 code. The prompt was
   rewritten around the two rules they verify, so those verdicts are stale even though the rules
   held here.
4. The margin is one call. Twelve is not a comfortable budget for this dataset: the only finding
   that earns the partial rather than a fail arrived on the last call the agent had.

## Reproducing this

```bash
python -m agent.run data/test_datasets/store_monthly_sales.csv --max-calls 12 \
    --trace-dir reports/traces/graded/round3
```
