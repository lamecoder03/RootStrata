"""
planner_loop.py — the hand-written planning loop: hand the model the file's profile up front, let it
plan investigations, run each through the guarded toolkit, feed results back, stop when it says so.
Exists because this loop is the explainable part of the project — no framework, so every message, every
rejection fed back for self-correction and every stopping condition is visible in one file.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable

from agent.llm import ChatModel, LLMResponse, ToolCall, assistant_message, tools_for_profile
from guardrails.audit import AuditLog
from guardrails.call_cap import CallCap, CallCapExceeded
from guardrails.executor import GuardedToolkit
from profiling.profiler import format_profile

# LLM round trips, which is not the same budget as the tool-call cap: one turn can request several
# calls, and a turn that only thinks costs no calls at all. Both ceilings are needed.
#
# This is a backstop against a loop that never converges, NOT the real budget - the call cap is.
# In practice the model issues about one call per turn, so a turn ceiling near the call cap makes
# turns the binding constraint and cuts investigations short for the wrong reason. run.py therefore
# defaults it to comfortably above the call budget; this value only applies when nothing else does.
MAX_TURNS = 14
# Headroom over the call cap: the plan turn, the final write-up, and a few thinking turns.
TURN_HEADROOM = 6

# The transcript grows by a full tool result every turn, and Groq's free tier refuses a single
# request larger than its whole per-minute token allowance (8000). So the loop manages its own
# context: older tool payloads are compacted to their headline numbers, and if that is not enough,
# the oldest exchanges are replaced by a note saying what ran.
#
# 4000 is calibrated, not guessed: the run that hit the limit was refused at 8074 real tokens and
# this estimator scored that same transcript around 5450, so char/4 under-counts by roughly 1.4x.
# 4000 estimated is about 5600 real, leaving room for a reasoning-heavy completion under the 8000 cap.
CONTEXT_TOKEN_BUDGET = 4000
# The most recent results stay in full — those are the ones the next decision depends on.
KEEP_FULL_RESULTS = 3
# Rough enough: this only decides *when* to compact, and compacting early is cheap.
CHARS_PER_TOKEN = 4

# What the five functions genuinely cannot do. Stated in the prompt so the agent declares a gap
# instead of inventing an answer — the time-series absence is the one it will hit most often.
TOOLKIT_LIMITATIONS = """\
- No time-series or trend analysis. There is no function for a value over time, no seasonality
  decomposition, no change-point detection and no forecasting. Grouping a measure by a date column
  with group_compare gives you per-period averages, but that is a comparison, not a trend.
- No regression and no multivariate analysis. You can stratify a correlation by ONE grouping column
  at a time; you cannot control for two variables at once or fit a model.
- No hypothesis testing beyond the p-value that comes back with a correlation.
- No filtering or slicing. Every function runs over all rows of the file. You cannot analyse a
  subset except through the grouping that group_compare and group_by already give you.
- No derived columns. You cannot compute a ratio, a difference, a percentage change or any other
  new value from existing columns.
- Grouping keys are capped at 30 distinct values, so a wide column (dates at daily or weekly grain,
  identifiers) cannot be used to group even when that is the question you want to ask."""

BEGIN_PROMPT = """\
Good. The toolkit is now available to you.

Work through that plan with tool calls. Revise it as results arrive - follow a surprising result,
drop a dead end. Remember to stratify a correlation before concluding anything about it, and to
localise any outliers you find before drawing a conclusion from them.

You may request several independent calls in one turn; there is no reason to wait a full round trip
between two questions that do not depend on each other.

A running list of every call you have already made, with its headline result, is kept at the top of
this conversation. Consult it before calling anything: repeating a call you have already made spends
budget and tells you nothing new. When further calls would not change your conclusions, reply with
no tool calls and your final answer under the required headings."""

LEDGER_HEADER = """\
CALLS ALREADY MADE IN THIS RUN, with their headline results. This list is complete and is kept up
to date for you. Do not repeat any of these calls - you already have the answer. Older results
further down the conversation may have been compacted to save space; this list is the record."""

SYSTEM_PROMPT = """\
You are an autonomous data analyst. You have one CSV file and a fixed toolkit of five analysis
functions. Your job is to decide what is worth investigating in this file, investigate it, and
report what you actually found.

