"""
llm.py — thin adapter over Groq's OpenAI-compatible chat completions endpoint.
Exists so the planning loop depends on one small interface it can be stubbed against in tests, rather
than on the SDK. Tool schemas come from registry.to_openai_tools(profile), narrowed to the columns of
whichever CSV is loaded, so the model is only ever offered arguments that exist in this file.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from dotenv import load_dotenv
from openai import APIConnectionError, APIStatusError, BadRequestError, OpenAI, RateLimitError

from toolkit.registry import to_openai_tools

DEFAULT_BASE_URL = "https://api.groq.com/openai/v1"
DEFAULT_MODEL = "openai/gpt-oss-120b"
# Low but not zero. This is analytic judgment, not brainstorming: we want the same file to produce
# the same reading twice, while leaving the model room to phrase a conclusion rather than a template.
DEFAULT_TEMPERATURE = 0.2
# gpt-oss exposes a reasoning budget on Groq. "medium" is enough to plan and to notice a reversal
# without spending the whole latency budget thinking about column names.
DEFAULT_REASONING_EFFORT = "medium"

MAX_RETRIES = 4
BACKOFF_SECONDS = 2.0
# How big a prompt preflight() sends. Large enough to be refused when only a few hundred tokens
# remain — the case that twice let a doomed batch start — and small enough that paying it on every
# run is cheap: ~3% of a run's ~60k. It is a real cost, paid to avoid a much larger wasted one.
PREFLIGHT_TOKENS = 2_000


class MissingCredentials(RuntimeError):
    """Raised when no GROQ_API_KEY is available, with the fix rather than a stack trace."""


class DailyQuotaExhausted(RuntimeError):
    """The per-day token allowance is gone. Distinct from a per-minute limit, which waiting fixes.

    Retrying this is not slow, it is wrong: four backed-off attempts against a daily cap fail four
    times and then report "failed after 4 attempts", which reads like a flaky network. Worse, a run
    that starts on a nearly empty budget dies partway and leaves a trace that looks like evidence.
    """


@dataclass(frozen=True)
class ToolCall:
    """One function call the model asked for. `raw_arguments` is kept so the trace is faithful."""

    id: str
    name: str
    arguments: dict[str, Any]
    raw_arguments: str
    parse_error: str | None = None


@dataclass(frozen=True)
class LLMResponse:
    """One assistant turn: its visible answer, its reasoning if the model exposes it, its calls."""

    content: str = ""
    reasoning: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    finish_reason: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0


class ChatModel(Protocol):
    """The whole surface the planning loop needs. A stub implementing this can drive it offline."""

    def complete(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None
    ) -> LLMResponse: ...


def assistant_message(response: LLMResponse) -> dict[str, Any]:
    """Rebuild the assistant message to append to the transcript.

    Reconstructed from our own dataclass rather than kept as an SDK object, so a stubbed model
    produces byte-identical transcripts to a real one.
    """
    message: dict[str, Any] = {"role": "assistant", "content": response.content or ""}
    if response.tool_calls:
        message["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.name, "arguments": call.raw_arguments},
            }
            for call in response.tool_calls
        ]
    return message


def tools_for_profile(profile: dict[str, Any]) -> list[dict[str, Any]]:
    """Tool schemas for one file, with unsatisfiable parameters removed.

    to_openai_tools() narrows every column parameter to the columns that would pass validation. If
    that leaves an *optional* parameter with no candidates the parameter is dropped, and if it
    leaves a *required* one empty the whole tool is dropped: offering a call that cannot be made is
    an invitation to waste budget discovering that.
    """
    usable: list[dict[str, Any]] = []
    for schema in to_openai_tools(profile):
        parameters = schema["function"]["parameters"]
        properties = parameters["properties"]
        required = parameters["required"]

        unusable = [
            name for name, spec in properties.items()
            if "enum" in spec and not spec["enum"]
        ]
        if any(name in required for name in unusable):
            continue
        for name in unusable:
            del properties[name]
        usable.append(schema)
    return usable


class GroqClient:
    """Groq via the OpenAI SDK, pointed at its OpenAI-compatible base URL."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        temperature: float = DEFAULT_TEMPERATURE,
        reasoning_effort: str | None = DEFAULT_REASONING_EFFORT,
    ) -> None:
        # Explicit path, not load_dotenv()'s default: the no-argument form finds the .env by
        # inspecting the caller's stack frame, which raises outright when this is imported from a
        # REPL or a piped script. The repo root is two levels up from this file and never moves.
        load_dotenv(Path(__file__).resolve().parent.parent / ".env")
        key = api_key or os.environ.get("GROQ_API_KEY")
        if not key:
            raise MissingCredentials(
                "GROQ_API_KEY is not set. Copy .env.example to .env and put your key in it "
                "(https://console.groq.com/keys), or export GROQ_API_KEY in this shell."
            )
        self.model = model or os.environ.get("GROQ_MODEL") or DEFAULT_MODEL
        self.temperature = temperature
        self.base_url = base_url or os.environ.get("GROQ_BASE_URL") or DEFAULT_BASE_URL
        self._reasoning_effort = reasoning_effort
        self._client = OpenAI(api_key=key, base_url=self.base_url)

    def preflight(self, needed_tokens: int = PREFLIGHT_TOKENS) -> None:
        """Check there is room for a real turn before starting a run. Raises DailyQuotaExhausted.

        The naive check — send a tiny request and see if it works — is worse than useless: it passes
        on the few hundred tokens left at the end of a quota and reports "quota is back", which is
        how two batches of eval runs came to die partway through. A large `max_completion_tokens`
        does not fix it either: the daily gate is measured on the PROMPT, so a one-line prompt is
        waved through however much output it reserves. That was measured, not assumed — a probe
        reserving 5,000 tokens passed with 1,146 left in the day.

        So the prompt itself is padded to the size of a real turn. The cost is honest: about
        `needed_tokens` when it passes, nothing when it fails, since a refused request is not
        charged. And it answers a narrow question — whether ONE turn fits — not whether a whole run
        will, which no probe can tell you.
        """
        padding = "the quick brown fox jumps over the lazy dog. " * (needed_tokens // 10)
        try:
            self._client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": f"Reply with one character: x\n{padding}"}],
                max_completion_tokens=1,
            )
        except RateLimitError as exc:
            if _is_daily_quota(exc):
                raise DailyQuotaExhausted(_quota_message(exc)) from exc
            # A per-minute limit says nothing about the day's budget, and waiting clears it.

    def complete(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None
    ) -> LLMResponse:
        """One chat completion, with retries on the failures that are worth retrying."""
        request: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
        }
        if tools:
            request["tools"] = tools
            request["tool_choice"] = "auto"
        if self._reasoning_effort:
            request["reasoning_effort"] = self._reasoning_effort

        last_error: Exception | None = None
        for attempt in range(MAX_RETRIES):
            try:
                completion = self._client.chat.completions.create(**request)
                return _parse_completion(completion)
            except BadRequestError as exc:
                # Not every deployment accepts reasoning_effort. Drop it once and try again rather
                # than failing a whole run over an optional parameter.
                if self._reasoning_effort and "reasoning" in str(exc).lower():
                    self._reasoning_effort = None
                    request.pop("reasoning_effort", None)
                    last_error = exc
                    continue
                raise
            except RateLimitError as exc:
                # Per-minute limits are what backoff is for. A per-day limit is not: it will still
                # be there in eight seconds, so say so once and stop.
                if _is_daily_quota(exc):
                    raise DailyQuotaExhausted(_quota_message(exc)) from exc
                last_error = exc
            except APIConnectionError as exc:
                last_error = exc
            except APIStatusError as exc:
                if exc.status_code < 500:
                    raise
                last_error = exc
            time.sleep(BACKOFF_SECONDS * (2**attempt))

        raise RuntimeError(f"Groq call failed after {MAX_RETRIES} attempts: {last_error}")


