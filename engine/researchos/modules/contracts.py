"""Module invocation contract (system-orchestrated only).

ResearchOS exposes exactly ONE user entry point: a research goal handed to the
orchestrator. Research / PaperRead / PaperOrganization / PaperScheduler remain
internal responsibilities with their own contracts, but there is no user-facing
"run this module alone" path (guide v2 R1, §1.8).

Consequently `ExecutionContext` carries a single execution mode. The fields
that used to distinguish standalone from orchestrated (`scope`, `budget`,
`allowedTools`, `outputContract`) stay, because the orchestrator still needs to
bound each module call -- but they now express *one* policy, not two.

This module is deliberately dependency-free so the plugin, the bridge and the
tests can all import it without pulling in the runtime.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

#: Only system orchestration remains (guide v2 §4.1).
MODES = ("system",)
DEFAULT_MODE = "system"
STATUSES = ("completed", "partial", "blocked", "failed")

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")
#: Logical agent identities. Each gets its own `(task_id, role_id) -> session`
#: binding, so any sub-agent that thinks on its own must appear here.
#:
#: `research.query` is the keyword planner; `organization.curate_semantic_relations`
#: and `organization.relation_reviewer` are Organization's two judgement roles.
#: All three run on the external model, so all three need a stable session --
#: answering them under the parent module's identity would mix a deterministic
#: write-back conversation with a semantic-review one.
ROLES = ("research", "research.query",
         "organization",
         "organization.curator",
         "organization.relation_reviewer",
         "scheduler", "read", "finalizer")


def safe_id(value: str) -> str:
    """A filesystem- and session-id-safe token.

    DSH child session ids double as persistence filenames, so the registry must
    never emit a name containing a separator or a shell-active character.
    """
    cleaned = _SAFE.sub("-", str(value or "")).strip("-")
    return cleaned or "x"


@dataclass(frozen=True)
class Budget:
    """A hard ceiling the module is expected to honour, not a suggestion."""

    max_steps: Optional[int] = None
    max_tokens: Optional[int] = None

    def allows_steps(self, used: int) -> bool:
        return self.max_steps is None or used < self.max_steps

    def allows_tokens(self, used: int) -> bool:
        return self.max_tokens is None or used < self.max_tokens

    def to_dict(self) -> dict:
        out: Dict[str, Any] = {}
        if self.max_steps is not None:
            out["max_steps"] = self.max_steps
        if self.max_tokens is not None:
            out["max_tokens"] = self.max_tokens
        return out

    @classmethod
    def from_dict(cls, value: Optional[dict]) -> "Budget":
        value = value or {}
        return cls(max_steps=value.get("max_steps"),
                   max_tokens=value.get("max_tokens"))


@dataclass(frozen=True)
class ExecutionContext:
    """One module invocation, fully described.

    `role_id` is the logical role (``read``, ``scheduler`` ...).  `task_id` is
    the top-level task.  Together they key the Session Registry, which is what
    makes "the same role in the same task always continues the same session" a
    lookup instead of a hope.
    """

    task_id: str
    role_id: str
    #: Always "system" under guide v2. Kept explicit so a future mode cannot be
    #: introduced by accident: constructing a context with anything else fails.
    mode: str = DEFAULT_MODE
    scope: str = ""
    output_contract: str = ""
    input_artifacts: List[str] = field(default_factory=list)
    budget: Budget = field(default_factory=Budget)
    allowed_tools: Optional[List[str]] = None
    trace_id: str = ""
    #: Logical subtask identity.  Parallel work gets its own subtask so one role
    #: is never mapped to two sessions inside a single task (guide R2).
    subtask_id: str = ""

    def __post_init__(self) -> None:
        if self.mode not in MODES:
            raise ValueError("mode must be one of {}, got {!r}".format(MODES, self.mode))
        if self.role_id not in ROLES:
            raise ValueError("unknown role_id {!r}; expected one of {}".format(
                self.role_id, ROLES))

    @property
    def session_key(self) -> str:
        """The registry key: one logical role, one task, one optional subtask."""
        parts = [self.task_id, self.role_id]
        if self.subtask_id:
            parts.append("sub:" + self.subtask_id)
        return "|".join(parts)


    def tool_allowed(self, name: str) -> bool:
        """`allowed_tools` is enforced in code, not only described in a prompt."""
        if self.allowed_tools is None:
            return True
        return name in self.allowed_tools

    def to_dict(self) -> dict:
        out: Dict[str, Any] = {
            "mode": self.mode,
            "task_id": self.task_id,
            "role_id": self.role_id,
            "scope": self.scope,
            "output_contract": self.output_contract,
            "input_artifacts": list(self.input_artifacts),
            "trace_id": self.trace_id,
        }
        if self.subtask_id:
            out["subtask_id"] = self.subtask_id
        budget = self.budget.to_dict()
        if budget:
            out["budget"] = budget
        if self.allowed_tools is not None:
            out["allowed_tools"] = list(self.allowed_tools)
        return out

    @classmethod
    def from_dict(cls, value: dict) -> "ExecutionContext":
        return cls(
            mode=value.get("mode", DEFAULT_MODE),
            task_id=value["task_id"],
            role_id=value["role_id"],
            scope=value.get("scope", ""),
            output_contract=value.get("output_contract", ""),
            input_artifacts=list(value.get("input_artifacts") or []),
            budget=Budget.from_dict(value.get("budget")),
            allowed_tools=value.get("allowed_tools"),
            trace_id=value.get("trace_id", ""),
            subtask_id=value.get("subtask_id", ""),
        )


def module_result(status: str, *, artifact_ids: Optional[List[str]] = None,
                  summary: str = "",
                  missing_dependencies: Optional[List[str]] = None,
                  errors: Optional[List[str]] = None,
                  provenance: Optional[List[str]] = None,
                  data: Any = None,
                  warnings: Optional[List[str]] = None,
                  meta: Optional[dict] = None) -> dict:
    """The one shape every module returns, in both modes.

    A module never returns a bare dict or free text: the caller has to be able
    to branch on `status` without guessing, and to find the artifacts it needs
    without copying large payloads through the conversation.
    """
    if status not in STATUSES:
        raise ValueError("unknown module status {!r}".format(status))
    return {
        "status": status,
        "artifact_ids": list(artifact_ids or []),
        "summary": summary,
        "missing_dependencies": list(missing_dependencies or []),
        "errors": list(errors or []),
        "provenance": list(provenance or []),
        "data": data,
        "warnings": list(warnings or []),
        "meta": dict(meta or {}),
    }


def completed(**kwargs) -> dict:
    return module_result("completed", **kwargs)


def partial(**kwargs) -> dict:
    return module_result("partial", **kwargs)


def blocked(*, missing_dependencies: Optional[List[str]] = None,
            errors: Optional[List[str]] = None, **kwargs) -> dict:
    kwargs.setdefault("missing_dependencies", missing_dependencies)
    kwargs.setdefault("errors", errors)
    return module_result("blocked", **kwargs)


def failed(*, errors: Optional[List[str]] = None, **kwargs) -> dict:
    kwargs.setdefault("errors", errors)
    return module_result("failed", **kwargs)


def idempotency_key(task_id: str, role_id: str, operation: str,
                    payload: Any) -> str:
    """A stable key for a write, so a retry cannot duplicate a stored record."""
    blob = json.dumps({"task": task_id, "role": role_id,
                       "op": operation, "payload": payload},
                      sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]
