"""Turns a finished agent run into a markdown findings report with one or two charts.

Rebuilds the evidence by replaying the run's audit log through the toolkit, which is
deterministic and involves no API call.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

# Run as a plain script, sys.path[0] is reports/ and the project packages are not importable.
# The repo root is one level up.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import matplotlib
matplotlib.use("Agg")           # no display: this writes PNG files, it never opens a window
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd              # noqa: E402

from guardrails.executor import GuardedToolkit  # noqa: E402
from guardrails.call_cap import CallCap  # noqa: E402
from guardrails.audit import AuditLog, OUTCOME_ALLOWED  # noqa: E402

# --- palette -------------------------------------------------------------------------------------
# Diverging blue/red for correlation sign, and a single-hue blue ramp for magnitude. Measured:
# worst pair CVD dE 23.8, normal-vision 31.6, both above the 8/15 floors and above 3:1 on the
# light surface.
POSITIVE = "#2a78d6"
NEGATIVE = "#d03b3b"
NEUTRAL = "#f0efec"
EMPHASIS = "#2a78d6"
RECEDED = "#b7d3f6"
INK = "#0b0b0b"
MUTED = "#898781"
GRID = "#e1e0d9"
AXIS = "#c3c2b7"
SURFACE = "#fcfcfb"

# A report carries one or two charts, never a gallery.
MAX_CHARTS = 2
# Below this spread a group comparison is not worth a chart.
MIN_INTERESTING_RATIO = 1.5

SECTION_HEADINGS = ("Findings", "Checked and not reported", "Not investigable with this toolkit")


# --- reading a finished run ------------------------------------------------------------------------

def read_findings(messages_path: Path) -> str:
    """Return the agent's final answer: the last assistant message with content in it."""
    messages = json.loads(messages_path.read_text(encoding="utf-8"))
    for message in reversed(messages):
        if message.get("role") == "assistant" and message.get("content", "").strip():
            return message["content"]
    return ""


def split_sections(findings: str) -> dict[str, str]:
    """Split the agent's answer on its required headings, tolerating stray markdown around them."""
    sections: dict[str, str] = {}
    current: str | None = None
    buffer: list[str] = []
    for line in findings.splitlines():
        heading = next((h for h in SECTION_HEADINGS if line.strip().lstrip("#").strip().rstrip(":")
                        .lower() == h.lower()), None)
        if heading and line.strip().startswith("#"):
            if current:
                sections[current] = "\n".join(buffer).strip()
            current, buffer = heading, []
        elif current:
            buffer.append(line)
    if current:
        sections[current] = "\n".join(buffer).strip()
    return sections


