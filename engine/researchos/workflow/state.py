"""Crash-safe run state, shared by the Director and every supervisor.

Byte-compatible with the original pipeline: one ``state.json`` per run,
atomically replaced, plus an append-only ``events.jsonl``.  What is new in this
implementation is that the state also records *who* is acting (``actor``) and
the per-agent trace directory, so a failed run can be explained turn by turn.
"""
from __future__ import annotations

import json
import os
import tempfile
from typing import Any, Dict, Iterable, Optional

from ..runtime.trace import EventLog, now


TERMINAL = ("completed", "failed", "blocked_research", "cancelled")


def literature_initial_state(run_id: str) -> dict:
    return {
        "schema_version": "2.0",
        "run_id": run_id,
        "status": "created",
        "cycle": 0,
        "knowledge_snapshot": None,
        "graph_version": None,
        "pending_tasks": [],
        "completed_task_ids": [],
        "completed_paper_ids": [],
        "failed_tasks": [],
        "reread_counts": {},
        "reread_log": [],
        "relation_review": {
            "status": "not_started",
            "terminal_status": None,
            "pending_path": None,
            "reviewed_at": None,
            "relations_submitted": None,
        },
        "created_at": now(),
        "updated_at": now(),
    }


class RunStore:
    def __init__(self, runs_dir: str, run_id: str):
        self._check(run_id)
        self.run_id = run_id
        self.root = os.path.join(os.path.abspath(runs_dir), run_id)
        self.state_path = os.path.join(self.root, "state.json")
        self.pending_dir = os.path.join(self.root, "pending")
        self.results_dir = os.path.join(self.root, "results")
        os.makedirs(self.pending_dir, exist_ok=True)
        os.makedirs(self.results_dir, exist_ok=True)

    @staticmethod
    def _check(run_id: str) -> None:
        allowed = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
        if not run_id or any(ch not in allowed for ch in run_id):
            raise ValueError("run_id may contain only letters, digits, '-' and '_'")

    @property
    def log(self) -> EventLog:
        return EventLog(os.path.join(self.root, "events.jsonl"))

    @property
    def agent_root(self) -> str:
        return os.path.join(self.root, "agents")

    def load(self, *, initial: Optional[dict] = None) -> dict:
        if not os.path.isfile(self.state_path):
            return (initial or literature_initial_state(self.run_id))
        with open(self.state_path, encoding="utf-8") as handle:
            return json.load(handle)

    def save(self, state: dict, *, actor: str = "") -> None:
        state = dict(state)
        state["updated_at"] = now()
        os.makedirs(self.root, exist_ok=True)
        fd, temp = tempfile.mkstemp(prefix="state-", suffix=".json", dir=self.root)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(state, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, self.state_path)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)

    def write_pending(self, task: dict, package: dict) -> str:
        path = os.path.join(self.pending_dir, task["task_id"] + ".json")
        self._write_json(path, {"task": task, "package": package})
        return path

    def write_result(self, task_id: str, result: dict) -> str:
        path = os.path.join(self.results_dir, task_id + ".json")
        self._write_json(path, result)
        return path

    def read_result(self, task_id: str):
        """A previously stored reading, or None.

        Reading is retried across pauses, so the result of a task must be
        recoverable without asking the reader again: re-dispatching a task whose
        answer already arrived would publish a duplicate work order.
        """
        path = os.path.join(self.results_dir, task_id + ".json")
        if not os.path.isfile(path):
            return None
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)

    def write_artifact(self, name: str, value: Any) -> str:
        path = os.path.join(self.root, "artifacts", name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self._write_json(path, value)
        return path

    @staticmethod
    def _write_json(path: str, value: Any) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        temporary = path + ".tmp"
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)

    def events(self) -> list:
        return self.log.read()

    def actors_seen(self) -> list:
        return sorted({row.get("actor") for row in self.events() if row.get("actor")})
