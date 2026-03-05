"""MCP tool registry — builds OpenAI function-calling schema from tool functions."""

import inspect
import json
import re
from collections.abc import Callable, Coroutine
from typing import Any

from ai_health_bot.mcp import tools as mcp_tools

# Map Python type annotations to JSON Schema types
_TYPE_MAP = {
    "str": "string",
    "int": "integer",
    "float": "number",
    "bool": "boolean",
    "dict": "object",
    "list": "array",
    "None": "null",
}


def _py_type_to_json(annotation: str) -> str:
    return _TYPE_MAP.get(annotation, "string")


def _parse_args_from_docstring(doc: str) -> dict[str, str]:
    """Extract 'Args:' section from docstring into {param: description} dict."""
    descriptions: dict[str, str] = {}
    in_args = False
    for line in doc.splitlines():
        stripped = line.strip()
        if stripped == "Args:":
            in_args = True
            continue
        if in_args:
            if stripped in {"Returns:", "Raises:", "Note:", "Notes:", "Example:", "Examples:"}:
                break
            m = re.match(r"^(\w+):\s*(.+)$", stripped)
            if m:
                descriptions[m.group(1)] = m.group(2)
    return descriptions


def _build_tool_schema(func: Callable) -> dict:
    """Build an OpenAI tool schema dict from a Python async function."""
    doc = inspect.getdoc(func) or ""
    # First paragraph = function description
    description = doc.split("\n\n")[0].replace("\n", " ").strip()

    param_descriptions = _parse_args_from_docstring(doc)
    sig = inspect.signature(func)

    properties: dict[str, dict] = {}
    required: list[str] = []

    for name, param in sig.parameters.items():
        ann = param.annotation
        ann_name = ann.__name__ if hasattr(ann, "__name__") else str(ann)
        # Handle Optional[X] → X
        ann_name = ann_name.replace("Optional[", "").replace("]", "").split("|")[0].strip()

        prop: dict[str, Any] = {"type": _py_type_to_json(ann_name)}
        if name in param_descriptions:
            prop["description"] = param_descriptions[name]

        properties[name] = prop

        # Required if no default
        if param.default is inspect.Parameter.empty:
            required.append(name)

    return {
        "type": "function",
        "function": {
            "name": func.__name__,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        },
    }


# Functions exposed to the LLM
_TOOL_FUNCTIONS: list[Callable] = [
    mcp_tools.query_prometheus,
    mcp_tools.query_prometheus_range,
    mcp_tools.get_active_alerts,
    mcp_tools.search_logs,
    mcp_tools.get_index_stats,
]

# Name → callable mapping for execution
TOOL_MAP: dict[str, Callable[..., Coroutine]] = {fn.__name__: fn for fn in _TOOL_FUNCTIONS}

# Pre-built schema list for OpenAI API
TOOLS_SCHEMA: list[dict] = [_build_tool_schema(fn) for fn in _TOOL_FUNCTIONS]


async def call_tool(name: str, arguments: str | dict) -> str:
    """
    Execute a tool by name with the given arguments.
    Returns the result serialized as a JSON string.
    """
    fn = TOOL_MAP.get(name)
    if fn is None:
        return json.dumps({"error": f"Unknown tool: {name!r}"})

    if isinstance(arguments, str):
        try:
            kwargs = json.loads(arguments)
        except json.JSONDecodeError as exc:
            return json.dumps({"error": f"Invalid JSON arguments: {exc}"})
    else:
        kwargs = arguments

    try:
        result = await fn(**kwargs)
        return json.dumps(result, default=str)
    except Exception as exc:
        return json.dumps({"error": str(exc)})
