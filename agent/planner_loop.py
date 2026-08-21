"""
planner_loop.py — the hand-written planning loop: hand the model the file's profile up front, let it
plan investigations, run each through the guarded toolkit, feed results back, stop when it says so.
Exists because this loop is the explainable part of the project — no framework, so every message, every
rejection fed back for self-correction and every stopping condition is visible in one file.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from agent.llm import ChatModel, LLMResponse, ToolCall, assistant_message, tools_for_profile
from guardrails.audit import AuditLog
from guardrails.call_cap import CallCap, CallCapExceeded
from guardrails.executor import GuardedToolkit, call_signature
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
# The number is calibrated, not guessed: the run that hit the limit was refused at 8074 real tokens
# and this estimator scored that same transcript around 5450, so char/4 under-counts by roughly
# 1.48x. That ratio is a worst case — it was measured on an untrimmed transcript of dense JSON
# payloads, which tokenise far worse than the English prose that dominates a trimmed one.
#
# 4900 estimated is therefore about 7250 real at worst, still under the 8000 cap with room for a
# reasoning-heavy completion. It was raised from 4400 when the Day 5 prompt rules pushed the
# un-droppable floor (see context_floor) above the old budget: at that point trimming had nothing
# left it was allowed to drop, and every request would have gone out over budget. The budget covers
# the whole request — messages AND the tool schemas, which are resent every turn.
CONTEXT_TOKEN_BUDGET = 4900
# What must stay free for live tool results after the un-droppable floor is paid for. Below this the
# agent is reasoning entirely from compacted headlines, which is not worth spending a run on.
MIN_RESULT_HEADROOM = 400
# The written plan is protected too — it is an assistant message with no tool_calls, so it sits in
# the prefix trimming never touches. It is model-written, so the floor budgets a nominal size.
PLAN_ALLOWANCE = 400
# The most recent results stay in full — those are the ones the next decision depends on.
KEEP_FULL_RESULTS = 3
# Rough enough: this only decides *when* to compact, and compacting early is cheap.
CHARS_PER_TOKEN = 4

# What the five functions genuinely cannot do. Stated in the prompt so the agent declares a gap
# instead of inventing an answer — the time-series absence is the one it will hit most often.
TOOLKIT_LIMITATIONS = """\
- No time-series or trend analysis: nothing for value-over-time, seasonality, change points or
  forecasting. Grouping by a date column gives per-period averages - a comparison, not a trend.
- No regression or multivariate analysis: you stratify by ONE grouping column at a time, and cannot
  control for two variables at once or fit a model.
- No hypothesis testing beyond the p-value returned with a correlation.
- No filtering, slicing or derived columns: every function runs over all rows, and grouping is the
  only subsetting there is.
- Grouping keys are capped at 30 distinct values, so wide columns (dates, identifiers) cannot be
  grouped on even when that is the question you want to ask."""

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
to date for you. Do not repeat any of these calls - you already have the answer, and a repeat is
REFUSED by the toolkit and still costs you a call. Older results further down the conversation may
have been compacted to save space; this list is the record."""

