"""Orchestrated run: drive the deterministic pipeline through the Session Registry.

`ExampleForDSH` published one work order after another and answered each with a
*fresh* child. That satisfies "one decision per order" but not R2: the reader
that revised a paper was a different session from the reader that first read it.

This module keeps the engine's existing state machine and mailbox unchanged --
it still calls `init`/`orders`/`reply`/`step`/`stage`/`submit` -- and changes
only *who answers* and *on which session*:

    for each work order:
        role  = order.agent_id
        phase = order.kind / stage
        plan  = bridge.plan(task, role, phase)     # idempotent; one session
        reply = runtime.turn(plan)                 # create-or-resume that session
        bridge.settle(...)
        engine.reply(order, reply)

Because `plan` is the only creation point, a role that appears in phase 3 gets
the same `session_id` it had in phase 1, and the run's `session-events.jsonl`
records that fact for the acceptance report.

The loop itself is intentionally in Python: it is a coordination decision, and
the guide says key state transitions should be deterministic code, not model
judgement. The model still makes every *research* decision -- it just does so on
a stable session.
"""
from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .contracts import ExecutionContext
from .dispatch import Dispatcher
from .registry import SessionRegistry

TERMINAL = ("completed", "failed", "blocked_material", "blocked_research",
            "cancelled")


def _here() -> str:
    return os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))


@dataclass
class RunReport:
    task_id: str
    status: str = "running"
    phase: str = ""
    orders_answered: int = 0
    sessions: Dict[str, str] = field(default_factory=dict)
    created: List[str] = field(default_factory=list)
    reused: List[str] = field(default_factory=list)
    stages: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    steps: int = 0

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "status": self.status,
            "phase": self.phase,
            "orders_answered": self.orders_answered,
            "sessions": dict(self.sessions),
            "created_sessions": list(self.created),
            "reused_sessions": list(self.reused),
            "stages": list(self.stages),
            "steps": self.steps,
            "errors": list(self.errors),
        }


class EngineCli:
    """Thin wrapper over `cli.py`, so the orchestrator needs no engine imports."""

    def __init__(self, engine_dir: str, python: str, config: str):
        self.engine_dir = engine_dir
        self.python = python
        self.config = config

    def __call__(self, args: List[str]) -> dict:
        completed = subprocess.run(
            [self.python, os.path.join(self.engine_dir, "cli.py"),
             "--config", self.config] + args,
            cwd=self.engine_dir, capture_output=True, text=True)
        text = (completed.stdout or "").strip()
        if not text:
            raise RuntimeError("cli.py {} produced no output: {}".format(
                args[0], (completed.stderr or "").strip()))
        return json.loads(text)


