"""The allowlist: one declarative ToolSpec per analysis function.

Each spec names the function's parameters and the column roles and cardinality each parameter
accepts. The validator enforces it against a real profile; to_openai_tools renders it as tool
schemas.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from profiling.profiler import (
    ROLE_BOOLEAN,
    ROLE_CATEGORICAL,
    ROLE_DATETIME,
    ROLE_NUMERIC,
)
from toolkit.functions import (
    compute_correlation,
    detect_outliers,
    get_summary_stats,
    group_compare,
    value_counts,
)

PARAM_COLUMN = "column"
PARAM_ENUM = "enum"

# Role groups. These express *what kind of thing* a parameter needs.
MEASURABLE_ROLES = (ROLE_NUMERIC, ROLE_BOOLEAN)
GROUPABLE_ROLES = (ROLE_CATEGORICAL, ROLE_BOOLEAN, ROLE_NUMERIC, ROLE_DATETIME)
COUNTABLE_ROLES = (ROLE_CATEGORICAL, ROLE_BOOLEAN, ROLE_NUMERIC, ROLE_DATETIME)
# `identifier` and `empty` are in no group: grouping by a primary key produces one row per group,
# and an all-null column supports no operation.

# Column parameters also carry a cardinality bound, checked against the profile's real
# distinct_count at validation time. Role alone is not enough: `revenue_usd` is numeric, but
# grouping by it yields one row per group.
MAX_GROUP_KEY_DISTINCT = 30    # group_compare returns at most 20 groups; beyond ~30 the result
                               # would be a truncated view of the data
MAX_VALUE_COUNTS_DISTINCT = 100  # a distribution view stays readable wider than a comparison


@dataclass(frozen=True)
class ParamSpec:
    """One parameter of one tool, and the conditions an argument must meet to fill it."""

    name: str
    kind: str
    description: str
    required: bool = True
    allowed_roles: tuple[str, ...] = ()
    max_distinct: int | None = None
    choices: tuple[str, ...] = ()
    default: Any = None


@dataclass(frozen=True)
class ToolSpec:
    """One allowlisted function: its name, its parameters, and which arguments must not collide."""

    name: str
    fn: Callable[..., dict]
    description: str
    params: tuple[ParamSpec, ...]
    # Parameter groups that must not all name the same column. Correlating a column with itself is
    # r = 1.0 by definition, and stratifying a column by itself yields one group per value.
    distinct_columns: tuple[tuple[str, ...], ...] = ()

    def param(self, name: str) -> ParamSpec | None:
        return next((p for p in self.params if p.name == name), None)

    @property
    def param_names(self) -> tuple[str, ...]:
        return tuple(p.name for p in self.params)


TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="get_summary_stats",
        fn=get_summary_stats,
        description="Centre, spread, quartiles and missingness for one numeric column.",
        params=(
            ParamSpec(
                name="column",
                kind=PARAM_COLUMN,
                description="the numeric column to summarise",
                allowed_roles=MEASURABLE_ROLES,
            ),
        ),
    ),
    ToolSpec(
        name="compute_correlation",
        fn=compute_correlation,
        description=(
            "Pearson and Spearman correlation between two numeric columns. Pass group_by to also "
            "compute the correlation inside each subgroup and learn whether it survives the split."
        ),
        params=(
            ParamSpec("col_a", PARAM_COLUMN, "first numeric column", allowed_roles=MEASURABLE_ROLES),
            ParamSpec("col_b", PARAM_COLUMN, "second numeric column", allowed_roles=MEASURABLE_ROLES),
            ParamSpec(
                name="group_by",
                kind=PARAM_COLUMN,
                description="optional low-cardinality column to stratify the correlation by",
                required=False,
                allowed_roles=GROUPABLE_ROLES,
                max_distinct=MAX_GROUP_KEY_DISTINCT,
            ),
        ),
        distinct_columns=(("col_a", "col_b"), ("col_a", "group_by"), ("col_b", "group_by")),
    ),
    ToolSpec(
        name="detect_outliers",
        fn=detect_outliers,
        description="Flag unusual values in one numeric column, by z-score or by the IQR fence.",
        params=(
            ParamSpec(
                name="column",
                kind=PARAM_COLUMN,
                description="the numeric column to scan",
                # numeric only, not boolean: an 'outlier' in a 0/1 column is a category, not an anomaly
                allowed_roles=(ROLE_NUMERIC,),
            ),
            ParamSpec(
                name="method",
                kind=PARAM_ENUM,
                description="'zscore' (mean-based, can be masked by large outlier clusters) or 'iqr'",
                required=False,
                choices=("zscore", "iqr"),
                default="zscore",
            ),
        ),
    ),
    ToolSpec(
        name="group_compare",
        fn=group_compare,
        description="Compare a numeric column across the levels of a low-cardinality grouping column.",
        params=(
            ParamSpec(
                name="group_col",
                kind=PARAM_COLUMN,
                description="the column to group by",
                allowed_roles=GROUPABLE_ROLES,
                max_distinct=MAX_GROUP_KEY_DISTINCT,
            ),
            ParamSpec(
                name="value_col",
                kind=PARAM_COLUMN,
                description="the numeric column to compare across groups",
                allowed_roles=MEASURABLE_ROLES,
            ),
        ),
        distinct_columns=(("group_col", "value_col"),),
    ),
    ToolSpec(
        name="value_counts",
        fn=value_counts,
        description="Frequency of each value in a column, capped to the most common ones.",
        params=(
            ParamSpec(
                name="column",
                kind=PARAM_COLUMN,
                description="the column whose value distribution to count",
                allowed_roles=COUNTABLE_ROLES,
                max_distinct=MAX_VALUE_COUNTS_DISTINCT,
            ),
        ),
    ),
)

_BY_NAME: dict[str, ToolSpec] = {tool.name: tool for tool in TOOLS}


def get_tool(name: str) -> ToolSpec | None:
    """Look up a tool by name. Returns None for anything not on the allowlist."""
    return _BY_NAME.get(name)


def tool_names() -> tuple[str, ...]:
    """Every function the agent is permitted to call, in registration order."""
    return tuple(_BY_NAME)


def describe_toolkit() -> str:
    """Render the allowlist as readable text."""
    lines: list[str] = []
    for tool in TOOLS:
        lines.append(f"{tool.name}({', '.join(_render_param(p) for p in tool.params)})")
        lines.append(f"    {tool.description}")
        for param in tool.params:
            if param.kind == PARAM_COLUMN:
                bound = f", max {param.max_distinct} distinct" if param.max_distinct else ""
                lines.append(
                    f"      - {param.name}: roles {'/'.join(param.allowed_roles)}{bound}"
                )
            else:
                lines.append(f"      - {param.name}: one of {'/'.join(param.choices)}")
        lines.append("")
    return "\n".join(lines).rstrip()


def _render_param(param: ParamSpec) -> str:
    return param.name if param.required else f"{param.name}={param.default!r}"


def to_openai_tools(profile: dict[str, Any]) -> list[dict[str, Any]]:
    """Render the allowlist as OpenAI-style tool schemas, narrowed to one file's real columns.

    The model is only offered columns that would pass validation. The validator re-checks anyway,
    since a model can emit a name that was never in its enum.
    """
    roles = {column["name"]: column for column in profile["columns"]}
    schemas: list[dict[str, Any]] = []

    for tool in TOOLS:
        properties: dict[str, Any] = {}
        required: list[str] = []
        for param in tool.params:
            if param.kind == PARAM_COLUMN:
                allowed = [
                    name for name, info in roles.items()
                    if info["role"] in param.allowed_roles
                    and (param.max_distinct is None or info["distinct_count"] <= param.max_distinct)
                ]
                properties[param.name] = {
                    "type": "string",
                    "description": param.description,
                    "enum": allowed,
                }
            else:
                properties[param.name] = {
                    "type": "string",
                    "description": param.description,
                    "enum": list(param.choices),
                }
            if param.required:
                required.append(param.name)

        schemas.append(
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": {
                        "type": "object",
                        "properties": properties,
                        "required": required,
                        "additionalProperties": False,
                    },
                },
            }
        )
    return schemas
