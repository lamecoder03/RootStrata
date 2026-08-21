# Day 5 graded runs — not yet run

Empty on purpose. The Day 5 prompt rules are in place and tested, but the runs that would grade
them have not happened: Groq's daily token budget was still ~198,600 of 200,000 spent when they
were attempted, and it refills too slowly to fit a ~60k-token run.

The first attempt did start, and burned 7,476 tokens before dying at turn 2. Its half-traces were
not kept — a trace of a run that never reached a conclusion is not evidence of anything, and
leaving it here would put a file named `marketing_weekly_trace.md` in the folder that grading reads
from. The two guards added in response are:

- `GroqClient.preflight()` — refuses to start a run the day's budget cannot feed a single turn to,
  before any audit log or trace file is created.
- `DailyQuotaExhausted` — a daily limit fails immediately instead of being retried four times and
  reported as "failed after 4 attempts", which reads like a flaky network.

When quota is available, this folder is filled by:

```bash
python -m agent.run data/test_datasets/<file>.csv --max-calls 12 --trace-dir reports/traces/graded/day5
```

and graded in `eval/day5_results.md` against `eval/ground_truth.md`.
