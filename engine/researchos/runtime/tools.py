"""Tool registry: the only way an agent can affect the world.

A tool is a named, described, envelope-returning callable plus a JSON schema
for its arguments.  The runtime validates arguments, executes the handler,
normalises the envelope, and records every call in the agent's trace.

Design rules (deliberately enforced here, not by convention the agent can break):

* No tool result escapes validation: a handler that returns a bare ``None`` or
  raises an exception is converted into a ``failed`` envelope.
* Tool visibility is per-agent.  An agent only sees the tools registered for
  that agent id, so "Read cannot browse the knowledge base" is a property of
  the registry rather than a sentence in a prompt.
"""
from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional

from .envelope import EnvelopeError, STATUSES, envelope, error, failed, now


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: Dict[str, Any] = field(default_factory=dict)

    def to_prompt_dict(self) -> dict:
        return {"name": self.name, "description": self.description,
                "parameters": self.parameters}


@dataclass
class Tool:
    spec: ToolSpec
    handler: Callable[..., Any]
    max_result_chars: int = 20000

    def invoke(self, arguments: Optional[dict]) -> dict:
        args = dict(arguments or {})
        problem = _validate_arguments(self.spec, args)
        if problem:
            return failed(error("E2_TOOL_ARGUMENTS_INVALID",
                                "{}: {}".format(self.spec.name, problem),
                                hint="按工具声明修正参数",
                                location="tools.{}".format(self.spec.name)))
        try:
            value = self.handler(**args)
        except EnvelopeError:
            raise
        except Exception as exc:  # noqa: BLE001 - boundary
            import traceback
            return failed(error("E2_TOOL_RAISED", "{}: {}".format(
                type(exc).__name__, exc), hint="查看工具实现与输入",
                location="tools.{}".format(self.spec.name),
                cause=traceback.format_exc()[-2000:]))
        return _normalise(value, self.spec.name, self.max_result_chars)


def _normalise(value: Any, tool_name: str, limit: int) -> dict:
    if not isinstance(value, dict) or "status" not in value or "ok" not in value:
        raise EnvelopeError(value)
    if value["status"] not in STATUSES:
        raise EnvelopeError(value)
    for key in ("warnings", "diagnostics"):
        if not isinstance(value.get(key), (list, dict)):
            raise EnvelopeError(value)
    encoded = _size(value)
    if encoded <= limit:
        return value
    meta = dict(value.get("meta") or {})
    meta["truncated"] = True
    warning = "工具 {} 返回 {} 字符，超过 {} 上限；已截断".format(
        tool_name, encoded, limit)
    # Degrade structurally instead of discarding everything. Dropping the whole
    # payload to `data=None` left the caller unable to tell "too big" from
    # "empty": a reader asking where a PDF lives got null back, re-asked with
    # identical arguments, and the run spun (1435 identical blocked calls).
    # Dropping the largest keys keeps the small, orientation-carrying ones.
    data = value.get("data")
    if isinstance(data, dict) and data:
        trimmed = dict(data)
        dropped = []
        while trimmed and _size(dict(value, data=trimmed)) > limit:
            largest = max(trimmed, key=lambda key: _size({key: trimmed[key]}))
            dropped.append(largest)
            del trimmed[largest]
        meta["dropped_keys"] = dropped
        if trimmed:
            return envelope("partial", data=trimmed,
                            warnings=list(value.get("warnings") or []) + [
                                warning + "（已省略 {}）".format(
                                    "、".join(dropped))],
                            diagnostics=dict(value.get("diagnostics") or {}),
                            meta=meta)
    return envelope("partial", data=None,
                    warnings=list(value.get("warnings") or []) + [warning],
                    diagnostics=dict(value.get("diagnostics") or {}),
                    meta=meta)


def _size(value: Any) -> int:
    import json
    try:
        return len(json.dumps(value, ensure_ascii=False))
    except (TypeError, ValueError):
        return 0


def _validate_arguments(spec: ToolSpec, args: dict) -> str:
    schema = spec.parameters or {}
    properties = schema.get("properties") or {}
    required = schema.get("required") or []
    missing = [name for name in required if name not in args]
    if missing:
        return "缺少必填参数 {}".format(", ".join(missing))
    if schema.get("additionalProperties") is False:
        extra = [name for name in args if name not in properties]
        if extra:
            return "存在未声明参数 {}".format(", ".join(extra))
    for name, value in args.items():
        declared = properties.get(name)
        if not declared:
            continue
        types = declared.get("type")
        if not types:
            continue
        expected = types if isinstance(types, list) else [types]
        if not _type_ok(value, expected):
            return "参数 {} 类型应为 {}".format(name, "/".join(expected))
        if "enum" in declared and value not in declared["enum"]:
            return "参数 {} 必须是 {}".format(name, declared["enum"])
    return ""


