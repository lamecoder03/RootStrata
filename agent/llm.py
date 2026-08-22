"""Adapter over Groq's OpenAI-compatible chat completions endpoint.

Gives the planning loop one small interface that can be stubbed in tests. Tool schemas come from
registry.to_openai_tools(profile), narrowed to the columns of the loaded CSV.
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
# Low but not zero: the same file should produce the same reading twice.
DEFAULT_TEMPERATURE = 0.2
# gpt-oss exposes a reasoning budget on Groq.
DEFAULT_REASONING_EFFORT = "medium"

MAX_RETRIES = 4
BACKOFF_SECONDS = 2.0
# Prompt size preflight() sends. Real requests in a run cost 3,200-5,700 tokens, so the probe must
# ask for more than one real turn: it spends exactly what it proves.
PREFLIGHT_TOKENS = 6_000


class MissingCredentials(RuntimeError):
    """Raised when no GROQ_API_KEY is available."""


class DailyQuotaExhausted(RuntimeError):
    """The per-day token allowance is gone.

    Distinct from a per-minute limit, which waiting fixes. Raised immediately rather than retried.
    """


@dataclass(frozen=True)
class ToolCall:
    """One function call the model asked for. `raw_arguments` is kept for the trace."""

    id: str
    name: str
    arguments: dict[str, Any]
    raw_arguments: str
    parse_error: str | None = None


@dataclass(frozen=True)
class LLMResponse:
    """One assistant turn: its visible answer, its reasoning if exposed, and its tool calls."""

    content: str = ""
    reasoning: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    finish_reason: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0


class ChatModel(Protocol):
    """The interface the planning loop depends on. A stub implementing this can drive it offline."""

    def complete(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None
    ) -> LLMResponse: ...


def assistant_message(response: LLMResponse) -> dict[str, Any]:
    """Rebuild the assistant message to append to the transcript.

    Reconstructed from the dataclass rather than the SDK object, so a stubbed model produces
    identical transcripts to a real one.
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

    to_openai_tools() narrows every column parameter to the columns that would pass validation. An
    optional parameter left with no candidates is dropped; a required one left empty drops the
    whole tool.
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
        # Explicit path rather than load_dotenv()'s default, which inspects the caller's stack
        # frame and raises when imported from a REPL or a piped script.
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
        """Check there is room for a real turn before starting a run.

        Raises DailyQuotaExhausted. The prompt itself is padded to the size of a real turn, because
        the daily gate is measured on the prompt: a one-line prompt is admitted however much output
        it reserves.

        Two limits: it answers whether one turn fits, not whether a whole run will, and it spends
        what it proves. Hence a default larger than a single turn.
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
        """One chat completion, retrying the failures worth retrying."""
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
                    # Not every deployment accepts reasoning_effort. Drop it once and retry.
                if self._reasoning_effort and "reasoning" in str(exc).lower():
                    self._reasoning_effort = None
                    request.pop("reasoning_effort", None)
                    last_error = exc
                    continue
                raise
            except RateLimitError as exc:
                    # Backoff fixes a per-minute limit. A per-day limit will still be there in
                    # eight seconds, so report it once and stop.
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
    """Detect a daily quota error. Groq distinguishes the two limits only in the error text."""
    text = str(exc).lower()
    return "tokens per day" in text or "(tpd)" in text


def _quota_message(exc: Exception) -> str:
    """Pull the limit, usage and reset numbers out of Groq's error message.

    Falls back to the whole text if the message shape changes.
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
    """Turn an SDK completion into an LLMResponse, tolerating malformed tool arguments."""
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
