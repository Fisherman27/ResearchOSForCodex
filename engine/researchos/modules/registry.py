"""Session Registry: `(task_id, role_id) -> session_id`, persisted and queryable.

This is the component that makes guide requirement R2 ("the same logical role in
one task always reuses one session") a *runtime* fact instead of a sentence in a
prompt.

Design in one paragraph
-----------------------
The registry owns the mapping and the lifecycle record. It does not itself run
an agent: the host adapter (the DSH plugin, or a test double) asks it to
`resolve()` a binding before dispatching, and reports back with `mark_running` /
`mark_settled` / `mark_failed`. Because the mapping is written *before* the
first turn runs and is never rewritten by a later turn, a process restart
reconstructs the same `session_id` from disk -- which is exactly requirement R3
(session continuity is not process residency).

Concurrency is handled here too: one session may have at most one in-flight
activation. `acquire()` refuses a second concurrent turn on the same session, or
queues it when the caller asked to wait. The guide is explicit that this must be
an actual mutex, not advice.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .contracts import safe_id

# Lifecycle states. A finished *phase* is not a finished *session*: `settled`
# means "this turn ended", the binding stays resumable for later phases.
CREATED = "created"
READY = "ready"
RUNNING = "running"
WAITING = "blocked"
SETTLED = "settled"
FAILED = "failed"
ARCHIVED = "archived"

STATES = (CREATED, READY, RUNNING, WAITING, SETTLED, FAILED, ARCHIVED)
#: States from which a later turn may resume the same session.
#: `FAILED` belongs here deliberately: a failed *turn* is not a dead *session*,
#: and minting a new session on retry is exactly what requirement R2 forbids.
RESUMABLE = (CREATED, READY, WAITING, SETTLED, FAILED)


class SessionBusy(RuntimeError):
    """The session already has an in-flight activation."""

    def __init__(self, session_id: str):
        super().__init__(
            "session {} already has an in-flight turn; a session may not be "
            "modified concurrently".format(session_id))
        self.session_id = session_id


class SessionNotResumable(RuntimeError):
    """The binding exists but cannot be continued; a new session is required."""


@dataclass
class Binding:
    """One durable role session inside one task."""

    task_id: str
    role_id: str
    session_id: str
    subtask_id: str = ""
    state: str = CREATED
    #: Which turns have run, in order. Kept so the run's evidence log can show
    #: that phases 2..n went to the *same* session rather than a fresh one.
    turns: int = 0
    created_at: float = 0.0
    updated_at: float = 0.0
    last_phase: str = ""
    last_error: str = ""
    #: Why a replacement session was minted, when one was. The guide requires
    #: recording the reason and the link to the abandoned session.
    replaced: Optional[str] = None
    checkpoints: List[str] = field(default_factory=list)
    meta: Dict[str, Any] = field(default_factory=dict)
    #: `running` is tracked in memory only: after a restart nothing is running.
    _inflight: bool = False

    @property
    def key(self) -> str:
        parts = [self.task_id, self.role_id]
        if self.subtask_id:
            parts.append("sub:" + self.subtask_id)
        return "|".join(parts)

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "role_id": self.role_id,
            "session_id": self.session_id,
            "subtask_id": self.subtask_id,
            "state": self.state,
            "turns": self.turns,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_phase": self.last_phase,
            "last_error": self.last_error,
            "replaced": self.replaced,
            "checkpoints": list(self.checkpoints),
            "meta": dict(self.meta),
        }

    @classmethod
    def from_dict(cls, value: dict) -> "Binding":
        return cls(
            task_id=value["task_id"],
            role_id=value["role_id"],
            session_id=value["session_id"],
            subtask_id=value.get("subtask_id", ""),
            state=value.get("state", CREATED),
            turns=int(value.get("turns") or 0),
            created_at=float(value.get("created_at") or 0.0),
            updated_at=float(value.get("updated_at") or 0.0),
            last_phase=value.get("last_phase", ""),
            last_error=value.get("last_error", ""),
            replaced=value.get("replaced"),
            checkpoints=list(value.get("checkpoints") or []),
            meta=dict(value.get("meta") or {}),
        )


def derive_session_id(task_id: str, role_id: str, subtask_id: str = "") -> str:
    """A deterministic, collision-free session id for one role in one task.

    Deterministic on purpose: if the registry file is lost but the task is
    resumed, the same logical pair still maps to the same id, so we do not
    silently fork a second history. The random suffix is only there to keep two
    identically-named tasks (different task ids) separate -- and they already
    are, because `task_id` is part of the input.
    """
    stem = "-".join(part for part in (
        safe_id(task_id), safe_id(role_id),
        safe_id(subtask_id) if subtask_id else "") if part)
    # A short digest keeps the id readable while staying unique per pair.
    import hashlib
    digest = hashlib.sha256(
        "{}|{}|{}".format(task_id, role_id, subtask_id).encode("utf-8")
    ).hexdigest()[:8]
    return "ros-{}-{}".format(stem[:96], digest)


class SessionRegistry:
    """Durable `(task, role[, subtask]) -> session_id` map with lifecycle tracking.

    One JSON file, atomically replaced on every mutation, so a killed process
    never leaves a half-written mapping. Mutating helpers take the file lock.

    A fresh session id is minted on first use, and that rule is the same for
    every call: identity never depends on which phase is asking. Execution
    policy upstream (scope, budget, tool grants) lives in `ExecutionContext`.
    """

    def __init__(self, root: str, *, clock=None):
        self.root = os.path.abspath(root)
        # No `makedirs` here: constructing a registry is a READ, and the host
        # asks for one on every dashboard poll. Creating the directory eagerly
        # meant a single query for an unknown run id left a phantom
        # `<runs_dir>/<id>/sessions/` behind. Both write paths create it.

        self.path = os.path.join(self.root, "registry.json")
        self.lock_path = self.path + ".lock"
        self._clock = clock or time.time
        # session ids with an in-flight activation in *this* process
        self._inflight: Dict[str, bool] = {}
        #: lock file path per held session, so release removes the right file
        self._lock_files: Dict[str, str] = {}
        self._bindings: Dict[str, Binding] = {}
        self._load()

    @contextmanager
    def _mutation_lock(self):
        """Serialize registry read-modify-write cycles across host processes."""
        os.makedirs(self.root, exist_ok=True)
        with open(self.lock_path, "a+", encoding="utf-8") as handle:
            try:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                unlock = lambda: fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except ImportError:  # Windows
                import msvcrt
                handle.seek(0)
                if handle.tell() == 0:
                    handle.write("0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                unlock = lambda: msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            try:
                self._load()
                yield
            finally:
                unlock()

    # -- persistence ----------------------------------------------------
    def _load(self) -> None:
        if not os.path.isfile(self.path):
            self._bindings = {}
            return
        try:
            with open(self.path, encoding="utf-8") as handle:
                raw = json.load(handle)
        except (OSError, ValueError):
            # A corrupt registry must not take the whole run down; it is
            # recoverable state (session ids are derivable), not the research
            # record. Record and start clean rather than crash.
            self._bindings = {}
            return
        rows = raw.get("bindings") or {}
        self._bindings = {key: Binding.from_dict(value)
                          for key, value in rows.items()}

    def _flush(self) -> None:
        payload = {
            "schema_version": "1.0",
            "updated_at": self._clock(),
            "bindings": {key: value.to_dict()
                         for key, value in sorted(self._bindings.items())},
        }
        os.makedirs(self.root, exist_ok=True)
        fd, temp = tempfile.mkstemp(prefix="registry-", suffix=".json",
                                    dir=self.root)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, self.path)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)

    # -- lookup / creation ----------------------------------------------
    def get(self, task_id: str, role_id: str,
            subtask_id: str = "") -> Optional[Binding]:
        key = "|".join([task_id, role_id] + (["sub:" + subtask_id]
                                             if subtask_id else []))
        return self._bindings.get(key)

    def resolve(self, task_id: str, role_id: str,
                subtask_id: str = "") -> Binding:
        """Find the binding for this role, or create one. Never creates a second.

        This is the single entry point every dispatch path must use. Calling it
        on every phase is safe: it returns the existing binding untouched.
        """
        with self._mutation_lock():
            existing = self.get(task_id, role_id, subtask_id)
            if existing is not None:
                return existing
            now = self._clock()
            binding = Binding(
                task_id=task_id,
                role_id=role_id,
                session_id=derive_session_id(task_id, role_id, subtask_id),
                subtask_id=subtask_id,
                state=READY,
                created_at=now,
                updated_at=now,
            )
            self._bindings[binding.key] = binding
            self._flush()
            return binding

    def replace(self, task_id: str, role_id: str, *, reason: str,
                subtask_id: str = "") -> Binding:
        """Mint a new session and record why the old one was abandoned.

        Only for the cases the guide allows: the original session is provably
        unresumable, the user asked for a reset, or the task identity changed.
        """
        with self._mutation_lock():
            old = self.get(task_id, role_id, subtask_id)
            now = self._clock()
            suffix = uuid.uuid4().hex[:6]
            binding = Binding(
                task_id=task_id,
                role_id=role_id,
                session_id="{}-{}-{}".format(
                    derive_session_id(task_id, role_id, subtask_id), "re", suffix),
                subtask_id=subtask_id,
                state=READY,
                created_at=now,
                updated_at=now,
                replaced=old.session_id if old else None,
                meta={"replace_reason": reason},
            )
            if old is not None:
                old.state = ARCHIVED
                old.updated_at = now
                old.meta["replaced_by"] = binding.session_id
            self._bindings[binding.key] = binding
            self._flush()
            return binding

    def reserve_roster(self, task_id: str, role_ids: List[str]) -> List[Binding]:
        """Pre-allocate stable identities without starting DSH activations."""
        return [self.resolve(task_id, role_id) for role_id in role_ids]

    def acquire_lease(self, session_id: str, *, ttl_seconds: int = 1800) -> str:
        """Acquire a cross-process lease that survives short-lived bridge calls."""
        path = os.path.join(self.root, "lease-{}.json".format(safe_id(session_id)))
        now = self._clock()
        token = uuid.uuid4().hex
        payload = {"token": token, "session_id": session_id,
                   "created_at": now, "expires_at": now + ttl_seconds}
        for _ in range(2):
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                try:
                    with open(path, encoding="utf-8") as handle:
                        current = json.load(handle)
                    if float(current.get("expires_at") or 0) > now:
                        raise SessionBusy(session_id)
                    os.unlink(path)
                    continue
                except SessionBusy:
                    raise
                except (OSError, ValueError, TypeError):
                    raise SessionBusy(session_id)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
                handle.flush()
                os.fsync(handle.fileno())
            return token
        raise SessionBusy(session_id)

    def release_lease(self, session_id: str, token: str) -> None:
        path = os.path.join(self.root, "lease-{}.json".format(safe_id(session_id)))
        if not token or not os.path.isfile(path):
            return
        try:
            with open(path, encoding="utf-8") as handle:
                current = json.load(handle)
            if current.get("token") != token:
                raise SessionBusy(session_id)
            os.unlink(path)
        except FileNotFoundError:
            return

    # -- lifecycle ------------------------------------------------------
    def acquire(self, session_id: str) -> None:
        """Take the per-session execution slot, or raise `SessionBusy`.

        The slot is a lock **file**, not a dict entry, because two Dispatcher
        instances (or two processes: the plugin and a CLI repair run) must see
        the same mutex. A dict guard would silently allow exactly the race the
        guide forbids.
        """
        os.makedirs(self.root, exist_ok=True)
        lock = os.path.join(self.root, "session-{}.lock".format(safe_id(session_id)))
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if self.is_busy(session_id):
                raise SessionBusy(session_id)
            # Stale lock from a killed process: the pid is gone, reclaim it.
            try:
                os.unlink(lock)
                fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except OSError:
                raise SessionBusy(session_id)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(str(os.getpid()))
        self._inflight[session_id] = True
        self._lock_files[session_id] = lock

    def release(self, session_id: str) -> None:
        lock = self._lock_files.pop(session_id, None)
        if lock and os.path.isfile(lock):
            try:
                os.unlink(lock)
            except OSError:
                pass
        self._inflight.pop(session_id, None)

    def is_busy(self, session_id: str) -> bool:
        """Whether another live holder owns the slot.

        A lock file whose recorded pid is no longer alive is stale and does not
        count as busy; that is what lets a crashed host recover.
        """
        if self._inflight.get(session_id):
            return True
        lock = os.path.join(self.root, "session-{}.lock".format(safe_id(session_id)))
        if not os.path.isfile(lock):
            return False
        try:
            with open(lock, encoding="utf-8") as handle:
                pid = int((handle.read() or "0").strip() or 0)
        except (OSError, ValueError):
            return True
        if pid <= 0:
            return True
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False          # holder is gone; lock is stale
        except PermissionError:
            return True           # a live process we cannot signal
        return True

    def mark_child_established(self, binding: Binding) -> Binding:
        """Record that the runtime actually created the child session.

        Needed because "no completed turn yet" is not the same as "no child
        exists": a turn that failed after the child was created must be resumed,
        not created again. The plugin calls this right after a successful
        startActivation; omitting it only costs one recoverable DUPLICATE_CHILD
        retry, so a missed call is not corruption.
        """
        with self._mutation_lock():
            current = self.get(binding.task_id, binding.role_id, binding.subtask_id)
            if current is None:
                raise KeyError(binding.key)
            current.meta["child_established"] = True
            current.updated_at = self._clock()
            self._flush()
            binding.__dict__.update(current.__dict__)
            return binding

    def bind_host_session(self, binding: Binding, host_session_id: str,
                          *, host: str = "codex") -> Binding:
        """Attach the host runtime's real child id to a durable logical binding.

        The deterministic ``session_id`` belongs to ResearchOS.  Codex chooses
        its own opaque child-agent id when ``spawn_agent`` succeeds.  Keeping
        both ids prevents a resumed coordinator from silently spawning a fresh
        role on every work order while avoiding the false claim that a disk id
        can recreate a child after the host chat itself has gone away.
        """
        if not host_session_id.strip():
            raise ValueError("host_session_id must not be empty")
        with self._mutation_lock():
            current = self.get(binding.task_id, binding.role_id,
                               binding.subtask_id)
            if current is None:
                raise KeyError(binding.key)
            previous = str(current.meta.get("host_session_id") or "")
            if previous and previous != host_session_id:
                current.meta.setdefault("host_session_history", []).append({
                    "host": current.meta.get("host", host),
                    "host_session_id": previous,
                    "replaced_at": self._clock(),
                })
            current.meta["host"] = host
            current.meta["host_session_id"] = host_session_id
            current.meta["child_established"] = True
            current.updated_at = self._clock()
            self._flush()
            binding.__dict__.update(current.__dict__)
            return binding

    def mark_running(self, binding: Binding, *, phase: str = "") -> Binding:
        with self._mutation_lock():
            current = self.get(binding.task_id, binding.role_id, binding.subtask_id)
            if current is None:
                raise KeyError(binding.key)
            current.state = RUNNING
            if phase:
                current.last_phase = phase
            current.updated_at = self._clock()
            self._flush()
            binding.__dict__.update(current.__dict__)
            return binding

    def mark_settled(self, binding: Binding, *, phase: str = "",
                     checkpoint: Optional[str] = None,
                     turn_output: Optional[dict] = None) -> Binding:
        """This *turn* is done; the session itself stays resumable (R3)."""
        with self._mutation_lock():
            current = self.get(binding.task_id, binding.role_id, binding.subtask_id)
            if current is None:
                raise KeyError(binding.key)
            current.state = SETTLED
            current.turns += 1
            if phase:
                current.last_phase = phase
            if checkpoint:
                current.checkpoints.append(checkpoint)
            if turn_output:
                current.meta.setdefault("last_turn", {}).update(turn_output)
            current.updated_at = self._clock()
            self._flush()
            binding.__dict__.update(current.__dict__)
            return binding

    def mark_failed(self, binding: Binding, *, error: str,
                    phase: str = "") -> Binding:
        # A failed turn does not invalidate the session: retrying continues it.
        with self._mutation_lock():
            current = self.get(binding.task_id, binding.role_id, binding.subtask_id)
            if current is None:
                raise KeyError(binding.key)
            current.state = FAILED
            current.last_error = error
            if phase:
                current.last_phase = phase
            current.updated_at = self._clock()
            self._flush()
            binding.__dict__.update(current.__dict__)
            return binding

    def archive_task(self, task_id: str) -> List[Binding]:
        """Close a finished task's sessions without deleting its artifacts."""
        with self._mutation_lock():
            touched = []
            for binding in self._bindings.values():
                if binding.task_id != task_id or binding.state == ARCHIVED:
                    continue
                if self.is_busy(binding.session_id):
                    continue
                binding.state = ARCHIVED
                binding.updated_at = self._clock()
                touched.append(binding)
            if touched:
                self._flush()
            return touched

    # -- introspection --------------------------------------------------
    def bindings(self, task_id: Optional[str] = None) -> List[Binding]:
        rows = list(self._bindings.values())
        if task_id is not None:
            rows = [row for row in rows if row.task_id == task_id]
        return sorted(rows, key=lambda row: (row.task_id, row.role_id,
                                             row.subtask_id))

    def snapshot(self) -> dict:
        return {
            "path": self.path,
            "count": len(self._bindings),
            "bindings": [row.to_dict() for row in self.bindings()],
            "inflight": sorted(self._inflight),
        }