class Orchestrator:
    """Runs one research task, dispatching every role turn through the registry.

    `turn_fn(plan, order) -> dict` performs one model turn on the planned
    session. The plugin supplies a DSH implementation; tests supply a fake.
    """

    def __init__(self, *, engine: EngineCli, registry: SessionRegistry,
                 turn_fn: Callable[[Any, dict], dict],
                 events_path: str = "", task_id: str = "",
                 max_steps: int = 200,
                 auto_answer: Optional[Callable[[dict], Optional[dict]]] = None):
        self.engine = engine
        self.registry = registry
        self.turn_fn = turn_fn
        self.task_id = task_id
        self.max_steps = max_steps
        self.auto_answer = auto_answer
        self.dispatcher = Dispatcher(registry, runtime=None,
                                     events_path=events_path)
        self.report = RunReport(task_id=task_id)

    # -- one order ------------------------------------------------------
    def _answer_order(self, order: dict) -> None:
        # `pending`/`orders` return a *summary* row; the order's actual payload
        # (role prompt, conversation, tool schemas, reply contract, task) comes
        # from `show`. Answering from the summary would hand the model an order
        # with no question in it -- the bug this comment exists to prevent.
        brief = self.engine(["show", "--run-id", self.task_id,
                             "--order", order["order_id"]])
        if brief.get("ok") is False or "order_id" not in brief:
            raise RuntimeError("cannot render order {}: {}".format(
                order.get("order_id"), json.dumps(brief, ensure_ascii=False)[:300]))
        role = order.get("agent_id") or brief.get("agent_id") or "read"
        phase = order.get("kind") or order.get("title") or "turn"
        # `subtask_id` must identify a *logical* parallel unit, not one turn.
        # Only PaperRead is genuinely per-paper (two papers are two independent
        # readings), so only it gets a subtask. Every other role is a singleton
        # inside the task: keying them by `order_id` would mint a new session for
        # every order and destroy exactly the continuity R2 requires.
        subtask = ""
        if role == "read":
            subtitle = str(order.get("paper_id") or brief.get("paper_id")
                           or (brief.get("metadata") or {}).get("paper_id")
                           or order.get("order_id") or "")
            subtask = "".join(ch if ch.isalnum() else "-" for ch in subtitle)
        context = ExecutionContext(
            mode="system", task_id=self.task_id, role_id=role,
            subtask_id=subtask, scope=order.get("scope", ""),
            output_contract=order.get("reply_contract", "") or "order_reply",
            trace_id=order.get("order_id", ""))

        # A mechanically-fixed order needs no model turn; answering it through
        # a session would waste a turn and pollute the role's history.
        if self.auto_answer is not None:
            precomputed = self.auto_answer(brief)
            if precomputed is not None:
                self.engine(["reply", "--run-id", self.task_id,
                             "--order", order["order_id"],
                             "--json", json.dumps(precomputed, ensure_ascii=False)])
                self.report.orders_answered += 1
                return

        plan = self.dispatcher.plan(context, phase=phase, instruction=phase)
        self.report.sessions[role] = plan.session_id
        if plan.created:
            self.report.created.append(plan.session_id)
        else:
            self.report.reused.append(plan.session_id)

        try:
            reply = self.turn_fn(plan, brief)
        except Exception as exc:  # noqa: BLE001 - runtime boundary
            binding = self.registry.get(self.task_id, role, subtask)
            if binding is not None:
                self.registry.mark_failed(binding, error=str(exc), phase=phase)
            raise
        binding = self.registry.get(self.task_id, role, subtask)
        if binding is not None:
            self.registry.mark_child_established(binding)
            self.registry.mark_settled(binding, phase=phase)
        self.engine(["reply", "--run-id", self.task_id, "--order",
                     order["order_id"], "--json",
                     json.dumps(reply, ensure_ascii=False)])
        self.report.orders_answered += 1

    # -- the loop -------------------------------------------------------
    def run(self) -> dict:
        for step in range(self.max_steps):
            pending = self.engine(["pending", "--run-id", self.task_id])
            self.report.steps = step + 1
            self.report.status = pending.get("status", "unknown")
            self.report.phase = pending.get("phase") or ""

            if self.report.status in TERMINAL:
                self._archive()
                return self.report.to_dict()

            stage_task = pending.get("stage_task") or {}
            if self.report.status == "awaiting_stage_agent" and stage_task.get("stage"):
                self._answer_stage(stage_task)
                self.engine(["step", "--run-id", self.task_id])
                continue

            orders = [row for row in (pending.get("orders") or [])
                      if row.get("has_reply") is not True]
            if orders:
                # Serial: one role session must never run two turns at once
                # (guide 6.3), and the engine publishes at most one per agent.
                for order in orders:
                    self._answer_order(order)
                self.engine(["step", "--run-id", self.task_id])
                continue

            before = "{}/{}/{}".format(self.report.status,
                                       pending.get("current_stage") or "",
                                       len(pending.get("orders") or []))
            moved = self.engine(["step", "--run-id", self.task_id])
            after = "{}/{}/{}".format(moved.get("status"),
                                      moved.get("current_stage") or "",
                                      len(moved.get("orders") or []))
            if before == after:
                self._archive()
                return self.report.to_dict()
        raise RuntimeError(
            "run {} did not settle within {} steps".format(self.task_id,
                                                           self.max_steps))

    def run_dir(self) -> str:
        """This run's directory, resolved by the engine (cached)."""
        cached = getattr(self, "_run_dir", "")
        if cached:
            return cached
        try:
            value = self.engine(["run-dir", "--run-id", self.task_id])
            resolved = str(value.get("run_dir") or "")
        except Exception:  # noqa: BLE001 - never fail a run over a debug dump
            resolved = ""
        if not resolved:
            resolved = os.path.join(_here(), "runs", self.task_id)
        self._run_dir = resolved
        return resolved

    def _answer_stage(self, stage_task: dict) -> None:
        stage = stage_task["stage"]
        role = "finalizer"
        context = ExecutionContext(
            mode="system", task_id=self.task_id, role_id=role,
            scope="finalization:" + stage, output_contract="stage:" + stage,
            trace_id="stage-" + stage)
        plan = self.dispatcher.plan(context, phase="stage:" + stage)
        self.report.sessions[role] = plan.session_id
        payload = self.turn_fn(plan, {"stage": stage,
                                      "packet": stage_task.get("packet")})
        binding = self.registry.get(self.task_id, role, "")
        if binding is not None:
            self.registry.mark_child_established(binding)
            self.registry.mark_settled(binding, phase="stage:" + stage)
        # The run directory comes from the engine, which is the only thing that
        # knows the configured `runs_dir`. Hard-coding `<engine>/runs` here wrote
        # payloads to an orphan directory for any other layout.
        path = os.path.join(self.run_dir(), "stage-payloads", stage + ".json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        submitted = self.engine(["submit", "--run-id", self.task_id,
                                 "--stage", stage, "--file", path])
        if submitted.get("ok") is not True:
            raise RuntimeError("stage {} rejected: {}".format(
                stage, json.dumps(submitted.get("error"), ensure_ascii=False)))
        self.report.stages.append(stage)

    def _archive(self) -> None:
        """Close the task's sessions; artifacts stay on disk (guide R2 tail)."""
        try:
            self.registry.archive_task(self.task_id)
        except Exception:  # noqa: BLE001 - archiving must not mask a result
            pass
