"""Drives the planning loop with a scripted model instead of the live API.

Covers rejection feedback, the call cap firing mid-batch, malformed tool arguments, the ledger,
context trimming, and the transcript staying a valid chat sequence throughout.
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
    """A ChatModel that replays prepared responses and records what it was asked."""

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
    """The profile is in the task prompt, so no tool call is spent discovering the schema."""
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
    """The rejection message reaches the model verbatim, naming the columns that would have worked."""
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
    """Malformed JSON never reaches the executor, so it costs no budget, but the model is told."""
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
    """One tool message per tool_call id, even after the budget is exhausted."""
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
    """Assert every assistant tool_call id is answered by exactly one following tool message."""
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
    """The ledger records every call and stays intact after trimming compacts the results."""
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


def _bare_run():
    """A PlannerRun with only the fields record_calls touches, for testing the ledger alone."""
    from agent.planner_loop import PlannerRun
    return PlannerRun(source="x", focus=None, model_name="stub",
                      system_prompt="", task_prompt="", profile={})


def test_a_repeated_call_updates_its_ledger_entry_instead_of_adding_one(training_kit):
    """A repeated call updates its existing ledger entry rather than adding a numbered one."""
    from agent.planner_loop import render_ledger

    kit = training_kit(max_calls=10)
    same = {"col_a": "weekly_training_hours", "col_b": "output_points"}
    model = ScriptedModel([
        say(PLAN),
        say("a", call("compute_correlation", same, "c1")),
        say("b", call("compute_correlation", {"column": "output_points"}, "c2")),   # something else
        say("c", call("compute_correlation", same, "c3")),                          # the repeat
        say("d", call("compute_correlation", same, "c4")),                          # and again
        say("## Findings\nDone."),
    ])
    run = run_planner(kit, model, model_name="stub")

    signatures = [signature for signature, _ in run.ledger]
    assert len(signatures) == len(set(signatures))              # no duplicate entries at all
    assert sum(1 for s in signatures if "weekly_training_hours" in s) == 1
    assert kit.cap.used == 4                                    # but every attempt still cost a call

    rendered = render_ledger(run.ledger, run.repeats)
    assert "ALREADY REQUESTED 3 TIMES" in rendered


def test_a_repeat_keeps_its_original_position_and_refreshes_its_headline(training_kit):
    """A repeat keeps its original position, which is when the answer was first obtained."""
    from agent.planner_loop import ToolInvocation, record_calls
    from agent.planner_loop import PlannerRun

    run = _bare_run()
    first = ToolInvocation("c1", "value_counts", {"column": "region"}, True, 9,
                           data={"n_distinct": 4, "values": [{"value": "North"}]})
    second = ToolInvocation("c2", "get_summary_stats", {"column": "output_points"}, True, 8,
                            data={"mean": 70.0, "median": 69.0, "missing_pct": 0.0})
    again = ToolInvocation("c3", "value_counts", {"column": "region"}, True, 7,
                           data={"n_distinct": 4, "values": [{"value": "South"}]})

    record_calls(run, [first, second])
    record_calls(run, [again])

    assert [s for s, _ in run.ledger] == [
        "value_counts(column='region')", "get_summary_stats(column='output_points')"
    ]
    assert run.ledger[0][1].endswith("top=South")        # the entry is refreshed, not stale
    assert run.repeats == {"value_counts(column='region')": 2}


def test_distinct_calls_are_never_collapsed(training_kit):
    """The signature is function plus arguments, so different arguments make a distinct call."""
    from agent.planner_loop import ToolInvocation, PlannerRun, record_calls

    run = _bare_run()
    pair = {"col_a": "weekly_training_hours", "col_b": "output_points"}
    record_calls(run, [
        ToolInvocation("c1", "compute_correlation", pair, True, 9, data={"overall": {}}),
        ToolInvocation("c2", "compute_correlation", {**pair, "group_by": "role_tier"}, True, 8,
                       data={"overall": {}, "group_by": "role_tier", "sign_reversal": True,
                             "attenuated": False, "subgroup_r_min": -0.6, "subgroup_r_max": -0.5}),
        ToolInvocation("c3", "compute_correlation", {"col_a": "tenure_months",
                                                     "col_b": "output_points"}, True, 7,
                       data={"overall": {}}),
    ])
    assert len(run.ledger) == 3
    assert run.repeats == {}


def test_a_repeated_rejection_is_also_deduplicated(training_kit):
    """Rejections are charged and deduplicated too, so a retried refusal is visible as a repeat."""
    kit = training_kit(max_calls=10)
    bad = {"group_col": "employee_id", "value_col": "output_points"}
    model = ScriptedModel([
        say(PLAN),
        say("a", call("group_compare", bad, "c1")),
        say("b", call("group_compare", bad, "c2")),
        say("## Findings\nDone."),
    ])
    run = run_planner(kit, model, model_name="stub")

    assert len(run.ledger) == 1
    assert "refused" in run.ledger[0][1]
    assert run.repeats[run.ledger[0][0]] == 2


def test_the_ledger_is_rewritten_in_place_not_appended(training_kit):
    """There is one ledger message, rewritten in place rather than appended to."""
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
    # A budget large enough that the loop never trims while running, so this test controls the
    # trimming itself rather than inspecting whatever the loop happened to leave behind.
    run = run_planner(kit, model, model_name="stub", context_budget=1_000_000)
    headlines = {inv.call_id: _headline(inv) for t in run.turns for inv in t.invocations}
    assert not any("elided" in str(m.get("content")) for m in run.messages)

    full = _estimate_tokens(run.messages)
    compacted = trim_transcript(run.messages, headlines, budget=full - 100, protected=5)

    assert _estimate_tokens(compacted) < full
    assert len(compacted) == len(run.messages)          # compaction only, nothing dropped yet
    assert any("payload compacted" in str(m.get("content")) for m in compacted)
    _assert_transcript_is_well_formed(compacted)


def test_the_context_budget_covers_the_tool_schemas_too(training_kit):
    """The context estimate includes the tool schemas, which are re-sent every turn and add about
    700 tokens."""
    from agent.planner_loop import _estimate_tokens as est

    messages = [{"role": "user", "content": "x" * 400}]
    assert est(messages) < est(messages, overhead=700)
    assert est(messages, overhead=700) - est(messages) == 700


@pytest.mark.parametrize("dataset", ["marketing", "stores", "training"])
def test_every_fixture_leaves_room_for_results_after_the_fixed_overhead(loaded, dataset):
    """Every fixture leaves room for tool results once the un-droppable floor is paid for.

    Without this check, a prompt edit that pushes the floor past the budget fails silently:
    trimming has nothing it may drop and every request goes out over budget.
    """
    from agent.planner_loop import (SYSTEM_PROMPT, TOOLKIT_LIMITATIONS, build_task_prompt,
                                    check_context_headroom, context_floor, MIN_RESULT_HEADROOM)

    _, profile = loaded[dataset]
    tools = tools_for_profile(profile)
    system_prompt = SYSTEM_PROMPT.format(limitations=TOOLKIT_LIMITATIONS)
    # The working prompt, not the plan-turn one: the plan turn's written-out toolkit is sent once
    # and swapped out, so it is not part of the floor every later request has to carry.
    working = build_task_prompt(profile, None, 12, tools, include_toolkit=False)
    floor = context_floor(system_prompt, working, tools)

    assert check_context_headroom(floor) >= MIN_RESULT_HEADROOM


def test_a_run_with_no_room_for_results_refuses_before_spending_a_request(training_kit):
    """check_context_headroom raises before the first request, not at the API."""
    from agent.planner_loop import check_context_headroom, context_floor

    kit = training_kit(max_calls=4)
    tools = tools_for_profile(kit.profile)
    floor = context_floor("x" * 40_000, "task", tools)

    with pytest.raises(ValueError, match="leaves .* tokens for tool results"):
        check_context_headroom(floor)

    model = ScriptedModel([say(PLAN)])
    with pytest.raises(ValueError, match="fixed floor"):
        run_planner(kit, model, context_budget=50)
    assert model.seen == []          # not one request was sent


# --- a dying API must not take the evidence with it ---------------------------------------------

class FailingModel:
    """Replays responses, then raises, standing in for the API failing mid-run."""

    def __init__(self, responses: list[LLMResponse], error: Exception) -> None:
        self._responses = list(responses)
        self._error = error
        self.calls = 0

    def complete(self, messages, tools=None):
        self.calls += 1
        if not self._responses:
            raise self._error
        return self._responses.pop(0)


def test_an_api_failure_ends_the_run_but_keeps_everything_gathered(training_kit):
    """An API failure ends the run but keeps the plan, the calls and the flags already gathered."""
    kit = training_kit(max_calls=10)
    model = FailingModel(
        [
            say(PLAN),
            say("Stratifying.", call("compute_correlation",
                                     {"col_a": "weekly_training_hours", "col_b": "output_points",
                                      "group_by": "role_tier"}, "c1")),
        ],
        RuntimeError("Groq call failed after 4 attempts: Error code: 429 - daily limit"),
    )
    run = run_planner(kit, model, model_name="stub")

    assert run.error is not None
    assert "429" in run.error
    assert run.stop_reason == "aborted: the model call failed"

    # everything before the failure survived
    assert len(run.turns) == 2
    assert run.turns[1].invocations[0].data["sign_reversal"] is True
    assert len(run.ledger) == 1
    assert run.cap.used == 1


def test_the_trace_of_an_aborted_run_still_renders_and_says_why(training_kit):
    kit = training_kit(max_calls=10)
    model = FailingModel(
        [say(PLAN), say("Working.", call("get_summary_stats", {"column": "output_points"}, "c1"))],
        RuntimeError("Error code: 429 - tokens per day exhausted"),
    )
    run = run_planner(kit, model, model_name="stub")
    trace = render_trace(run)

    assert "aborted: the model call failed" in trace
    assert "tokens per day exhausted" in trace
    assert "cut short before the agent could write up" in trace
    assert "get_summary_stats" in trace          # the evidence is still there
    assert "## Audit log" in trace


def test_a_failure_on_the_very_first_call_still_returns_a_run(training_kit):
    kit = training_kit(max_calls=10)
    run = run_planner(kit, FailingModel([], RuntimeError("dead on arrival")), model_name="stub")

    assert run.error is not None
    assert run.turns == []
    assert render_trace(run)                     # renders rather than raising


def test_an_aborted_run_does_not_spend_a_doomed_write_up_call(training_kit):
    """An aborted run skips the write-up rather than spending a request that cannot succeed."""
    kit = training_kit(max_calls=10)
    model = FailingModel([say(PLAN), say("Working.", call("get_summary_stats",
                                                          {"column": "output_points"}, "c1"))],
                         RuntimeError("429"))
    run_planner(kit, model, model_name="stub")

    # plan, one acting turn, one failed attempt -- and no extra write-up attempt after that
    assert model.calls == 3


# --- the planning turn is told what the toolkit actually is --------------------------------------

def test_the_plan_turn_is_given_the_real_function_list(training_kit):
    """The plan turn is given the function list in prose, since it is offered no tool schemas."""
    kit = training_kit()
    model = ScriptedModel([say(PLAN), say("## Findings\nDone.")])
    run_planner(kit, model, model_name="stub")

    plan_request = "\n".join(str(m.get("content", "")) for m in model.seen[0])
    for function in ("get_summary_stats", "compute_correlation", "detect_outliers",
                     "group_compare", "value_counts"):
        assert function in plan_request
    assert model.tool_schemas[0] is None          # still no callable tools on the plan turn


def test_the_function_list_is_rendered_from_the_schemas_not_written_out(training_kit):
    """The listing is derived from to_openai_tools(profile), so a tool dropped for this file
    disappears from it too."""
    from agent.planner_loop import format_toolkit

    kit = training_kit()
    tools = [t for t in tools_for_profile(kit.profile)
             if t["function"]["name"] != "detect_outliers"]
    rendered = format_toolkit(tools, [c["name"] for c in kit.profile["columns"]])

    assert "detect_outliers" not in rendered
    assert "group_compare(group_col, value_col)" in rendered
    assert "group_by?" in rendered                     # optional arguments are marked
    assert "method?=zscore|iqr" not in rendered        # that tool was dropped, so neither is its enum


def test_a_non_column_enum_is_spelled_out_but_column_enums_are_not(training_kit):
    """Column enums are omitted, since the profile lists them directly above. `method` appears
    nowhere else, so it is written out."""
    from agent.planner_loop import format_toolkit

    kit = training_kit()
    tools = tools_for_profile(kit.profile)
    rendered = format_toolkit(tools, [c["name"] for c in kit.profile["columns"]])

    assert "method?=zscore|iqr" in rendered
    assert "output_points" not in rendered             # no column enum is repeated here


def test_the_function_list_is_dropped_once_the_real_schemas_arrive(training_kit):
    """The prose function list is dropped once the real schemas arrive, so it is not paid for
    twice on every turn."""
    kit = training_kit()
    model = ScriptedModel([
        say(PLAN),
        say("a", call("get_summary_stats", {"column": "output_points"})),
        say("## Findings\nDone."),
    ])
    run_planner(kit, model, model_name="stub")

    plan_request = model.seen[0][1]["content"]
    later_request = model.seen[-1][1]["content"]

    assert "THE TOOLKIT" in plan_request
    assert "THE TOOLKIT" not in later_request
    assert "DATASET:" in later_request                 # the profile itself stays
    assert model.tool_schemas[-1] is not None          # because the real schemas are there instead


def test_the_prompt_gives_the_other_functions_real_coverage():
    """The system prompt gives the non-correlation functions their own section, and names all
    five functions."""
    from agent.planner_loop import SYSTEM_PROMPT, TOOLKIT_LIMITATIONS

    prompt = SYSTEM_PROMPT.format(limitations=TOOLKIT_LIMITATIONS)
    for function in ("get_summary_stats", "value_counts", "group_compare", "detect_outliers"):
        assert function in prompt, f"{function} is never named in the system prompt"

    # Measured by section rather than by keyword, which is what "coverage" actually means.
    sections = dict(_sections(prompt))
    correlation = sum(len(sections[name]) for name in sections if name in (
        "STRATIFY BEFORE YOU CONCLUDE",
        "AN UNUSUALLY HIGH CORRELATION IS A SUSPECT, NOT A HEADLINE",
        "QUOTE THE FLAGS, NEVER RECOMPUTE THEM"))
    others = len(sections["THE OTHER FOUR FUNCTIONS ARE NOT A SIDESHOW"])

    assert others > 0
    # Correlation still leads, since it has the most ways to mislead, but the ratio is bounded.
    assert correlation / others < 4, f"correlation {correlation} lines vs other tools {others}"


def _sections(prompt: str) -> list[tuple[str, list[str]]]:
    """Split the system prompt on its ALL-CAPS headings."""
    import re

    found: list[tuple[str, list[str]]] = []
    for line in prompt.splitlines():
        if re.fullmatch(r"[A-Z][A-Z ,\-'\"0-9]{8,}", line.strip()):
            found.append((line.strip(), []))
        elif found and line.strip():
            found[-1][1].append(line)
    return found


# --- the duplicate guard, seen from the loop -----------------------------------------------------

def test_a_repeated_call_comes_back_as_a_rejection_the_model_can_read(training_kit):
    """A duplicate refusal reaches the model through the same path as any other rejection."""
    kit = training_kit(max_calls=10)
    same = {"column": "output_points"}
    model = ScriptedModel([
        say(PLAN),
        say("a", call("get_summary_stats", same, "c1")),
        say("b", call("get_summary_stats", same, "c2")),
        say("## Findings\nDone."),
    ])
    run = run_planner(kit, model, model_name="stub")

    repeat = run.turns[2].invocations[0]
    assert repeat.ok is False
    assert repeat.error_code == "DUPLICATE_CALL"

    tool_messages = [m for m in model.seen[-1] if m["role"] == "tool"]
    fed_back = json.loads(tool_messages[-1]["content"])          # the reply to the repeat
    assert fed_back["ok"] is False
    assert "already run" in fed_back["error"]


def test_a_refused_duplicate_does_not_erase_the_answer_in_the_ledger(training_kit):
    """A refused duplicate does not overwrite the ledger entry holding the original answer."""
    from agent.planner_loop import render_ledger

    kit = training_kit(max_calls=10)
    same = {"column": "output_points"}
    model = ScriptedModel([
        say(PLAN),
        say("a", call("get_summary_stats", same, "c1")),
        say("b", call("get_summary_stats", same, "c2")),
        say("## Findings\nDone."),
    ])
    run = run_planner(kit, model, model_name="stub")

    assert len(run.ledger) == 1
    signature, headline = run.ledger[0]
    assert "mean=" in headline and "refused" not in headline
    assert "ALREADY REQUESTED 2 TIMES" in render_ledger(run.ledger, run.repeats)


def test_the_ledger_and_the_executor_agree_on_what_one_call_is(training_kit):
    """The ledger takes the executor's own call signature, so both agree on what one call is."""
    kit = training_kit(max_calls=10)
    pair = {"col_a": "tenure_months", "col_b": "output_points"}
    model = ScriptedModel([
        say(PLAN),
        say("a", call("compute_correlation", pair, "c1")),
        say("b", call("compute_correlation", {**pair, "group_by": None}, "c2")),
        say("## Findings\nDone."),
    ])
    run = run_planner(kit, model, model_name="stub")

    assert len(run.ledger) == 1
    assert run.repeats[run.ledger[0][0]] == 2
    assert run.turns[2].invocations[0].error_code == "DUPLICATE_CALL"
