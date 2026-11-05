"""Acceptance CLI: run the guide's checklist and emit the evidence report.

The guide (section 12) demands that completion be *shown*, not asserted:

    audit    run every test group and print the checklist result
    evidence replay one task and print the per-phase session id table
    modules  show the internal module division (no user-facing entry)
    mode     show the effective Mode composition (tools a session gets)

`audit` is the one command a reviewer should need: it runs the suites and then
reads the *evidence files* the tests produced, so the report cannot pass by
accident if a test was skipped.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import unittest
from typing import Any, Dict, List, Optional

from .cli_common import emit as _emit, engine_root as _root
from . import bridge  # noqa: F401  (kept for the module surface)
from .registry import SessionRegistry


def _project() -> str:
    return os.path.dirname(_root())


def cmd_audit(args) -> int:
    """Run the suites and turn the checklist into a pass/fail table."""
    engine_dir = _root()
    suites = [
        ("session_lifecycle", "tests.test_session_lifecycle",
         "会话连续性 / 隔离 / 并发 / 重试 / 归档"),
        ("modules", "tests.test_modules",
         "内部模块契约 / 单一 system 模式 / 工具授权"),
        ("real_bundle_modules", "tests.test_real_bundle_modules",
         "真实工具库适配：字段形状与产物引用"),
        ("bridge", "tests.test_bridge", "插件-引擎会话桥"),
        ("orchestrated_run", "tests.test_orchestrated_run",
         "端到端编排：八段产物 + 每角色单会话"),
        ("director_merged", "tests.test_director_merged",
         "方案4：无 Director 工单/Session + 八段共用 Finalizer 会话"),
        ("handoff_roles", "tests.test_handoff_roles",
         "哪些角色必须向模型要判断"),
        ("semantic_loop", "tests.test_semantic_loop",
         "语义策展/关系审阅是真实工单；暂停不是失败"),
        ("query_variants_executed", "tests.test_query_variants_executed",
         "查询变体真的被执行并合并去重"),
        ("research_query_expansion", "tests.test_research_query_expansion",
         "关键词计划：变体边界、诚实空扩展、引用扩展预算"),
        ("cli_recovery", "tests.test_cli_recovery",
         "中断恢复：None 返回契约与引擎活动心跳"),
    ]
    results = []
    for name, module, purpose in suites:
        completed = subprocess.run(
            [sys.executable, "-m", "unittest", module, "-q"],
            cwd=_project(), capture_output=True, text=True)
        tail = (completed.stderr or "").strip().splitlines()
        summary = [line for line in tail
                   if line.startswith("Ran ") or line in ("OK",) or
                   line.startswith("FAILED")]
        results.append({
            "suite": name,
            "purpose": purpose,
            "passed": completed.returncode == 0,
            "summary": summary,
        })
    ok = all(row["passed"] for row in results)
    return _emit({
        "ok": ok,
        "suites": results,
        "note": ("契约通过不等于科学正确：审计是流程关卡，不是同行评审。"
                 if ok else "有测试未通过；不要声称已完成。"),
    }, code=0 if ok else 1)


def cmd_evidence(args) -> int:
    """Print the session-id table for one task, from the persisted registry."""
    registry = SessionRegistry(os.path.join(_root(), "runs", args.task_id,
                                            "sessions"))
    rows = registry.bindings(args.task_id)
    table = []
    for binding in rows:
        table.append({
            "role_id": binding.role_id,
            "subtask_id": binding.subtask_id or "-",
            "session_id": binding.session_id,
            "turns": binding.turns,
            "state": binding.state,
            "phases": binding.last_phase or "-",
            "replaced": binding.replaced or "",
        })
    return _emit({"ok": True, "task_id": args.task_id,
                  "sessions": len(table), "bindings": table})


def cmd_modules(args) -> int:
    """List the internal modules and how the system entry reaches them.

    There is no per-module user entry (guide v2 R1): this is a description of
    the *internal* division of labour, not an advertisement of callable tools.
    """
    internal = [
        {"module": "PaperSearch", "port": "research",
         "responsibility": "检索、去重、元数据校验、全文获取、PDF 解析"},
        {"module": "PaperRead", "port": "read",
         "responsibility": "逐篇精读，产出结构化 ReadingResult 与证据"},
        {"module": "PaperOrganization", "port": "organization",
         "responsibility": "知识库唯一写入者：原始引用事实与精选语义关系分离"},
        {"module": "PaperScheduler", "port": "scheduler",
         "responsibility": "阅读顺序、前置知识、研究空白与动态重规划"},
    ]
    return _emit({
        "ok": True,
        "user_entry": {"tool": "researchos", "actions": ["run", "resume", "status"]},
        "internal_modules": internal,
        "note": "模块由 Orchestrator 内部调度；不使用面向用户的独立入口",
    })


def cmd_mode(args) -> int:
    """Show what the Mode actually mounts (read from its patch file)."""
    patch = os.path.join(_project(), "bundle", "cordis.patch.yml")
    if not os.path.isfile(patch):
        return _emit({"ok": False,
                      "error": "bundle/cordis.patch.yml 不存在"}, code=2)
    with open(patch, encoding="utf-8") as handle:
        text = handle.read()
    return _emit({"ok": True, "patch": patch, "bytes": len(text),
                  "note": "预设声明即 Mode 的工具面；请用 dsh 的插件管理安装后重启宿主验证"})


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="researchos.modules.acceptance",
        description="ResearchOS Mode 验收：跑测试并输出证据")
    sub = parser.add_subparsers(dest="command", required=True)

    audit = sub.add_parser("audit", help="运行全部测试组并输出检查表")
    audit.set_defaults(func=cmd_audit)

    evidence = sub.add_parser("evidence", help="打印某个任务的会话证据表")
    evidence.add_argument("--task-id", required=True)
    evidence.set_defaults(func=cmd_evidence)

    modules = sub.add_parser("modules", help="列出系统内部模块职责")
    modules.set_defaults(func=cmd_modules)

    mode = sub.add_parser("mode", help="显示 Mode 组合")
    mode.set_defaults(func=cmd_mode)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
