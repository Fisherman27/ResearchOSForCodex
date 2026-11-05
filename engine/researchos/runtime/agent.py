"""Agent: a bounded, tool-using, traceable conversation loop.

An agent is *not* a prompt.  It is four things bound together:

    role + system prompt      what this agent is responsible for
    tool registry (scoped)    what this agent is allowed to do
    policy                    how one model turn is produced
    budget                    how many turns / tool calls it may spend

The loop is the same for every agent in the system, including the Director.
That is what makes "Director coordinates Research/Organization/Scheduler/Read"
a structural fact instead of a description: the Director is an agent whose
tools happen to be ``run_research``, ``run_literature``, ``advance_stage`` --
i.e. other agents.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .envelope import envelope, error, failed, ok
from .handoff import HandoffRequired, SessionStore, dict_to_message
from .messages import Message, ToolCall, bound_task_id, bound_paper_id
from .policy import Policy, PolicyRequest
from .tools import ToolRegistry
from .trace import Blackboard, EventLog, ToolTrace, TraceWriter, TurnTrace


@dataclass
class Budget:
    max_turns: int = 8
    max_tool_calls: int = 32

    def spendable(self, turns: int, calls: int) -> bool:
        return turns < self.max_turns and calls < self.max_tool_calls


@dataclass
class AgentContext:
    """Run-scoped services handed to every agent invocation."""

    run_id: str
    agent_id: str
    parent_id: Optional[str]
    blackboard: Blackboard
    log: EventLog
    traces: TraceWriter
    workspace: str
    registry: ToolRegistry
    settings: Dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentResult:
    agent_id: str
    role: str
    payload: Any = None
    text: str = ""
    turns: int = 0
    tool_calls: int = 0
    stopped_because: str = ""
    trace_path: str = ""

    def envelope(self) -> dict:
        return ok({"payload": self.payload, "text": self.text,
                   "turns": self.turns, "tool_calls": self.tool_calls,
                   "stopped_because": self.stopped_because,
                   "trace_path": self.trace_path},
                  diagnostics={"agent_id": self.agent_id, "role": self.role},
                  meta={"agent": self.agent_id})


@dataclass
class Agent:
    """Role + prompt + scoped tools + policy + budget + optional output contract.

    ``output_contract`` is enforced at the agent boundary: a payload that fails
    it never leaves the agent.  This is how "only a contract-valid
    ReadingResult enters Organization" is guaranteed structurally instead of
    being a sentence in a prompt.
    """

    role: str
    system_prompt: str
    tools: ToolRegistry
    policy: Policy
    budget: Budget = field(default_factory=Budget)
    id: str = ""
    output_contract: Optional[Callable[[Any], dict]] = None
    #: When set, the conversation persists across separate ``one_shot`` calls.
    #: This is what makes an agent an *agent* rather than a per-call function:
    #: it can accumulate what it learned, its own judgements, and revise them.
    #: Handoff pauses already persist the conversation; a persistent session
    #: extends the same mechanism to the ordinary multi-cycle loop.
    persistent_session: Any = None
    #: Optional ``fn(metadata) -> [{kind, path, ...}]``.  When set, the files a
    #: handoff order should carry are resolved from the turn's metadata, so the
    #: policy does not have to know how to find a paper.
    attachment_resolver: Any = None

    def converse(self, context: AgentContext, messages: List[Message], *,
                 metadata: Optional[dict] = None) -> AgentResult:
        """Run the think/act loop until the policy stops asking for tools."""
        reset = getattr(self.policy, "reset", None)
        if callable(reset):
            reset()
        conversation: List[Message] = list(messages)
        # In handoff mode the conversation must survive process restarts: the
        # host agent answers one turn at a time, possibly hours apart.
        session = getattr(self.policy, "session", None) or self.persistent_session
        session_agent = getattr(self.policy, "agent_key", "") or context.agent_id
        pending_calls: List[ToolCall] = []
        resolved: Dict[str, Any] = {}
        if isinstance(session, SessionStore):
            stored = session.load(session_agent)
            if stored:
                conversation = [dict_to_message(row)
                                for row in stored.get("messages") or []]
                pending_calls = [ToolCall(name=item["name"],
                                          arguments=item.get("arguments") or {},
                                          call_id=item.get("call_id") or "call_x")
                                 for item in stored.get("pending_calls") or []]
                resolved = dict(stored.get("resolved_calls") or {})
                # A persisted unanswered call belongs to the question that
                # produced it. If this turn names a DIFFERENT task, replaying it
                # would re-read the abandoned paper and drop the new task on the
                # floor — the state would hold the new task while the work order
                # still asked the old one. Drop the stale call and take the new
                # question; a resume (same task, or no task at all) is untouched.
                incoming_paper = ""
                for message in reversed(messages):
                    if message.role == "user" and (message.content or "").strip():
                        incoming_paper = bound_paper_id(message.content)
                        break
                stored_paper = ""
                for row in reversed(conversation):
                    if row.role == "user" and (row.content or "").strip():
                        stored_paper = bound_paper_id(row.content)
                        break
                if incoming_paper and stored_paper and incoming_paper != stored_paper:
                    conversation = conversation + [m for m in messages
                                                   if m.role == "user"]
                    pending_calls = []
                    resolved = {}
        turns = 0
        calls = 0
        last_text = ""
        stopped = "policy_finished"
        while True:
            if pending_calls:
                # A paused child had asked for tools that were never executed
                # (the pause happened while waiting on an external turn).  Run
                # them now before asking anyone for a new decision.
                results, paused = self._execute(
                    pending_calls, context, conversation, resolved=resolved)
                calls += len(results)
                if paused is not None:
                    if isinstance(session, SessionStore):
                        session.save(
                            session_agent,
                            [m.to_dict() for m in conversation],
                            pending_calls=[{"name": paused.name,
                                            "arguments": dict(paused.arguments),
                                            "call_id": paused.call_id}])
                    return AgentResult(
                        agent_id=context.agent_id, role=self.role,
                        payload={"awaiting_external_agent": True,
                                 "via": paused.name},
                        text="", turns=turns, tool_calls=calls,
                        stopped_because="awaiting_external_agent",
                        trace_path=context.traces.write(TurnTrace(
                            agent_id=context.agent_id, role=self.role,
                            turn=turns, stopped_because="awaiting_external_agent")))
                pending_calls = []
                resolved = {}
                if isinstance(session, SessionStore):
                    session.save(session_agent,
                                 [m.to_dict() for m in conversation])
                continue
            if not self.budget.spendable(turns, calls):
                stopped = "budget_exhausted"
                break
            turn_metadata = dict(metadata or {})
            if self.attachment_resolver is not None:
                try:
                    found = self.attachment_resolver(turn_metadata) or []
                except Exception:
                    found = []
                if found:
                    turn_metadata.setdefault("attachments", found)
            request = PolicyRequest(
                agent_id=context.agent_id, role=self.role,
                system_prompt=self.system_prompt,
                messages=list(conversation),
                tools=self.tools.describe(),
                metadata=turn_metadata)
            turn = turns + 1
            try:
                reply = self.policy.respond(request)
            except HandoffRequired as waiting:
                # The agent cannot proceed without an external LLM turn.  This
                # is a *pause*, not a failure: the work order is already on
                # disk, and the next run() continues from here.
                if isinstance(session, SessionStore):
                    # Persist completed sub-results too: a delegation that
                    # already finished must not be replayed on resume, or the
                    # child would be asked to do its work a second time.
                    session.save(session_agent,
                                 [message.to_dict() for message in conversation],
                                 resolved_calls=resolved)
                waiting_trace = TurnTrace(
                    agent_id=context.agent_id, role=self.role, turn=turn,
                    text="", stopped_because="awaiting_external_agent")
                trace_path = context.traces.write(waiting_trace)
                context.log.append("handoff_required", {
                    "order_id": waiting.order.order_id,
                    "agent_id": waiting.order.agent_id}, actor=context.agent_id)
                return AgentResult(agent_id=context.agent_id, role=self.role,
                                   payload={"awaiting_external_agent": True,
                                            "order_id": waiting.order.order_id},
                                   text="", turns=turn - 1, tool_calls=calls,
                                   stopped_because="awaiting_external_agent",
                                   trace_path=trace_path)
            last_text = reply.text or last_text
            conversation.append(reply.message())
            trace = TurnTrace(agent_id=context.agent_id, role=self.role,
                              turn=turn, text=reply.text, usage=dict(reply.usage))
            if not reply.wants_tools:
                trace.stopped_because = "final"
                trace_path = context.traces.write(trace)
                payload = reply.payload
                if self.output_contract is not None:
                    checked = self.output_contract(payload)
                    if isinstance(checked, dict) and checked.get("status"):
                        payload = {"__contract__": checked, "value": payload}
                if isinstance(session, SessionStore):
                    if self.persistent_session is None:
                        # A handoff pause is over: the conversation has served
                        # its purpose.  A persistent agent keeps it on purpose.
                        session.clear(session_agent)
                    else:
                        session.save(session_agent,
                                     [m.to_dict() for m in conversation])
                return AgentResult(agent_id=context.agent_id, role=self.role,
                                   payload=payload, text=last_text,
                                   turns=turn, tool_calls=calls,
                                   stopped_because="final", trace_path=trace_path)
            results, paused_call = self._execute(
                reply.tool_calls, context, conversation)
            calls += len(results)
            for call, result in zip(reply.tool_calls, results):
                trace.tool_calls.append(ToolTrace(
                    name=call.name, arguments=dict(call.arguments),
                    status=result.get("status", "failed"),
                    ok=bool(result.get("ok")),
                    warnings=list(result.get("warnings") or []),
                    error_code=(result.get("error") or {}).get("code")))
            if paused_call is not None:
                trace.stopped_because = "awaiting_external_agent"
                trace_path = context.traces.write(trace)
                if isinstance(session, SessionStore):
                    # Persist the conversation *and* the unanswered call: the
                    # child was asked but has not answered, so on resume the
                    # parent retries that same call instead of asking its model
                    # for a brand new decision.
                    session.save(
                        session_agent,
                        [m.to_dict() for m in conversation],
                        pending_calls=[{"name": paused_call.name,
                                        "arguments": dict(paused_call.arguments),
                                        "call_id": paused_call.call_id}],
                        resolved_calls=resolved)
                return AgentResult(
                    agent_id=context.agent_id, role=self.role,
                    payload={"awaiting_external_agent": True,
                             "via": paused_call.name},
                    text=last_text, turns=turn, tool_calls=calls,
                    stopped_because="awaiting_external_agent",
                    trace_path=trace_path)
            turns = turn
            context.traces.write(trace)
            if isinstance(session, SessionStore):
                session.save(session_agent,
                             [message.to_dict() for message in conversation])
        trace = TurnTrace(agent_id=context.agent_id, role=self.role,
                          turn=turns, text=last_text,
                          stopped_because=stopped)
        path = context.traces.write(trace)
        return AgentResult(agent_id=context.agent_id, role=self.role,
                           payload=None, text=last_text, turns=turns,
                           tool_calls=calls, stopped_because=stopped,
                           trace_path=path)

    def _execute(self, calls, context, conversation, resolved=None):
        """Execute tool calls in order, appending each result to the conversation.

        Returns ``(results, paused_call)``.  If a call hits a subagent that is
        waiting for an external turn, execution stops *before* appending that
        result: the call stays outstanding and will be re-attempted on resume.
        Recording it as a completed tool result would permanently replace the
        real answer with a pause marker.
        """
        results = []
        resolved = resolved if resolved is not None else {}
        for call in calls:
            if call.call_id in resolved:
                # This call already produced a real result before the pause and
                # must not be re-executed: delegating again would restart a
                # finished child and publish a duplicate work order.
                result = resolved[call.call_id]
                conversation.append(Message.tool(call, result))
                results.append(result)
                continue
            result = self.tools.call(call.name, call.arguments)
            context.log.append("tool_called", {
                "tool": call.name, "arguments": _trim(call.arguments),
                "status": result.get("status"), "ok": result.get("ok")},
                actor=context.agent_id)
            if (result.get("error") or {}).get("code") == \
                    "E2_AWAITING_EXTERNAL_AGENT":
                return results, call
            conversation.append(Message.tool(call, result))
            resolved[call.call_id] = result
            results.append(result)
        return results, None

    def one_shot(self, context: AgentContext, user_content: str, *,
                 metadata: Optional[dict] = None) -> AgentResult:
        """Single-message invocation, the common case for worker agents."""
        return self.converse(context, [Message.user(user_content)],
                             metadata=metadata)


def _trim(value: Any, limit: int = 200) -> Any:
    """Keep the event log readable without losing the argument's shape."""
    if isinstance(value, str) and len(value) > limit:
        return value[:limit] + "..."
    if isinstance(value, dict):
        return {key: _trim(item, limit) for key, item in value.items()}
    if isinstance(value, list):
        return [_trim(item, limit) for item in value[:20]]
    return value


def require_payload(result: AgentResult, *, code: str, hint: str) -> dict:
    """Turn an agent result into an envelope, enforcing that it produced JSON."""
    if result.stopped_because != "final":
        return failed(error(code, "Agent {} 未给出最终答复（{}）".format(
            result.agent_id, result.stopped_because),
            hint="提高 max_turns 或检查工具调用是否形成死循环",
            location="agents.{}".format(result.agent_id)))
    if result.payload is None:
        return failed(error(code, "Agent {} 的答复为空".format(result.agent_id),
                            hint=hint, location="agents.{}".format(result.agent_id)))
    return ok(result.payload)


