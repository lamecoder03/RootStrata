"""
test_planner_loop.py — drives the loop with a scripted model instead of Groq.
Exists because the parts of the loop most worth checking are the ones that are awkward to trigger
against a live API: a rejection being fed back for self-correction, the call cap firing mid-batch,
malformed tool arguments, and the transcript staying a valid chat sequence throughout.
"""

from __future__ import annotations

import json

import pytest

from agent.llm import LLMResponse, ToolCall, tools_for_profile
from agent.planner_loop import run_planner
from agent.trace import render_trace
from guardrails.audit import AuditLog
from guardrails.call_cap import CallCap
from guardrails.executor import GuardedToolkit


class ScriptedModel:
    """A ChatModel that replays prepared responses and remembers what it was asked."""

    def __init__(self, responses: list[LLMResponse]) -> None:
        self._responses = list(responses)
        self.seen: list[list[dict]] = []
        self.tool_schemas: list[list[dict] | None] = []

    def complete(self, messages, tools=None):
        self.seen.append([dict(m) for m in messages])
        self.tool_schemas.append(tools)
        if not self._responses:
            return LLMResponse(content="## Findings\nNothing further.", finish_reason="stop")
        return self._responses.pop(0)


def call(name: str, arguments: dict, call_id: str = "c1") -> ToolCall:
    raw = json.dumps(arguments)
    return ToolCall(id=call_id, name=name, arguments=arguments, raw_arguments=raw)


PLAN = "Plan: 1) summarise output_points, 2) correlate hours with output, 3) stratify by role_tier."


def say(content: str, *calls: ToolCall, reasoning: str = "") -> LLMResponse:
    return LLMResponse(
        content=content,
        reasoning=reasoning,
        tool_calls=tuple(calls),
        finish_reason="tool_calls" if calls else "stop",
        prompt_tokens=100,
        completion_tokens=50,
    )


@pytest.fixture
def training_kit(loaded):
    def _make(max_calls: int = 10):
        df, profile = loaded["training"]
        return GuardedToolkit(df, profile, call_cap=CallCap(max_calls), audit_log=AuditLog())
    return _make


# --- the profile is context, not a tool call ------------------------------------------------

def test_the_profile_is_handed_over_up_front(training_kit):
    """The agent should never have to spend a round trip discovering its own schema."""
    kit = training_kit()
    model = ScriptedModel([say(PLAN), say("## Findings\nDone.")])
    run = run_planner(kit, model, model_name="stub")

    task = run.messages[1]["content"]
    assert task.startswith("Here is the file.")
    for column in ("employee_id", "role_tier", "weekly_training_hours", "output_points"):
        assert column in task
    assert "identifier" in task and "categorical" in task     # roles came along too
    assert "450" in task                                       # and the row count
    assert run.cap.used == 0                                   # no call was needed to learn it


def test_the_task_prompt_names_the_usable_grouping_keys(training_kit):
    kit = training_kit()
    run = run_planner(kit, ScriptedModel([say(PLAN), say("## Findings\nDone.")]), model_name="stub")
    task = run.messages[1]["content"]

    assert "Columns usable as a grouping key" in task
    assert "role_tier" in task
    # employee_id is an identifier, so it is never offered as a grouping key
    grouping_line = next(l for l in task.splitlines() if l.startswith("Columns usable"))
    assert "employee_id" not in grouping_line


def test_a_focus_is_framed_as_a_nudge_not_a_question(training_kit):
    kit = training_kit()
    run = run_planner(kit, ScriptedModel([say(PLAN), say("## Findings\nDone.")]),
                      focus="regional differences", model_name="stub")
    task = run.messages[1]["content"]

    assert "FOCUS: regional differences" in task
    assert "prioritisation nudge, not as a question to answer" in task
    assert "still stratify before concluding" in task


def test_no_focus_is_the_default(training_kit):
    run = run_planner(training_kit(), ScriptedModel([say(PLAN), say("## Findings\nDone.")]), model_name="stub")
    assert run.focus is None
    assert "FOCUS" not in run.messages[1]["content"]


# --- the loop itself --------------------------------------------------------------------------

