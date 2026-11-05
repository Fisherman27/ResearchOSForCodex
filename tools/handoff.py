#!/usr/bin/env python3
"""Work-order driver for the ResearchOS multi-agent engine.

A host agent (Codex, DSH, a human) reads one work order, spawns a real
subagent with exactly the system prompt, conversation and tool list the order
contains, and writes the reply back.  The engine then executes the requested
tools, validates the result and advances its state machine.

Commands::

    init    --run-id ID --question Q   create the run and publish order #1
    orders  --run-id ID                list pending work orders (JSON)
    order   --run-id ID --order O      print one order in full (JSON)
    reply   --run-id ID --order O --file F   write a reply from a file
    resume  --run-id ID                consume replies and advance one step
    status  --run-id ID                print run + literature state

The host never reads the knowledge base and never executes a tool itself: it
only produces *thinking*.  The engine owns execution, contracts and gates.
"""
from __future__ import annotations

import argparse
import json
import os
import sys


HERE = os.path.abspath(os.path.dirname(__file__))
ROOT = os.path.dirname(HERE)
ENGINE = os.path.join(ROOT, "engine")
for path in (ENGINE, ROOT):
    if path not in sys.path:
        sys.path.insert(0, path)

from researchos import ResearchSystem, SystemConfig  # noqa: E402


DEFAULT_CONFIG = os.path.join(ROOT, "config", "handoff.json")


def _system(args) -> ResearchSystem:
    config = SystemConfig.from_file(args.config or DEFAULT_CONFIG)
    return ResearchSystem(config, args.run_id)


def _emit(value, *, indent=None) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=indent, default=str))


def _state_summary(system: ResearchSystem) -> dict:
    state = system.store.load()
    literature = {}
    path = os.path.join(system.config.runs_dir,
                        system.run_id + "-literature", "state.json")
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as handle:
            literature = json.load(handle)
    return {
        "run_id": system.run_id,
        "run_dir": system.store.root,
        "status": state.get("status"),
        "phase": state.get("phase"),
        "current_stage": state.get("current_stage"),
        "final_artifact": state.get("final_artifact"),
        "stages": {
            "completed": sorted((state.get("final_artifacts") or {}).keys()),
            "pending": (state.get("pending_final_task") or {}).get("stage"),
        },
        "literature": {
            "status": literature.get("status"),
            "cycle": literature.get("cycle"),
            "completed_papers": len(literature.get("completed_paper_ids") or []),
            "ideation": (literature.get("ideation") or {}).get("status"),
        },
        "orders_pending": len(system.handoff.pending() if system.handoff else []),
    }


def _state_signature(system: ResearchSystem) -> tuple:
    """What ``resume`` may change: run state, child state, pending orders."""
    state = system.store.load()
    literature_path = os.path.join(system.config.runs_dir,
                                   system.run_id + "-literature", "state.json")
    literature = ""
    if os.path.isfile(literature_path):
        with open(literature_path, encoding="utf-8") as handle:
            literature = handle.read()
    orders = tuple(sorted(row["order_id"]
                          for row in (system.handoff.pending()
                                      if system.handoff else [])))
    return (json.dumps(state, sort_keys=True, default=str),
            literature, orders)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="ResearchOS work-order driver")
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    common = argparse.ArgumentParser(add_help=False)
    # ``--config`` is accepted before *and* after the subcommand: a host agent
    # composes commands by appending flags, and requiring a fixed position
    # produces avoidable failures.
    common.add_argument("--config", default=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", parents=[common], help="创建运行并发布第一张工单")
    init.add_argument("--run-id", required=True)
    init.add_argument("--question", required=True)
    init.add_argument("--search-options", default="{}",
                      help="JSON，例如 {\"papers\": [...]}（memory 后端需要）")

    for name, help_text in (("orders", "列出待处理工单"),
                            ("resume", "消费回复并推进一步"),
                            ("status", "打印运行状态")):
        command = sub.add_parser(name, parents=[common], help=help_text)
        command.add_argument("--run-id", required=True)

    order = sub.add_parser("order", parents=[common], help="打印一张完整工单")
    order.add_argument("--run-id", required=True)
    order.add_argument("--order", required=True)

    reply = sub.add_parser("reply", parents=[common], help="写入对某张工单的回复")
    reply.add_argument("--run-id", required=True)
    reply.add_argument("--order", required=True)
    reply.add_argument("--file", required=True)

    args = parser.parse_args(argv)
    system = _system(args)

    if args.command == "init":
        options = json.loads(args.search_options or "{}")
        result = system.start(args.question, search_options=options)
        _emit({"start": result.get("status"), "summary": _state_summary(system)},
              indent=2)
        return 0 if result.get("ok") else 1

    if args.command == "orders":
        rows = system.handoff.pending() if system.handoff else []
        _emit([{"order_id": row["order_id"], "seq": row.get("seq"),
                "agent_id": row["agent_id"], "role": row.get("role"),
                "tool": (row.get("metadata") or {}).get("tool"),
                "tools": [tool["name"] for tool in row.get("tools") or []]}
               for row in rows], indent=2)
        return 0

    if args.command == "order":
        path = os.path.join(system.handoff.outbox, args.order + ".json") \
            if system.handoff else ""
        if not path or not os.path.isfile(path):
            print("工单不存在：{}".format(args.order), file=sys.stderr)
            return 1
        with open(path, encoding="utf-8") as handle:
            _emit(json.load(handle), indent=2)
        return 0

    if args.command == "reply":
        if not system.handoff:
            print("当前配置不是 handoff 模式", file=sys.stderr)
            return 2
        with open(args.file, encoding="utf-8") as handle:
            payload = json.load(handle)
        written = system.handoff.reply(args.order, payload)
        _emit({"ok": True, "written": written})
        return 0

    if args.command == "resume":
        if not system.handoff:
            print("当前配置不是 handoff 模式", file=sys.stderr)
            return 2
        # One ``resume`` call is a single, idempotent step.  Repeat it while
        # the engine still makes progress, so a host answering a batch of
        # replies gets the next orders in the same invocation.  The bound is a
        # safety net against a buggy transition loop, not a normal exit path.
        steps = 0
        for _ in range(400):
            before = _state_signature(system)
            system.resume()
            steps += 1
            if _state_signature(system) == before:
                break
        _emit({"steps": steps, "summary": _state_summary(system)}, indent=2)
        return 0

    if args.command == "status":
        _emit(_state_summary(system), indent=2)
        state = system.store.load()
        return 0 if state.get("status") not in ("failed",) else 1

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
