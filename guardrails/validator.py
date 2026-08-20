"""
validator.py — checks a requested (function, arguments) pair against the toolkit allowlist and the
loaded CSV's real profile: the function must exist, every argument must be declared, and every column
argument must name a real column whose inferred role and cardinality suit the operation.
Exists because each file has a different schema, so it raises a ValidationError naming what would work.
"""

from __future__ import annotations

import difflib
from typing import Any

from profiling.profiler import ROLE_EMPTY
from toolkit.registry import PARAM_COLUMN, PARAM_ENUM, ParamSpec, ToolSpec, get_tool, tool_names

# How many alternatives to name in a rejection message. Bounded for the same reason tool output is:
# the message is fed back to the model, and a 200-column list is not a helpful correction.
MAX_SUGGESTIONS = 8

# Error codes, so callers can branch on the kind of failure without parsing prose.
UNKNOWN_FUNCTION = "UNKNOWN_FUNCTION"
UNKNOWN_ARGUMENT = "UNKNOWN_ARGUMENT"
MISSING_ARGUMENT = "MISSING_ARGUMENT"
INVALID_ARGUMENT_TYPE = "INVALID_ARGUMENT_TYPE"
UNKNOWN_COLUMN = "UNKNOWN_COLUMN"
WRONG_COLUMN_ROLE = "WRONG_COLUMN_ROLE"
COLUMN_TOO_WIDE = "COLUMN_TOO_WIDE"
EMPTY_COLUMN = "EMPTY_COLUMN"
INVALID_ENUM_VALUE = "INVALID_ENUM_VALUE"
DUPLICATE_COLUMN = "DUPLICATE_COLUMN"


class ValidationError(Exception):
    """A rejected call. Carries a machine-readable code and, where possible, a usable alternative."""

    def __init__(self, code: str, message: str, suggestion: str = "") -> None:
        self.code = code
        self.suggestion = suggestion
        super().__init__(f"{message}{(' ' + suggestion) if suggestion else ''}")


