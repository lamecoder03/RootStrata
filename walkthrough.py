"""
walkthrough.py — drives the toolkit and guardrails by hand across all three eval datasets, mixing
legitimate calls with deliberately invalid ones and printing what actually came back.
Exists because a green test suite proves the guardrails work but shows nobody what they do; this is
the readable version. Every call goes through GuardedToolkit, so the audit log at the end is the run.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any

from guardrails.audit import AuditLog
from guardrails.call_cap import CallCapExceeded
from guardrails.executor import GuardedToolkit
from toolkit.registry import describe_toolkit, tool_names

DATA_DIR = Path(__file__).parent / "data" / "test_datasets"
AUDIT_PATH = Path(__file__).parent / "reports" / "walkthrough_audit.jsonl"
WIDTH = 92


# ---------------------------------------------------------------------------------------------
# printing helpers
# ---------------------------------------------------------------------------------------------

def banner(title: str, subtitle: str = "") -> None:
    print("\n" + "=" * WIDTH)
    print(f" {title}")
    if subtitle:
        print(f" {subtitle}")
    print("=" * WIDTH)


def section(title: str) -> None:
    print(f"\n--- {title} " + "-" * max(0, WIDTH - len(title) - 5))


def wrapped(text: str, prefix: str) -> None:
    for line in textwrap.wrap(text, WIDTH - len(prefix)):
        print(f"{prefix}{line}")


def attempt(kit: GuardedToolkit, function: str, arguments: dict[str, Any], note: str = "") -> None:
    """Make one guarded call and print it the way a reviewer would want to read it."""
    signature = ", ".join(f"{k}={v}" for k, v in arguments.items())
    print(f"\n  > {function}({signature})")
    try:
        result = kit.call(function, arguments)
    except CallCapExceeded as exc:
        print(f"      CALL CAP RAISED -- {exc}")
        return

    if result.ok:
        for line in render(function, result.data):
            print(f"      {line}")
    else:
        print(f"      REFUSED [{result.error_code}]")
        wrapped(result.error, "        ")
    if note:
        wrapped(note, "      | ")


def render(function: str, data: dict[str, Any]) -> list[str]:
    """Turn one tool result into a few readable lines. Dispatch by tool, since shapes differ."""
    if function == "compute_correlation":
        return _render_correlation(data)
    if function == "detect_outliers":
        return _render_outliers(data)
    if function == "group_compare":
        return _render_group_compare(data)
    if function == "get_summary_stats":
        return _render_summary(data)
    if function == "value_counts":
        return _render_value_counts(data)
    return [str(data)]


def _render_correlation(data: dict[str, Any]) -> list[str]:
    overall = data["overall"]
    lines = [
        f"pearson r = {overall['pearson_r']:+.3f} ({overall['strength']}, {overall['direction']})"
        f"   spearman r = {overall['spearman_r']:+.3f}   n = {overall['n']}"
    ]
    if data.get("group_by") is None:
        return lines

    lines.append(f"stratified by {data['group_by']}: {data['n_groups_analysed']} subgroups")
    for group in data["groups"]:
        lines.append(f"    {group['group']:<24} r = {group['pearson_r']:+.3f}   n = {group['n']}")
    verdict = "SIGN REVERSAL" if data["sign_reversal"] else (
        "ATTENUATED" if data["attenuated"] else "holds within subgroups"
    )
    lines.append(f"flags: sign_reversal={data['sign_reversal']}  attenuated={data['attenuated']}"
                 f"  -> {verdict}")
    lines.append(f"summary: {data['stratification_summary']}")
    return lines


def _render_outliers(data: dict[str, Any]) -> list[str]:
    if data.get("note"):
        return [f"no outliers computed: {data['note']}"]
    lines = [
        f"method={data['method']}  flagged {data['n_outliers']} of {data['n_present']} rows "
        f"({data['outlier_pct']}%)",
        f"fence: [{data['lower_bound']:,.0f} .. {data['upper_bound']:,.0f}]"
        f"   flagged range: {data['outlier_value_min']:,.0f} .. {data['outlier_value_max']:,.0f}"
        if data["n_outliers"] else "no rows outside the fence",
    ]
    for example in data["examples"][:4]:
        lines.append(f"    row {example['row_index']:>4}   value {example['value']:>12,.0f}"
                     f"   score {example['score']:+.2f}")
    if data["truncated"]:
        lines.append(f"    ... {data['n_outliers'] - data['n_examples_returned']} more not shown "
                     f"(output is bounded)")
    return lines


def _render_group_compare(data: dict[str, Any]) -> list[str]:
    lines = [
        f"{data['n_groups_total']} groups over {data['n_rows_used']} rows   "
        f"overall mean {data['overall_mean']:,.0f} / median {data['overall_median']:,.0f}",
        f"{'group':<22}{'n':>5}{'mean':>14}{'median':>14}{'x overall median':>18}",
    ]
    head = data["groups"][:5]
    tail = data["groups"][-2:] if len(data["groups"]) > 7 else []
    for group in head:
        lines.append(
            f"    {group['group']:<18}{group['n']:>5}{group['mean']:>14,.0f}"
            f"{group['median']:>14,.0f}{group['median_ratio_to_overall_median']:>18.2f}"
        )
    if tail:
        lines.append(f"    ... {data['n_groups_returned'] - len(head) - len(tail)} more ...")
        for group in tail:
            lines.append(
                f"    {group['group']:<18}{group['n']:>5}{group['mean']:>14,.0f}"
                f"{group['median']:>14,.0f}{group['median_ratio_to_overall_median']:>18.2f}"
            )
    if data["truncated"]:
        lines.append(f"    ({data['truncation_note']})")
    lines.append(
        f"highest={data['highest_group']['group']}  lowest={data['lowest_group']['group']}  "
        f"ratio={data['highest_over_lowest_ratio']}x"
    )
    return lines


def _render_summary(data: dict[str, Any]) -> list[str]:
    if data.get("note"):
        return [data["note"]]
    return [
        f"n={data['n_present']} present, {data['n_missing']} missing ({data['missing_pct']}%)",
        f"mean {data['mean']:,.2f}   median {data['median']:,.2f}   std {data['std']:,.2f}",
        f"min {data['min']:,.2f}   p25 {data['p25']:,.2f}   p75 {data['p75']:,.2f}   "
        f"max {data['max']:,.2f}",
        f"mean/median gap ratio = {data['mean_median_gap_ratio']}",
    ]


def _render_value_counts(data: dict[str, Any]) -> list[str]:
    lines = [f"{data['n_distinct']} distinct values, {data['n_missing']} missing "
             f"({data['missing_pct']}%)"]
    for item in data["values"][:6]:
        lines.append(f"    {item['value']:<24}{item['count']:>6}  {item['pct']:>6.2f}%")
    if data["truncated"]:
        lines.append(f"    ... {data['other_distinct']} more values, {data['other_count']} rows "
                     f"(output is bounded)")
    return lines


def show_schema(kit: GuardedToolkit) -> None:
    """Print the runtime schema — literally the thing the validator checks arguments against."""
    print("\n  runtime schema (what validation is checked against):")
    for column in kit.profile["columns"]:
        print(f"    {column['name']:<24}{column['role']:<14}"
              f"{column['distinct_count']:>6} distinct{column['missing_pct']:>8}% missing")


# ---------------------------------------------------------------------------------------------
# the three files
# ---------------------------------------------------------------------------------------------

def marketing(audit: AuditLog) -> None:
    banner("FILE 1/3 -- marketing_weekly.csv",
           "planted: a genuine correlation, r = +0.800 (eval/ground_truth.md)")
    kit = GuardedToolkit.from_csv(DATA_DIR / "marketing_weekly.csv", audit_log=audit, max_calls=40)
    show_schema(kit)

    section("legitimate calls")
    attempt(kit, "value_counts", {"column": "region"},
            "Orientation. 5 regions, evenly balanced -- a usable grouping key.")
    attempt(kit, "get_summary_stats", {"column": "ad_spend_usd"})
    attempt(kit, "compute_correlation", {"col_a": "ad_spend_usd", "col_b": "conversions"},
            "This is the planted finding. r = +0.800, exactly as ground_truth.md records.")
    attempt(kit, "compute_correlation", {"col_a": "ad_spend_usd", "col_b": "impressions"},
            "DECOY. The strongest correlation in the file and completely uninformative: "
            "impressions are generated as spend x a fixed CPM. An agent that ranks findings by "
            "|r| alone leads its report with this.")
    attempt(kit, "compute_correlation", {"col_a": "ad_spend_usd", "col_b": "support_tickets"},
            "DECOY. Pure Poisson noise. The toolkit calls it negligible so the agent has no "
            "excuse for reporting it.")
    attempt(kit, "get_summary_stats", {"column": "avg_order_value_usd"},
            "The only notable thing about this column is that 6.92% of it is missing.")

    section("calls the guardrails refuse")
    attempt(kit, "get_summary_stats", {"column": "region"},
            "Right function, wrong kind of column. Note the message names columns that WOULD "
            "work -- that is what lets the agent correct itself instead of giving up.")
    attempt(kit, "compute_correlation", {"col_a": "region", "col_b": "week_start"},
            "Correlating two non-numeric columns. Caught on col_a before pandas is touched.")
    attempt(kit, "compute_correlation", {"col_a": "conversions", "col_b": "conversions"},
            "r = 1.0 by definition. A finding that is really just an identity.")
    attempt(kit, "group_compare", {"group_col": "week_start", "value_col": "conversions"},
            "The interesting rejection: week_start has the right ROLE (datetime) but 52 distinct "
            "values. Role says what a column is; cardinality says whether the operation means "
            "anything. You need both checks.")
    attempt(kit, "get_summary_stats", {"column": "ad_spend"},
            "A plausible typo. The validator suggests the real column name back.")


def stores(audit: AuditLog) -> None:
    banner("FILE 2/3 -- store_monthly_sales.csv",
           "planted: STORE_07 at ~8.2x every other store; a uniform Nov-2024 seasonal decoy")
    kit = GuardedToolkit.from_csv(DATA_DIR / "store_monthly_sales.csv", audit_log=audit, max_calls=40)
    show_schema(kit)

    section("legitimate calls")
    attempt(kit, "get_summary_stats", {"column": "revenue_usd"},
            "Mean 77.9k against a median of 48.5k. That gap is the first hint that one segment "
            "is dragging the average -- quoting this mean as 'typical store revenue' is the "
            "documented way to fail this dataset.")
    attempt(kit, "detect_outliers", {"column": "revenue_usd", "method": "zscore"},
            "Z-score finds 18 rows, all of them STORE_07 -- but STORE_07 has 24 extreme months. "
            "Its own 24 rows inflate the standard deviation enough to hide 6 of themselves. That "
            "is masking, and it is why the toolkit offers a second method.")
    attempt(kit, "detect_outliers", {"column": "revenue_usd", "method": "iqr"},
            "IQR catches all 24 STORE_07 months -- and 10 more rows, 9 of which are the Nov-2024 "
            "seasonal bump. Neither method alone separates the anomaly from the seasonality.")
    attempt(kit, "group_compare", {"group_col": "store_id", "value_col": "revenue_usd"},
            "This is the call that localises it: STORE_07, 8.3x the lowest store. Outlier "
            "detection says THAT rows are extreme; group_compare says WHICH segment they are.")
    attempt(kit, "group_compare", {"group_col": "month", "value_col": "revenue_usd"},
            "And this is the decoy in the open: 2024-11 is up across the board. Uniform across "
            "every store and confined to one calendar month is the signature of seasonality, "
            "not an incident.")

    section("calls the guardrails refuse")
    attempt(kit, "value_counts", {"column": "revenue_usd"},
            "Numeric, so the role check passes -- but 288 distinct values in 288 rows makes the "
            "output meaningless. The cardinality bound is the only thing that catches this.")
    attempt(kit, "detect_outliers", {"column": "store_id"},
            "Outliers in a label column is not a question the toolkit can answer.")
    attempt(kit, "detect_outliers", {"column": "revenue_usd", "method": "isolation_forest"},
            "A real algorithm, just not one on the allowlist. This is the shape of the whole "
            "design: if it is not in the registry, the agent cannot reach it.")
    attempt(kit, "group_compare", {"group_col": "region", "value_col": "store_id"},
            "Grouping key is fine; you cannot average a store label.")
    attempt(kit, "run_sql", {"query": "SELECT * FROM stores"},
            "Not a function at all. The refusal lists the five that exist -- the agent's entire "
            "action space, enumerable in one line.")


def training(audit: AuditLog) -> None:
    banner("FILE 3/3 -- training_productivity.csv  [THE TRAP]",
           "planted: r = +0.760 pooled that reverses to -0.55 inside every subgroup")
    kit = GuardedToolkit.from_csv(DATA_DIR / "training_productivity.csv", audit_log=audit,
                                  max_calls=40)
    show_schema(kit)

    section("legitimate calls -- the trap, sprung and then caught")
    attempt(kit, "compute_correlation",
            {"col_a": "weekly_training_hours", "col_b": "output_points"},
            "Taken alone this is a strong, clean, actionable-looking result. An agent that stops "
            "here writes 'increase training to raise output' and is exactly wrong.")
    attempt(kit, "compute_correlation",
            {"col_a": "weekly_training_hours", "col_b": "output_points", "group_by": "role_tier"},
            "One extra argument, opposite conclusion. Every tier is NEGATIVE: within a tier, "
            "coaching hours go to the weaker performers. The pooled positive was manufactured by "
            "pooling three tiers with different means.")
    attempt(kit, "compute_correlation",
            {"col_a": "tenure_months", "col_b": "output_points", "group_by": "role_tier"},
            "The second trap, and the highest |r| in the file. This one does not reverse -- it "
            "VANISHES. tenure_months is just a proxy for role_tier.")
    attempt(kit, "compute_correlation",
            {"col_a": "peer_review_score", "col_b": "output_points", "group_by": "role_tier"},
            "The control, and the reason the trap cannot be beaten by cynicism. This "
            "relationship is real: positive pooled AND positive in all three tiers. An agent "
            "that has learned to call everything confounded fails here just as hard.")

    section("calls the guardrails refuse")
    attempt(kit, "group_compare", {"group_col": "employee_id", "value_col": "output_points"},
            "Grouping by a primary key: 450 groups of one row each. The profiler tagged "
            "employee_id as an identifier at load time, so this never reaches pandas.")
    attempt(kit, "compute_correlation",
            {"col_a": "weekly_training_hours", "col_b": "output_points",
             "group_by": "employee_id"},
            "Same mistake in the argument that matters most -- stratifying by an identifier "
            "would give one row per group and a correlation of nothing.")
    attempt(kit, "get_summary_stats", {"column": "role_tier"})
    attempt(kit, "get_summary_stats", {"column": "output_points", "limit": 10_000},
            "An argument the spec never declared. Nothing rides in on **kwargs.")


# ---------------------------------------------------------------------------------------------
# the cap, and the record
# ---------------------------------------------------------------------------------------------

def call_cap_demo(audit: AuditLog) -> None:
    banner("THE CALL CAP", "budget of 3, then a fourth attempt")
    kit = GuardedToolkit.from_csv(DATA_DIR / "marketing_weekly.csv", audit_log=audit, max_calls=3)

    print("\n  Note the second call is invalid. It is still charged -- if rejection were free, an")
    print("  agent emitting garbage would loop forever without ever reaching its ceiling.\n")

    for function, arguments in [
        ("value_counts", {"column": "region"}),
        ("get_summary_stats", {"column": "region"}),        # rejected, still costs budget
        ("get_summary_stats", {"column": "conversions"}),
        ("compute_correlation", {"col_a": "ad_spend_usd", "col_b": "conversions"}),
    ]:
        print(f"  cap: used={kit.cap.used}/{kit.cap.limit}, remaining={kit.cap.remaining}")
        try:
            result = kit.call(function, arguments)
            verdict = "ok" if result.ok else f"refused [{result.error_code}]"
            print(f"    {function}(...) -> {verdict}")
        except CallCapExceeded as exc:
            print(f"    {function}(...) -> RAISED CallCapExceeded: {exc}")
            print("\n  It raised. It did not return False for a caller to forget to check --")
            print("  that is the entire point of the guarantee.")


def show_audit(audit: AuditLog) -> None:
    banner("THE AUDIT LOG", f"every attempted call in this run, including the refused ones")
    print()
    print(audit.format_table(width=118))
    counts = audit.counts_by_outcome()
    print()
    wrapped(
        f"{counts['allowed']} calls ran, {counts['rejected']} were refused by the validator and "
        f"{counts['capped']} by the call cap. A log with only the {counts['allowed']} successes "
        f"in it would hide everything the agent tried to do -- which is the half that matters "
        f"when you are deciding whether to trust it.",
        "  ",
    )
    if audit.path is not None:
        print(f"\n  written to: {audit.path}")


def main() -> None:
    banner("RootStrata -- toolkit and guardrail walkthrough",
           "The fixed action space, and the three things that constrain it")
    wrapped(
        "Every call below goes through GuardedToolkit.call(), which charges the call cap, "
        "validates the arguments against the loaded file's real profile, runs the tool, and "
        "appends to the audit log. There is no second path.",
        "  ",
    )
    print(f"\n  The allowlist -- {len(tool_names())} functions, the agent's entire action space:\n")
    for line in describe_toolkit().splitlines():
        print(f"    {line}".rstrip())

    audit = AuditLog(AUDIT_PATH)
    marketing(audit)
    stores(audit)
    training(audit)
    call_cap_demo(audit)
    show_audit(audit)
    print()


if __name__ == "__main__":
    main()
