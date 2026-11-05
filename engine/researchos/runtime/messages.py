"""Message, tool-call and tool-result types shared by every agent.

The whole point of this module is that agents talk to the runtime in one
vocabulary, and the runtime talks to agents in one vocabulary.  Nothing here
knows about papers, reading tasks, or knowledge bases.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


ROLES = ("system", "user", "assistant", "tool")


def new_id(prefix: str) -> str:
    return "{}_{}".format(prefix, uuid.uuid4().hex[:12])


@dataclass
class ToolCall:
    """One request from an agent to invoke a tool."""

    name: str
    arguments: Dict[str, Any] = field(default_factory=dict)
    call_id: str = field(default_factory=lambda: new_id("call"))

    def to_dict(self) -> dict:
        return {"call_id": self.call_id, "name": self.name,
                "arguments": dict(self.arguments)}


@dataclass
class Message:
    """One turn in an agent conversation.

    ``tool_calls`` is only meaningful for assistant messages; ``tool_call_id``
    only for tool messages.  Adapters that target a provider SDK translate this
    into whatever shape that SDK wants -- the runtime never sees provider types.
    """

    role: str
    content: str = ""
    tool_calls: List[ToolCall] = field(default_factory=list)
    tool_call_id: Optional[str] = None
    name: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if self.role not in ROLES:
            raise ValueError("unknown role: {}".format(self.role))
        if self.role == "tool" and not self.tool_call_id:
            raise ValueError("tool messages require tool_call_id")
        if self.tool_calls and self.role != "assistant":
            raise ValueError("only assistant messages may carry tool_calls")

    def to_dict(self) -> dict:
        value = {"role": self.role, "content": self.content}
        if self.tool_calls:
            value["tool_calls"] = [call.to_dict() for call in self.tool_calls]
        if self.tool_call_id:
            value["tool_call_id"] = self.tool_call_id
        if self.name:
            value["name"] = self.name
        if self.metadata:
            value["metadata"] = dict(self.metadata)
        return value

    @classmethod
    def system(cls, content: str, **kwargs) -> "Message":
        return cls(role="system", content=content, **kwargs)

    @classmethod
    def user(cls, content: str, **kwargs) -> "Message":
        return cls(role="user", content=content, **kwargs)

    @classmethod
    def assistant(cls, content: str = "", tool_calls=None, **kwargs) -> "Message":
        return cls(role="assistant", content=content,
                   tool_calls=list(tool_calls or []), **kwargs)

    @classmethod
    def tool(cls, call: ToolCall, payload: Any, **kwargs) -> "Message":
        if not isinstance(payload, str):
            payload = json.dumps(payload, ensure_ascii=False)
        return cls(role="tool", content=payload, tool_call_id=call.call_id,
                   name=call.name, **kwargs)


@dataclass
class AgentReply:
    """What an agent policy returns after one model turn.

    Exactly one of ``tool_calls`` or ``payload`` is expected in practice, but
    both are allowed: a turn may say something *and* ask for a tool.  The
    runtime loop decides what to do with the combination.
    """

    text: str = ""
    tool_calls: List[ToolCall] = field(default_factory=list)
    payload: Optional[Any] = None
    usage: Dict[str, Any] = field(default_factory=dict)

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)

    def message(self) -> Message:
        return Message.assistant(self.text, tool_calls=self.tool_calls)


def coerce_reply(value: Any) -> AgentReply:
    """Normalise whatever an executor returned into an AgentReply.

    Accepts AgentReply, a JSON object string, a dict, or plain text.  This is
    the single place where adapter sloppiness is absorbed.
    """
    if isinstance(value, AgentReply):
        return value
    if isinstance(value, dict):
        if "tool_calls" in value or "payload" in value or "text" in value:
            calls = []
            for item in value.get("tool_calls") or []:
                if isinstance(item, ToolCall):
                    calls.append(item)
                else:
                    calls.append(ToolCall(
                        name=item["name"],
                        arguments=item.get("arguments") or {},
                        call_id=item.get("call_id") or new_id("call")))
            return AgentReply(text=str(value.get("text") or ""),
                              tool_calls=calls, payload=value.get("payload"))
        return AgentReply(payload=value)
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith("{") or stripped.startswith("["):
            try:
                return AgentReply(payload=json.loads(stripped), text=value)
            except json.JSONDecodeError:
                pass
        return AgentReply(text=value, payload=value)
    return AgentReply(text=str(value), payload=value)


def bound_task_id(text: str) -> str:
    """The `task_id` a prompt is about, when the prompt names one.

    The reader's prompt is `{"task": {...}}`, so the task is readable from the
    text itself. Both the handoff policy and the agent loop need this to tell
    "a new task" from "the same task, resumed" — and a shared definition is what
    keeps the two answers identical.
    """
    import json
    try:
        value = json.loads(text or "")
    except (TypeError, ValueError):
        return ""
    if not isinstance(value, dict):
        return ""
    task = value.get("task")
    return str(task.get("task_id") or "") if isinstance(task, dict) else ""


def bound_paper_id(text: str) -> str:
    """The paper a reading prompt is about.

    Task ids are not unique per paper — the supervisor can reuse one id for a
    different target — so the paper, not the task id, is what tells "a new
    reading task" from "the same one resumed".
    """
    import json
    try:
        value = json.loads(text or "")
    except (TypeError, ValueError):
        return ""
    if not isinstance(value, dict):
        return ""
    task = value.get("task")
    if not isinstance(task, dict):
        return ""
    return str(task.get("target_paper") or "")