def validate_call(
    profile: dict[str, Any], function_name: str, arguments: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Validate one call against one file's profile; return the normalised arguments or raise.

    It returns arguments rather than a boolean on purpose. A check whose failure mode is a falsy
    return value is a check somebody eventually forgets to read; a raise cannot be ignored, and the
    normalised dict is what the caller needs next anyway.
    """
    arguments = dict(arguments or {})
    tool = get_tool(function_name)
    if tool is None:
        raise ValidationError(
            UNKNOWN_FUNCTION,
            f"{function_name!r} is not on the toolkit allowlist.",
            f"Allowed functions: {', '.join(tool_names())}.",
        )

    _reject_undeclared_arguments(tool, arguments)

    columns = {column["name"]: column for column in profile["columns"]}
    resolved: dict[str, Any] = {}

    for param in tool.params:
        value = arguments.get(param.name)
        if value is None:
            if param.required:
                raise ValidationError(
                    MISSING_ARGUMENT,
                    f"{tool.name} requires the argument {param.name!r} ({param.description}).",
                )
            resolved[param.name] = param.default
            continue

        if param.kind == PARAM_COLUMN:
            resolved[param.name] = _validate_column(tool, param, value, columns)
        elif param.kind == PARAM_ENUM:
            resolved[param.name] = _validate_enum(tool, param, value)
        else:  # pragma: no cover - guards against a registry entry with an unhandled kind
            raise ValidationError(
                INVALID_ARGUMENT_TYPE, f"parameter kind {param.kind!r} has no validation rule."
            )

    _reject_colliding_columns(tool, resolved)
    return resolved


def _reject_undeclared_arguments(tool: ToolSpec, arguments: dict[str, Any]) -> None:
    """Refuse arguments the spec never declared, so nothing rides in on **kwargs."""
    unexpected = sorted(set(arguments) - set(tool.param_names))
    if unexpected:
        raise ValidationError(
            UNKNOWN_ARGUMENT,
            f"{tool.name} does not accept the argument(s) {', '.join(repr(a) for a in unexpected)}.",
            f"It accepts: {', '.join(tool.param_names)}.",
        )


def _validate_column(
    tool: ToolSpec, param: ParamSpec, value: Any, columns: dict[str, dict[str, Any]]
) -> str:
    """Check one column argument against the file's actual schema, in existence -> role -> width order."""
    if not isinstance(value, str):
        raise ValidationError(
            INVALID_ARGUMENT_TYPE,
            f"{tool.name}.{param.name} must be a column name as a string, got {type(value).__name__}.",
        )

    info = columns.get(value)
    if info is None:
        close = difflib.get_close_matches(value, list(columns), n=3, cutoff=0.6)
        raise ValidationError(
            UNKNOWN_COLUMN,
            f"column {value!r} does not exist in {profile_name(columns)}.",
            f"Did you mean: {', '.join(close)}?" if close
            else f"Available columns: {_bounded(list(columns))}.",
        )

    role = info["role"]
    if role == ROLE_EMPTY:
        raise ValidationError(
            EMPTY_COLUMN,
            f"column {value!r} is entirely null, so {tool.name} has nothing to work with.",
        )

    if role not in param.allowed_roles:
        usable = [name for name, c in columns.items() if c["role"] in param.allowed_roles]
        raise ValidationError(
            WRONG_COLUMN_ROLE,
            f"{tool.name}.{param.name} needs a column of role "
            f"{'/'.join(param.allowed_roles)}, but {value!r} is {role}.",
            f"Columns that would work: {_bounded(usable)}." if usable
            else "No column in this file has a suitable role.",
        )

    # The cardinality bound is what stops a *correctly typed* but meaningless argument — a wide
    # categorical used as a group key. Role says what it is; distinct_count says whether it fits.
    if param.max_distinct is not None and info["distinct_count"] > param.max_distinct:
        narrower = [
            name for name, c in columns.items()
            if c["role"] in param.allowed_roles and c["distinct_count"] <= param.max_distinct
        ]
        raise ValidationError(
            COLUMN_TOO_WIDE,
            f"{tool.name}.{param.name} allows at most {param.max_distinct} distinct values, but "
            f"{value!r} has {info['distinct_count']}.",
            f"Narrower columns: {_bounded(narrower)}." if narrower
            else "No column in this file is narrow enough for this parameter.",
        )

    return value


def _validate_enum(tool: ToolSpec, param: ParamSpec, value: Any) -> str:
    """Check a fixed-choice argument. Case-sensitive: the registry is the spelling authority."""
    if not isinstance(value, str) or value not in param.choices:
        raise ValidationError(
            INVALID_ENUM_VALUE,
            f"{tool.name}.{param.name} must be one of {', '.join(repr(c) for c in param.choices)}, "
            f"got {value!r}.",
        )
    return value


def _reject_colliding_columns(tool: ToolSpec, resolved: dict[str, Any]) -> None:
    """Refuse calls where two parameters name the same column and the result would be degenerate."""
    for group in tool.distinct_columns:
        named = [resolved.get(name) for name in group]
        present = [value for value in named if value is not None]
        if len(present) > 1 and len(set(present)) < len(present):
            raise ValidationError(
                DUPLICATE_COLUMN,
                f"{tool.name} needs {' and '.join(group)} to be different columns, "
                f"but all were {present[0]!r}.",
            )


def profile_name(columns: dict[str, Any]) -> str:
    """Small helper so the 'unknown column' message says what it searched, not just that it failed."""
    return f"this file ({len(columns)} columns)"


def _bounded(names: list[str]) -> str:
    """Join a list of column names, truncating so a rejection message stays prompt-sized."""
    if len(names) <= MAX_SUGGESTIONS:
        return ", ".join(names)
    return f"{', '.join(names[:MAX_SUGGESTIONS])} (+{len(names) - MAX_SUGGESTIONS} more)"
