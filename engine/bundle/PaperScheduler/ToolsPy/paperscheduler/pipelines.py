"""PaperScheduler 流水线。"""
from __future__ import annotations

from typing import List, Optional

from . import _bootstrap
from .elements import build_reading_task, plan_reading, select_context
from .errors import from_exception, make_error
from .result import failed, ok, partial


def next_reading_tasks(kb: str, *, planner_strategy: str = "chronological",
                       context_strategy: str = "none", batch_size: int = 1,
                       context_budget_tokens: int = 4000,
                       reading_strategy_id: str = "S0",
                       completed: Optional[List[str]] = None,
                       task_prefix: str = "read") -> dict:
    """从 Organization 一致快照生成下一批 ReadingTask。"""
    params = {"kb": kb, "planner_strategy": planner_strategy,
              "context_strategy": context_strategy, "batch_size": batch_size}
    try:
        paths, query = _bootstrap.load_organization()
        cfg_result = paths.resolve_kb(kb)
        if not cfg_result.ok:
            return failed(make_error("STATE_ORGANIZATION_UNAVAILABLE",
                                     str(cfg_result.error),
                                     location="pipelines.next_reading_tasks"),
                          tool="next_reading_tasks", params=params)
        cfg = cfg_result.data
        ready = paths.assert_ready(cfg)
        if not ready.ok:
            return failed(make_error("STATE_ORGANIZATION_UNAVAILABLE",
                                     str(ready.error),
                                     location="pipelines.next_reading_tasks"),
                          tool="next_reading_tasks", params=params)
        snap_result = query.get_graph_snapshot(cfg)
        if not snap_result.ok:
            return failed(make_error("STATE_ORGANIZATION_UNAVAILABLE",
                                     str(snap_result.error),
                                     location="pipelines.next_reading_tasks"),
                          tool="next_reading_tasks", params=params)
        snapshot = snap_result.data
        plan_env = plan_reading(snapshot, strategy=planner_strategy,
                                batch_size=batch_size, completed=completed)
        if not plan_env["ok"]:
            return plan_env
        tasks, warnings = [], list(plan_env.get("warnings") or [])
        by_id = {p["paper_id"]: p for p in snapshot["papers"]}
        for index, pid in enumerate(plan_env["data"]["selected"], 1):
            knowledge_result = query.get_related_knowledge(cfg, pid)
            if not knowledge_result.ok:
                knowledge = []
                warnings.append("{} 的相关知识查询失败：{}".format(pid, knowledge_result.error))
            else:
                knowledge = knowledge_result.data
            context_env = select_context(
                pid, knowledge, strategy=context_strategy,
                budget_tokens=context_budget_tokens)
            if not context_env["ok"]:
                return context_env
            warnings.extend(context_env.get("warnings") or [])
            task_id = "{}_{}_{}".format(task_prefix,
                                         snapshot["snapshot_id"].replace("org_", ""),
                                         index)
            task_env = build_reading_task(
                by_id[pid], plan_env["data"], context_env["data"],
                task_id=task_id, reading_strategy_id=reading_strategy_id)
            if not task_env["ok"]:
                return task_env
            tasks.append(task_env["data"])
        data = {"snapshot_id": snapshot["snapshot_id"],
                "plan": plan_env["data"], "tasks": tasks}
        return (partial if warnings else ok)(
            data, tool="next_reading_tasks", params=params, warnings=warnings,
            diagnostics={"task_count": len(tasks)})
    except Exception as exc:
        return failed(from_exception(exc, location="pipelines.next_reading_tasks"),
                      tool="next_reading_tasks", params=params)
