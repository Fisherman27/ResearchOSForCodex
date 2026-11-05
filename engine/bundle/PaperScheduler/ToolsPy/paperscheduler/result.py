"""PaperScheduler 统一返回信封。"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


STATUSES = {"ok", "partial", "empty", "blocked", "failed"}


def envelope(status: str, *, data: Any = None, error: Optional[dict] = None,
             warnings: Optional[List[str]] = None,
             diagnostics: Optional[Dict[str, Any]] = None,
             tool: str = "", params: Optional[dict] = None) -> dict:
    if status not in STATUSES:
        raise ValueError("unknown status: {}".format(status))
    return {
        "ok": status in ("ok", "partial", "empty"),
        "status": status,
        "data": data,
        "error": error,
        "warnings": warnings or [],
        "diagnostics": diagnostics or {},
        "meta": {
            "tool": tool,
            "params": params or {},
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "version": "0.1.0",
        },
    }


def ok(data: Any = None, **kwargs) -> dict:
    return envelope("ok", data=data, **kwargs)


def empty(data: Any = None, **kwargs) -> dict:
    return envelope("empty", data=data, **kwargs)


def partial(data: Any = None, **kwargs) -> dict:
    return envelope("partial", data=data, **kwargs)


def failed(error: dict, **kwargs) -> dict:
    return envelope("failed", error=error, **kwargs)


def blocked(error: dict, **kwargs) -> dict:
    return envelope("blocked", error=error, **kwargs)


def is_usable(value: dict) -> bool:
    return bool(value and value.get("ok") and value.get("status") in
                ("ok", "partial", "empty"))