def _type_ok(value: Any, expected: List[str]) -> bool:
    mapping = {
        "string": lambda v: isinstance(v, str),
        "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
        "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
        "boolean": lambda v: isinstance(v, bool),
        "array": lambda v: isinstance(v, list),
        "object": lambda v: isinstance(v, dict),
        "null": lambda v: v is None,
    }
    return any(mapping.get(name, lambda v: True)(value) for name in expected)


class ToolRegistry:
    """A per-agent view over a set of tools."""

    def __init__(self, tools: Optional[Iterable[Tool]] = None):
        self._tools: Dict[str, Tool] = {}
        for tool in tools or []:
            self.register(tool)

    def register(self, tool: Tool) -> "ToolRegistry":
        if tool.spec.name in self._tools:
            raise ValueError("duplicate tool: {}".format(tool.spec.name))
        self._tools[tool.spec.name] = tool
        return self

    def with_tools(self, specs: Iterable[tuple]) -> "ToolRegistry":
        """Build a derived registry: ``[(name, description, schema, fn), ...]``."""
        registry = ToolRegistry(self._tools.values())
        for name, description, schema, handler in specs:
            registry.register(Tool(ToolSpec(name, description, schema), handler))
        return registry

    def subset(self, names: Iterable[str]) -> "ToolRegistry":
        chosen = []
        for name in names:
            if name not in self._tools:
                raise KeyError("unknown tool: {}".format(name))
            chosen.append(self._tools[name])
        return ToolRegistry(chosen)

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    def __len__(self) -> int:
        return len(self._tools)

    def names(self) -> List[str]:
        return sorted(self._tools)

    def specs(self) -> List[ToolSpec]:
        return [self._tools[name].spec for name in self.names()]

    def describe(self) -> List[dict]:
        return [spec.to_prompt_dict() for spec in self.specs()]

    def call(self, name: str, arguments: Optional[dict] = None) -> dict:
        tool = self._tools.get(name)
        if tool is None:
            return failed(error("E2_TOOL_UNKNOWN",
                                "该 Agent 没有名为 {} 的工具".format(name),
                                hint="可用工具：{}".format(", ".join(self.names()) or "无"),
                                location="tools.call"))
        return tool.invoke(arguments)


def tool(name: str, description: str, parameters: Optional[dict] = None):
    """Decorator turning a function into a Tool."""

    def wrap(fn: Callable[..., Any]) -> Tool:
        return Tool(ToolSpec(name=name, description=description,
                             parameters=parameters or {"type": "object",
                                                       "properties": {}}), fn)
    return wrap


def function_tools(module, names: Iterable[str]) -> List[Tool]:
    """Adapt plain functions into tools, deriving argument schema from signature.

    Used while the real tool libraries are not yet ported: the scaffold exposes
    stub functions and this adapter keeps the call surface honest (the agent
    still sees real argument names and required/optional status).
    """
    tools = []
    for name in names:
        fn = getattr(module, name)
        tools.append(Tool(ToolSpec(name, (inspect.getdoc(fn) or "").split("\n")[0],
                                   schema_from_signature(fn)), fn))
    return tools


def schema_from_signature(fn: Callable[..., Any], *,
                          defaults: Optional[Dict[str, dict]] = None) -> dict:
    defaults = defaults or {}
    signature = inspect.signature(fn)
    properties: Dict[str, Any] = {}
    required: List[str] = []
    for parameter in signature.parameters.values():
        if parameter.name in ("self", "cls"):
            continue
        if parameter.kind in (parameter.VAR_POSITIONAL, parameter.VAR_KEYWORD):
            continue
        declared = dict(defaults.get(parameter.name) or {})
        declared.setdefault("type", _annotation_type(parameter.annotation))
        properties[parameter.name] = declared
        if parameter.default is parameter.empty:
            required.append(parameter.name)
    return {"type": "object", "properties": properties, "required": required,
            "additionalProperties": False}


def _annotation_type(annotation: Any) -> str:
    if annotation is inspect.Parameter.empty:
        return "string"
    # ``from __future__ import annotations`` turns annotations into strings, so
    # a tool built from a real function must resolve names, not compare objects.
    if isinstance(annotation, str):
        annotation = {"int": int, "float": float, "bool": bool, "str": str,
                      "list": list, "dict": dict}.get(annotation.strip(),
                                                       annotation)
    mapping = {int: "integer", float: "number", bool: "boolean",
               str: "string", list: "array", dict: "object"}
    return mapping.get(annotation, "string")


def timestamped(value: dict, *, tool: str, params: Optional[dict] = None) -> dict:
    meta = dict(value.get("meta") or {})
    meta.setdefault("tool", tool)
    meta["params"] = dict(params or {})
    meta.setdefault("timestamp", now())
    value["meta"] = meta
    return value