def _is_daily_quota(exc: Exception) -> bool:
    """Groq distinguishes the two limits only in the error text, so that is what we read."""
    text = str(exc).lower()
    return "tokens per day" in text or "(tpd)" in text


def _quota_message(exc: Exception) -> str:
    """Pull the numbers out of Groq's message so the caller learns how short it is, not just that.

    The raw error is a JSON blob inside a Python exception string; the three numbers that decide
    whether waiting is worth it are buried in prose. Falls back to the whole text if the shape
    changes, because a slightly ugly message beats a swallowed one.
    """
    text = str(exc)
    numbers = re.search(r"Limit (\d+), Used (\d+), Requested (\d+)", text)
    retry = re.search(r"try again in ([\dhms.]+)", text)
    if not numbers:
        return f"Groq daily token quota exhausted: {text}"
    limit, used, requested = (int(g) for g in numbers.groups())
    wait = f" Groq suggests retrying in {retry.group(1).rstrip(chr(46))}." if retry else ""
    return (
        f"Groq daily token quota exhausted: {used:,} of {limit:,} used, and this request needed "
        f"{requested:,} more ({limit - used:,} left).{wait} A full run costs roughly 50-95k tokens, "
        f"so it is worth waiting for real headroom rather than starting a run that will die partway."
    )


def _parse_completion(completion: Any) -> LLMResponse:
    """Turn an SDK completion into our own dataclass, tolerating malformed tool arguments."""
    choice = completion.choices[0]
    message = choice.message

    calls: list[ToolCall] = []
    for raw in message.tool_calls or []:
        raw_arguments = raw.function.arguments or "{}"
        try:
            parsed = json.loads(raw_arguments)
            error = None
            if not isinstance(parsed, dict):
                parsed, error = {}, f"expected a JSON object, got {type(parsed).__name__}"
        except json.JSONDecodeError as exc:
            parsed, error = {}, str(exc)
        calls.append(
            ToolCall(
                id=raw.id,
                name=raw.function.name,
                arguments=parsed,
                raw_arguments=raw_arguments,
                parse_error=error,
            )
        )

    usage = getattr(completion, "usage", None)
    return LLMResponse(
        content=message.content or "",
        # gpt-oss returns its chain of thought here on Groq; absent on models that do not expose it.
        reasoning=getattr(message, "reasoning", None) or "",
        tool_calls=tuple(calls),
        finish_reason=choice.finish_reason or "",
        prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
        completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
    )