def test_a_plan_then_calls_then_findings_run(training_kit):
    kit = training_kit()
    model = ScriptedModel([
        say(PLAN),
        say("Plan: check training hours against output, then stratify by role_tier.",
            call("compute_correlation",
                 {"col_a": "weekly_training_hours", "col_b": "output_points"})),
        say("Strong pooled r. Stratifying before I believe it.",
            call("compute_correlation",
                 {"col_a": "weekly_training_hours", "col_b": "output_points",
                  "group_by": "role_tier"}, call_id="c2")),
        say("## Findings\n1. The pooled correlation reverses within every tier."),
    ])
    run = run_planner(kit, model, model_name="stub")

    assert run.stop_reason == "model finished"
    assert len(run.turns) == 4   # plan + two acting turns + the answer
    assert run.cap.used == 2
    assert "reverses within every tier" in run.findings

    stratified = run.turns[2].invocations[0]
    assert stratified.ok
    assert stratified.data["sign_reversal"] is True


def test_tool_results_reach_the_model_with_the_remaining_budget(training_kit):
    kit = training_kit(max_calls=6)
    model = ScriptedModel([
        say(PLAN),
        say("Checking.", call("get_summary_stats", {"column": "output_points"})),
        say("## Findings\nDone."),
    ])
    run_planner(kit, model, model_name="stub")

    tool_message = next(m for m in model.seen[-1] if m["role"] == "tool")
    payload = json.loads(tool_message["content"])
    assert payload["ok"] is True
    assert payload["calls_remaining"] == 5
    assert payload["result"]["mean"] == pytest.approx(70.0, abs=0.1)


# --- self-correction from a rejection -----------------------------------------------------------

def test_a_rejection_is_fed_back_with_the_columns_that_would_have_worked(training_kit):
    """The rejection message is the self-correction mechanism, so it must arrive verbatim."""
    kit = training_kit()
    model = ScriptedModel([
        say(PLAN),
        say("Grouping by employee to see per-person differences.",
            call("group_compare", {"group_col": "employee_id", "value_col": "output_points"})),
        say("That was an identifier. Using role_tier instead.",
            call("group_compare", {"group_col": "role_tier", "value_col": "output_points"},
                 call_id="c2")),
        say("## Findings\n1. Output rises with role tier."),
    ])
    run = run_planner(kit, model, model_name="stub")

    refused = run.turns[1].invocations[0]
    assert refused.ok is False
    assert refused.error_code == "WRONG_COLUMN_ROLE"

    fed_back = json.loads(next(m for m in model.seen[2] if m["role"] == "tool")["content"])
    assert fed_back["ok"] is False
    assert "identifier" in fed_back["error"]
    assert "role_tier" in fed_back["error"]        # the alternative it needs to recover
    assert "correct the arguments" in fed_back["hint"]

    assert run.turns[2].invocations[0].ok is True  # and the corrected call went through
    assert run.cap.used == 2                       # the rejection was charged


def test_malformed_tool_arguments_are_reported_without_charging_budget(training_kit):
    """Bad JSON never reaches the executor, so it costs nothing — but the model must be told."""
    kit = training_kit()
    broken = ToolCall(id="c1", name="get_summary_stats", arguments={},
                      raw_arguments="{oops", parse_error="Expecting property name")
    model = ScriptedModel([say(PLAN), say("Calling.", broken), say("## Findings\nDone.")])
    run = run_planner(kit, model, model_name="stub")

    invocation = run.turns[1].invocations[0]
    assert invocation.ok is False
    assert invocation.error_code == "MALFORMED_ARGUMENTS"
    assert kit.cap.used == 0
    assert len(kit.audit) == 0


# --- the ceilings -------------------------------------------------------------------------------

def test_the_call_cap_ends_the_run_and_still_produces_findings(training_kit):
    kit = training_kit(max_calls=2)
    model = ScriptedModel([
        say(PLAN),
        say("Working.", call("get_summary_stats", {"column": "output_points"}, "c1")),
        say("More.", call("get_summary_stats", {"column": "tenure_months"}, "c2")),
        say("More still.", call("get_summary_stats", {"column": "peer_review_score"}, "c3")),
        say("## Findings\n1. Partial, the budget ran out."),
    ])
    run = run_planner(kit, model, model_name="stub")

    assert run.stop_reason == "call cap reached"
    assert "budget ran out" in run.findings
    last_user = [m for m in model.seen[-1] if m["role"] == "user"][-1]
    assert "Stop investigating now" in last_user["content"]
    assert model.tool_schemas[-1] is None          # the write-up turn had no tools to reach for


