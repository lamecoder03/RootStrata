"""Hard ceiling on the number of tool calls an agent run may attempt.

spend() raises CallCapExceeded when the budget is exhausted. Every attempt is charged,
including calls the validator rejects.
"""

from __future__ import annotations

# Default tool-call budget for a run; callers may pass their own.
DEFAULT_MAX_CALLS = 25


class CallCapExceeded(RuntimeError):
    """Raised when an agent attempts more calls than its budget allows."""

    def __init__(self, limit: int, attempted: int) -> None:
        self.limit = limit
        self.attempted = attempted
        super().__init__(
            f"call cap reached: {limit} calls allowed, attempt #{attempted} refused"
        )


class CallCap:
    """Counter with a fixed ceiling. Has no reset; construct a new instance per run."""

    __slots__ = ("_limit", "_used")

    def __init__(self, limit: int = DEFAULT_MAX_CALLS) -> None:
        if not isinstance(limit, int) or isinstance(limit, bool):
            raise TypeError(f"call cap limit must be an int, got {type(limit).__name__}")
        if limit < 1:
            raise ValueError(f"call cap limit must be at least 1, got {limit}")
        self._limit = limit
        self._used = 0

    @property
    def limit(self) -> int:
        """The configured ceiling."""
        return self._limit

    @property
    def used(self) -> int:
        """Attempts charged so far. Read-only."""
        return self._used

    @property
    def remaining(self) -> int:
        """Attempts left before spend() raises."""
        return self._limit - self._used

    def spend(self) -> int:
        """Charge one attempt and return the new total. Raises CallCapExceeded at the ceiling."""
        if self._used >= self._limit:
            raise CallCapExceeded(self._limit, self._used + 1)
        self._used += 1
        return self._used

    def __repr__(self) -> str:
        return f"CallCap(used={self._used}, limit={self._limit})"
