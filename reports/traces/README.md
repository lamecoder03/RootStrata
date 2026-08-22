# Reasoning traces

Two sets, kept apart on purpose.

## `diagnostics/` — pre-grading

The first live runs. These are what the prompt and the loop were debugged *against*, so they are
not evidence of quality — they are the record of how the failures were found. They exposed:

- a bare `ratio 8.32` in the ledger being read as "8.3x the overall mean" (it is highest/lowest),
- `median_ratio_to_overall` being read as a ratio to the *mean* (it is against the median),
- `attenuated` being under-weighted against `sign_reversal` in the system prompt,
- one signal reported as several findings (STORE_07 and "the East region" are the same store),
- and an API failure destroying a run's trace entirely (`aborted-trap-run/`).

## `graded/` — one folder per grading round

Fresh runs on fixed code, never used for tuning. Same discipline as never testing on your training
data: the runs that shaped a fix cannot also be the evidence the fix worked. Each round therefore
gets its own folder and is never overwritten, so a regression stays visible next to the run it
regressed from.

- `graded/round1/` — the first fresh round, scored in `eval/round1_results.md`. Only `marketing_weekly`
  finished; it **failed**, leading its report with the `impressions` tautology, and it is what
  exposed the two bugs the Round 2 prompt rules address. The other two aborted on the daily token cap.
- `graded/round2/` — fresh runs after the Round 2 rules (derived-identity check above r ≈ 0.95; flags
  quoted from the toolkit rather than recomputed). Scored in `eval/round2_results.md`:
  `training_productivity` **passes** — the trap dataset's first pass on fixed code —
  and `marketing_weekly` and `store_monthly_sales` are both **partial**. The store dataset needed a
  second attempt, run alone rather than third in a batch; its aborted first attempt is not kept.