SYSTEM_PROMPT = """\
You are an autonomous data analyst. You have one CSV file and a fixed toolkit of five analysis
functions. Your job is to decide what is worth investigating in this file, investigate it, and
report what you actually found.

HOW YOU WORK

1. You already have the file's schema profile and the toolkit. Do not spend a call rediscovering
   either; plan from what you were given, not from what a file like this usually contains.
2. Write a short plan first: three to six specific things worth checking in THIS file, naming the
   actual columns and why each question is worth asking.
3. Work the plan with tool calls, revising as results arrive: follow a surprise, drop a dead end.
   Order it by what would matter most if true, not by the order you happened to write it in - the
   budget usually runs out before the list does.
4. Stop when further calls would not change your conclusions, and write up what you found.

JUDGMENT - THIS IS THE PART THAT MATTERS

A number is not a finding. Deciding which numbers mean something is the entire job.

STRATIFY BEFORE YOU CONCLUDE

A pooled correlation is not yet evidence. When one looks worth reporting and the file has a
plausible grouping column, re-run it with group_by set BEFORE concluding anything. Every stratified
result carries two flags, two different ways a pooled number can be fake. Equally disqualifying.

- "sign_reversal": true - the relationship RUNS THE OTHER WAY inside the subgroups. The pooled
  number is an artefact of mixing groups whose means differ. Report the within-group direction as
  the real one, name the grouping column as the confounder, and never recommend acting on the
  pooled number.
- "attenuated": true - the relationship VANISHES inside the subgroups: the strongest subgroup r is
  less than half the pooled one. It is explained by the grouping variable, not by the two columns
  you correlated. Treat it exactly as a reversal: not a finding about those two columns, not
  strong or robust or consistent, not high confidence. If there is a finding here it is about the
  grouping variable, and the honest headline is that the apparent relationship is explained away.
- Both false - it survived that split, which rules out that one column as the confounder. Say so,
  then read the next two rules before you call anything robust.

If you stratify the same pair by more than one grouping column, your confidence follows the LEAST
favourable result, never the most convenient one. A pair that holds by one grouping and attenuates
by another is an attenuated pair. A reassuring split never out-votes a disqualifying one. State
every stratification you ran on a pair, including the ones that undermined it.

AN UNUSUALLY HIGH CORRELATION IS A SUSPECT, NOT A HEADLINE

A pooled r above roughly 0.95 is rarely a discovery about the world. It is usually a formula -
revenue = units x price, impressions = spend x rate - and an identity survives EVERY stratification
perfectly, so a clean split on one tells you nothing at all.

Above 0.95 the question is therefore not how confident to be, but whether one column is DERIVED
from the other: check the names, roles and units, and ask what would have to be true for the fit to
be that tight. If it plausibly is a derivation, report it as a property of how the file was built -
never as a finding about the world, never as your leading finding, and never as robust, strong or
reliable, words that do not describe an identity. If you cannot see a derivation, report it as an
unexplained near-identity at low confidence, and still not first. A high r is a reason to look
harder, never confidence already earned.

QUOTE THE FLAGS, NEVER RECOMPUTE THEM

The toolkit computes those two flags and is the only authority on those two words. When you use
either word about a pair, quote the value the tool returned for that exact pair and grouping -
"attenuated: false, as returned by compute_correlation" - and never derive it from the subgroup
numbers yourself. Your arithmetic does not overrule the flag; claiming a reversal or an attenuation
the flag does not support is a fabricated finding.

Describing the pattern in words is welcome - which subgroups are weaker, where the spread is widest
- kept separate from the quoted flag. If your reading seems to disagree with the flag, say so
plainly ("attenuated: false, though North's 0.28 is well below the pooled 0.45") rather than
deciding it in your own favour.

THE OTHER FOUR FUNCTIONS ARE NOT A SIDESHOW

Correlation gets the most words above because it has the most ways to mislead you, not because it
is the most valuable. An anomaly localised to a segment, or one group far outside the rest, is
usually the more actionable finding, and neither is reachable by correlating anything.

- group_compare gives a finding an ADDRESS. "Revenue varies by region" is a fact about a column;
  "one store runs at eight times the others" is actionable. An aggregate is made of its members, so
  one extreme member moves the group containing it: when a column looks skewed or an outlier
  appears, compare against the most specific grouping key available, not just a broad one.
- detect_outliers tells you THAT rows are extreme, never what they are. It is half a finding until
  group_compare says which segment or period they belong to. An effect appearing everywhere at once
  is seasonality or a definition, not an anomaly.
- get_summary_stats earns a call when mean and median disagree: a wide gap is the tell that a few
  extreme rows are dragging the average.
- value_counts shows a categorical column's shape - balanced levels, one dominating, an unexpected
  level - worth knowing before you group by it.

ONE SIGNAL IS ONE FINDING

Columns are often mechanically linked: revenue tracks units sold, foot traffic tracks both. So one
phenomenon usually shows up several times - in several correlated columns, or in a segment and again
in the wider group containing it. That is ONE finding with corroborating evidence, not several.
Write it once and list the corroboration beneath it. Before adding a numbered finding, ask whether
it is the same signal seen through a different column: a reader who counts five findings should be
able to act on five different things.

WEIGHING WHAT YOU FIND

- Not every strong correlation is a finding. Some are too self-evident to deserve attention even
  when nothing derives one column from the other.
- Quote a ratio against the denominator its key names: "highest_over_lowest_ratio" is highest over
  lowest, "median_ratio_to_overall_median" is against the median. Never restate one against another
  baseline.
- Keep what you measured separate from what you are inferring. You cannot test causation here.


WHEN A CALL IS REJECTED

Rejections are informative: the error names the columns that would have worked, or the values the
argument accepts. Read it and correct the call rather than guessing again - rejections cost budget.
Never repeat a rejected call unchanged, and never abandon a question because one attempt failed.
A call you have already completed is refused as a duplicate: the answer is in the ledger above, and
asking again cannot change it, because the data does not change between calls.

WHAT THIS TOOLKIT CANNOT DO

{limitations}

If a worthwhile investigation needs something on that list, do not guess and do not quietly drop
it. Record it under "Not investigable with this toolkit" with what you would have checked.

BUDGET

Every tool result says how many calls remain. When it is nearly gone, stop and write up.

YOUR FINAL ANSWER

When you are finished, reply with no tool calls, using exactly these headings:

## Findings
Numbered, most important first, ONE distinct signal per entry - corroborating evidence from other
columns belongs inside the entry it supports, never as an entry of its own. For each: what you
found, the numbers supporting it, every stratification you ran on that pair including any that
undermined it, your confidence, and any caveat a reader needs. Say so explicitly where a
stratification changed your reading. Quote the sign_reversal and attenuated values the toolkit
returned for each stratification you name; do not restate them from the subgroup numbers.

## Checked and not reported
What you investigated that did not earn a place in the findings, one line each on why. Only calls
you really made.

## Not investigable with this toolkit
Questions this file raises that the five functions cannot answer. Omit if empty."""


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
    # Canonical identity of the call, from the executor. Empty when the call never reached it.
    signature: str = ""


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
    # Set when a model call failed and ended the run early. The trace reports it rather than
    # pretending the agent chose to stop.
    error: str | None = None
    # (call signature, headline result) for every DISTINCT call made, in the order first made. Never
    # trimmed: this is what stops the agent re-running work whose full result has since been
    # compacted out of the window. Deduplicated by signature — see `record_calls`.
    ledger: list[tuple[str, str]] = field(default_factory=list)
    # signature -> how many times it has been requested. Only signatures asked more than once appear.
    repeats: dict[str, int] = field(default_factory=dict)
    audit: AuditLog | None = None
    cap: CallCap | None = None

    @property
    def total_tokens(self) -> int:
        return sum(t.prompt_tokens + t.completion_tokens for t in self.turns)

    @property
    def rejections(self) -> list[ToolInvocation]:
        return [inv for turn in self.turns for inv in turn.invocations if not inv.ok]


