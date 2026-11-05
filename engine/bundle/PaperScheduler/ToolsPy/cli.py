#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json

import paperscheduler


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="PaperScheduler CLI")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list")
    doc = sub.add_parser("doc")
    doc.add_argument("symbol")
    plan = sub.add_parser("next")
    plan.add_argument("--kb", required=True)
    plan.add_argument("--planner", default="chronological")
    plan.add_argument("--context", default="none")
    plan.add_argument("--batch-size", type=int, default=1)
    plan.add_argument("--reading-strategy", default="S0")
    args = parser.parse_args(argv)
    if args.command == "list":
        result = paperscheduler.list_tools()
    elif args.command == "doc":
        result = paperscheduler.read_symbol(args.symbol)
    else:
        result = paperscheduler.next_reading_tasks(
            args.kb, planner_strategy=args.planner,
            context_strategy=args.context, batch_size=args.batch_size,
            reading_strategy_id=args.reading_strategy)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
