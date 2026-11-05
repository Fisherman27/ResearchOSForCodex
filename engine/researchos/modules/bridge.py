"""Session bridge: the JSON contract between the DSH plugin and this engine.

The plugin must not invent session identities, and the engine must not know how
DSH runs a child. This command group is the seam:

    reserve -- pre-allocate the fixed task roster without starting agents
    acquire -- decide the session and hold an execution lease
    settle  -- record that the turn finished, with its checkpoint
    fail    -- record that it errored (the session stays resumable)
    state   -- read the binding back, for the plugin's report
    trace   -- the audit trail, for the acceptance report

A JS caller does:

    plan  -> {session_id, first_turn, turns, ...}
    (start a child with childId=session_id, or sendMessage to it)
    settle -> {turns: n}

There is no path here that creates a second session for a pair that already has
one; `plan` is the only creation point and it is idempotent.
"""
from __future__ import annotations

import argparse
import json
import os
from typing import Any

from .cli_common import emit as _emit, engine_root as _root
from .contracts import ExecutionContext, ROLES
from .dispatch import Dispatcher
from .registry import SessionRegistry


def _run_root(args) -> str:
    """Where the engine actually put this run.

    The engine's `runs_dir` comes from the config file, not from the product
    directory, so a caller must not assume `<product>/runs`. Resolving it through
    `SystemConfig` keeps the bridge correct for every deployment (including the
    temp directories the tests use) instead of only the default layout.
    """
    if getattr(args, "run_root", ""):
        return os.path.abspath(args.run_root)
    # Fall back to the SAME config the CLI defaults to. Returning
    # `<engine>/runs/<task>` here used to disagree with `cli.py`, which defaults
    # to `engine/config/mode.json`: the engine wrote `state.json` under the
    # configured `runs_dir` while the bridge looked in `<engine>/runs`, so the
    # dashboard answered `not_found` for a run that existed and the workbench
    # stayed on its onboarding screen forever.
    config_path = args.config or os.path.join(_root(), "config", "mode.json")
    if os.path.isfile(config_path):
        from researchos import SystemConfig  # noqa: WPS433 - engine package
        config = SystemConfig.from_file(config_path)
        return os.path.join(config.runs_dir, args.task_id)
    return os.path.join(_root(), "runs", args.task_id)


def _registry(args) -> SessionRegistry:
    base = args.sessions_dir or os.path.join(_run_root(args), "sessions")
    return SessionRegistry(base)


def _dispatcher(args) -> Dispatcher:
    events = args.events or os.path.join(_run_root(args),
                                         "session-events.jsonl")
    return Dispatcher(_registry(args), runtime=None, events_path=events)


def _context(args) -> ExecutionContext:
    return ExecutionContext(
        mode=args.mode, task_id=args.task_id, role_id=args.role_id,
        subtask_id=args.subtask_id or "", scope=args.scope or "",
        trace_id=args.trace_id or "")


def cmd_plan(args) -> int:
    dispatch = _dispatcher(args)
    plan = dispatch.plan(_context(args), phase=args.phase,
                         instruction=args.instruction or "")
    return _emit({"ok": True, "plan": plan.to_dict()})


def cmd_acquire(args) -> int:
    dispatch = _dispatcher(args)
    plan = dispatch.plan(_context(args), phase=args.phase,
                         instruction=args.instruction or "")
    registry = dispatch.registry
    token = registry.acquire_lease(plan.session_id, ttl_seconds=args.lease_ttl)
    return _emit({"ok": True, "plan": plan.to_dict(), "lease_token": token})


def cmd_reserve(args) -> int:
    registry = _registry(args)
    roles = args.roles or ["research", "organization", "scheduler", "finalizer"]
    bindings = registry.reserve_roster(args.task_id, roles)
    # The engine owns `runs_dir`; the plugin must not re-derive it from
    # `productDir`, which is only correct for the default layout. Reporting the
    # resolved root here keeps one authority for the path.
    return _emit({"ok": True, "task_id": args.task_id,
                  "run_root": _run_root(args),
                  "bindings": [binding.to_dict() for binding in bindings]})


def _release_lease(args, registry, binding) -> None:
    if args.lease_token:
        registry.release_lease(binding.session_id, args.lease_token)


def cmd_settle(args) -> int:
    registry = _registry(args)
    binding = registry.get(args.task_id, args.role_id, args.subtask_id or "")
    if binding is None:
        return _emit({"ok": False,
                      "error": "no binding for ({}, {})".format(
                          args.task_id, args.role_id)}, code=2)
    checkpoint = None
    output = None
    if args.output_file and os.path.isfile(args.output_file):
        with open(args.output_file, encoding="utf-8") as handle:
            output = json.load(handle)
        checkpoint = args.output_file
    registry.mark_settled(binding, phase=args.phase or binding.last_phase,
                          checkpoint=checkpoint,
                          turn_output=output if isinstance(output, dict) else None)
    _release_lease(args, registry, binding)
    return _emit({"ok": True, "session_id": binding.session_id,
                  "turns": binding.turns, "state": binding.state})


def cmd_fail(args) -> int:
    registry = _registry(args)
    binding = registry.get(args.task_id, args.role_id, args.subtask_id or "")
    if binding is None:
        return _emit({"ok": False, "error": "no binding"}, code=2)
    registry.mark_failed(binding, error=args.error or "unknown error",
                         phase=args.phase or "")
    _release_lease(args, registry, binding)
    return _emit({"ok": True, "session_id": binding.session_id,
                  "state": binding.state,
                  "resumable": True})


