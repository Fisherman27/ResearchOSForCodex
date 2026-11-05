#!/usr/bin/env python3
"""Codex-native control surface for ResearchOS.

The deterministic engine still owns state, tools and contracts.  This adapter
collapses the host chores into three operations:

``next``   return each pending job with its stable logical session and whether
           Codex should spawn or follow up an existing child;
``bind``   attach the opaque child id returned by ``spawn_agent``;
``submit`` accept one child's JSON reply, advance the engine, then return the
           next dispatch set.

It deliberately does not try to call Codex collaboration tools from Python.
Those are host capabilities available to the Lead, not subprocess APIs.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, Tuple


HERE = os.path.abspath(os.path.dirname(__file__))
ROOT = os.path.dirname(HERE)
ENGINE = os.path.join(ROOT, "engine")
for path in (ENGINE, ROOT):
    if path not in sys.path:
        sys.path.insert(0, path)

from researchos import ResearchSystem, SystemConfig  # noqa: E402
from researchos.modules.registry import SessionRegistry  # noqa: E402


DEFAULT_CONFIG = os.path.join(ROOT, "config", "handoff.json")
TERMINAL = {"completed", "blocked_research", "failed"}
ROLE_KITS = {
    "research": "researchos-research",
    "organization": "researchos-organization",
    "scheduler": "researchos-scheduler",
    "read": "researchos-reader",
    "finalizer": "researchos-finalizer",
    "director": "researchos-director",  # old-run compatibility only
}


def _emit(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def _system(args) -> ResearchSystem:
    return ResearchSystem(SystemConfig.from_file(args.config), args.run_id)


def _registry(system: ResearchSystem) -> SessionRegistry:
    return SessionRegistry(os.path.join(system.store.root, "sessions"))


def _json_object(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _read_task(order: Dict[str, Any]) -> Dict[str, Any]:
    metadata = order.get("metadata") or {}
    candidates = [
        (metadata.get("initial") or {}).get("task"),
        metadata.get("task"),
        _json_object(order.get("question")).get("task"),
    ]
    return next((value for value in candidates if isinstance(value, dict)), {})


def _identity(order: Dict[str, Any]) -> Tuple[str, str]:
    """Map a work order to the stable Codex role and optional Reader group."""
    raw = str(order.get("agent_id") or "").lower()
    base = raw.split(".", 1)[0]
    if base == "reader":
        base = "read"
    if base not in ROLE_KITS:
        base = raw or "unknown"
    if base != "read":
        return base, ""
    task = _read_task(order)
    group = str(task.get("reader_group_id") or "").strip()
    if not group:
        group = str(task.get("target_paper") or task.get("paper_id")
                    or task.get("task_id") or order.get("order_id") or "read").strip()
    return "read", group


def _binding_view(registry: SessionRegistry, run_id: str,
                  role: str, subtask: str = "") -> Dict[str, Any]:
    binding = registry.resolve(run_id, role, subtask)
    host_id = str((binding.meta or {}).get("host_session_id") or "")
    return {
        "role": role,
        "subtask": subtask,
        "logical_session_id": binding.session_id,
        "host_session_id": host_id or None,
        "action": "followup" if host_id else "spawn",
        "skill": ROLE_KITS.get(role),
    }


def _jobs(system: ResearchSystem, *, compact: bool = False) -> Dict[str, Any]:
    state = system.store.load()
    registry = _registry(system)
    jobs = []
    for order in (system.handoff.pending() if system.handoff else []):
        role, subtask = _identity(order)
        job = {
            "kind": "work_order",
            "order_id": order.get("order_id"),
            "agent_id": order.get("agent_id"),
            "dispatch": _binding_view(registry, system.run_id, role, subtask),
            "reply_contract": "one JSON object: tool_calls or payload",
        }
        if compact:
            job["order_path"] = os.path.join(system.handoff.outbox,
                                              str(order.get("order_id")) + ".json")
        else:
            job["order"] = order
        jobs.append(job)

    pending = state.get("pending_final_task") or {}
    if state.get("status") == "awaiting_stage_agent" and pending:
        jobs.append({
            "kind": "final_stage",
            "stage": pending.get("stage"),
            "packet_path": pending.get("path"),
            "dispatch": _binding_view(registry, system.run_id, "finalizer"),
            "reply_contract": "one stage payload JSON object",
        })

    return {
        "ok": True,
        "run_id": system.run_id,
        "run_dir": system.store.root,
        "status": state.get("status"),
        "phase": state.get("phase"),
        "terminal": state.get("status") in TERMINAL,
        "jobs": jobs,
    }


def _pump(system: ResearchSystem) -> int:
    """Advance until the engine stops changing or publishes the next order."""
    steps = 0
    for _ in range(400):
        before_state = json.dumps(system.store.load(), sort_keys=True, default=str)
        before_orders = tuple(sorted(
            row.get("order_id") for row in
            (system.handoff.pending() if system.handoff else [])))
        system.resume()
        steps += 1
        after_state = json.dumps(system.store.load(), sort_keys=True, default=str)
        after_orders = tuple(sorted(
            row.get("order_id") for row in
            (system.handoff.pending() if system.handoff else [])))
        if (before_state, before_orders) == (after_state, after_orders):
            break
        if after_orders and not any(system.handoff.has_reply(value)
                                    for value in after_orders):
            break
    return steps


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="ResearchOS Codex 原生控制入口")
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    parser.add_argument("--run-id", required=True)
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="创建运行并立即返回首批 Codex Job")
    init.add_argument("--question", required=True)
    init.add_argument("--search-options", default="{}")
    init.add_argument("--full", action="store_true",
                      help="内联完整工单；默认只返回路径，避免污染 Lead 上下文")

    next_cmd = sub.add_parser("next", help="返回下一批及其固定 Codex Session")
    next_cmd.add_argument("--full", action="store_true")

    bind = sub.add_parser("bind", help="登记 spawn_agent 返回的真实 child id")
    bind.add_argument("--host-session-id", required=True)
    target = bind.add_mutually_exclusive_group(required=True)
    target.add_argument("--order")
    target.add_argument("--role", choices=tuple(ROLE_KITS))
    bind.add_argument("--subtask", default="")

    submit = sub.add_parser("submit", help="提交工单回复并自动推进")
    submit.add_argument("--order", required=True)
    submit.add_argument("--file", required=True)
    submit.add_argument("--full", action="store_true")

    submit_stage = sub.add_parser("submit-stage", help="提交 Finalizer 阶段结果并自动推进")
    submit_stage.add_argument("--stage", required=True)
    submit_stage.add_argument("--file", required=True)
    submit_stage.add_argument("--full", action="store_true")

    advance = sub.add_parser("advance", help="无新回复时请求引擎继续推进")
    advance.add_argument("--full", action="store_true")

    recover = sub.add_parser("recover", help="从健康的文献检查点重开父级失败")
    recover.add_argument("--full", action="store_true")

    args = parser.parse_args(argv)
    system = _system(args)

    if args.command == "init":
        options = _json_object(args.search_options)
        started = system.start(args.question, search_options=options)
        result = _jobs(system, compact=not args.full)
        result["start"] = started.get("status")
        result["ok"] = bool(started.get("ok"))
        _emit(result)
        return 0 if started.get("ok") else 1

    if args.command == "next":
        _emit(_jobs(system, compact=not args.full))
        return 0

    if args.command == "bind":
        role, subtask = args.role, args.subtask
        if args.order:
            path = os.path.join(system.handoff.outbox, args.order + ".json")
            if not os.path.isfile(path):
                _emit({"ok": False, "error": "order_not_found", "order": args.order})
                return 2
            with open(path, encoding="utf-8") as handle:
                role, subtask = _identity(json.load(handle))
        registry = _registry(system)
        binding = registry.resolve(system.run_id, role, subtask)
        registry.bind_host_session(binding, args.host_session_id, host="codex")
        _emit({"ok": True, "dispatch": _binding_view(
            registry, system.run_id, role, subtask)})
        return 0

    if args.command == "submit":
        if not system.handoff:
            _emit({"ok": False, "error": "not_handoff_mode"})
            return 2
        with open(args.file, encoding="utf-8") as handle:
            reply = json.load(handle)
        system.handoff.reply(args.order, reply)
        steps = _pump(system)
        result = _jobs(system, compact=not args.full)
        result["steps"] = steps
        _emit(result)
        return 0

    if args.command == "submit-stage":
        with open(args.file, encoding="utf-8") as handle:
            payload = json.load(handle)
        outcome = system.submit_final_result(args.stage, payload)
        if not outcome.get("ok"):
            _emit({"ok": False, "submission": outcome,
                   "next": _jobs(system, compact=not args.full)})
            return 1
        steps = _pump(system)
        result = _jobs(system, compact=not args.full)
        result["ok"] = True
        result["steps"] = steps
        _emit(result)
        return 0

    if args.command == "recover":
        outcome = system.recover_failed_literature()
        if not outcome.get("ok"):
            _emit({"ok": False, "recovery": outcome,
                   "next": _jobs(system, compact=not args.full)})
            return 1
        steps = _pump(system)
        result = _jobs(system, compact=not args.full)
        result["recovery"] = outcome.get("status")
        result["steps"] = steps
        _emit(result)
        return 0

    steps = _pump(system)
    result = _jobs(system, compact=not args.full)
    result["steps"] = steps
    _emit(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
