"""Model-facing policy boundary.

An agent does not call a model SDK.  It calls a ``Policy``.  A policy receives
the conversation so far plus the tools it may ask for, and returns an
``AgentReply``.  Everything provider-specific (streaming, retries, auth,
tool-call formatting) lives behind this interface, which is why the framework
can be unit-tested with a scripted policy and no network access.

Two policies ship here:

* ``CallablePolicy`` wraps any function, so an existing ``executor(messages,
  tools)`` adapts without changes.
* ``ScriptedPolicy`` replays a fixed sequence of replies, used by the test
  suite to drive the whole system deterministically.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Protocol

from .messages import AgentReply, Message, ToolCall, coerce_reply


@dataclass
class PolicyRequest:
    """Everything a model needs, and nothing else."""

    agent_id: str
    role: str
    system_prompt: str
    messages: List[Message]
    tools: List[dict]
    metadata: Dict[str, Any] = field(default_factory=dict)

    def as_payload(self) -> dict:
        """A provider-neutral JSON view, handy for logging and replay."""
        return {"agent_id": self.agent_id, "role": self.role,
                "system_prompt": self.system_prompt,
                "messages": [message.to_dict() for message in self.messages],
                "tools": list(self.tools), "metadata": dict(self.metadata)}


class Policy(Protocol):
    name: str

    def respond(self, request: PolicyRequest) -> AgentReply:
        ...


class CallablePolicy:
    """Adapt ``fn(request) -> reply`` or ``fn(messages=..., tools=...) -> reply``."""

    name = "callable"

    def __init__(self, fn: Callable[..., Any], *, name: str = "callable"):
        self.fn = fn
        self.name = name

    def respond(self, request: PolicyRequest) -> AgentReply:
        try:
            return coerce_reply(self.fn(request))
        except TypeError:
            return coerce_reply(self.fn(
                messages=[message.to_dict() for message in
                          ([Message.system(request.system_prompt)] + request.messages)],
                tools=request.tools))


class ScriptedPolicy:
    """Deterministic policy for tests and dry runs.

    Accepts either a list of replies (consumed in order) or a function that maps
    a request to a reply.  A reply may be an AgentReply, a dict, or a JSON string.
    """

    name = "scripted"

    def __init__(self, script):
        self._script = script
        self._cursor = 0
        self.requests: List[PolicyRequest] = []

    def respond(self, request: PolicyRequest) -> AgentReply:
        self.requests.append(request)
        if callable(self._script):
            return coerce_reply(self._script(request))
        if self._cursor >= len(self._script):
            raise RuntimeError("scripted policy exhausted after {} replies".format(
                self._cursor))
        item = self._script[self._cursor]
        self._cursor += 1
        return coerce_reply(item)


class JsonPolicyMixin:
    """Helper for policies that must produce one JSON object.

    Kept separate from Policy so that a text-only agent does not get JSON
    semantics forced on it by accident.
    """

    @staticmethod
    def parse_json(text: str) -> Any:
        stripped = (text or "").strip()
        if stripped.startswith("```"):
            lines = [line for line in stripped.splitlines()
                     if not line.strip().startswith("```")]
            stripped = "\n".join(lines).strip()
        return json.loads(stripped)


def tool_call(name: str, **arguments) -> ToolCall:
    """Convenience constructor used by scripted tests and thin adapters."""
    return ToolCall(name=name, arguments=arguments)


class SingleToolPolicy:
    """Deterministic policy: call one tool, then return its envelope as payload.

    This is how a module agent performs a *deterministic* step while still
    being a real agent: the tool call, its arguments, its result and the final
    payload all appear in the agent trace.  Replacing this policy with an LLM
    policy is the only change needed to let the same agent make the decision
    itself, which is exactly the seam the upgrade is meant to expose.
    """

    name = "single_tool"

    def __init__(self, tool_name: str, arguments: Optional[dict] = None, *,
                 arguments_builder: Optional[Callable[[PolicyRequest], dict]] = None):
        self.tool_name = tool_name
        self.arguments = dict(arguments or {})
        self.arguments_builder = arguments_builder
        self._started = False

    def reset(self):
        self._started = False


    def respond(self, request: PolicyRequest) -> AgentReply:
        if not self._started:
            self._started = True
            args = (self.arguments_builder(request) if self.arguments_builder
                    else dict(self.arguments))
            return AgentReply(text="", tool_calls=[ToolCall(self.tool_name, args)])
        last = request.messages[-1] if request.messages else None
        if last is None or last.role != "tool":
            raise RuntimeError(
                "SingleToolPolicy expected a tool result from {}".format(
                    self.tool_name))
        try:
            payload = json.loads(last.content)
        except json.JSONDecodeError as exc:
            raise RuntimeError("tool result is not JSON: {}".format(exc))
        return AgentReply(text="", payload=payload)


class DirectivePolicy:
    """Deterministic policy for an agent that only reads its instruction.

    Used by the Director's "plan" turn: it answers with the payload that was
    passed in metadata, which keeps the state machine in charge while the
    agent boundary still exists and is traced.
    """

    name = "directive"

    def respond(self, request: PolicyRequest) -> AgentReply:
        return AgentReply(text="", payload=request.metadata.get("payload"))


@dataclass
class Step:
    """One declarative tool call in a deterministic agent plan.

    ``arguments`` may be a dict or a callable receiving ``(initial, results)``.
    ``as_`` names the tool result so later steps can depend on it.
    """

    tool: str
    arguments: Any = None
    as_: str = ""

    def resolve(self, initial: dict, results: dict) -> dict:
        arguments = self.arguments
        if callable(arguments):
            arguments = arguments(initial, results)
        return dict(arguments or {})


class PlanPolicy:
    """Deterministic multi-step policy used by supervisor agents.

    It is a *policy*, not a workflow: the agent loop still owns the turns, the
    tool registry still owns execution, and every step lands in the trace.  The
    plan simply encodes the order of operations that the module already had.
    Swapping this for an LLM policy is the intended upgrade path.
    """

    name = "plan"

    def __init__(self, steps, *, return_step: str = "", stop_on_error: bool = True):
        self.steps = list(steps)
        self.return_step = return_step
        self.stop_on_error = stop_on_error
        self._cursor = 0
        self._results: Dict[str, Any] = {}
        self._pending: Optional[Step] = None

    def reset(self):
        self._cursor = 0
        self._results = {}
        self._pending = None


    def respond(self, request: PolicyRequest) -> AgentReply:
        initial = dict(request.metadata.get("initial") or {})
        last = request.messages[-1] if request.messages else None
        if self._pending is not None and last is not None and last.role == "tool":
            try:
                value = json.loads(last.content)
            except json.JSONDecodeError as exc:
                raise RuntimeError("tool result is not JSON: {}".format(exc))
            key = self._pending.as_ or self._pending.tool
            self._results[key] = value
            failed_here = not value.get("ok")
            self._pending = None
            if failed_here and self.stop_on_error:
                return AgentReply(text="", payload=value)
        if self._cursor < len(self.steps):
            step = self.steps[self._cursor]
            self._cursor += 1
            self._pending = step
            return AgentReply(text="", tool_calls=[ToolCall(
                step.tool, step.resolve(initial, self._results))])
        if not self._results:
            raise RuntimeError("PlanPolicy produced no tool result")
        chosen = (self._results.get(self.return_step)
                  if self.return_step else list(self._results.values())[-1])
        return AgentReply(text="", payload=chosen)
