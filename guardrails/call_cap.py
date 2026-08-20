"""
call_cap.py — a hard ceiling on how many tool calls one agent run may attempt.
Exists because looping is an agent's default failure mode, and CLAUDE.md requires a limit that
cannot be quietly ignored: spend() RAISES CallCapExceeded instead of returning False, so there is no
branch a caller can forget to write. Every attempt is charged, including calls the validator rejects.
"""

from __future__ import annotations

# A budget that fits a report: enough calls to profile, follow two or three leads and stratify one
# of them, but not enough to wander. Day 3 can pass its own value; this is the default.
DEFAULT_MAX_CALLS = 25


class CallCapExceeded(RuntimeError):
    """Raised the moment an agent asks for one more call than its budget allows."""

    def __init__(self, limit: int, attempted: int) -> None:
        self.limit = limit
        self.attempted = attempted
        super().__init__(
            f"call cap reached: {limit} calls allowed, attempt #{attempted} refused"
        )


class CallCap:
    """A one-way counter. It only goes up, and it stops the run by raising when it hits the ceiling.

    There is deliberately no reset() and no setter for `used`: a cap you can rewind is not a cap, and
    a new run should construct a new CallCap. The counter is private, so the guarantee holds against
    a caller who forgets to check — not against one who reaches into `_used`, which Python cannot
    prevent and which would be a deliberate act rather than an oversight.
    """

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
        """The ceiling this run was constructed with."""
        return self._limit

    @property
    def used(self) -> int:
        """Attempts charged so far. Read-only — assigning to it raises AttributeError."""
        return self._used

    @property
    def remaining(self) -> int:
        """Attempts left before the next spend() raises."""
        return self._limit - self._used

    def spend(self) -> int:
        """Charge one attempt and return the new total, or raise CallCapExceeded at the ceiling."""
        if self._used >= self._limit:
            raise CallCapExceeded(self._limit, self._used + 1)
        self._used += 1
        return self._used

    def __repr__(self) -> str:
        return f"CallCap(used={self._used}, limit={self._limit})"
