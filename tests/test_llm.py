"""
test_llm.py — pins the difference between the two rate limits Groq enforces.
Exists because treating them alike cost two batches of eval runs: a per-minute limit clears if you
wait, a per-day one does not, and retrying the latter four times produces "failed after 4 attempts",
which reads like a flaky network. These tests hold the distinction, the numbers in the message, and
the preflight that refuses to start a run the day's budget cannot finish.
"""

from __future__ import annotations

import pytest
from openai import APIConnectionError, RateLimitError

from agent.llm import (DailyQuotaExhausted, GroqClient, _is_daily_quota, _quota_message)


TPD_BODY = (
    "Error code: 429 - {'error': {'message': 'Rate limit reached for model "
    "`openai/gpt-oss-120b` in organization `org_01m0` service tier `on_demand` on tokens per day "
    "(TPD): Limit 200000, Used 199491, Requested 4128. Please try again in 26m3.408s.'}}"
)
TPM_BODY = (
    "Error code: 429 - {'error': {'message': 'Rate limit reached for model "
    "`openai/gpt-oss-120b` on tokens per minute (TPM): Limit 8000, Used 7900, Requested 4128. "
    "Please try again in 2.5s.'}}"
)


class _FakeResponse:
    status_code = 429
    headers: dict[str, str] = {}
    request = None


def rate_limit(body: str) -> RateLimitError:
    return RateLimitError(body, response=_FakeResponse(), body=None)


class RecordingClient:
    """Stands in for the OpenAI SDK client, counting how many times a request was actually sent."""

    def __init__(self, error: Exception) -> None:
        self.error = error
        self.attempts = 0
        self.completions = self
        self.chat = self

    def create(self, **kwargs):
        self.attempts += 1
        raise self.error


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-key-not-used")
    return GroqClient()


# --- telling the two limits apart -----------------------------------------------------------------

def test_a_daily_limit_is_recognised_and_a_per_minute_one_is_not():
    assert _is_daily_quota(rate_limit(TPD_BODY)) is True
    assert _is_daily_quota(rate_limit(TPM_BODY)) is False


def test_the_daily_message_carries_the_numbers_that_decide_whether_to_wait():
    message = _quota_message(rate_limit(TPD_BODY))
    assert "199,491 of 200,000" in message
    assert "509 left" in message          # the headroom, which a 1-token probe would have passed
    assert "26m3.408s" in message


def test_an_unrecognised_shape_is_passed_through_rather_than_swallowed():
    message = _quota_message(rate_limit("429 - something new and unparsed"))
    assert "something new and unparsed" in message


# --- what the retry loop does with each ------------------------------------------------------------

def test_a_daily_limit_fails_on_the_first_attempt_instead_of_backing_off(client):
    """Four backed-off retries against a daily cap waste 30 seconds to learn nothing."""
    fake = RecordingClient(rate_limit(TPD_BODY))
    client._client = fake

    with pytest.raises(DailyQuotaExhausted, match="199,491 of 200,000"):
        client.complete([{"role": "user", "content": "hi"}])
    assert fake.attempts == 1


def test_a_per_minute_limit_is_still_retried(client):
    """The limit that waiting actually fixes must keep its backoff."""
    from agent.llm import MAX_RETRIES

    fake = RecordingClient(rate_limit(TPM_BODY))
    client._client = fake
    monkeypatched_sleep = []

    import agent.llm as llm
    original = llm.time.sleep
    llm.time.sleep = monkeypatched_sleep.append
    try:
        with pytest.raises(RuntimeError, match=f"failed after {MAX_RETRIES} attempts"):
            client.complete([{"role": "user", "content": "hi"}])
    finally:
        llm.time.sleep = original

    assert fake.attempts == MAX_RETRIES
    assert len(monkeypatched_sleep) == MAX_RETRIES


def test_a_connection_error_is_retried_too(client):
    fake = RecordingClient(APIConnectionError(request=None))
    client._client = fake

    import agent.llm as llm
    original = llm.time.sleep
    llm.time.sleep = lambda _seconds: None
    try:
        with pytest.raises(RuntimeError):
            client.complete([{"role": "user", "content": "hi"}])
    finally:
        llm.time.sleep = original
    assert fake.attempts > 1


# --- the preflight ---------------------------------------------------------------------------------

def test_preflight_pads_the_prompt_because_the_daily_gate_measures_the_prompt(client):
    """Two bugs this pins. A 1-token probe passes on the 509 tokens left at the end of a quota. And
    reserving output instead — max_completion_tokens=5000 on a one-line prompt — also passes, which
    was measured against the live API with 1,146 tokens left. Only prompt size is gated daily."""
    from agent.llm import PREFLIGHT_TOKENS

    seen: dict = {}

    class Probe(RecordingClient):
        def create(self, **kwargs):
            seen.update(kwargs)
            return None

    client._client = Probe(None)
    client.preflight()

    prompt_chars = len(seen["messages"][0]["content"])
    assert prompt_chars // 4 >= PREFLIGHT_TOKENS * 0.8      # the prompt carries the weight
    assert seen["max_completion_tokens"] == 1               # and the reply costs nothing
    assert PREFLIGHT_TOKENS >= 2000


def test_preflight_raises_on_a_daily_limit(client):
    client._client = RecordingClient(rate_limit(TPD_BODY))
    with pytest.raises(DailyQuotaExhausted):
        client.preflight()


def test_preflight_tolerates_a_per_minute_limit(client):
    """A minute-scale limit says nothing about the day's budget, and the run's own retries clear it."""
    client._client = RecordingClient(rate_limit(TPM_BODY))
    client.preflight()          # must not raise
