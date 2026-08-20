"""
llm.py — thin adapter over Groq's OpenAI-compatible chat completions endpoint.
Exists so the planning loop depends on one small interface it can be stubbed against in tests, rather
than on the SDK. Tool schemas come from registry.to_openai_tools(profile), narrowed to the columns of
whichever CSV is loaded, so the model is only ever offered arguments that exist in this file.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
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


class MissingCredentials(RuntimeError):
    """Raised when no GROQ_API_KEY is available, with the fix rather than a stack trace."""


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
        load_dotenv()
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
            except (RateLimitError, APIConnectionError) as exc:
                last_error = exc
            except APIStatusError as exc:
                if exc.status_code < 500:
                    raise
                last_error = exc
            time.sleep(BACKOFF_SECONDS * (2**attempt))

        raise RuntimeError(f"Groq call failed after {MAX_RETRIES} attempts: {last_error}")


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
