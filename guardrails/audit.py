"""Append-only record of every tool call the agent attempts, allowed or not.

Entries are frozen dataclasses handed out as a tuple and mirrored to a JSONL file opened in
append mode.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

OUTCOME_ALLOWED = "allowed"
OUTCOME_REJECTED = "rejected"   # the validator refused it
OUTCOME_CAPPED = "capped"       # the call cap refused it
OUTCOME_ERROR = "error"         # it ran and blew up

OUTCOMES = (OUTCOME_ALLOWED, OUTCOME_REJECTED, OUTCOME_CAPPED, OUTCOME_ERROR)

# Detail text is truncated before it is stored to keep log lines readable.
MAX_DETAIL_CHARS = 400


@dataclass(frozen=True)
class AuditEntry:
    """One attempted call. Frozen, with arguments held as JSON text rather than a live dict."""

    seq: int
    timestamp: str
    function: str
    arguments_json: str
    outcome: str
    detail: str = ""
    duration_ms: float | None = None
    result_summary: str = ""

    @property
    def arguments(self) -> dict[str, Any]:
        """The call's arguments, as a fresh copy on every read."""
        return json.loads(self.arguments_json)

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "timestamp": self.timestamp,
            "function": self.function,
            "arguments": self.arguments,
            "outcome": self.outcome,
            "detail": self.detail,
            "duration_ms": self.duration_ms,
            "result_summary": self.result_summary,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True)


class AuditLog:
    """Append-only call log, optionally mirrored to a JSONL file.

    Exposes no way to delete, clear, reorder or overwrite an entry. `entries` returns a tuple
    of frozen records.
    """

    __slots__ = ("_entries", "_path")

    def __init__(self, path: str | Path | None = None) -> None:
        self._entries: list[AuditEntry] = []
        self._path = Path(path) if path is not None else None
        if self._path is not None:
            self._path.parent.mkdir(parents=True, exist_ok=True)

    def record(
        self,
        function: str,
        arguments: dict[str, Any] | None,
        outcome: str,
        detail: str = "",
        duration_ms: float | None = None,
        result_summary: str = "",
    ) -> AuditEntry:
        """Append one attempt and return the entry, which carries its sequence number."""
        if outcome not in OUTCOMES:
            raise ValueError(f"unknown audit outcome {outcome!r}; expected one of {OUTCOMES}")

        entry = AuditEntry(
            seq=len(self._entries) + 1,
            timestamp=datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            function=function,
            # default=str records an unserialisable argument rather than raising.
            arguments_json=json.dumps(arguments or {}, sort_keys=True, default=str),
            outcome=outcome,
            detail=_truncate(detail),
            duration_ms=round(duration_ms, 2) if duration_ms is not None else None,
            result_summary=_truncate(result_summary),
        )
        self._entries.append(entry)

        if self._path is not None:
            # Append mode only: "w" would truncate the existing log.
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(entry.to_json() + "\n")
        return entry

    @property
    def entries(self) -> tuple[AuditEntry, ...]:
        """Every attempt, oldest first, as an immutable tuple."""
        return tuple(self._entries)

    @property
    def path(self) -> Path | None:
        return self._path

    def __len__(self) -> int:
        return len(self._entries)

    def counts_by_outcome(self) -> dict[str, int]:
        """How many attempts landed in each outcome."""
        counts = {outcome: 0 for outcome in OUTCOMES}
        for entry in self._entries:
            counts[entry.outcome] += 1
        return counts

    def format_table(self, width: int = 118) -> str:
        """Render the log as an aligned table."""
        # Fixed columns: seq(3) + gap(2) + outcome(9) + gap(1) + function(20) + gap(1)
        #              + arguments(34) + gap(1). The remainder goes to the detail text.
        fixed = 71
        detail_width = max(20, width - fixed)
        header = f"{'#':>3}  {'outcome':<9} {'function':<20} {'arguments':<34} detail"
        lines = [header, "-" * width]
        for entry in self._entries:
            arguments = json.dumps(entry.arguments, sort_keys=True)
            lines.append(
                f"{entry.seq:>3}  {entry.outcome:<9} {entry.function[:19]:<20} "
                f"{arguments[:33]:<34} {entry.detail[:detail_width]}".rstrip()
            )
        counts = self.counts_by_outcome()
        lines.append("-" * width)
        lines.append(
            "  ".join(f"{outcome}={counts[outcome]}" for outcome in OUTCOMES)
            + f"  total={len(self._entries)}"
        )
        return "\n".join(lines)


def _truncate(text: str) -> str:
    """Truncate text to `limit`, marking anything cut."""
    text = str(text or "")
    return text if len(text) <= MAX_DETAIL_CHARS else text[: MAX_DETAIL_CHARS - 3] + "..."
