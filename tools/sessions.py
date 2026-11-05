#!/usr/bin/env python3
"""Durable Codex child-session bindings for one ResearchOS run.

This utility does not create Codex agents.  It gives the coordinating Lead a
disk-backed identity to plan before ``spawn_agent`` and a place to bind the
opaque agent id returned by Codex.  Later work orders can then use
``followup_task`` with the same id.  If a different Codex chat cannot resume
that id, ``replace`` records the reason before a new child is bound.
"""
from __future__ import annotations

import argparse
import json
import os
import sys


HERE = os.path.abspath(os.path.dirname(__file__))
ROOT = os.path.dirname(HERE)
ENGINE = os.path.join(ROOT, "engine")
sys.path.insert(0, ENGINE)

from researchos import SystemConfig  # noqa: E402
from researchos.modules.registry import SessionRegistry  # noqa: E402


DEFAULT_CONFIG = os.path.join(ROOT, "config", "handoff.json")


def _registry(config_path: str, run_id: str) -> SessionRegistry:
    config = SystemConfig.from_file(config_path)
    return SessionRegistry(os.path.join(config.runs_dir, run_id, "sessions"))


def _emit(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="ResearchOS Codex 会话登记")
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    parser.add_argument("--run-id", required=True)
    sub = parser.add_subparsers(dest="command", required=True)

    for name in ("plan", "get", "bind", "replace"):
        command = sub.add_parser(name)
        command.add_argument("--role", required=True)
        command.add_argument("--subtask", default="")
        if name == "bind":
            command.add_argument("--host-session-id", required=True)
        if name == "replace":
            command.add_argument("--reason", required=True)
    sub.add_parser("list")

    args = parser.parse_args(argv)
    registry = _registry(args.config, args.run_id)

    if args.command == "list":
        _emit({"ok": True, "bindings": [row.to_dict()
                                          for row in registry.bindings(args.run_id)]})
        return 0

    if args.command == "plan":
        binding = registry.resolve(args.run_id, args.role, args.subtask)
        value = binding.to_dict()
        value["needs_spawn"] = not bool(
            (binding.meta or {}).get("host_session_id"))
        _emit({"ok": True, "binding": value})
        return 0

    if args.command == "get":
        binding = registry.get(args.run_id, args.role, args.subtask)
        if binding is None:
            _emit({"ok": False, "error": "binding_not_found"})
            return 2
        _emit({"ok": True, "binding": binding.to_dict()})
        return 0

    if args.command == "bind":
        binding = registry.resolve(args.run_id, args.role, args.subtask)
        registry.bind_host_session(binding, args.host_session_id, host="codex")
        _emit({"ok": True, "binding": binding.to_dict()})
        return 0

    binding = registry.replace(args.run_id, args.role, reason=args.reason,
                               subtask_id=args.subtask)
    _emit({"ok": True, "binding": binding.to_dict(), "needs_spawn": True})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