def replay_evidence(csv_path: Path, audit_path: Path) -> list[dict[str, Any]]:
    """Re-run the calls the audit log recorded, to recover the full result behind each one.

    The audit log stores what was called and a headline, not the whole payload. The replay is exact
    because the toolkit is deterministic over a fixed file, and it involves no model.
    """
    rows = [json.loads(line) for line in
            audit_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    allowed = [r for r in rows if r["outcome"] == OUTCOME_ALLOWED]

    # A generous cap and a throwaway log: this is a replay, not a fresh investigation.
    toolkit = GuardedToolkit.from_csv(csv_path, call_cap=CallCap(max(1, len(allowed) + 1)),
                                      audit_log=AuditLog())
    evidence = []
    for row in allowed:
        result = toolkit.call(row["function"], row["arguments"])
        if result.ok:
            evidence.append({"function": row["function"], "arguments": row["arguments"],
                             "data": result.data, "seq": row["seq"]})
    return evidence


def audit_summary(audit_path: Path) -> dict[str, Any]:
    rows = [json.loads(line) for line in
            audit_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["outcome"]] = counts.get(row["outcome"], 0) + 1
    return {"total": len(rows), "counts": counts, "rows": rows}


# --- charts ----------------------------------------------------------------------------------------

def _style(ax) -> None:
    """Apply the shared chart styling."""
    ax.set_facecolor(SURFACE)
    ax.figure.set_facecolor(SURFACE)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(AXIS)
        ax.spines[side].set_linewidth(0.8)
    ax.tick_params(colors=MUTED, labelsize=9, length=0)
    ax.grid(True, axis="x", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)


def chart_stratification(item: dict[str, Any], path: Path) -> str:
    """Plot subgroup correlations against the pooled one.

    Bars are coloured by sign, so a pooled +0.76 beside three bars at -0.55 reads as a reversal.
    """
    data = item["data"]
    groups = data["groups"]
    pooled = data["overall"]["pearson_r"]
    names = [g["group"] for g in groups]
    values = [g["pearson_r"] for g in groups]

    fig, ax = plt.subplots(figsize=(8, 0.55 * len(groups) + 2.6))
    positions = range(len(groups))
    ax.barh(list(positions), values, height=0.62,
            color=[POSITIVE if v >= 0 else NEGATIVE for v in values])

    ax.axvline(0, color=AXIS, linewidth=1.0)
    ax.axvline(pooled, color=INK, linewidth=1.4, linestyle="--")
    # Placed above the plot: anchored to a bar, this label lands on top of the bars it is meant to
    # be compared against.
    ax.annotate(f"pooled r = {pooled:+.3f}", xy=(pooled, 1.0), xycoords=("data", "axes fraction"),
                xytext=(0, 6), textcoords="offset points", color=INK, fontsize=9,
                va="bottom", ha="center", clip_on=False)

    for y, value in zip(positions, values):
        offset = 5 if value >= 0 else -5
        ax.annotate(f"{value:+.3f}", xy=(value, y), xytext=(offset, 0),
                    textcoords="offset points", va="center",
                    ha="left" if value >= 0 else "right", fontsize=9, color=INK)

    ax.set_yticks(list(positions))
    ax.set_yticklabels(names)
    ax.set_xlabel("Pearson r", color=MUTED, fontsize=9)
    span = max(0.25, max(abs(min(values + [pooled])), abs(max(values + [pooled]))) * 1.35)
    ax.set_xlim(-span, span)
    ax.set_title(f"{data['col_a']} vs {data['col_b']}, split by {data['group_by']}",
                 color=INK, fontsize=11, loc="left", pad=14)
    _style(ax)
    fig.tight_layout()
    fig.savefig(path, dpi=160, facecolor=SURFACE)
    plt.close(fig)
    return data["stratification_summary"]


def chart_group_means(item: dict[str, Any], path: Path) -> str:
    """Plot the mean per group, emphasising the extreme one, against the overall median.

    The median rather than the mean, since a runaway segment is exactly what drags a mean.
    """
    data = item["data"]
    groups = sorted(data["groups"], key=lambda g: g["mean"] or 0, reverse=True)
    names = [g["group"] for g in groups]
    values = [g["mean"] for g in groups]
    top = max(values)

    fig, ax = plt.subplots(figsize=(8, 0.42 * len(groups) + 2.4))
    positions = range(len(groups))
    ax.barh(list(positions), values, height=0.66,
            color=[EMPHASIS if v == top else RECEDED for v in values])

    median = data["overall_median"]
    ax.axvline(median, color=INK, linewidth=1.2, linestyle="--")
    ax.annotate(f"overall median {median:,.0f}", xy=(median, -0.9), xytext=(6, 0),
                textcoords="offset points", color=INK, fontsize=9, va="center")

    for y, value in zip(positions, values):
        ax.annotate(f"{value:,.0f}", xy=(value, y), xytext=(5, 0), textcoords="offset points",
                    va="center", fontsize=9, color=INK if value == top else MUTED)

    ax.set_yticks(list(positions))
    ax.set_yticklabels(names)
    ax.invert_yaxis()
    ax.set_xlabel(data["value_col"], color=MUTED, fontsize=9)
    ax.set_xlim(0, top * 1.18)
    ax.set_title(f"Mean {data['value_col']} by {data['group_col']}",
                 color=INK, fontsize=11, loc="left", pad=14)
    _style(ax)
    fig.tight_layout()
    fig.savefig(path, dpi=160, facecolor=SURFACE)
    plt.close(fig)

    ratio = data["highest_over_lowest_ratio"]
    return (f"{data['highest_group']['group']} is {ratio}x the lowest group "
            f"({data['lowest_group']['group']}); overall median {median:,.0f}.")


def pick_charts(evidence: list[dict[str, Any]]) -> list[tuple[str, dict[str, Any]]]:
    """Choose at most two charts.

    A confounded stratification outranks everything else.
    """
    stratified = [e for e in evidence
                  if e["function"] == "compute_correlation" and e["data"].get("group_by")
                  and e["data"].get("groups")]
    comparisons = [e for e in evidence
                   if e["function"] == "group_compare" and e["data"].get("groups")]

    chosen: list[tuple[str, dict[str, Any]]] = []

    flagged = [e for e in stratified if e["data"]["sign_reversal"] or e["data"]["attenuated"]]
    if flagged:
        flagged.sort(key=lambda e: (not e["data"]["sign_reversal"],
                                    -abs(e["data"]["overall"]["pearson_r"] or 0)))
        chosen.append(("stratification", flagged[0]))
    elif stratified:
        # Ranked by widest spread across subgroups rather than by strength: the strongest
        # correlation in a file is often the most mechanical one, and five bars all at 0.995 show
        # nothing.
        stratified.sort(key=lambda e: -((e["data"].get("subgroup_r_max") or 0)
                                        - (e["data"].get("subgroup_r_min") or 0)))
        chosen.append(("stratification", stratified[0]))

    spread = [e for e in comparisons
              if (e["data"].get("highest_over_lowest_ratio") or 0) >= MIN_INTERESTING_RATIO]
    pool = spread or comparisons
    if pool:
        pool.sort(key=lambda e: -(e["data"].get("highest_over_lowest_ratio") or 0))
        chosen.append(("group_means", pool[0]))

    return chosen[:MAX_CHARTS]


# --- the report -------------------------------------------------------------------------------------

def render_report(
    source: str, profile: dict[str, Any], sections: dict[str, str],
    evidence: list[dict[str, Any]], audit: dict[str, Any], charts: list[tuple[str, str, str]],
    trace_path: Path, cap_limit: int,
) -> str:
    """Assemble the markdown: findings, then the evidence they rest on, then provenance."""
    counts = audit["counts"]
    out: list[str] = [
        f"# {source} — findings report",
        "",
        f"*{profile['row_count']:,} rows x {profile['column_count']} columns. "
        f"{counts.get(OUTCOME_ALLOWED, 0)} analyses run under a {cap_limit}-call budget"
        + (f", {counts.get('rejected', 0)} calls refused by the guardrails" if counts.get("rejected")
           else "")
        + f". Generated {datetime.now():%Y-%m-%d %H:%M}.*",
        "",
        "---",
        "",
        "## Findings",
        "",
        sections.get("Findings", "_The agent returned no findings._"),
        "",
    ]

    if charts:
        out += ["---", "", "## Supporting charts", ""]
        for filename, title, caption in charts:
            out += [f"**{title}**", "", f"![{title}]({filename})", "", f"*{caption}*", ""]

    out += ["---", "", "## Evidence", "",
            "Every analysis this report rests on. Reproduced by replaying the run's audit log "
            "through the toolkit, so nothing here is a claim the agent did not actually check.",
            "", "| # | analysis | result |", "|---|---|---|"]
    for item in evidence:
        arguments = ", ".join(f"{k}={v}" for k, v in item["arguments"].items() if v is not None)
        out.append(f"| {item['seq']} | `{item['function']}({arguments})` | {_headline(item)} |")
    out.append("")

    if sections.get("Checked and not reported"):
        out += ["---", "", "## Checked and set aside", "",
                sections["Checked and not reported"], ""]

    if sections.get("Not investigable with this toolkit"):
        out += ["---", "", "## Outside what this toolkit can answer", "",
                "The agent can only call five fixed analysis functions. It recorded these as "
                "questions it could not reach rather than guessing at them:", "",
                sections["Not investigable with this toolkit"], ""]

    out += [
        "---", "", "## How this was produced", "",
        f"- **Dataset:** `{source}` — {profile['row_count']:,} rows, "
        f"{profile['column_count']} columns, {profile['duplicate_row_count']} duplicates",
        f"- **Agent:** chose and ran {counts.get(OUTCOME_ALLOWED, 0)} of a possible {cap_limit} "
        f"analyses from a fixed five-function toolkit. It never wrote or executed code.",
        f"- **Guardrails:** {audit['total']} calls attempted, "
        + ", ".join(f"{n} {outcome}" for outcome, n in sorted(counts.items())) + ".",
        f"- **Full reasoning trace:** `{trace_path}`",
        "",
    ]
    return "\n".join(out) + "\n"


def _headline(item: dict[str, Any]) -> str:
    """One cell's worth of result, with every ratio naming its own denominator."""
    data, fn = item["data"], item["function"]
    if fn == "compute_correlation":
        overall = data["overall"]
        if overall.get("pearson_r") is None:
            return overall.get("note", "undefined")
        text = f"r = {overall['pearson_r']:+.3f} ({overall['strength']}), n = {overall['n']}"
        if data.get("group_by"):
            flag = ("**sign reversal**" if data["sign_reversal"]
                    else "**attenuated**" if data["attenuated"] else "holds within subgroups")
            text += (f"; by `{data['group_by']}` subgroup r "
                     f"{data['subgroup_r_min']:+.3f}..{data['subgroup_r_max']:+.3f} — {flag}")
        return text
    if fn == "group_compare":
        return (f"{data['n_groups_total']} groups; highest {data['highest_group']['group']} "
                f"(mean {data['highest_group']['mean']:,.0f}), "
                f"highest/lowest ratio {data['highest_over_lowest_ratio']}x; "
                f"overall median {data['overall_median']:,.0f}")
    if fn == "detect_outliers":
        if data.get("note"):
            return data["note"]
        return (f"{data['method']}: {data['n_outliers']} of {data['n_present']} rows outside "
                f"[{data['lower_bound']:,.0f}, {data['upper_bound']:,.0f}]")
    if fn == "get_summary_stats":
        if data.get("note"):
            return data["note"]
        return (f"mean {data['mean']:,.2f}, median {data['median']:,.2f}, "
                f"{data['missing_pct']}% missing")
    if fn == "value_counts":
        return (f"{data['n_distinct']} distinct, most common "
                f"{data['values'][0]['value']} ({data['values'][0]['pct']}%)"
                if data.get("values") else f"{data['n_distinct']} distinct")
    return "—"


def build(csv_path: Path, trace_dir: Path, out_dir: Path, stem: str | None = None) -> Path:
    """Produce one report from one completed run."""
    stem = stem or csv_path.stem
    messages_path = trace_dir / f"{stem}_messages.json"
    audit_path = trace_dir / f"{stem}_audit.jsonl"
    trace_path = trace_dir / f"{stem}_trace.md"
    for required in (messages_path, audit_path):
        if not required.is_file():
            raise FileNotFoundError(f"no run artefact at {required} — run the agent first")

    out_dir.mkdir(parents=True, exist_ok=True)
    findings = read_findings(messages_path)
    sections = split_sections(findings)
        # An aborted run has no write-up, only a half-finished turn, so it is reported as such
        # rather than promoted into a "Findings" section the model never wrote.
    if "Findings" not in sections:
        sections = {"Findings": (
            "> **This run did not finish.** It ended before the agent wrote its conclusions, so "
            "there are no findings to report. The evidence below is what it had gathered by then "
            "and is complete and trustworthy as far as it goes — it is simply not a conclusion.")}
    evidence = replay_evidence(csv_path, audit_path)
    audit = audit_summary(audit_path)
    profile = GuardedToolkit.from_csv(csv_path, call_cap=CallCap(1)).profile

    charts: list[tuple[str, str, str]] = []
    for kind, item in pick_charts(evidence):
        filename = f"{stem}_{kind}.png"
        if kind == "stratification":
            caption = chart_stratification(item, out_dir / filename)
            title = (f"{item['data']['col_a']} vs {item['data']['col_b']}, "
                     f"split by {item['data']['group_by']}")
        else:
            caption = chart_group_means(item, out_dir / filename)
            title = f"Mean {item['data']['value_col']} by {item['data']['group_col']}"
        charts.append((filename, title, caption))

    cap_limit = max((r["seq"] for r in audit["rows"]), default=0)
    report = render_report(csv_path.name, profile, sections, evidence, audit, charts,
                           trace_path, cap_limit)
    report_path = out_dir / f"{stem}_report.md"
    report_path.write_text(report, encoding="utf-8")
    return report_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Turn a completed agent run into a markdown report with charts.")
    parser.add_argument("csv_path", type=Path, help="the CSV the run analysed")
    parser.add_argument("--trace-dir", type=Path, default=Path("reports/traces/graded"),
                        help="where the run's _messages.json and _audit.jsonl live")
    parser.add_argument("--out-dir", type=Path, default=Path("reports/generated"),
                        help="where to write the report and its charts")
    parser.add_argument("--stem", default=None, help="artefact stem (defaults to the CSV name)")
    args = parser.parse_args(argv)

    path = build(args.csv_path, args.trace_dir, args.out_dir, args.stem)
    print(f"wrote {path}")
    for png in sorted(args.out_dir.glob(f"{args.stem or args.csv_path.stem}_*.png")):
        print(f"      {png}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
