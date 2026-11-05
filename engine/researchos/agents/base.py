"""Agent construction kit.

Every concrete agent is built from the same three ingredients:

    * a scoped tool registry (built here from ports / sibling agents)
    * a system prompt (loaded from ``prompts/``)
    * a policy (rule / scripted / llm / supervisor)

This module owns the wiring so that individual agent files stay readable and
so that "which agent can touch what" is a single visible table.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

from ..config import SystemConfig
from ..ports.contracts import (OrganizationPort, ReadPort, ResearchPort,
                               SchedulerPort)
from ..runtime.agent import Agent, AgentContext, Budget
from ..runtime.envelope import blocked, ok
from ..runtime.envelope import error as envelope_error
from ..runtime.policy import Policy, SingleToolPolicy
from ..runtime.tools import Tool, ToolRegistry, ToolSpec, schema_from_signature
from ..runtime.trace import Blackboard, TraceWriter
from ..workflow.prompts import PromptLibrary
from ..workflow.state import RunStore


@dataclass
class Ports:
    research: ResearchPort
    organization: OrganizationPort
    scheduler: SchedulerPort
    read: ReadPort


@dataclass
class AgentKit:
    """Run-scoped wiring shared by every agent in one system instance."""

    config: SystemConfig
    prompts: PromptLibrary
    ports: Ports
    run_id: str
    store: RunStore
    blackboard: Blackboard
    traces: TraceWriter
    workspace: str
    policy_factory: Optional[Callable[[str, str], Policy]] = None
    registry: ToolRegistry = field(default_factory=ToolRegistry)
    # Every agent that can own a handoff work order, keyed by its kit agent id.
    # Populated by ``register_agent`` so a resume can dispatch directly to the
    # paused agent instead of replaying the whole parent chain.
    agent_index: Dict[str, Any] = field(default_factory=dict)
    #: Executor for the ideation capability.  ``None`` means "no model
    #: attached": the Scheduler then returns the assembled prompts so a host
    #: agent can answer them (the same rule as the handoff layer).
    ideation_executor: Any = None
    #: The handoff mailbox, when the run is in handoff mode.  Agents and the
    #: supervisor ask it whether an external answer has arrived, so a paused
    #: loop can distinguish "still waiting" from "resume now".
    handoff: Any = None

    def register_agent(self, agent: Any, name: Optional[str] = None) -> Any:
        """Index an agent under its id and any alias used in work orders.

        Subagents are addressed by ``<parent_id>.<tool_name>`` in a handoff
        order, which is not the same as the child's own ``id``.  Registering
        both names lets a paused order resume the right agent directly.
        """
        self.agent_index[agent.id] = agent
        if name:
            self.agent_index[name] = agent
        return agent

    def context(self, agent_id: str, *, parent_id: Optional[str] = None) -> AgentContext:
        return AgentContext(
            run_id=self.run_id, agent_id=agent_id, parent_id=parent_id,
            blackboard=self.blackboard, log=self.store.log,
            traces=self.traces, workspace=self.workspace,
            registry=self.registry,
            settings={"policy": self.config.policy_for(agent_id)})

    def budget(self, *, turns: Optional[int] = None,
               calls: Optional[int] = None) -> Budget:
        return Budget(max_turns=turns or self.config.max_agent_turns,
                      max_tool_calls=calls or max(8, self.config.max_agent_turns * 4))

    def resolve_policy(self, agent_id: str, default: Policy) -> Policy:
        if self.policy_factory is None:
            return default
        return self.policy_factory(agent_id, default.name) or default


def tool_from(fn: Callable[..., Any], *, name: Optional[str] = None,
              description: Optional[str] = None,
              parameters: Optional[dict] = None) -> Tool:
    tool_name = name or fn.__name__
    summary = description or (fn.__doc__ or tool_name).strip().split("\n")[0]
    schema = parameters or schema_from_signature(fn)
    return Tool(ToolSpec(tool_name, summary, schema), fn)


def make_tool(name: str, description: str, parameters: dict,
              handler: Callable[..., Any]) -> Tool:
    return Tool(ToolSpec(name, description, parameters), handler)


def agent_tool(child: Agent, *, name: str, description: str,
               parameters: Optional[dict] = None,
               payload_builder: Optional[Callable[[dict], str]] = None,
               kit: Optional[AgentKit] = None,
               parent_id: Optional[str] = None) -> Tool:
    """Expose a child agent as a callable tool of its parent.

    This is the subagent mechanism: the parent sees a tool, the child appears
    as a nested agent with its own prompt, tools, budget and trace directory.
    The parent therefore cannot reach into the child's evidence -- it only gets
    the envelope the child chose to report.

    ``extra`` keyword arguments are forwarded to the child both as task content
    and as ``metadata["initial"]``, so a deterministic child policy can read its
    inputs without parsing prose.
    """
    schema = parameters or {"type": "object", "properties": {
        "request": {"type": "string",
                    "description": "交给子智能体的完整任务描述"}},
        "required": [], "additionalProperties": True}

    def handler(request: str = "", **extra):
        if kit is None:
            raise RuntimeError("agent_tool requires kit to build a context")
        # ``__tool__`` lets a parent request a *specific* capability of the
        # child instead of letting the child choose.  Without it, a routed child
        # agent falls back to its default behaviour -- which is what makes the
        # same subagent usable both autonomously and under explicit direction.
        route = extra.pop("__tool__", None)
        # A subagent keeps its OWN identity.  Scoping it to the parent
        # (``director.ask_research``) would multiply identities per delegation
        # path and, in handoff mode, publish one work order per path instead of
        # one per module.  The parent link is recorded separately.
        child_id = child.id or name
        context = kit.context(child_id, parent_id=parent_id)
        if payload_builder is not None:
            content = payload_builder(extra)
        elif request:
            content = request
        else:
            content = json_dumps(extra)
        metadata = {"task": request, "initial": dict(extra),
                    "arguments": dict(extra)}
        if route:
            metadata["tool"] = route
        metadata.update(extra)
        result = child.one_shot(context, content, metadata=metadata)
        if result.stopped_because == "awaiting_external_agent":
            # The child has not produced an answer; it is waiting for its own
            # external turn.  The parent must therefore stay paused *without*
            # recording a tool result, otherwise on resume it would treat the
            # pause marker as the child's answer.  The runtime recognises this
            # code and keeps the call outstanding.
            return blocked(envelope_error(
                "E2_AWAITING_EXTERNAL_AGENT",
                "子智能体 {} 正在等待外部 LLM 回合".format(child_id),
                hint="宿主 Agent 需处理 handoff outbox 后重新 run()",
                context={"child_agent": child_id}),
                data={"child_agent": child_id}, meta={"child_agent": child_id})
        payload = result.payload
        # Transparent delegation: if the child produced an envelope, the parent
        # receives *that* envelope, annotated with who produced it.  Wrapping it
        # again would force every parent to unwrap `data.payload` by hand and
        # would hide the child's status from the parent's control flow.
        if isinstance(payload, dict) and "status" in payload and "ok" in payload:
            value = dict(payload)
        else:
            # An external agent may answer with the *domain* object rather than
            # a full envelope (a handoff reply is often just the result).  Treat
            # that as a successful result so the parent sees `data` directly
            # instead of a nested `data.payload`.
            value = ok(payload)
        meta = dict(value.get("meta") or {})
        meta["child_agent"] = child_id
        meta["child_trace"] = result.trace_path
        meta["child_turns"] = result.turns
        value["meta"] = meta
        return value

    return Tool(ToolSpec(name, description, schema), handler)


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=None)


def situation_payload(**values) -> str:
    return json.dumps(values, ensure_ascii=False)


__all__ = [
    "AgentKit", "Ports", "agent_tool", "make_tool", "tool_from",
    "SingleToolPolicy", "json_dumps",
]
