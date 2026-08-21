"""
run.py — the CLI: `python -m agent.run <csv> [--focus "..."]`.
Exists to wire the four pieces together in one readable place — load and profile the CSV, build the
guarded toolkit around it, drive the planning loop, write the trace — and to stream progress to the
terminal while it runs, so a long run is watchable rather than a silent wait for a file.
"""

from __future__ import annotations

import argparse
import sys
import textwrap
from pathlib import Path
from typing import Any

from agent.llm import DEFAULT_REASONING_EFFORT, GroqClient, MissingCredentials
from agent.planner_loop import CONTEXT_TOKEN_BUDGET, MAX_TURNS, TURN_HEADROOM, run_planner
from agent.trace import write_trace
from guardrails.audit import AuditLog
from guardrails.call_cap import DEFAULT_MAX_CALLS
from guardrails.executor import GuardedToolkit

DEFAULT_TRACE_DIR = Path(__file__).resolve().parent.parent / "reports" / "traces"
WIDTH = 96


def _wrap(text: str, prefix: str = "    ") -> str:
    return "\n".join(textwrap.fill(line, WIDTH, initial_indent=prefix, subsequent_indent=prefix)
                     for line in text.splitlines() if line.strip())


def make_reporter(quiet: bool):
    """Stream the run to the terminal as it happens. Events come from the loop, not from guessing.

    Every print is wrapped: the console is a progress display, and a display problem must never
    destroy a run that has already spent real API budget. The trace file is the durable record.
    """

    def report(event: str, payload: Any) -> None:
        try:
            _report(event, payload)
        except Exception as exc:                       # noqa: BLE001 - printing is best-effort
            print(f"  [could not render this event: {type(exc).__name__}]")

    def _report(event: str, payload: Any) -> None:
        if quiet:
            return
        if event == "plan":
            print("\n--- plan " + "-" * (WIDTH - 10))
            print(_wrap(payload.content.strip() or "(the model returned no plan text)"))
        elif event == "turn":
            index, response = payload
            print(f"\n--- turn {index} " + "-" * (WIDTH - 12))
            if response.content.strip():
                print(_wrap(response.content.strip()))
        elif event == "invocation":
            arguments = ", ".join(f"{k}={v!r}" for k, v in payload.arguments.items())
            status = "ok" if payload.ok else f"REFUSED [{payload.error_code}]"
            print(f"  > {payload.function}({arguments}) -> {status} "
                  f"[{payload.calls_remaining} left]")
            if not payload.ok:
                print(_wrap(payload.error or "", "      "))
            elif payload.function == "compute_correlation" and payload.data.get("group_by"):
                print(_wrap(payload.data["stratification_summary"], "      "))
        elif event == "aborted":
            print(f"\n[run aborted] {payload}")
            print("[the trace will still be written from everything gathered so far]")
        elif event == "wrapping_up":
            print(f"\n[{payload}] asking for a final write-up with no tools available")
        elif event == "finished":
            print(f"\n--- final answer " + "-" * (WIDTH - 18))
            print(payload)

    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m agent.run",
        description="Run the AutoSight planning loop over one CSV and save a reasoning trace.",
    )
    parser.add_argument("csv_path", help="the CSV to investigate")
    parser.add_argument(
        "--focus",
        default=None,
        help="optional prioritisation nudge, e.g. \"regional differences\". The agent still "
             "exercises full judgment; it is not a question to answer.",
    )
    parser.add_argument("--max-calls", type=int, default=DEFAULT_MAX_CALLS,
                        help=f"tool-call budget (default {DEFAULT_MAX_CALLS})")
    parser.add_argument("--max-turns", type=int, default=None,
                        help=f"LLM round-trip ceiling (default: max-calls + {TURN_HEADROOM}, so the "
                             f"call cap stays the binding budget)")
    parser.add_argument("--trace-dir", type=Path, default=DEFAULT_TRACE_DIR,
                        help="where to write the trace (default reports/traces/)")
    parser.add_argument("--context-budget", type=int, default=CONTEXT_TOKEN_BUDGET,
                        help=f"estimated prompt-token ceiling before the transcript is compacted "
                             f"(default {CONTEXT_TOKEN_BUDGET}; raise it on a paid Groq tier)")
    parser.add_argument("--model", default=None, help="override GROQ_MODEL")
    parser.add_argument("--reasoning-effort", default=DEFAULT_REASONING_EFFORT,
                        choices=["low", "medium", "high"])
    parser.add_argument("--quiet", action="store_true", help="suppress the live stream")
    args = parser.parse_args(argv)

    # Model output is arbitrary Unicode and a Windows console defaults to cp1252, which raises on
    # something as ordinary as a narrow no-break space. Replace rather than raise: losing a
    # character from the live view is nothing, losing a paid-for run to a print() is not.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    # The call cap is meant to be the budget that bites. Turns only backstop a non-converging loop,
    # so they scale with it rather than sitting at a fixed number below it.
    max_turns = args.max_turns or max(MAX_TURNS, args.max_calls + TURN_HEADROOM)

    csv_path = Path(args.csv_path)
    stem = csv_path.stem + ("_focused" if args.focus else "")
    trace_dir = args.trace_dir
    trace_dir.mkdir(parents=True, exist_ok=True)

    try:
        model = GroqClient(model=args.model, reasoning_effort=args.reasoning_effort)
    except MissingCredentials as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    toolkit = GuardedToolkit.from_csv(
        csv_path,
        audit_log=AuditLog(trace_dir / f"{stem}_audit.jsonl"),
        max_calls=args.max_calls,
    )

    if not args.quiet:
        print("=" * WIDTH)
        print(f" {csv_path.name} -- {toolkit.profile['row_count']} rows x "
              f"{toolkit.profile['column_count']} columns")
        print(f" model={model.model}  budget={args.max_calls} calls / {max_turns} turns  "
              f"focus={args.focus or 'none'}")
        print("=" * WIDTH)

    run = run_planner(
        toolkit,
        model,
        focus=args.focus,
        max_turns=max_turns,
        model_name=model.model,
        on_event=make_reporter(args.quiet),
        context_budget=args.context_budget,
    )

    paths = write_trace(run, trace_dir, stem)
    counts = run.audit.counts_by_outcome()
    print(f"\n{'=' * WIDTH}")
    print(f" stopped: {run.stop_reason}   turns: {len(run.turns)}   "
          f"calls: {run.cap.used}/{run.cap.limit} "
          f"({counts['allowed']} ran, {counts['rejected']} refused)   "
          f"tokens: {run.total_tokens:,}")
    if run.error:
        print(f" ABORTED: {run.error}")
    for label, path in paths.items():
        print(f" {label:<9} {path}")
    return 1 if run.error else 0


if __name__ == "__main__":
    raise SystemExit(main())
