"""Independent PaperScheduler Agent Workflow."""
from __future__ import annotations

import json
import os
import sys
from typing import Callable, Optional


HERE = os.path.abspath(os.path.dirname(__file__))
TOOLS = os.path.abspath(os.path.join(HERE, "..", "ToolsPy"))
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)

import paperscheduler  # noqa: E402
from paperscheduler import _bootstrap  # noqa: E402
from paperscheduler.elements import build_reading_task, select_context  # noqa: E402


class SchedulerWorkflow:
    def __init__(self, kb: str):
        self.kb = os.path.abspath(kb)
        self.prompt_path = os.path.join(HERE, "prompts", "reading_planner.md")

    def create_tasks(self, *, planner_strategy="chronological",
                     context_strategy="none", batch_size=1,
                     context_budget_tokens=4000, reading_strategy_id="S0",
                     research_goal="", agent_executor: Optional[Callable] = None):
        if planner_strategy != "agent":
            return paperscheduler.next_reading_tasks(
                self.kb, planner_strategy=planner_strategy,
                context_strategy=context_strategy, batch_size=batch_size,
                context_budget_tokens=context_budget_tokens,
                reading_strategy_id=reading_strategy_id)
        if agent_executor is None:
            return self._fail("S_AGENT_MISSING", "agent 策略需要 agent_executor")
        return self._agent_tasks(
            agent_executor, context_strategy=context_strategy,
            batch_size=batch_size, context_budget_tokens=context_budget_tokens,
            reading_strategy_id=reading_strategy_id, research_goal=research_goal)

    def request_context(self, task: dict, question: str, *, budget_tokens=1000):
        """Only searches Organization; selection remains inside the task snapshot boundary."""
        paths, org_query = _bootstrap.load_organization()
        cfg = paths.resolve_kb(self.kb).data
        candidates = org_query.get_related_knowledge(cfg, task["target_paper"])
        if not candidates.ok:
            return self._fail("S_CONTEXT_QUERY_FAILED", str(candidates.error))
        selected = select_context(task["target_paper"], candidates.data,
                                  strategy=task.get("context_strategy", "structured"),
                                  budget_tokens=budget_tokens)
        if selected["ok"]:
            selected["data"]["question"] = question
            selected["data"]["task_id"] = task["task_id"]
            selected["data"]["knowledge_snapshot"] = task.get("knowledge_snapshot")
        return selected

    def _agent_tasks(self, executor, **options):
        paths, org_query = _bootstrap.load_organization()
        cfg_result = paths.resolve_kb(self.kb)
        if not cfg_result.ok:
            return self._fail("S_ORGANIZATION_UNAVAILABLE", str(cfg_result.error))
        snapshot_result = org_query.get_graph_snapshot(cfg_result.data)
        if not snapshot_result.ok:
            return self._fail("S_ORGANIZATION_UNAVAILABLE", str(snapshot_result.error))
        snapshot = snapshot_result.data
        candidates = [p for p in snapshot["papers"]
                      if p.get("reading_depth") in (None, "", "unread")
                      and p.get("pdf_available") is not False]
        if not candidates:
            missing = [p["paper_id"] for p in snapshot["papers"]
                       if p.get("reading_depth") in (None, "", "unread")
                       and p.get("pdf_available") is False]
            return {"ok": True, "status": "empty",
                    "data": {"snapshot_id": snapshot["snapshot_id"],
                             "plan": {"selected": [], "ordered": [],
                                      "unavailable_material": missing}, "tasks": []},
                    "error": None,
                    "warnings": ["未读论文均缺少本地 PDF"] if missing else [],
                    "diagnostics": {}, "meta": {"tool": "SchedulerWorkflow"}}
        payload = {"snapshot": snapshot, "candidates": candidates,
                   "batch_size": options["batch_size"],
                   "research_goal": options["research_goal"]}
        try:
            with open(self.prompt_path, encoding="utf-8") as handle:
                prompt = handle.read()
            proposal = executor([
                {"role": "system", "content": prompt},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ])
            if isinstance(proposal, str):
                proposal = json.loads(proposal)
            selected = proposal.get("selected") or []
        except Exception as exc:
            return self._fail("S_AGENT_OUTPUT_INVALID", str(exc))
        allowed = {p["paper_id"] for p in candidates}
        if (not selected or len(selected) > options["batch_size"] or
                any(pid not in allowed for pid in selected)):
            return self._fail("S_AGENT_OUTPUT_INVALID",
                              "selected 必须是候选集内的非空列表且不超过 batch_size")
        by_id = {p["paper_id"]: p for p in candidates}
        plan = {"snapshot_id": snapshot["snapshot_id"], "strategy": "agent",
                "selected": selected, "ordered": selected,
                "reasons": proposal.get("reasons") or {}}
        tasks, warnings = [], []
        for index, pid in enumerate(selected, 1):
            knowledge = org_query.get_related_knowledge(cfg_result.data, pid)
            context = select_context(pid, knowledge.data if knowledge.ok else [],
                                     strategy=options["context_strategy"],
                                     budget_tokens=options["context_budget_tokens"])
            if not context["ok"]:
                return context
            task = build_reading_task(
                by_id[pid], plan, context["data"],
                task_id="agent_{}_{}".format(snapshot["snapshot_id"][4:], index),
                reading_strategy_id=options["reading_strategy_id"],
                reading_focus=(proposal.get("reading_focus") or {}).get(pid, []))
            if not task["ok"]:
                return task
            tasks.append(task["data"])
            warnings.extend(context.get("warnings") or [])
        return {"ok": True, "status": "partial" if warnings else "ok",
                "data": {"snapshot_id": snapshot["snapshot_id"],
                         "plan": plan, "tasks": tasks}, "error": None,
                "warnings": warnings, "diagnostics": {}, "meta": {"tool": "SchedulerWorkflow"}}

    @staticmethod
    def _fail(code, message):
        return {"ok": False, "status": "failed", "data": None,
                "error": {"code": code, "message": message, "retryable": False,
                          "hint": "检查 Agent 输出或模块前置状态"},
                "warnings": [], "diagnostics": {}, "meta": {"tool": "SchedulerWorkflow"}}
