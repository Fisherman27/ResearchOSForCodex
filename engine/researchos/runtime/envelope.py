"""The single return envelope used by tools, agents and orchestrators.

Semantics are inherited from ``specs/ToolsPyRules.md`` so that a tool written
for the ported tool libraries and an agent written for this framework look the
same to a caller:

    ok        -> all good
    partial   -> usable result with declared degradations
    empty     -> executed successfully, result set is empty (NOT a failure)
    blocked   -> missing precondition; retrying is pointless
    failed    -> execution error; read code/retryable
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional


STATUSES = ("ok", "partial", "empty", "blocked", "failed")
_OK_STATUSES = ("ok", "partial", "empty")


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def error(code: str, message: str, *, retryable: bool = False,
          hint: str = "", location: str = "", cause: str = "",
          context: Optional[dict] = None) -> dict:
    return {"code": code, "message": message, "retryable": retryable,
            "hint": hint, "location": location, "cause": cause,
            "context": dict(context or {})}


def envelope(status: str, *, data: Any = None, err: Optional[dict] = None,
             warnings: Optional[Iterable[str]] = None,
             diagnostics: Optional[dict] = None, meta: Optional[dict] = None) -> dict:
    if status not in STATUSES:
        raise ValueError("unknown status: {}".format(status))
    return {
        "ok": status in _OK_STATUSES,
        "status": status,
        "data": data,
        "error": err,
        "warnings": list(warnings or []),
        "diagnostics": dict(diagnostics or {}),
        "meta": dict(meta or {}),
    }


def ok(data=None, **kwargs) -> dict:
    return envelope("ok", data=data, **kwargs)


def partial(data=None, *, warnings=None, **kwargs) -> dict:
    return envelope("partial", data=data, warnings=warnings, **kwargs)


def empty(data=None, *, warnings=None, **kwargs) -> dict:
    return envelope("empty", data=data, warnings=warnings, **kwargs)


def blocked(err: dict, **kwargs) -> dict:
    return envelope("blocked", err=err, **kwargs)


def failed(err: dict, **kwargs) -> dict:
    return envelope("failed", err=err, **kwargs)


def fail(code: str, message: str, **kwargs) -> dict:
    return failed(error(code, message, **kwargs))


def is_usable(value: Optional[dict]) -> bool:
    return bool(value and value.get("ok") and value.get("status") in _OK_STATUSES)


def escalation(value: Optional[dict]) -> dict:
    """Collapse an envelope into the fields a parent agent needs to react."""
    value = value or {}
    return {"ok": bool(value.get("ok")), "status": value.get("status"),
            "error": value.get("error"), "warnings": list(value.get("warnings") or [])}


class EnvelopeError(RuntimeError):
    """Raised internally when a handler breaks the envelope contract."""

    def __init__(self, value: Any):
        super().__init__("handler returned a non-envelope value: {!r}".format(value))
        self.value = value


def is_external_pause(value) -> bool:
    """Whether an agent result is a *pause* rather than an answer.

    Lives here, next to the envelope it inspects, because more than one caller
    needs it: the literature supervisor and the pipeline controller. It used to
    be defined in both, and only one copy gained the tolerance below — so the
    other still raised `AttributeError` on the exact input that motivated the
    fix. One definition is the only way that cannot happen again.

    A dispatch that yields no envelope hands back ``None``; that is "no pause",
    not a crash.
    """
    if not isinstance(value, dict):
        return False
    return (value.get("error") or {}).get("code") == "E2_AWAITING_EXTERNAL_AGENT" or (
        value.get("data") or {}).get("awaiting_external_agent") is True
