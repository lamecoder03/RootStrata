"""Writes a completed run out in readable form.

Emits a narrative markdown trace covering the reasoning, the calls and their outcomes, plus the
raw message transcript and the audit log beside it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agent.planner_loop import PlannerRun, ToolInvocation

# Reasoning text is kept but bounded, since gpt-oss can emit a lot of it.
MAX_REASONING_CHARS = 4000


def write_trace(run: PlannerRun, directory: Path, stem: str) -> dict[str, Path]:
    """Write the three artefacts for one run and return where they went."""
    directory.mkdir(parents=True, exist_ok=True)
    paths = {
        "trace": directory / f"{stem}_trace.md",
        "messages": directory / f"{stem}_messages.json",
    }
    paths["trace"].write_text(render_trace(run), encoding="utf-8")
    paths["messages"].write_text(
        json.dumps(run.messages, indent=2, default=str), encoding="utf-8"
    )
    if run.audit is not None and run.audit.path is not None:
        paths["audit"] = run.audit.path
    return paths


def render_trace(run: PlannerRun) -> str:
    """Render the narrative trace: header, inputs, every turn, the answer, the audit."""
    out: list[str] = []
    out += _header(run)
    out += _given(run)
    for turn in run.turns:
        out += _turn(turn)
    out += _findings(run)
    out += _audit(run)
    return "\n".join(out) + "\n"


def _header(run: PlannerRun) -> list[str]:
    cap = run.cap
    counts = run.audit.counts_by_outcome() if run.audit else {}
    return [
        f"# Reasoning trace — {run.source}",
        "",
        "| | |",
        "|---|---|",
        f"| file | `{run.source}` ({run.profile['row_count']} rows x "
        f"{run.profile['column_count']} cols) |",
        f"| focus | {('`' + run.focus + '`') if run.focus else '_none (unfocused run)_'} |",
        f"| model | `{run.model_name}` |",
        f"| stop reason | **{run.stop_reason}** |",
        *([f"| error | `{run.error}` |"] if run.error else []),
        f"| LLM turns | {len(run.turns)} |",
        f"| tool calls | {cap.used if cap else 0} of {cap.limit if cap else 0} budget |",
        f"| allowed / rejected | {counts.get('allowed', 0)} / "
        f"{counts.get('rejected', 0) + counts.get('capped', 0)} |",
        f"| tokens | {run.total_tokens:,} |",
        "",
    ]


def _given(run: PlannerRun) -> list[str]:
    """Render both prompts in full."""
    return [
        "---",
        "",
        "## What the agent was given",
        "",
        "<details><summary>System prompt</summary>",
        "",
        "```",
        run.system_prompt,
        "```",
        "",
        "</details>",
        "",
        "<details><summary>Task message (the profile, handed over up front rather than fetched)"
        "</summary>",
        "",
        "```",
        run.task_prompt,
        "```",
        "",
        "</details>",
        "",
    ]


# How each kind of turn is labelled in the trace: one plan, N acting turns, and either a
# self-chosen ending or a forced write-up.
TURN_LABELS = {
    "plan": "Turn {index} - planning (no tools offered on this turn)",
    "act": "Turn {index}",
    "writeup": "Turn {index} - forced write-up (budget or turn limit reached)",
}


def _turn(turn) -> list[str]:
    label = TURN_LABELS.get(turn.kind, "Turn {index}").format(index=turn.index)
    out = ["---", "", f"## {label}", ""]

    if turn.reasoning:
        reasoning = turn.reasoning.strip()
        if len(reasoning) > MAX_REASONING_CHARS:
            reasoning = reasoning[:MAX_REASONING_CHARS] + "\n\n[... reasoning truncated ...]"
        out += ["<details><summary>Model reasoning</summary>", "", "```", reasoning, "```", "",
                "</details>", ""]

    if turn.content.strip():
        out += ["**Said:**", "", turn.content.strip(), ""]

    if turn.invocations:
        out += [f"**Called {len(turn.invocations)} tool(s):**", ""]
        for invocation in turn.invocations:
            out += _invocation(invocation)
    return out


def _invocation(invocation: ToolInvocation) -> list[str]:
    arguments = ", ".join(f"{k}={v!r}" for k, v in invocation.arguments.items())
    marker = "OK" if invocation.ok else "REFUSED"
    out = [f"- `{invocation.function}({arguments})` -> **{marker}** "
           f"_(budget left: {invocation.calls_remaining})_"]

    if not invocation.ok:
        out += [f"  - `[{invocation.error_code}]` {invocation.error}"]
    else:
        for line in _summarise(invocation.function, invocation.data or {}):
            out.append(f"  - {line}")
    out.append("")
    return out


def _summarise(function: str, data: dict[str, Any]) -> list[str]:
    """Compact result lines for the trace.

    For correlations this reports the stratification flags rather than the full group list.
    """
    if function == "compute_correlation":
        overall = data.get("overall", {})
        if overall.get("pearson_r") is None:
            return [overall.get("note", "correlation undefined")]
        lines = [
            f"pooled r = {overall['pearson_r']:+.3f} ({overall['strength']}), "
            f"spearman = {overall['spearman_r']:+.3f}, n = {overall['n']}"
        ]
        if data.get("group_by"):
            groups = ", ".join(
                f"{g['group']}: {g['pearson_r']:+.3f}" for g in data.get("groups", [])[:6]
            )
            lines.append(f"by `{data['group_by']}` -> {groups}")
            lines.append(
                f"**sign_reversal={data['sign_reversal']}, attenuated={data['attenuated']}**"
            )
            lines.append(f"_{data['stratification_summary']}_")
        return lines

    if function == "detect_outliers":
        if data.get("note"):
            return [data["note"]]
        return [
            f"{data['method']}: flagged {data['n_outliers']} of {data['n_present']} rows "
            f"({data['outlier_pct']}%), fence [{data['lower_bound']:,.0f} .. "
            f"{data['upper_bound']:,.0f}]"
        ]

    if function == "group_compare":
        if data.get("note"):
            return [data["note"]]
        return [
            f"{data['n_groups_total']} groups; highest {data['highest_group']['group']} "
            f"(mean {data['highest_group']['mean']:,.1f}), lowest {data['lowest_group']['group']} "
            f"(mean {data['lowest_group']['mean']:,.1f}), highest/lowest ratio "
            f"{data['highest_over_lowest_ratio']}x",
            f"overall mean {data['overall_mean']:,.1f} vs median {data['overall_median']:,.1f}",
        ]

    if function == "get_summary_stats":
        if data.get("note"):
            return [data["note"]]
        return [
            f"mean {data['mean']:,.2f}, median {data['median']:,.2f}, "
            f"min {data['min']:,.2f}, max {data['max']:,.2f}, "
            f"missing {data['missing_pct']}%, mean/median gap {data['mean_median_gap_ratio']}"
        ]

    if function == "value_counts":
        top = ", ".join(f"{v['value']} ({v['count']})" for v in data.get("values", [])[:5])
        return [f"{data['n_distinct']} distinct, {data['missing_pct']}% missing; top: {top}"]

    return [json.dumps(data, default=str)[:300]]


def _findings(run: PlannerRun) -> list[str]:
    if run.error and not run.findings.strip():
        return [
            "---", "", "## Final answer", "",
            "**None — the run was cut short before the agent could write up.**", "",
            f"`{run.error}`", "",
            "The turns above are still the record of what it investigated and what the "
            "stratification flags told it; only the write-up is missing.", "",
        ]
    return ["---", "", "## Final answer", "",
            run.findings.strip() or "_(the model returned no findings text)_", ""]


def _audit(run: PlannerRun) -> list[str]:
    if run.audit is None:
        return []
    return [
        "---",
        "",
        "## Audit log",
        "",
        "Every call the agent attempted, including the refused ones.",
        "",
        "```",
        run.audit.format_table(width=118),
        "```",
        "",
    ]
