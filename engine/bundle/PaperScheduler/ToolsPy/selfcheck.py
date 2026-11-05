#!/usr/bin/env python3
"""完全离线的契约自检。"""
from __future__ import annotations

import paperscheduler
from paperscheduler.result import blocked, empty, failed, ok, partial
from paperscheduler.errors import make_error


def main() -> int:
    checks = []

    def check(name, condition):
        checks.append((name, bool(condition)))

    for status, env in (
        ("ok", ok()), ("partial", partial()), ("empty", empty()),
        ("blocked", blocked(make_error("INPUT_INVALID", "x"))),
        ("failed", failed(make_error("INPUT_INVALID", "x"))),
    ):
        check("envelope." + status, env["status"] == status and
              env["ok"] == (status in ("ok", "partial", "empty")))
        check("fields." + status, all(key in env for key in
              ("ok", "status", "data", "error", "warnings", "diagnostics", "meta")))
    snapshot = {
        "snapshot_id": "org_demo",
        "papers": [
            {"paper_id": "old", "year": 2020, "reading_depth": "unread"},
            {"paper_id": "new", "year": 2022, "reading_depth": "unread"},
        ],
        "citations": [{"from_paper": "new", "to_paper": "old", "internal": 1}],
        "paper_relations": [{"src_paper": "new", "dst_paper": "old"}],
    }
    for strategy in ("chronological", "citation_based", "dependency_based"):
        env = paperscheduler.plan_reading(snapshot, strategy=strategy)
        check("planner." + strategy, env["ok"] and env["data"]["selected"] == ["old"])
    names = {row["name"] for row in paperscheduler.list_tools()["data"]}
    check("registry.exports", all(callable(getattr(paperscheduler, name, None))
          for name in names))
    index = paperscheduler.build_symbol_index()
    check("docs.index", index["ok"] and all(
        symbol in index["data"] for symbol in ("E10", "E20", "E30", "E31", "P10")))
    check("docs.read", paperscheduler.read_symbol("E10")["ok"])
    for name, passed in checks:
        print(("PASS" if passed else "FAIL") + " " + name)
    failed_count = sum(not passed for _, passed in checks)
    print("{} passed, {} failed".format(len(checks) - failed_count, failed_count))
    return 1 if failed_count else 0


if __name__ == "__main__":
    raise SystemExit(main())
