"""A reusable state-machine policy.

Every supervisor in this system (Director, LiteratureSupervisor) is a real
agent: it has a prompt, a scoped tool registry and a trace.  What it does *not*
have is an LLM in the default configuration.  Instead its policy is a state
machine that inspects shared state and emits exactly one tool call per turn.

This keeps three properties the loop depends on:

1. the transition table is explicit and reviewable;
2. every transition is a traced tool call by a named agent;
3. swapping in an LLM policy changes only who picks the next step.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .messages import AgentReply, ToolCall
from .policy import PolicyRequest


@dataclass
class Decision:
    tool: str
    arguments: Dict[str, Any] = field(default_factory=dict)
    note: str = ""


@dataclass
class Final:
    payload: Any = None
    text: str = ""


class StateMachinePolicy:
    """``rules`` are tried in order; first non-None Decision wins."""

    name = "state_machine"

    def __init__(self, rules: List[Callable[[dict], Optional[Decision]]],
                 *, terminal: Callable[[dict], bool],
                 enter: Optional[Callable[[], dict]] = None,
                 state_reader: Optional[Callable[[], dict]] = None):
        self.rules = rules
        self.terminal = terminal
        self.enter = enter
        self.state_reader = state_reader
        self._started = False
        self._final: Optional[Any] = None

    def reset(self):
        self._started = False
        self._final = None


    def respond(self, request: PolicyRequest) -> AgentReply:
        if self.state_reader is not None:
            state = dict(self.state_reader() or {})
        else:
            state = dict(request.metadata.get("state") or {})
        if not self._started:
            self._started = True
            if self.enter is not None:
                state = self.enter() or state
        if self._final is not None:
            return AgentReply(text="", payload=self._final)
        if self.terminal(state):
            self._final = state
            return AgentReply(text="", payload=state)
        for rule in self.rules:
            decision = rule(state)
            if decision is None:
                continue
            return AgentReply(text=decision.note,
                              tool_calls=[ToolCall(decision.tool,
                                                   decision.arguments)])
        return AgentReply(text="no rule matched", payload=None)


class DelegatingPolicy:
    """Calls a fixed sequence of tools once, then returns the last envelope.

    Used for supervisors that have no branching: the state machine lives in the
    other agent, not here.
    """

    name = "delegating"

    def __init__(self, steps: List[Decision], *, return_last: bool = True):
        self.steps = steps
        self.return_last = return_last
        self._cursor = 0
        self._results: List[dict] = []

    def reset(self):
        self._cursor = 0
        self._results = []


    def respond(self, request: PolicyRequest) -> AgentReply:
        last = request.messages[-1] if request.messages else None
        if last is not None and last.role == "tool" and self._cursor > 0:
            import json
            try:
                self._results.append(json.loads(last.content))
            except json.JSONDecodeError:
                self._results.append({})
        if self._cursor < len(self.steps):
            step = self.steps[self._cursor]
            self._cursor += 1
            return AgentReply(text=step.note,
                              tool_calls=[ToolCall(step.tool, step.arguments)])
        return AgentReply(text="", payload=self._results[-1] if self._results else None)


class RouterPolicy:
    """Deterministic policy that routes one request to one named capability.

    The calling agent passes ``metadata["tool"]`` and ``metadata["arguments"]``.
    The module agent then performs exactly that call through its own registry,
    so the call appears in *this* agent's trace -- the agent is choosing among
    its capabilities, which is the part an LLM policy will later replace.

    ``fallback`` covers the case where no explicit routing was requested.
    """

    name = "router"

    def __init__(self, fallback: Optional[object] = None):
        self.fallback = fallback
        self._started = False

    def reset(self):
        self._started = False


    def respond(self, request: PolicyRequest) -> AgentReply:
        metadata = dict(request.metadata or {})
        tool = metadata.get("tool")
        # ``agent_tool`` flattens the routed arguments into the metadata and
        # also records them under ``initial.arguments``.  Prefer the nested copy:
        # a turn that re-enters the module carries older flat keys alongside, and
        # routing with those ran the *previous* tool with the *previous*
        # arguments (an answered order was re-asked forever).
        routed = (metadata.get("initial") or {}).get("arguments")
        if tool is not None and isinstance(routed, dict):
            metadata["arguments"] = dict(routed)
        if tool is None and metadata.get("decision_turn"):
            if self.fallback is not None:
                # A rule fallback is still a policy: it ranks from the same
                # situation the LLM would see.  It is the deterministic baseline
                # an experiment compares against, not a failure mode.
                return self.fallback.respond(request)
            return AgentReply(
                text="",
                payload={"selected": [], "reasons": {},
                         "note": "未配置决策策略"})
        if tool is None:
            if self.fallback is not None:
                return self.fallback.respond(request)
            return AgentReply(text="no tool requested", payload=None)
        # A routable call is stateless: it either issues the requested tool or
        # returns the most recent tool result.  ``_started`` must NOT leak
        # across separate invocations, because a persistent agent re-enters with
        # a different request each cycle.
        last = request.messages[-1] if request.messages else None
        if last is not None and last.role == "tool":
            import json
            try:
                return AgentReply(payload=json.loads(last.content))
            except json.JSONDecodeError:
                return AgentReply(payload=None)
        return AgentReply(text="", tool_calls=[ToolCall(
            tool, dict(metadata.get("arguments") or {}))])