HOW YOU WORK

1. You already have the file's complete schema profile: column names, inferred roles, distinct
   counts, missingness and numeric summary statistics. Do not ask for it and do not spend a tool
   call rediscovering it. Plan from what is in front of you.
2. In your first message, write a short plan: three to six specific things worth checking in THIS
   file. Name the actual columns involved in each and say why the question is worth asking. Ground
   it in the roles and cardinalities you were given, not in what a file like this usually contains.
3. Then work the plan with tool calls. Revise as results arrive: follow a surprising result, drop
   a dead end.
4. Stop when further calls would not change your conclusions, and write up what you found.

JUDGMENT - THIS IS THE PART THAT MATTERS

A number is not a finding. Deciding which numbers mean something is the entire job.

- When you find a correlation worth reporting and the file has a plausible grouping column, re-run
  compute_correlation with group_by set BEFORE concluding anything about it. An unstratified
  correlation is not yet evidence.
- If a result comes back with "sign_reversal": true, the pooled correlation is an artefact of that
  grouping: the relationship runs the OTHER WAY inside the subgroups. Your conclusion must change
  accordingly. Report the within-group direction as the real one, name the grouping column as the
  confounder, and do not recommend acting on the pooled number.
- If "attenuated": true, the relationship largely vanishes within subgroups. It is explained by the
  grouping variable rather than by the two columns you correlated. Say so.
- If both flags are false, the relationship survived stratification. That makes it a materially
  stronger finding than an unstratified correlation of the same size, and worth saying out loud.
- Not every strong correlation is a finding. Ask whether one column is mechanically derived from
  the other, and whether the relationship is too self-evident to deserve a reader's attention.
- detect_outliers tells you THAT some rows are extreme. It does not tell you what they are. Use
  group_compare to find which segment or period they belong to before drawing any conclusion from
  them. Consider whether an effect that shows up everywhere at once is really an anomaly.
- Keep what you measured separate from what you are inferring. You cannot test causation here.

WHEN A CALL IS REJECTED

Rejections are normal, and they are informative. The error names the columns that WOULD have worked
for that parameter, or the values that argument accepts. Read it and issue a corrected call. Do not
repeat a rejected call unchanged, and do not abandon a question because the first attempt was
refused. Rejected calls are charged against your budget, so read the message rather than guessing
again.

WHAT THIS TOOLKIT CANNOT DO

{limitations}

If an investigation you judge worthwhile needs something on that list, do not guess at the answer
and do not quietly drop the thread. Record it under "Not investigable with this toolkit", saying
what you would have checked and what was missing.

BUDGET

Every tool result tells you how many calls remain. When the budget is nearly gone, stop
investigating and write up what you have.

YOUR FINAL ANSWER

When you are finished, reply with no tool calls, using exactly these headings:

## Findings
Numbered, most important first. For each: what you found, the specific numbers supporting it, how
confident you are, and any caveat a reader needs. Where stratification changed your reading of a
relationship, say so explicitly.

## Checked and not reported
What you investigated that did not earn a place in the findings, with one line on why.

