"""Unified module layer: contracts, session registry, and module services.

This package is the boundary the guide asks for: every research module is
reachable through one facade with one result shape under the system
orchestrator, and the `(task_id, role_id) -> session_id` mapping lives in a
persisted registry rather than in process memory.
"""
from .contracts import (Budget, ExecutionContext, ExecutionContext as Context,
                       MODES, ROLES, STATUSES, blocked, completed, failed,
                       idempotency_key, module_result, partial, safe_id)
from .registry import (ARCHIVED, Binding, CREATED, FAILED, READY, RESUMABLE,
                       RUNNING, SETTLED, SessionBusy, SessionNotResumable,
                       SessionRegistry, WAITING, derive_session_id)
from .service import ArtifactStore, ModuleServices

__all__ = [
    "Budget", "ExecutionContext", "Context", "MODES", "ROLES", "STATUSES",
    "blocked", "completed", "failed", "idempotency_key", "module_result",
    "partial", "safe_id",
    "ARCHIVED", "Binding", "CREATED", "FAILED", "READY", "RESUMABLE",
    "RUNNING", "SETTLED", "SessionBusy", "SessionNotResumable",
    "SessionRegistry", "WAITING", "derive_session_id",
    "ArtifactStore", "ModuleServices",
]