def cmd_state(args) -> int:
    registry = _registry(args)
    if args.role_id:
        binding = registry.get(args.task_id, args.role_id, args.subtask_id or "")
        if binding is None:
            return _emit({"ok": False, "error": "no binding"}, code=2)
        return _emit({"ok": True, "binding": binding.to_dict()})
    return _emit({"ok": True, "run_root": _run_root(args),
                  "registry": registry.snapshot()})


def cmd_trace(args) -> int:
    dispatch = _dispatcher(args)
    rows = dispatch.trace(args.task_id)
    # A compact evidence table: which phases used which session id.
    by_role: dict = {}
    for row in rows:
        if row.get("event") != "session_plan":
            continue
        key = row["role_id"]
        entry = by_role.setdefault(key, {"session_id": row["session_id"],
                                         "phases": [], "created_count": 0,
                                         "activation_count": 0})
        entry["phases"].append(row.get("phase"))
        if row.get("created"):
            entry["created_count"] += 1
        if row.get("first_turn"):
            entry["activation_count"] += 1
    return _emit({"ok": True, "task_id": args.task_id, "roles": by_role,
                  "events": len(rows)})


def cmd_replace(args) -> int:
    if not args.reason:
        return _emit({"ok": False,
                      "error": "replace requires --reason"}, code=2)
    registry = _registry(args)
    binding = registry.replace(args.task_id, args.role_id,
                               reason=args.reason,
                               subtask_id=args.subtask_id or "")
    return _emit({"ok": True, "binding": binding.to_dict()})


def cmd_started(args) -> int:
    """The runtime confirms it created the child for this session."""
    registry = _registry(args)
    binding = registry.get(args.task_id, args.role_id, args.subtask_id or "")
    if binding is None:
        return _emit({"ok": False, "error": "no binding"}, code=2)
    registry.mark_child_established(binding)
    return _emit({"ok": True, "session_id": binding.session_id,
                  "child_established": True})


def cmd_archive(args) -> int:
    registry = _registry(args)
    touched = registry.archive_task(args.task_id)
    return _emit({"ok": True,
                  "archived": [row.session_id for row in touched]})


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="researchos.modules.bridge",
        description="会话桥：DSH 插件与引擎之间的 JSON 契约")
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--role-id", required=False, choices=ROLES)
    parser.add_argument("--subtask-id", default="")
    parser.add_argument("--mode", default="system", choices=("system",))
    parser.add_argument("--scope", default="")
    parser.add_argument("--trace-id", default="")
    parser.add_argument("--sessions-dir", default="")
    parser.add_argument("--events", default="")
    #: Run root or config; either lets the bridge find the engine's runs_dir.
    parser.add_argument("--run-root", default="")
    parser.add_argument("--config", default="")

    sub = parser.add_subparsers(dest="command", required=True)

    plan = sub.add_parser("plan", help="决定（或复用）本回合的会话")
    plan.add_argument("--phase", required=True)
    plan.add_argument("--instruction", default="")
    plan.set_defaults(func=cmd_plan)

    acquire = sub.add_parser("acquire", help="决定会话并获取跨进程执行租约")
    acquire.add_argument("--phase", required=True)
    acquire.add_argument("--instruction", default="")
    acquire.add_argument("--lease-ttl", type=int, default=1800)
    acquire.set_defaults(func=cmd_acquire)

    reserve = sub.add_parser("reserve", help="预留任务的固定 Agent roster")
    reserve.add_argument("--roles", nargs="*", choices=ROLES)
    reserve.set_defaults(func=cmd_reserve)

    settle = sub.add_parser("settle", help="记录本回合完成")
    settle.add_argument("--phase", default="")
    settle.add_argument("--output-file", default="")
    settle.add_argument("--lease-token", default="")
    settle.set_defaults(func=cmd_settle)

    fail = sub.add_parser("fail", help="记录本回合失败（会话仍可续）")
    fail.add_argument("--error", required=True)
    fail.add_argument("--phase", default="")
    fail.add_argument("--lease-token", default="")
    fail.set_defaults(func=cmd_fail)

    state = sub.add_parser("state", help="读取绑定")
    state.set_defaults(func=cmd_state)

    trace = sub.add_parser("trace", help="会话复用证据表")
    trace.set_defaults(func=cmd_trace)

    replace = sub.add_parser("replace", help="显式替换会话（需原因）")
    # Not `required=True`: a missing reason must be a JSON refusal, not an
    # argparse exit that gives the plugin an unparseable stderr.
    replace.add_argument("--reason", default="")
    replace.set_defaults(func=cmd_replace)

    started = sub.add_parser("started",
                             help="运行时确认已创建子会话（之后一律续话）")
    started.set_defaults(func=cmd_started)

    archive = sub.add_parser("archive", help="任务结束归档会话")
    archive.set_defaults(func=cmd_archive)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if not args.role_id and args.command not in ("state", "trace", "archive", "reserve"):
        return _emit({"ok": False, "error": "--role-id is required for this command"},
                     code=2)
    try:
        return args.func(args)
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        return _emit({"ok": False,
                      "error": "{}: {}".format(type(exc).__name__, exc)}, code=2)


if __name__ == "__main__":
    raise SystemExit(main())