## Not investigable with this toolkit
Questions this file raises that the five functions cannot answer. Omit this section if empty."""


@dataclass(frozen=True)
class ToolInvocation:
    """One attempted call and what came back, as the trace will show it."""

    call_id: str
    function: str
    arguments: dict[str, Any]
    ok: bool
    calls_remaining: int
    data: dict[str, Any] | None = None
    error: str | None = None
    error_code: str | None = None


@dataclass(frozen=True)
class Turn:
    """One LLM round trip: what it thought, what it said, and what it asked to run."""

    index: int
    reasoning: str
    content: str
    invocations: tuple[ToolInvocation, ...]
    prompt_tokens: int
    completion_tokens: int
    kind: str = "act"   # "plan" | "act" | "writeup"


@dataclass
class PlannerRun:
    """Everything about one run, kept whole so the trace can show how it got there."""

    source: str
    focus: str | None
    model_name: str
    system_prompt: str
    task_prompt: str
    profile: dict[str, Any]
    turns: list[Turn] = field(default_factory=list)
    messages: list[dict[str, Any]] = field(default_factory=list)
    findings: str = ""
    stop_reason: str = ""
    # (call signature, headline result) for every call made, in order. Never trimmed: this is what
    # stops the agent re-running work whose full result has since been compacted out of the window.
    ledger: list[tuple[str, str]] = field(default_factory=list)
    audit: AuditLog | None = None
    cap: CallCap | None = None

    @property
    def total_tokens(self) -> int:
        return sum(t.prompt_tokens + t.completion_tokens for t in self.turns)

    @property
    def rejections(self) -> list[ToolInvocation]:
        return [inv for turn in self.turns for inv in turn.invocations if not inv.ok]


def build_task_prompt(profile: dict[str, Any], focus: str | None, max_calls: int,
                      tools: list[dict[str, Any]]) -> str:
    """The user turn: the profile as upfront context, plus the budget and any focus nudge.

    The profile is handed over rather than fetched. Making the agent spend a tool call to learn its
    own schema would be a round trip to discover something already known at load time.
    """
    lines = [
        "Here is the file. Its schema profile is already loaded and is given to you in full below;",
        "it is your starting context, not something to fetch.",
        "",
        format_profile(profile),
        "",
        f"TOOL CALL BUDGET: {max_calls} calls for this whole run. Rejected calls are charged too.",
    ]

    grouping = _grouping_keys(tools)
    if grouping:
        lines += [
            "",
            f"Columns usable as a grouping key in this file: {', '.join(grouping)}",
            "(a grouping key needs a suitable role and at most 30 distinct values, so wide columns "
            "are not offered)",
        ]

    if focus:
        lines += [
            "",
            f"FOCUS: {focus}",
            "Treat this as a prioritisation nudge, not as a question to answer. Weight your plan "
            "toward it, but keep exercising full judgment: still stratify before concluding, still "
            "check for confounding, and still report anything important you find outside the "
            "focus. If the data does not support the focus, say that plainly rather than "
            "manufacturing a finding to fit it.",
        ]

    lines += [
        "",
        "First, write your plan. You have no tools on this turn - think about what is worth "
        "investigating in this specific file, and say so. The toolkit is handed to you next turn.",
    ]
    return "\n".join(lines)


def _grouping_keys(tools: list[dict[str, Any]]) -> list[str]:
    """Read the acceptable grouping columns straight out of the schemas the model is being given."""
    for schema in tools:
        if schema["function"]["name"] == "group_compare":
            spec = schema["function"]["parameters"]["properties"].get("group_col", {})
            return list(spec.get("enum", []))
    return []


def run_planner(
    toolkit: GuardedToolkit,
    model: ChatModel,
    focus: str | None = None,
    max_turns: int = MAX_TURNS,
    model_name: str = "",
    on_event: Callable[[str, Any], None] | None = None,
    plan_first: bool = True,
    context_budget: int = CONTEXT_TOKEN_BUDGET,
) -> PlannerRun:
    """Drive the loop to a stopping point and return the whole run, transcript included.

    `plan_first` spends one tool-less turn on a written plan before any tool is offered. Asking for
    a plan in the prompt is not enough on its own: a model given tools will often answer with tool
    calls and empty content, and the plan — the most readable part of a trace — never gets written.
    It costs an LLM round trip and no tool-call budget.
    """
    profile = toolkit.profile
    tools = tools_for_profile(profile)
    max_calls = toolkit.cap.limit

    system_prompt = SYSTEM_PROMPT.format(limitations=TOOLKIT_LIMITATIONS)
    task_prompt = build_task_prompt(profile, focus, max_calls, tools)

    run = PlannerRun(
        source=profile["source"],
        focus=focus,
        model_name=model_name,
        system_prompt=system_prompt,
        task_prompt=task_prompt,
        profile=profile,
        audit=toolkit.audit,
        cap=toolkit.cap,
    )
    # The ledger sits at a fixed index and is rewritten in place every turn. It lives inside the
    # protected prefix, so trimming can compact the detailed results below it but can never remove
    # the record that those calls happened — which is what stops the agent re-running them.
    ledger_index = 2
    run.messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": task_prompt},
        {"role": "system", "content": render_ledger([])},
    ]

    def emit(event: str, payload: Any = None) -> None:
        if on_event is not None:
            on_event(event, payload)

    # call_id -> one-line digest, used when an old tool payload has to be compacted away
    headlines: dict[str, str] = {}

    first_index = 1
    if plan_first:
        plan = model.complete(run.messages, tools=None)
        run.messages.append(assistant_message(plan))
        run.messages.append({"role": "user", "content": BEGIN_PROMPT})
        run.turns.append(_turn(1, plan, (), kind="plan"))
        emit("plan", plan)
        first_index = 2

    protected = len(run.messages)

    for index in range(first_index, max_turns + 1):
        run.messages[ledger_index] = {"role": "system", "content": render_ledger(run.ledger)}
        run.messages = trim_transcript(run.messages, headlines, budget=context_budget,
                                       protected=protected)
        response = model.complete(run.messages, tools=tools)
        run.messages.append(assistant_message(response))

        if not response.tool_calls:
            run.turns.append(_turn(index, response, ()))
            run.findings = response.content
            run.stop_reason = "model finished"
            emit("finished", run.findings)
            break

        emit("turn", (index, response))
        invocations, exhausted = _run_tool_calls(
            toolkit, response.tool_calls, run.messages, emit, headlines
        )
        run.turns.append(_turn(index, response, invocations))
        run.ledger += [
            (_signature(inv.function, inv.arguments), _headline(inv)) for inv in invocations
        ]

        if exhausted:
            run.stop_reason = "call cap reached"
            break
    else:
        run.stop_reason = "turn limit reached"

    if run.stop_reason != "model finished":
        emit("wrapping_up", run.stop_reason)
        run.messages[ledger_index] = {"role": "system", "content": render_ledger(run.ledger)}
        run.messages = trim_transcript(run.messages, headlines, budget=context_budget,
                                       protected=protected)
        writeup = _forced_writeup(model, run.messages, run.stop_reason)
        run.turns.append(_turn(len(run.turns) + 1, writeup, (), kind="writeup"))
        run.findings = writeup.content
        emit("finished", run.findings)

    return run


def _run_tool_calls(
    toolkit: GuardedToolkit,
    calls: tuple[ToolCall, ...],
    messages: list[dict[str, Any]],
    emit: Callable[[str, Any], None],
    headlines: dict[str, str],
) -> tuple[tuple[ToolInvocation, ...], bool]:
    """Execute one turn's calls through the single door, appending a tool message for each.

    Every call gets a reply even after the budget is gone. The chat protocol requires one tool
    message per tool_call id, so skipping the tail of a batch would make the next request invalid.
    """
    invocations: list[ToolInvocation] = []
    exhausted = False

    for call in calls:
        if exhausted:
            invocation = _failed(call, toolkit, "CALL_CAP_EXCEEDED",
                                 "budget was exhausted earlier in this same turn")
        elif call.parse_error:
            # Never reached the executor, so it costs no budget — but the model still needs to know.
            invocation = _failed(call, toolkit, "MALFORMED_ARGUMENTS",
                                 f"arguments were not valid JSON: {call.parse_error}")
        else:
            try:
                result = toolkit.call(call.name, call.arguments)
            except CallCapExceeded as exc:
                exhausted = True
                invocation = _failed(call, toolkit, "CALL_CAP_EXCEEDED", str(exc))
            else:
                invocation = ToolInvocation(
                    call_id=call.id,
                    function=call.name,
                    arguments=call.arguments,
                    ok=result.ok,
                    calls_remaining=toolkit.cap.remaining,
                    data=result.data,
                    error=result.error,
                    error_code=result.error_code,
                )

        invocations.append(invocation)
        headlines[call.id] = _headline(invocation)
        messages.append(
            {"role": "tool", "tool_call_id": call.id, "content": _tool_payload(invocation)}
        )
        emit("invocation", invocation)

    return tuple(invocations), exhausted


def _failed(call: ToolCall, toolkit: GuardedToolkit, code: str, message: str) -> ToolInvocation:
    return ToolInvocation(
        call_id=call.id,
        function=call.name,
        arguments=call.arguments,
        ok=False,
        calls_remaining=toolkit.cap.remaining,
        error=message,
        error_code=code,
    )


def render_ledger(entries: list[tuple[str, str]]) -> str:
    """The always-present memory of what has already been run, in one compact block."""
    if not entries:
        return LEDGER_HEADER + "\n\n(nothing yet)"
    lines = [f"{i}. {signature} -> {headline}" for i, (signature, headline) in enumerate(entries, 1)]
    return LEDGER_HEADER + "\n\n" + "\n".join(lines)


def _signature(function: str, arguments: dict[str, Any]) -> str:
    rendered = ", ".join(f"{key}={value!r}" for key, value in sorted(arguments.items()))
    return f"{function}({rendered})"


def _estimate_tokens(messages: list[dict[str, Any]]) -> int:
    """Cheap character-based estimate. Only used to decide when to compact, so precision is waste."""
    return sum(len(json.dumps(message, default=str)) for message in messages) // CHARS_PER_TOKEN


def _headline(invocation: ToolInvocation) -> str:
    """The few numbers from a result that a later turn might still need to cite."""
    if not invocation.ok:
        return f"refused: {invocation.error_code}"
    data = invocation.data or {}
    if invocation.function == "compute_correlation":
        overall = data.get("overall", {})
        parts = [f"r={overall.get('pearson_r')}", f"n={overall.get('n')}"]
        if data.get("group_by"):
            parts.append(f"by {data['group_by']}: sign_reversal={data['sign_reversal']}, "
                         f"attenuated={data['attenuated']}, "
                         f"subgroup r {data['subgroup_r_min']}..{data['subgroup_r_max']}")
        return "; ".join(parts)
    if invocation.function == "detect_outliers":
        return (f"{data.get('method')}: {data.get('n_outliers')} outliers of "
                f"{data.get('n_present')} rows")
    if invocation.function == "group_compare":
        high, low = data.get("highest_group", {}), data.get("lowest_group", {})
        return (f"{data.get('n_groups_total')} groups; highest {high.get('group')}"
                f"={high.get('mean')}, lowest {low.get('group')}={low.get('mean')}, "
                f"ratio {data.get('highest_over_lowest_ratio')}")
    if invocation.function == "get_summary_stats":
        return f"mean={data.get('mean')}, median={data.get('median')}, missing={data.get('missing_pct')}%"
    if invocation.function == "value_counts":
        return f"{data.get('n_distinct')} distinct, top={data.get('values', [{}])[0].get('value')}"
    return "result elided"


def trim_transcript(
    messages: list[dict[str, Any]],
    headlines: dict[str, str],
    budget: int = CONTEXT_TOKEN_BUDGET,
    protected: int | None = None,
) -> list[dict[str, Any]]:
    """Shrink the transcript to fit the budget, cheapest loss first.

    Two passes, in order of what hurts least: compact old tool payloads down to their headline
    numbers, and only if that is still not enough, drop whole old exchanges. Exchanges are dropped
    as units — an assistant message with tool_calls and its tool replies go together, because a
    tool_call without its reply makes the request malformed.
    """
    if _estimate_tokens(messages) <= budget:
        return messages

    trimmed = [dict(message) for message in messages]

    tool_positions = [i for i, m in enumerate(trimmed) if m["role"] == "tool"]
    for position in tool_positions[:-KEEP_FULL_RESULTS] if KEEP_FULL_RESULTS else tool_positions:
        call_id = trimmed[position].get("tool_call_id", "")
        headline = headlines.get(call_id)
        if headline is None:
            continue
        compacted = json.dumps({"ok": None, "summary": headline, "note": "payload compacted"})
        if len(compacted) < len(trimmed[position]["content"]):
            trimmed[position]["content"] = compacted
        if _estimate_tokens(trimmed) <= budget:
            return trimmed

    # Still over. Drop the oldest exchanges, keeping the protected prefix: the system prompt, the
    # profile, the ledger, the plan and the message that handed over the toolkit.
    prefix = _protected_prefix(trimmed) if protected is None else protected
    while _estimate_tokens(trimmed) > budget:
        end = _first_exchange_end(trimmed, prefix)
        if end is None:
            break
        dropped = [m for m in trimmed[prefix:end] if m["role"] == "assistant"]
        names = sorted({
            c["function"]["name"] for m in dropped for c in m.get("tool_calls", [])
        })
        note = {
            "role": "assistant",
            "content": f"[earlier turn elided to stay within the context budget; it ran: "
                       f"{', '.join(names) or 'no tools'}]",
        }
        trimmed = trimmed[:prefix] + [note] + trimmed[end:]
        prefix += 1

    return trimmed


def _protected_prefix(messages: list[dict[str, Any]]) -> int:
    """How many leading messages are never dropped: the system prompt, the profile, and the plan."""
    prefix = 0
    for message in messages:
        if message["role"] in ("system", "user") or not message.get("tool_calls"):
            prefix += 1
        else:
            break
        if prefix >= 4:
            break
    return prefix


def _first_exchange_end(messages: list[dict[str, Any]], start: int) -> int | None:
    """Index just past the first assistant-plus-tool-replies block at or after `start`."""
    index = start
    while index < len(messages) and messages[index]["role"] != "assistant":
        index += 1
    if index >= len(messages):
        return None
    end = index + 1
    while end < len(messages) and messages[end]["role"] == "tool":
        end += 1
    return end if end < len(messages) else None


def _tool_payload(invocation: ToolInvocation) -> str:
    """What the model sees back. The rejection text is passed through verbatim: it names the
    columns that would have worked, and that message is the whole self-correction mechanism."""
    if invocation.ok:
        payload: dict[str, Any] = {"ok": True, "result": invocation.data}
    else:
        payload = {
            "ok": False,
            "error_code": invocation.error_code,
            "error": invocation.error,
            "hint": "This call was refused. Read the message, correct the arguments, and try again.",
        }
    payload["calls_remaining"] = invocation.calls_remaining
    return json.dumps(payload, default=str)


def _forced_writeup(
    model: ChatModel, messages: list[dict[str, Any]], stop_reason: str
) -> LLMResponse:
    """Ask for findings with no tools attached, so a run that hits a ceiling still reports.

    A run that dies silently at its budget is worse than one that says what it had time to learn.
    """
    messages.append(
        {
            "role": "user",
            "content": (
                f"Stop investigating now: {stop_reason}. Write your final answer from the evidence "
                "you already have, using the required headings. Do not request any more tool calls. "
                "Say plainly which questions you had to leave open because the run ended early."
            ),
        }
    )
    response = model.complete(messages, tools=None)
    messages.append(assistant_message(response))
    return response


def _turn(index: int, response: LLMResponse, invocations: tuple[ToolInvocation, ...],
          kind: str = "act") -> Turn:
    return Turn(
        index=index,
        reasoning=response.reasoning,
        content=response.content,
        invocations=invocations,
        prompt_tokens=response.prompt_tokens,
        completion_tokens=response.completion_tokens,
        kind=kind,
    )
