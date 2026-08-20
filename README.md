# AutoSight — Autonomous Dataset Insight Agent

Point it at a CSV it has never seen. It decides what is worth investigating, investigates,
and writes a markdown findings report with supporting charts.

> **Status: Day 1 — scaffold.** The profiler and eval fixtures work. The toolkit, guardrails
> and agent loop are not written yet.

---

## The design decision

**The agent can only call a fixed set of pre-built analysis functions. It never writes or
executes free-form code.**

The obvious alternative — let the LLM emit pandas and `exec()` it — was rejected deliberately.
Sandboxing LLM-authored code properly needs process isolation, a filesystem jail, syscall
filtering and resource limits; anything less (AST denylists, scrubbing `__builtins__`) is
routinely bypassed and amounts to security theatre. That is a bigger project than this one,
so the action space is bounded instead of policed.

What the allowlist buys:

- The complete set of things the agent can do is **enumerable and reviewable before it ever
  sees a file** — not "any Python".
- The audit log records `correlation_matrix(cols=[...])`, a reviewable decision, rather than a
  blob of generated code.
- A bad answer traces to a bad *choice of tool*, not to subtly wrong generated code that ran.

The cost is accepted openly: the agent is **less flexible** than a code-writing agent and can
only ask questions the toolkit can answer. Same principle as allowlisting tables in a SQL
agent — except every CSV has a different schema, so the allowlist is validated against the
**CSV's runtime schema** discovered by the profiler, not against a fixed table list.

## Guardrails

1. **Function allowlist, validated against the runtime schema.** Column arguments must name
   columns that actually exist, with a dtype the operation accepts.
2. **A hard call cap that raises.** Exceeding the tool-call budget throws. It does not return
   `False` — a limit a caller can forget to check is not a limit.
3. **An append-only audit log.** Every *attempted* call is recorded with its arguments and
   whether it was allowed, including the rejected ones.

## Quickstart

```bash
pip install -r requirements.txt
cp .env.example .env        # then add your GROQ_API_KEY

python data/generate_test_datasets.py                          # rebuild the eval fixtures
python -m profiling.profiler data/test_datasets/marketing_weekly.csv
python -m profiling.profiler data/test_datasets/marketing_weekly.csv --json
```

## Layout

```
data/
  generate_test_datasets.py   seeded, reproducible fixture generator
  test_datasets/              the three eval CSVs (committed)
profiling/profiler.py         any CSV -> row count, dtypes, missing %, roles, stats
toolkit/                      the fixed allowlisted analysis functions   (Day 2)
guardrails/                   allowlist, call cap, audit log             (Day 2)
agent/                        hand-written planning loop over Groq       (Day 3)
reports/                      generated reports and charts (gitignored)
eval/ground_truth.md          exactly what is planted in each dataset
```

## Eval datasets

Three synthetic CSVs with precisely documented planted findings — see
[`eval/ground_truth.md`](eval/ground_truth.md).

| dataset | planted | tests |
|---|---|---|
| `marketing_weekly.csv` | `ad_spend` -> `conversions`, r = **+0.800** | Finding a real signal without leading with a tautological r = 0.994 decoy |
| `store_monthly_sales.csv` | `STORE_07` at **8.17x** every other store | Localising an anomaly, and not flagging a uniform seasonal bump as one |
| `training_productivity.csv` | r = **+0.760** pooled, **-0.55** within every tier | **The trap.** Judgment, not pattern-matching |

The third dataset also contains a genuine correlation that *survives* the same breakdown, so
the trap cannot be beaten by reflexively calling everything confounded.

## Stack

pandas / numpy / scipy / matplotlib, and Groq (`openai/gpt-oss-120b`) through its
OpenAI-compatible endpoint. No database. No LangChain — the agent loop is hand-written so it
can be explained line by line.

## Roadmap

- [x] **Day 1** — scaffold, profiler, eval fixtures, ground truth
- [ ] **Day 2** — the toolkit and the three guardrails
- [ ] **Day 3** — the agent loop, report writer, charts, and grading against ground truth
