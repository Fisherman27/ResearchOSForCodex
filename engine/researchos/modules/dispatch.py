"""Dispatch: the one code path every role turn goes through.

The guide's standard flow (section 6.1) is:

    binding = registry.get(task, role)
    if binding exists:  session_id = binding.session_id   # resume
    else:               session_id = new; registry.insert(...)
    acquire per-session lock; send to that session; persist; release

This module implements exactly that, and it is *the only* place that may decide
to create a session. Everything above it (the DSH plugin, the CLI, a test) sees
two calls:

    plan  -> what the next turn needs (session id, whether it is the first turn)
    settle/fail -> record the outcome

`plan` never creates a second binding for a pair that already has one, which is
what makes requirement R2 testable rather than aspirational: a three-phase
role produces three turns and one session id.

The actual turn execution is performed by a `TurnRuntime`. Two implementations
exist:

    DshTurnRuntime  -- the DSH plugin's side: a reserved child id on the first
                       turn, `send_message` (cold resume) afterwards
    FakeTurnRuntime -- tests: records what it was asked to do, returns canned
                       output, and can simulate a process restart

A runtime only executes; it never mints identities. That separation is what the
guide warns against collapsing (section 3.3).
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Protocol

from .contracts import ExecutionContext
from .registry import Binding, SessionBusy, SessionRegistry, RESUMABLE, RUNNING


class TurnRuntime(Protocol):
    """Executes one model turn on an already-decided session identity."""

    def create_turn(self, session_id: str, *, role_id: str, phase: str,
                    instruction: str, context: ExecutionContext) -> dict:
        ...

    def resume_turn(self, session_id: str, *, role_id: str, phase: str,
                    instruction: str, context: ExecutionContext) -> dict:
        ...


class DispatchError(RuntimeError):
    pass


@dataclass
class TurnPlan:
    """What the caller must do for the next turn of one role."""

    task_id: str
    role_id: str
    subtask_id: str
    session_id: str
    phase: str
    first_turn: bool
    turns: int
    state: str
    created: bool
    instruction: str = ""
    #: Present only when the binding had to be replaced; the reason and the old
    #: session id travel with the plan so the decision is auditable.
    replaced: Optional[str] = None
    replace_reason: str = ""

    def to_dict(self) -> dict:
        out = {
            "task_id": self.task_id,
            "role_id": self.role_id,
            "subtask_id": self.subtask_id,
            "session_id": self.session_id,
            "phase": self.phase,
            "first_turn": self.first_turn,
            "turns": self.turns,
            "state": self.state,
            "created": self.created,
            "instruction": self.instruction,
        }
        if self.replaced:
            out["replaced"] = self.replaced
            out["replace_reason"] = self.replace_reason
        return out


class Dispatcher:
    """Owns the registry and the lock discipline; delegates execution."""

    def __init__(self, registry: SessionRegistry, runtime: Optional[TurnRuntime],
                 *, events_path: Optional[str] = None):
        self.registry = registry
        self.runtime = runtime
        self.events_path = events_path

    # -- audit trail ----------------------------------------------------
    def _log(self, event: str, **fields) -> None:
        """Append one execution event. Never contains credentials or paper text."""
        if not self.events_path:
            return
        row = {"at": time.time(), "event": event}
        row.update({key: value for key, value in fields.items() if value is not None})
        os.makedirs(os.path.dirname(self.events_path), exist_ok=True)
        with open(self.events_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    # -- planning -------------------------------------------------------
    def plan(self, context: ExecutionContext, *, phase: str,
             instruction: str = "") -> TurnPlan:
        """Decide the session identity for the next turn of this role.

        This is the *only* creation point. `resolve` returns the existing
        binding unchanged, so calling `plan` once per phase is correct and the
        three phases of a role share one session id.
        """
        before = self.registry.get(context.task_id, context.role_id,
                                   context.subtask_id)
        binding = self.registry.resolve(context.task_id, context.role_id,
                                        context.subtask_id)
        created = before is None

        if binding.state not in RESUMABLE and binding.state != RUNNING:
            # A terminal/unresumable state (e.g. archived mid-task) means the
            # caller must be explicit: we refuse rather than silently forking.
            raise DispatchError(
                "session {} for ({}, {}) is {} and cannot accept a new turn; "
                "call replace() with a documented reason first".format(
                    binding.session_id, context.task_id, context.role_id,
                    binding.state))
        if self.registry.is_busy(binding.session_id):
            raise SessionBusy(binding.session_id)

        self._log("session_plan", task_id=context.task_id,
                  role_id=context.role_id, session_id=binding.session_id,
                  phase=phase, created=created, turns=binding.turns,
                  first_turn=not binding.meta.get("child_established"),
                  mode=context.mode)
        return TurnPlan(
            task_id=context.task_id,
            role_id=context.role_id,
            subtask_id=context.subtask_id,
            session_id=binding.session_id,
            phase=phase,
            # "First turn" means "the child session does not exist yet", NOT
            # "no turn has completed". A failed turn leaves turns == 0 while the
            # child is already on disk; calling create again would raise
            # DUPLICATE_CHILD instead of continuing the same session.
            first_turn=not binding.meta.get("child_established"),
            turns=binding.turns,
            state=binding.state,
            created=created,
            instruction=instruction,
        )

    # -- execution ------------------------------------------------------
    def run_turn(self, context: ExecutionContext, *, phase: str,
                 instruction: str) -> dict:
        """Plan, lock, execute on the exact session, and persist the outcome.

        A failure marks the *binding* failed but keeps it resumable: the guide
        requires that a retry does not mint a new session just because the last
        turn errored (section 6.3).
        """
        plan = self.plan(context, phase=phase, instruction=instruction)
        if self.runtime is None:
            raise DispatchError("no turn runtime configured")
        binding = self._binding(plan)
        self.registry.acquire(plan.session_id)
        try:
            self.registry.mark_running(binding, phase=phase)
            self._log("session_running", task_id=context.task_id,
                      role_id=context.role_id, session_id=plan.session_id,
                      phase=phase, first_turn=plan.first_turn)
            runner = (self.runtime.create_turn if plan.first_turn
                      else self.runtime.resume_turn)
            try:
                output = runner(
                    plan.session_id,
                    role_id=context.role_id,
                    phase=phase,
                    instruction=instruction,
                    context=context,
                )
                # The child now exists on the runtime side. Record it so a later
                # phase resumes instead of creating a second child, even if this
                # turn subsequently fails (see registry.mark_child_established).
                if plan.first_turn:
                    self.registry.mark_child_established(binding)
            except Exception as exc:  # noqa: BLE001 - runtime boundary
                self.registry.mark_failed(binding, error=str(exc), phase=phase)
                self._log("session_failed", task_id=context.task_id,
                          role_id=context.role_id,
                          session_id=plan.session_id, phase=phase,
                          error="{}: {}".format(type(exc).__name__, exc))
                raise
            self.registry.mark_settled(binding, phase=phase,
                                       turn_output=output if isinstance(output, dict) else None)
            self._log("session_settled", task_id=context.task_id,
                      role_id=context.role_id, session_id=plan.session_id,
                      phase=phase, turns=binding.turns,
                      resumed=not plan.first_turn)
            return {
                "session_id": plan.session_id,
                "phase": phase,
                "first_turn": plan.first_turn,
                "turns": binding.turns,
                "created": plan.created,
                "output": output,
            }
        finally:
            self.registry.release(plan.session_id)

    def _binding(self, plan: TurnPlan) -> Binding:
        binding = self.registry.get(plan.task_id, plan.role_id, plan.subtask_id)
        if binding is None or binding.session_id != plan.session_id:
            raise DispatchError(
                "registry binding for ({}, {}) changed between plan and run".format(
                    plan.task_id, plan.role_id))
        return binding

    # -- verification helpers -------------------------------------------
    def trace(self, task_id: str) -> List[dict]:
        """Every session-related event for a task, for the acceptance report."""
        if not self.events_path or not os.path.isfile(self.events_path):
            return []
        rows = []
        with open(self.events_path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if row.get("task_id") == task_id:
                    rows.append(row)
        return rows


class FakeTurnRuntime:
    """Test runtime: records calls, returns canned output, can drop residency.

    `restart()` clears its in-memory idea of which sessions are live, which is
    how the tests prove that continuity survives a process restart (guide 6.5
    item 4) rather than relying on a warm process.
    """

    def __init__(self, responder: Optional[Callable[[str, str, str], dict]] = None):
        self.calls: List[dict] = []
        self.responder = responder or (lambda session_id, role, phase: {
            "phase": phase, "role": role, "note": "canned"})
        #: session ids this fake believes are resident in memory
        self.resident: set = set()

    def create_turn(self, session_id, *, role_id, phase, instruction, context):
        self.calls.append({"kind": "create", "session_id": session_id,
                           "role_id": role_id, "phase": phase,
                           "instruction": instruction})
        self.resident.add(session_id)
        return self.responder(session_id, role_id, phase)

    def resume_turn(self, session_id, *, role_id, phase, instruction, context):
        self.calls.append({"kind": "resume", "session_id": session_id,
                           "role_id": role_id, "phase": phase,
                           "instruction": instruction})
        self.resident.add(session_id)
        return self.responder(session_id, role_id, phase)

    def restart(self) -> None:
        """Simulate the host process going away: memory is gone, disk is not."""
        self.resident.clear()