def build_task_prompt(profile: dict[str, Any], focus: str | None, max_calls: int,
                      tools: list[dict[str, Any]], include_toolkit: bool = True) -> str:
    """The user turn: the profile as upfront context, plus the budget and any focus nudge.

    The profile is handed over rather than fetched. Making the agent spend a tool call to learn its
    own schema would be a round trip to discover something already known at load time.

    `include_toolkit` writes out the function list in prose. The planning turn needs it — it is
    given no schemas, and without this it plans against a remembered toolkit. Every later turn is
    given the real schemas, so carrying the prose copy as well would pay for the same information
    twice, every turn, out of a context floor that cannot be trimmed. So the loop asks for it once
    and then swaps it out.
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

    if include_toolkit:
        lines += [
            "",
            "THE TOOLKIT - these functions and nothing else. '?' marks an optional argument; every "
            "column argument is checked against the profile above, and a rejection names what "
            "would have worked:",
            "",
            format_toolkit(tools, [column["name"] for column in profile["columns"]]),
            "",
            "Plan with all of them, not just the most familiar. An outlier localised to a segment, "
            "or one group far outside the rest, is as much a finding as a correlation.",
            "",
            "First, write your plan. You cannot call anything on this turn - think about what is "
            "worth investigating in this file, and say so. The functions become callable next turn.",
        ]
    return "\n".join(lines)


def format_toolkit(tools: list[dict[str, Any]],
                   column_names: list[str] | None = None) -> str:
    """Render the tool schemas as prose for the planning turn, which is given no schemas of its own.

    The plan turn deliberately runs with `tools=None` — a model handed tools answers with tool calls
    and empty content, and the plan, the most readable part of a trace, never gets written. But it
    was also being given no *description* of the toolkit, so it planned against a remembered one. A
    graded run's planning reasoning read: "We have only five functions ... maybe others? Not listed
    but typical toolkit includes ... But we assume these." It never used the two it failed to guess.

    Rendered from `to_openai_tools(profile)` rather than written out by hand, so it cannot drift from
    what is actually offered, and so a parameter dropped for this file disappears from here too.
    """
    columns = set(column_names or ())
    lines = []
    for schema in tools:
        function = schema["function"]
        parameters = function["parameters"]
        required = set(parameters.get("required", []))
        rendered = []
        for name, spec in parameters["properties"].items():
            choices = spec.get("enum") or []
            # Column enums are omitted: the profile above already lists every column with its role,
            # and repeating five column lists here costs more context than the plan turn is worth.
            # Enums that are NOT columns — like the outlier method — appear nowhere else, so they do.
            allowed = "" if not choices or set(choices) <= columns else f"={'|'.join(map(str, choices))}"
            rendered.append(f"{name}{'' if name in required else '?'}{allowed}")
        # First sentence only. The full schemas, descriptions included, arrive next turn; this
        # listing exists so the plan knows what EXISTS, not to restate the whole contract.
        summary = function["description"].split(". ")[0].rstrip(".")
        lines.append(f"- {function['name']}({', '.join(rendered)})\n    {summary}.")
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
    # The schemas are re-sent on every turn, so they are part of every request's size.
    schema_overhead = len(json.dumps(tools)) // CHARS_PER_TOKEN

    system_prompt = SYSTEM_PROMPT.format(limitations=TOOLKIT_LIMITATIONS)
    # Two versions of the same message. The plan turn is offered no schemas, so it gets the toolkit
    # written out; every turn after that is given the real schemas and would otherwise pay for the
    # same list twice, forever, out of a floor that cannot be trimmed. The second version replaces
    # the first as soon as the plan is written.
    task_prompt = build_task_prompt(profile, focus, max_calls, tools)
    working_prompt = build_task_prompt(profile, focus, max_calls, tools, include_toolkit=False)
    # Before spending a single request: does anything fit alongside the fixed overhead? Measured on
    # the *working* prompt, since that is the one re-sent on every turn.
    check_context_headroom(context_floor(system_prompt, working_prompt, tools), context_budget)

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
        plan = _safe_complete(model, run.messages, None, run)
        if plan is None:
            return run
        run.messages.append(assistant_message(plan))
        run.messages.append({"role": "user", "content": BEGIN_PROMPT})
        run.turns.append(_turn(1, plan, (), kind="plan"))
        emit("plan", plan)
        first_index = 2
        # The written-out toolkit has done its job; the real schemas take over from here.
        run.messages[1] = {"role": "user", "content": working_prompt}

    protected = len(run.messages)

    for index in range(first_index, max_turns + 1):
        run.messages[ledger_index] = {"role": "system", "content": render_ledger(run.ledger, run.repeats)}
        run.messages = trim_transcript(run.messages, headlines, budget=context_budget,
                                       protected=protected, overhead=schema_overhead)
        response = _safe_complete(model, run.messages, tools, run)
        if response is None:
            emit("aborted", run.error)
            break
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
        record_calls(run, invocations)

        if exhausted:
            run.stop_reason = "call cap reached"
            break
    else:
        run.stop_reason = "turn limit reached"

    # A run that ended because the API died has no working API to write its findings with, so it
    # skips the write-up rather than spending a doomed request. The trace still gets written.
    if run.stop_reason != "model finished" and run.error is None:
        emit("wrapping_up", run.stop_reason)
        run.messages[ledger_index] = {"role": "system", "content": render_ledger(run.ledger, run.repeats)}
        run.messages = trim_transcript(run.messages, headlines, budget=context_budget,
                                       protected=protected, overhead=schema_overhead)
        writeup = _safe_complete(model, run.messages, None, run, writeup_for=run.stop_reason)
        if writeup is not None:
            run.turns.append(_turn(len(run.turns) + 1, writeup, (), kind="writeup"))
            run.findings = writeup.content
            emit("finished", run.findings)

    return run


def _safe_complete(
    model: ChatModel,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    run: PlannerRun,
    writeup_for: str | None = None,
) -> LLMResponse | None:
    """One model call that records its own failure instead of raising through the loop.

    A run that dies on an API error still has everything before that point worth keeping - the plan,
    the calls, the flags it saw. Letting the exception escape would throw all of it away before the
    trace is written, which is exactly what a trace exists to prevent. Returning None means "stop,
    but keep what you have"; run.error carries the reason so the trace can say what happened.
    """
    if writeup_for is not None:
        messages.append({
            "role": "user",
            "content": (
                f"Stop investigating now: {writeup_for}. Write your final answer from the evidence "
                "you already have, using the required headings. Do not request any more tool calls. "
                "Say plainly which questions you had to leave open because the run ended early."
            ),
        })
    try:
        response = model.complete(messages, tools=tools)
    except Exception as exc:                          # noqa: BLE001 - any failure ends the run
        run.error = f"{type(exc).__name__}: {exc}"
        run.stop_reason = "aborted: the model call failed"
        return None
    if writeup_for is not None:
        messages.append(assistant_message(response))
    return response


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
                    # The executor's own canonical signature, not one re-derived here: the ledger and
                    # the duplicate guard must agree about what counts as the same call.
                    signature=result.signature,
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


def record_calls(run: PlannerRun, invocations: Sequence[ToolInvocation]) -> None:
    """Fold this turn's calls into the ledger, deduplicated by call signature.

    A repeat must not become a new numbered entry. When it did, the list grew every turn and read
    like progress: one graded run issued the identical `compute_correlation(foot_traffic,
    units_sold)` three times with a correct ledger in front of it each time, and the ledger showed
    three separate lines rather than one line asked for three times.

    The signature — same function, same arguments — is the identity. A repeat updates the existing
    entry in place, keeping its original position because that is when the answer was obtained, and
    increments a count that `render_ledger` surfaces. Note that the call cap is still charged for
    it: the call was made, and the audit log records every attempt. What changes is only whether the
    agent can see that it is asking again instead of advancing.
    """
    for invocation in invocations:
        signature = invocation.signature or call_signature(invocation.function,
                                                           invocation.arguments)
        headline = _headline(invocation)
        for position, (existing, kept) in enumerate(run.ledger):
            if existing == signature:
                # A repeat is now refused by the executor, so its headline reads "refused:
                # DUPLICATE_CALL". Writing that over the entry would erase the answer the ledger
                # exists to preserve — the agent would be told it already ran the call and shown
                # nothing it could use. A failure never overwrites a success.
                if invocation.ok or not kept.startswith("refused"):
                    run.ledger[position] = (signature, headline if invocation.ok else kept)
                run.repeats[signature] = run.repeats.get(signature, 1) + 1
                break
        else:
            run.ledger.append((signature, headline))


def render_ledger(entries: list[tuple[str, str]],
                  repeats: dict[str, int] | None = None) -> str:
    """The always-present memory of what has already been run, in one compact block."""
    if not entries:
        return LEDGER_HEADER + "\n\n(nothing yet)"
    counts = repeats or {}
    lines = []
    for index, (signature, headline) in enumerate(entries, 1):
        asked = counts.get(signature, 1)
        # Naming the repetition is the point: the model that repeated a call had the answer in front
        # of it and did not notice it was re-asking rather than progressing.
        again = (f"   [ALREADY REQUESTED {asked} TIMES - the answer did not change, "
                 f"and each attempt cost a call]") if asked > 1 else ""
        lines.append(f"{index}. {signature} -> {headline}{again}")
    return LEDGER_HEADER + "\n\n" + "\n".join(lines)


def _estimate_tokens(messages: list[dict[str, Any]], overhead: int = 0) -> int:
    """Cheap character-based estimate of one whole request. Precision is waste; direction is not.

    `overhead` is for anything sent alongside the messages - in practice the tool schemas, which go
    up on every turn. Leaving them out made the estimate quietly optimistic by about 700 tokens.
    """
    body = sum(len(json.dumps(message, default=str)) for message in messages) // CHARS_PER_TOKEN
    return body + overhead


def context_floor(system_prompt: str, task_prompt: str, tools: list[dict[str, Any]]) -> int:
    """Estimated size of the part of every request that trimming is never allowed to touch.

    The system prompt, the profile, the ledger header, the written plan and the tool schemas go up
    on every single turn and cannot be compacted or dropped. If that floor approaches the budget,
    `trim_transcript` has nothing left it may remove and will hand back an over-budget request
    anyway — silently, until the API refuses it mid-run. So the floor is measured up front rather
    than discovered. The plan is model-written and so is counted at a nominal size; everything else
    is measured exactly.
    """
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": task_prompt},
        {"role": "system", "content": render_ledger([])},
        {"role": "assistant", "content": "x" * (PLAN_ALLOWANCE * CHARS_PER_TOKEN)},
        {"role": "user", "content": BEGIN_PROMPT},
    ]
    return _estimate_tokens(messages, len(json.dumps(tools)) // CHARS_PER_TOKEN)


def check_context_headroom(floor: int, budget: int = CONTEXT_TOKEN_BUDGET) -> int:
    """Refuse to start a run whose fixed overhead leaves no room for results. Returns the headroom.

    Raised rather than warned, and raised *before* the first request, because the alternative is a
    run that burns real quota to reach an HTTP 413 — or worse, one that completes while reasoning
    only from compacted headlines and looks like a genuine result.
    """
    headroom = budget - floor
    if headroom < MIN_RESULT_HEADROOM:
        raise ValueError(
            f"context budget {budget} leaves {headroom} tokens for tool results after a fixed "
            f"floor of {floor} (system prompt + profile + ledger + tool schemas); "
            f"{MIN_RESULT_HEADROOM} is the minimum. Shorten the system prompt, or raise the budget "
            f"if the model's request ceiling allows it."
        )
    return headroom


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
        # Name the ratio. A bare "ratio 8.32" in the ledger is what a previous run read as
        # "8.3x the overall mean" when it is highest-over-lowest; the overall mean ratio was 5.1.
        return (f"{data.get('n_groups_total')} groups; highest {high.get('group')}"
                f"={high.get('mean')}, lowest {low.get('group')}={low.get('mean')}, "
                f"highest/lowest={data.get('highest_over_lowest_ratio')}; "
                f"overall mean={data.get('overall_mean')}, median={data.get('overall_median')}")
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
    overhead: int = 0,
) -> list[dict[str, Any]]:
    """Shrink the transcript to fit the budget, cheapest loss first.

    Two passes, in order of what hurts least: compact old tool payloads down to their headline
    numbers, and only if that is still not enough, drop whole old exchanges. Exchanges are dropped
    as units — an assistant message with tool_calls and its tool replies go together, because a
    tool_call without its reply makes the request malformed.
    """
    if _estimate_tokens(messages, overhead) <= budget:
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
        if _estimate_tokens(trimmed, overhead) <= budget:
            return trimmed

    # Still over. Drop the oldest exchanges, keeping the protected prefix: the system prompt, the
    # profile, the ledger, the plan and the message that handed over the toolkit.
    prefix = _protected_prefix(trimmed) if protected is None else protected
    while _estimate_tokens(trimmed, overhead) > budget:
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