def test_every_tool_call_gets_a_reply_even_after_the_budget_dies(training_kit):
    """The chat protocol needs one tool message per tool_call id; a short batch would 400."""
    kit = training_kit(max_calls=1)
    model = ScriptedModel([
        say(PLAN),
        say("Three at once.",
            call("get_summary_stats", {"column": "output_points"}, "c1"),
            call("get_summary_stats", {"column": "tenure_months"}, "c2"),
            call("get_summary_stats", {"column": "peer_review_score"}, "c3")),
        say("## Findings\nPartial."),
    ])
    run = run_planner(kit, model, model_name="stub")

    assert len(run.turns[1].invocations) == 3
    assert [i.ok for i in run.turns[1].invocations] == [True, False, False]
    _assert_transcript_is_well_formed(run.messages)


def test_the_turn_limit_also_forces_a_write_up(training_kit):
    kit = training_kit(max_calls=50)
    model = ScriptedModel(
        [say(PLAN)]
        # max_turns=3 covers the plan turn plus two acting turns, so two are enough to exhaust it
        + [say("Thinking.", call("get_summary_stats", {"column": "output_points"}, f"c{i}"))
           for i in range(2)]
        + [say("## Findings\nStopped at the turn limit.")]
    )
    run = run_planner(kit, model, max_turns=3, model_name="stub")

    assert run.stop_reason == "turn limit reached"
    assert "turn limit" in run.findings


def _assert_transcript_is_well_formed(messages: list[dict]) -> None:
    """Every assistant tool_call id must be answered by exactly one following tool message."""
    for index, message in enumerate(messages):
        if message["role"] != "assistant" or not message.get("tool_calls"):
            continue
        expected = [c["id"] for c in message["tool_calls"]]
        replies = []
        for following in messages[index + 1:]:
            if following["role"] != "tool":
                break
            replies.append(following["tool_call_id"])
        assert replies == expected, f"turn at {index} expected {expected}, got {replies}"


# --- schemas and trace --------------------------------------------------------------------------

def test_schemas_are_narrowed_to_the_loaded_file(loaded):
    _, training = loaded["training"]
    _, stores = loaded["stores"]

    training_tools = {t["function"]["name"]: t for t in tools_for_profile(training)}
    group_col = training_tools["group_compare"]["function"]["parameters"]["properties"]["group_col"]

    assert "role_tier" in group_col["enum"]
    assert "employee_id" not in group_col["enum"]        # identifier
    assert "store_id" not in group_col["enum"]           # a different file's column

    store_tools = {t["function"]["name"]: t for t in tools_for_profile(stores)}
    store_group = store_tools["group_compare"]["function"]["parameters"]["properties"]["group_col"]
    assert "store_id" in store_group["enum"]
    assert "revenue_usd" not in store_group["enum"]      # 288 distinct, over the cardinality bound


def test_a_tool_with_no_usable_column_is_not_offered():
    import pandas as pd
    from profiling.profiler import profile_dataframe

    # Text only: nothing to summarise, correlate or scan for outliers.
    profile = profile_dataframe(
        pd.DataFrame({"team": ["alpha", "beta"] * 30, "shift": ["am", "pm"] * 30}), "text.csv"
    )
    offered = {t["function"]["name"] for t in tools_for_profile(profile)}

    assert "value_counts" in offered
    assert "group_compare" not in offered      # needs a numeric value_col
    assert "detect_outliers" not in offered
    assert "compute_correlation" not in offered


def test_an_optional_parameter_with_no_candidates_is_dropped_not_emptied():
    import pandas as pd
    from profiling.profiler import profile_dataframe

    # Numeric columns only, all wide: nothing qualifies as a group_by for compute_correlation.
    frame = pd.DataFrame({f"m{i}": [float(j) + i for j in range(60)] for i in range(3)})
    tools = {t["function"]["name"]: t for t in tools_for_profile(profile_dataframe(frame, "w.csv"))}
    properties = tools["compute_correlation"]["function"]["parameters"]["properties"]

    assert "col_a" in properties
    assert "group_by" not in properties


