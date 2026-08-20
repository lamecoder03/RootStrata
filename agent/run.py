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
from agent.planner_loop import MAX_TURNS, run_planner
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
    """Stream the run to the terminal as it happens. Events come from the loop, not from guessing."""

    def report(event: str, payload: Any) -> None:
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
    parser.add_argument("--max-turns", type=int, default=MAX_TURNS,
                        help=f"LLM round-trip ceiling (default {MAX_TURNS})")
    parser.add_argument("--trace-dir", type=Path, default=DEFAULT_TRACE_DIR,
                        help="where to write the trace (default reports/traces/)")
    parser.add_argument("--model", default=None, help="override GROQ_MODEL")
    parser.add_argument("--reasoning-effort", default=DEFAULT_REASONING_EFFORT,
                        choices=["low", "medium", "high"])
    parser.add_argument("--quiet", action="store_true", help="suppress the live stream")
    args = parser.parse_args(argv)

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
        print(f" model={model.model}  budget={args.max_calls} calls  "
              f"focus={args.focus or 'none'}")
        print("=" * WIDTH)

    run = run_planner(
        toolkit,
        model,
        focus=args.focus,
        max_turns=args.max_turns,
        model_name=model.model,
        on_event=make_reporter(args.quiet),
    )

    paths = write_trace(run, trace_dir, stem)
    counts = run.audit.counts_by_outcome()
    print(f"\n{'=' * WIDTH}")
    print(f" stopped: {run.stop_reason}   turns: {len(run.turns)}   "
          f"calls: {run.cap.used}/{run.cap.limit} "
          f"({counts['allowed']} ran, {counts['rejected']} refused)   "
          f"tokens: {run.total_tokens:,}")
    for label, path in paths.items():
        print(f" {label:<9} {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
