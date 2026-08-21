# Reasoning traces

Two sets, kept apart on purpose.

## `diagnostics/` — Day 3

The first live runs. These are what the prompt and the loop were debugged *against*, so they are
not evidence of quality — they are the record of how the failures were found. They exposed:

- a bare `ratio 8.32` in the ledger being read as "8.3x the overall mean" (it is highest/lowest),
- `median_ratio_to_overall` being read as a ratio to the *mean* (it is against the median),
- `attenuated` being under-weighted against `sign_reversal` in the system prompt,
- one signal reported as several findings (STORE_07 and "the East region" are the same store),
- and an API failure destroying a run's trace entirely (`aborted-trap-run/`).

## `graded/` — Day 4

Fresh runs on the fixed code, never used for tuning. These are what `eval/day4_results.md` grades.
Same discipline as never testing on your training data: the diagnostics shaped the fixes, so they
cannot also be the evidence the fixes worked.