def test_the_trace_shows_the_reversal_and_the_rejection(training_kit):
    kit = training_kit()
    model = ScriptedModel([
        say(PLAN),
        say("Plan: stratify before concluding.",
            call("group_compare", {"group_col": "employee_id", "value_col": "output_points"}),
            reasoning="The pooled number could be Simpson's."),
        say("Identifier refused. Stratifying the correlation instead.",
            call("compute_correlation",
                 {"col_a": "weekly_training_hours", "col_b": "output_points",
                  "group_by": "role_tier"}, "c2")),
        say("## Findings\n1. Reverses within every tier."),
    ])
    run = run_planner(kit, model, model_name="stub")
    trace = render_trace(run)

    assert "Reasoning trace" in trace
    assert "The pooled number could be Simpson's." in trace       # reasoning is preserved
    assert "REFUSED" in trace and "WRONG_COLUMN_ROLE" in trace    # so is the refusal
    assert "sign_reversal=True" in trace                          # and the flag that matters
    assert "## Final answer" in trace
    assert "## Audit log" in trace
    assert "System prompt" in trace                               # what it was given, in full


# --- context management: the ledger and the trimmer ---------------------------------------------

def test_the_ledger_records_every_call_and_survives_trimming(training_kit):
    """Trimming compacts old results, so the ledger is what stops the agent re-running them."""
    from agent.planner_loop import render_ledger, trim_transcript

    kit = training_kit(max_calls=10)
    model = ScriptedModel([
        say(PLAN),
        say("a", call("get_summary_stats", {"column": "output_points"}, "c1")),
        say("b", call("group_compare", {"group_col": "role_tier", "value_col": "output_points"}, "c2")),
        say("c", call("group_compare", {"group_col": "employee_id", "value_col": "output_points"}, "c3")),
        say("## Findings\nDone."),
    ])
    run = run_planner(kit, model, model_name="stub")

    signatures = [signature for signature, _ in run.ledger]
    assert len(run.ledger) == 3
    assert "get_summary_stats(column='output_points')" in signatures
    assert any("employee_id" in s for s in signatures)
    assert any("refused" in headline for _, headline in run.ledger)   # refusals are remembered too

    rendered = render_ledger(run.ledger)
    assert "Do not repeat any of these calls" in rendered
    assert "mean=" in rendered

    # Even squeezed to nothing, the ledger message is inside the protected prefix and survives.
    squeezed = trim_transcript(run.messages, {}, budget=1, protected=5)
    assert any("Do not repeat any of these calls" in str(m.get("content")) for m in squeezed)
    _assert_transcript_is_well_formed(squeezed)


def test_the_ledger_is_rewritten_in_place_not_appended(training_kit):
    """One ledger message, always current -- not a growing pile of stale ones."""
    from agent.planner_loop import LEDGER_HEADER

    kit = training_kit(max_calls=10)
    model = ScriptedModel([
        say(PLAN),
        say("a", call("get_summary_stats", {"column": "output_points"}, "c1")),
        say("b", call("get_summary_stats", {"column": "tenure_months"}, "c2")),
        say("## Findings\nDone."),
    ])
    run = run_planner(kit, model, model_name="stub")

    ledgers = [m for m in run.messages if LEDGER_HEADER in str(m.get("content", ""))]
    assert len(ledgers) == 1
    assert "output_points" in ledgers[0]["content"]
    assert "tenure_months" in ledgers[0]["content"]


def test_trimming_compacts_old_results_before_dropping_anything(training_kit):
    from agent.planner_loop import _estimate_tokens, _headline, trim_transcript

    kit = training_kit(max_calls=10)
    model = ScriptedModel(
        [say(PLAN)]
        + [say(f"s{i}", call("group_compare",
                             {"group_col": "role_tier", "value_col": "output_points"}, f"c{i}"))
           for i in range(5)]
        + [say("## Findings\nDone.")]
    )
    run = run_planner(kit, model, model_name="stub")
    headlines = {inv.call_id: _headline(inv) for t in run.turns for inv in t.invocations}

    full = _estimate_tokens(run.messages)
    compacted = trim_transcript(run.messages, headlines, budget=full - 200, protected=5)

    assert _estimate_tokens(compacted) < full
    assert len(compacted) == len(run.messages)          # compaction only, nothing dropped yet
    assert any("payload compacted" in str(m.get("content")) for m in compacted)
    _assert_transcript_is_well_formed(compacted)
