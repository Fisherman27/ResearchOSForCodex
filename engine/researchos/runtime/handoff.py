"""Hand-off protocol: let an *external* agent be the LLM for one subagent.

This is the layer that makes the "hand the system to DSH and let it spawn
one subagent per role" workflow work.

The runtime never calls a model itself.  Instead, when an agent is configured
with a :class:`HandoffPolicy`, every think/act turn is materialised as a JSON
**work order** on disk:

    runs/<run>/handoff/outbox/<seq>-<agent>.json

The host agent (Codex, DSH, a human) reads that file, spawns or continues a real
LLM subagent with exactly the system prompt, conversation and tool list it
contains, and writes the reply to:

    runs/<run>/handoff/inbox/<seq>-<agent>.json

A reply is one of two shapes::

    {"tool_calls": [{"name": "...", "arguments": {...}}]}
    {"payload": {...}}            # final answer for this agent

On the next :meth:`ResearchSystem.run` the runtime consumes the reply.  If it
asked for tools, the runtime executes them *inside the process that owns the
knowledge base* and writes a follow-up work order containing the tool results.
If it supplied a payload, the agent finishes and the orchestrator continues.

So the division of labour is:

    host agent  ->  thinking (one LLM subagent per system agent)
    the engine  ->  tool execution, contracts, state, gates

The model never gets raw filesystem or database access; it only ever gets the
tools that were granted to that agent.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .messages import AgentReply, Message, ToolCall, bound_task_id
from .policy import PolicyRequest


AWAITING = "awaiting_external_agent"
_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _safe(value: str) -> str:
    return _SAFE.sub("_", value or "agent")


class HandoffRequired(RuntimeError):
    """Raised when an agent needs an external turn that has not arrived yet."""

    def __init__(self, order: "WorkOrder"):
        super().__init__("awaiting external agent for {}".format(order.agent_id))
        self.order = order


@dataclass
class WorkOrder:
    """Everything an external LLM needs, and nothing more."""

    seq: int
    agent_id: str
    role: str
    system_prompt: str
    messages: List[dict]
    tools: List[dict]
    #: The message the external agent must answer.  With a persistent agent the
    #: conversation accumulates, so "the last message" is not reliable: an empty
    #: assistant turn can follow the question.
    question: str = ""
    #: Files the external agent is authorised to read, as ``{kind, path}``.
    #: The mailbox passes *references*, not bytes: a PDF is large and the host
    #: already has filesystem access, so copying it through JSON would be waste.
    attachments: List[dict] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    reply_contract: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "order_id": self.order_id,
            "seq": self.seq,
            "agent_id": self.agent_id,
            "role": self.role,
            "system_prompt": self.system_prompt,
            "messages": self.messages,
            "question": self.question,
            "tools": self.tools,
            "attachments": self.attachments,
            "metadata": self.metadata,
            "reply_contract": self.reply_contract,
        }

    @property
    def order_id(self) -> str:
        return "{:04d}-{}".format(self.seq, _safe(self.agent_id))

    @classmethod
    def build(cls, seq: int, request: PolicyRequest) -> "WorkOrder":
        order = cls(
            seq=seq, agent_id=request.agent_id, role=request.role,
            system_prompt=request.system_prompt,
            messages=[message.to_dict() for message in request.messages],
            tools=list(request.tools),
            question=_question(request),
            attachments=list(request.metadata.get("attachments") or []),
            metadata=dict(request.metadata),
            reply_contract=REPLY_CONTRACT)
        # Host-facing classification (kind/title/guidance).  It is derived from
        # the fields above and used only for display and routing, so a failure
        # here must never block publication.
        try:
            from ..host_guide import describe
            host = describe(order.to_dict())
            order.metadata.setdefault("order_kind", host["kind"])
            order.metadata.setdefault("title", host["title"])
        except Exception:  # noqa: BLE001 - presentation must not break delivery
            pass
        return order


REPLY_CONTRACT = {
    "summary": "写回 inbox/<order_id>.json，二选一：请求工具，或给出最终结果。",
    "shapes": {
        "tool_calls": {"tool_calls": [{"name": "<tool>",
                                       "arguments": {"<arg>": "<value>"}}]},
        "final": {"payload": {"<你的结果>": "..."}},
    },
    "rules": [
        "只能调用 order.tools 中声明的工具，参数必须符合其 JSON Schema。",
        "一次可以请求多个工具调用；结果会在下一个 order 中返回。",
        "给出 payload 即结束该 Agent 的本次任务，之后不能再调用工具。",
        "payload 必须满足该 Agent 的输出契约（若 metadata 中有说明）。",
        "不要臆造工具结果；看不到的数据就说看不到。",
    ],
}


class HandoffQueue:
    """File-based mailbox between the runtime and the host agent."""

    def __init__(self, root: str):
        self.root = os.path.abspath(root)
        self.outbox = os.path.join(self.root, "outbox")
        self.inbox = os.path.join(self.root, "inbox")
        self.archive = os.path.join(self.root, "done")
        #: Withdrawn orders. Separate from `done/`, which means "answered".
        self.cancelled = os.path.join(self.root, "cancelled")
        for path in (self.outbox, self.inbox, self.archive):
            os.makedirs(path, exist_ok=True)
        self._seq = self._max_seq()

    def _max_seq(self) -> int:
        highest = 0
        for directory in (self.outbox, self.inbox, self.archive):
            for name in os.listdir(directory):
                match = re.match(r"^(\d{4})-", name)
                if match:
                    highest = max(highest, int(match.group(1)))
        return highest

    def next_seq(self) -> int:
        self._seq += 1
        return self._seq

    # -- writing ---------------------------------------------------------
    def publish(self, order: WorkOrder) -> str:
        path = os.path.join(self.outbox, order.order_id + ".json")
        _write_json(path, order.to_dict())
        return path

    def reply(self, order_id: str, reply: dict) -> str:
        path = os.path.join(self.inbox, order_id + ".json")
        _write_json(path, reply)
        return path

    # -- reading ---------------------------------------------------------
    def pending(self) -> List[dict]:
        rows = []
        for name in sorted(os.listdir(self.outbox)):
            if not name.endswith(".json"):
                continue
            path = os.path.join(self.outbox, name)
            with open(path, encoding="utf-8") as handle:
                rows.append(json.load(handle))
        return rows

    def claim(self, order_id: str) -> Optional[dict]:
        """Consume a reply, archiving both sides.  Returns the reply or None."""
        reply_path = os.path.join(self.inbox, order_id + ".json")
        if not os.path.isfile(reply_path):
            return None
        with open(reply_path, encoding="utf-8") as handle:
            reply = json.load(handle)
        # A malformed host answer must remain retryable.  Previously both the
        # order and its answer were moved to ``done/`` before the policy parsed
        # the envelope.  If parsing then failed, the run was left in
        # ``awaiting_external_agent`` with an empty outbox: there was no job the
        # host could correct or resume.  Every ResearchOS handoff uses the same
        # top-level envelope, so validate it before either side is archived.
        if not isinstance(reply, dict):
            raise ValueError("handoff reply must be a JSON object")
        if "tool_calls" not in reply and "payload" not in reply:
            raise ValueError("reply must contain either tool_calls or payload")
        order_path = os.path.join(self.outbox, order_id + ".json")
        _move(reply_path, self.inbox, self.archive, order_id + ".reply.json")
        if os.path.isfile(order_path):
            _move(order_path, self.outbox, self.archive, order_id + ".order.json")
        return reply

    def reopen(self, order_id: str) -> bool:
        """Restore an archived order after a failed legacy claim.

        New malformed replies are never archived, but existing runs may have
        been stranded by the old claim-before-validate behaviour.  Reopening
        restores only the order; the rejected reply remains in ``done/`` as an
        audit record and the host can submit a corrected answer normally.
        """
        archived = os.path.join(self.archive, order_id + ".order.json")
        target = os.path.join(self.outbox, order_id + ".json")
        if os.path.isfile(target):
            return True
        if not os.path.isfile(archived):
            return False
        _move(archived, self.archive, self.outbox, order_id + ".json")
        return True

    def retire(self, order_id: str) -> bool:
        """Withdraw an unanswered order, archiving it under `cancelled/`.

        Not `done/`: an archived order there implies an answer was applied. A
        withdrawn order is the opposite — it can never be answered, and leaving
        it in the outbox blocks the host from advancing at all, because `step`
        refuses to move while anything is unanswered.
        """
        order_path = os.path.join(self.outbox, order_id + ".json")
        if not os.path.isfile(order_path):
            return False
        os.makedirs(self.cancelled, exist_ok=True)
        return _move(order_path, self.outbox, self.cancelled,
                     order_id + ".order.json")

    def has_reply(self, order_id: str) -> bool:
        return os.path.isfile(os.path.join(self.inbox, order_id + ".json"))


def _move(source: str, _from: str, target_dir: str, name: str) -> None:
    os.replace(source, os.path.join(target_dir, name))


def _write_json(path: str, value: Any) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix="handoff-", suffix=".json",
                                dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


class HandoffPolicy:
    """Policy that delegates every turn to an external agent via the queue.

    Behaviour per turn:

    1. if the previous order for this conversation has an inbox reply, consume
       it: tool_calls -> execute inside the runtime; payload -> finish;
    2. otherwise publish a new work order and raise :class:`HandoffRequired`,
       which the agent loop turns into ``stopped_because=awaiting_external_agent``.
    """

    name = "handoff"

    def __init__(self, queue: HandoffQueue, *, agent_id: str = "",
                 session=None, agent_key: str = "", ledger=None):
        self.queue = queue
        self.agent_id = agent_id
        self.session = session
        self.ledger = ledger
        # Intentionally empty: sessions are keyed by the *context* id so that a
        # subagent addressed through two different parents keeps two histories.
        self.agent_key = ""
        self._orders: Dict[str, WorkOrder] = {}
        #: Answers already consumed from the mailbox but not yet used by the
        #: planning path.  Kept so a resume cannot lose a decision.
        self._unclaimed: Dict[str, Any] = {}
        self._completed: Dict[str, Dict[str, Any]] = {}

    def reset(self):
        self._orders.clear()

    def respond(self, request: PolicyRequest) -> AgentReply:
        # Idempotency: if this agent already finished exactly this request, hand
        # back the same answer instead of publishing a fresh order.  Without it,
        # a parent that replays a paused delegation after the child finished
        # would restart the child, which publishes a new order, and the protocol
        # would cycle forever.
        # Idempotency is scoped to *retries of the same turn*, not to every
        # turn of a persistent agent.  A decision turn must be answered fresh
        # each cycle: the situation has changed, and replaying the previous
        # answer would make the agent decide the same thing forever.
        # Delegation (`__tool__` routing) is different -- it is deterministic
        # given the same inputs, so those answers are safe to reuse.
        key = _request_key(request)
        # A *delegation* is the supervisor asking a module to use one capability
        # (``metadata.tool``).  A decision turn inside a routed module carries
        # ``decision_turn`` and must stay fresh.  When both flags appear the
        # turn is re-entering the module for a *different* request (the next
        # reading, the next ideation call), which is a delegation again.  Without
        # this, every reading after the first looked like a new decision, the
        # module re-answered its own already-answered order, and the loop never
        # moved.
        delegating = bool(request.metadata.get("tool")) and (
            not request.metadata.get("decision_turn")
            or bool(request.metadata.get("initial")))
        if delegating:
            cached = (self.ledger.get(request.agent_id, key)
                      if self.ledger is not None else None)
            if cached is None:
                cached = self._completed.get(request.agent_id, {}).get(key)
            if cached is not None:
                return AgentReply(payload=cached)
        # One Agent object can be invoked under several context ids (``research``
        # directly, ``director.ask_research`` when delegated to).  Orders are
        # per-context, so the bookkeeping must be too -- a single ``_order`` slot
        # would let one caller consume another caller's reply.
        unclaimed = self._unclaimed.pop(request.agent_id, None)
        if unclaimed is not None and not delegating:
            return AgentReply(payload=unclaimed)
        # A request that names a concrete task is identified by that task. A
        # decision turn is a new question by design and must keep the protocol's
        # one-turn-at-a-time reuse; matching by key there would retire an order
        # the host is legitimately still working on.
        bound_task = bound_task_id(_question(request))
        order = self._orders.get(request.agent_id)
        if bound_task and order is not None and _order_key(order.to_dict()) != key:
            # The cached order belongs to a superseded request.
            self.queue.retire(order.order_id)
            self._orders.pop(request.agent_id, None)
            order = None
        if order is None:
            order = self._outstanding(request.agent_id, key if bound_task else "")
        if order is not None:
            reply = self.queue.claim(order.order_id)
            if reply is None:
                self._orders[request.agent_id] = order
                raise HandoffRequired(order)
            self._orders.pop(request.agent_id, None)
            answer = self._interpret(reply)
            if answer.payload is not None:
                # Hold the answer until the *caller that asked for it* retrieves
                # it.  A resume may consume the reply before the planning path
                # runs; without this handoff the decision would be lost and the
                # agent would publish a duplicate order on the next call.
                self._unclaimed[request.agent_id] = answer.payload
                if delegating:
                    self._remember(request.agent_id, key, answer.payload)
            return answer
        order = WorkOrder.build(self.queue.next_seq(), request)
        self._orders[request.agent_id] = order
        self.queue.publish(order)
        raise HandoffRequired(order)

    def discard_unclaimed(self, agent_id: str) -> None:
        """Drop an answer nobody consumed.

        A persistent agent may have answered a turn that the planning path then
        abandoned (because the cycle changed underneath it).  Keeping that answer
        would make the next decision replay it instead of asking again.
        """
        self._unclaimed.pop(agent_id, None)

    def _remember(self, agent_id: str, key: str, payload: Any) -> None:
        self._completed.setdefault(agent_id, {})[key] = payload
        if self.ledger is not None:
            self.ledger.put(agent_id, key, payload)

    def _outstanding(self, agent_id: str,
                     request_key: str = "") -> Optional[WorkOrder]:
        """The open order for this agent, **only if it asks the same thing**.

        Idempotency must be about the request, not the agent. Matching by agent
        alone meant a new task was silently dropped whenever an older unanswered
        order for the same role existed: the supervisor assembled the new
        ReadingTask into `state.json`, then re-published the *previous* order, so
        the reader answered a question about a paper the run had already
        abandoned. The reader never saw the new task and the loop could not move.
        """
        for row in self.queue.pending():
            if row.get("agent_id") != agent_id:
                continue
            if request_key and _order_key(row) != request_key:
                # A different question. It can never be answered usefully, and
                # while it sits in the outbox `step` reports "unanswered" and
                # refuses to advance. Withdraw it and publish the real one.
                self.queue.retire(str(row.get("order_id") or ""))
                continue
            return WorkOrder(
                    seq=int(row["seq"]), agent_id=row["agent_id"],
                    role=row.get("role", ""),
                    system_prompt=row.get("system_prompt", ""),
                    messages=row.get("messages") or [],
                    tools=row.get("tools") or [],
                    metadata=row.get("metadata") or {},
                    reply_contract=row.get("reply_contract") or {})
        return None

    @staticmethod
    def _interpret(reply: dict) -> AgentReply:
        if not isinstance(reply, dict):
            raise ValueError("handoff reply must be a JSON object")
        if "tool_calls" in reply:
            calls = []
            for item in reply.get("tool_calls") or []:
                if not isinstance(item, dict) or not item.get("name"):
                    raise ValueError("each tool call needs a name")
                calls.append(ToolCall(name=item["name"],
                                      arguments=dict(item.get("arguments") or {}),
                                      call_id=item.get("call_id") or
                                      "call_{}".format(len(calls) + 1)))
            if not calls:
                raise ValueError("tool_calls must not be empty")
            return AgentReply(text=str(reply.get("text") or ""), tool_calls=calls)
        if "payload" in reply:
            return AgentReply(text=str(reply.get("text") or ""),
                              payload=reply["payload"])
        raise ValueError("reply must contain either tool_calls or payload")


def _question(request: PolicyRequest) -> str:
    """What this turn is asking, as text the external agent can read.

    Prefers the newest non-empty user message.  A routed capability call (the
    supervisor asking the agent to use one of its tools) carries its arguments
    in metadata instead of prose, so that path is handled explicitly: otherwise
    the order would ship with empty messages and the host would have nothing to
    answer.
    """
    for message in reversed(request.messages):
        if message.role == "user" and (message.content or "").strip():
            return message.content
    arguments = dict((request.metadata or {}).get("arguments") or {})
    if arguments:
        import json as _json
        return _json.dumps({"tool": (request.metadata or {}).get("tool"),
                            "arguments": arguments}, ensure_ascii=False)
    return ""


def _order_key(row: dict) -> str:
    """The request key a published order corresponds to.

    `WorkOrder.build` stores the rendered question verbatim, and `_request_key`
    hashes exactly `agent_id|question`, so an order's key is derivable without
    storing anything new — including for orders published before this existed.
    """
    import hashlib
    raw = "{}|{}".format(row.get("agent_id") or "", row.get("question") or "")
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _request_key(request: PolicyRequest) -> str:
    """Fingerprint of *this* request, not of the conversation.

    The last message is the one being answered.  Keying on the first message
    would make every cycle of a persistent agent look like the same request
    (the first message never changes), so cycle 2 would silently receive cycle
    1's answer and the run would stall on a stale decision.
    """
    import hashlib
    latest = _question(request)
    raw = "{}|{}".format(request.agent_id, latest)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def read_order(path: str) -> dict:
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def inbox_instructions(order: dict) -> str:
    """Human/agent-readable brief for one work order."""
    return (
        "# Work order {order_id}\n\n"
        "Agent: `{agent_id}`  (role: {role})\n\n"
        "## System prompt\n\n{system_prompt}\n\n"
        "## Current question\n\n{question}\n\n"
        "## Conversation so far\n\n{messages}\n\n"
        "## Tools you may request\n\n{tools}\n\n"
        "## Attachments\n\n{attachments}\n\n"
        "## How to reply\n\n{contract}\n\n"
        "Write the reply to `inbox/{order_id}.json`.\n"
    ).format(
        order_id=order["order_id"], agent_id=order["agent_id"],
        role=order["role"], system_prompt=order["system_prompt"],
        messages=_render(order.get("messages") or []),
        question=order.get("question") or "",
        tools=json.dumps(order.get("tools") or [], ensure_ascii=False, indent=2),
        attachments=json.dumps(order.get("attachments") or [],
                               ensure_ascii=False, indent=2),
        contract=json.dumps(order.get("reply_contract") or {},
                            ensure_ascii=False, indent=2))


def _render(messages: List[dict]) -> str:
    lines = []
    for message in messages:
        role = message.get("role")
        content = message.get("content") or ""
        if message.get("tool_calls"):
            content += "\n[tool_calls] " + json.dumps(
                message["tool_calls"], ensure_ascii=False)
        lines.append("### {}\n\n{}".format(role, content))
    return "\n\n".join(lines)


class AnswerLedger:
    """Persistent record of finished agent answers.

    A parent may replay a delegation *after* the child finished in a separate
    process or after the run was reloaded.  Keeping finished answers on disk
    makes delegation idempotent across process boundaries, which is required for
    a host agent that answers one work order per invocation.
    """

    def __init__(self, root: str):
        self.path = os.path.join(os.path.abspath(root), "answers.json")
        self._values: Dict[str, Any] = {}
        self._load()

    def _load(self):
        if os.path.isfile(self.path):
            with open(self.path, encoding="utf-8") as handle:
                self._values = json.load(handle)

    def get(self, agent_id: str, key: str) -> Any:
        return self._values.get("{}|{}".format(agent_id, key))

    def put(self, agent_id: str, key: str, payload: Any) -> None:
        self._values["{}|{}".format(agent_id, key)] = payload
        _write_json(self.path, self._values)

    def clear(self) -> None:
        self._values = {}
        if os.path.isfile(self.path):
            os.unlink(self.path)


class SessionStore:
    """Persist an agent's conversation across external pauses.

    Without this, resuming a run would restart every agent from its seed
    message, so a parent would forget that it had already delegated.  The store
    is keyed by agent id inside the run and is only used in handoff mode.
    """

    def __init__(self, root: str):
        self.root = os.path.join(os.path.abspath(root), "sessions")
        os.makedirs(self.root, exist_ok=True)

    def _path(self, agent_id: str) -> str:
        return os.path.join(self.root, _safe(agent_id) + ".json")

    def load(self, agent_id: str) -> Optional[dict]:
        """Return ``{"messages": [...], "pending_calls": [...]}`` or None."""
        path = self._path(agent_id)
        if not os.path.isfile(path):
            return None
        with open(path, encoding="utf-8") as handle:
            value = json.load(handle)
        if isinstance(value, list):        # legacy shape
            return {"messages": value, "pending_calls": []}
        return value

    def save(self, agent_id: str, messages: List[dict],
             pending_calls: Optional[List[dict]] = None,
             resolved_calls: Optional[dict] = None) -> str:
        path = self._path(agent_id)
        _write_json(path, {"messages": messages,
                           "pending_calls": list(pending_calls or []),
                           "resolved_calls": dict(resolved_calls or {})})
        return path

    def clear(self, agent_id: str) -> None:
        path = self._path(agent_id)
        if os.path.isfile(path):
            os.unlink(path)

    def pending_agents(self) -> List[str]:
        return sorted(name[:-5] for name in os.listdir(self.root)
                      if name.endswith(".json"))

    def with_pending_calls(self) -> List[str]:
        """Agents holding unexecuted tool calls from a previous pause."""
        found = []
        for name in os.listdir(self.root):
            if not name.endswith(".json"):
                continue
            with open(os.path.join(self.root, name), encoding="utf-8") as handle:
                value = json.load(handle)
            if isinstance(value, dict) and value.get("pending_calls"):
                found.append(name[:-5])
        return sorted(found)


def message_to_dict(message) -> dict:
    return message.to_dict()


def dict_to_message(row: dict):
    from .messages import Message, ToolCall
    calls = [ToolCall(name=item["name"],
                      arguments=item.get("arguments") or {},
                      call_id=item.get("call_id") or "call_x")
             for item in row.get("tool_calls") or []]
    return Message(role=row["role"], content=row.get("content", ""),
                   tool_calls=calls, tool_call_id=row.get("tool_call_id"),
                   name=row.get("name"))
