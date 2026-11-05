"""Observability: one trace per agent turn, one shared event log per run.

The trace is what makes the multi-agent claim *checkable*.  A run is not
"coordinated" because a diagram says so; it is coordinated because every
message, tool call and hand-off is recorded with the agent that produced it
and the agent that consumed it.
"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field

from .envelope import now
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


@dataclass
class ToolTrace:
    name: str
    arguments: Dict[str, Any]
    status: str
    ok: bool
    duration_ms: int = 0
    warnings: List[str] = field(default_factory=list)
    error_code: Optional[str] = None

    def to_dict(self) -> dict:
        return {"name": self.name, "arguments": self.arguments,
                "status": self.status, "ok": self.ok,
                "duration_ms": self.duration_ms,
                "warnings": list(self.warnings),
                "error_code": self.error_code}


@dataclass
class TurnTrace:
    agent_id: str
    role: str
    turn: int
    text: str = ""
    tool_calls: List[ToolTrace] = field(default_factory=list)
    usage: Dict[str, Any] = field(default_factory=dict)
    stopped_because: str = ""

    def to_dict(self) -> dict:
        return {"agent_id": self.agent_id, "role": self.role, "turn": self.turn,
                "text": self.text,
                "tool_calls": [item.to_dict() for item in self.tool_calls],
                "usage": dict(self.usage),
                "stopped_because": self.stopped_because}


class EventLog:
    """Append-only JSONL log with fsync; one event per state transition."""

    def __init__(self, path: str):
        self.path = os.path.abspath(path)
        os.makedirs(os.path.dirname(self.path), exist_ok=True)

    def append(self, event_type: str, data: Optional[dict] = None, *,
               actor: Optional[str] = None) -> dict:
        row = {"timestamp": now(), "event": event_type,
               "actor": actor, "data": data or {}}
        with open(self.path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        return row

    def read(self) -> List[dict]:
        if not os.path.isfile(self.path):
            return []
        with open(self.path, encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]


class TraceWriter:
    """Persists turn traces under ``runs/<id>/agents/<agent_id>/``."""

    def __init__(self, root: str):
        self.root = os.path.abspath(root)

    def write(self, trace: TurnTrace) -> str:
        directory = os.path.join(self.root, "agents", trace.agent_id)
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, "trace.jsonl")
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(trace.to_dict(), ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        return path


class Blackboard:
    """Shared run-scoped key/value store for cross-agent hand-off.

    Deliberately dumb: it stores what an agent publishes and nothing else.  It
    is *not* the knowledge base and it does not interpret values.  Using a
    blackboard instead of direct agent-to-agent calls keeps every hand-off
    traceable and lets a supervisor restart a worker without replaying the
    whole conversation.
    """

    def __init__(self, path: str):
        self.path = os.path.abspath(path)
        self._values: Dict[str, Any] = {}
        self._load()

    def _load(self):
        if os.path.isfile(self.path):
            with open(self.path, encoding="utf-8") as handle:
                self._values = json.load(handle)

    def put(self, key: str, value: Any, *, producer: str = "") -> str:
        self._values[key] = {"value": value, "producer": producer, "at": now()}
        self._flush()
        return key

    def get(self, key: str, default: Any = None) -> Any:
        entry = self._values.get(key)
        return default if entry is None else entry["value"]

    def entry(self, key: str) -> Optional[dict]:
        return self._values.get(key)

    def keys(self) -> List[str]:
        return sorted(self._values)

    def _flush(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        fd, temp = tempfile.mkstemp(prefix="blackboard-", suffix=".json",
                                    dir=os.path.dirname(self.path))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(self._values, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, self.path)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)
