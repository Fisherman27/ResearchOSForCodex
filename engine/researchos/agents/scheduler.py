"""Scheduler agent: reading order and authorised context.

Responsibilities:

* read one consistent Organization snapshot;
* filter candidates (already read / no PDF);
* order them by a deterministic strategy;
* pick the next batch;
* select authorised prerequisite knowledge under a token budget;
* filter that same pool on demand when the Reader asks a question.

The planner is a *subagent*.  Its rule policy is the same
deterministic algorithm; an LLM policy can be dropped in behind the identical
tool boundary, and the parent cannot tell the difference except by trace.
"""
from __future__ import annotations

import json
import os
from typing import Optional

from .base import AgentKit, agent_tool
from ..ports.ideation import build_landscape, prepare_payload, run_ideation, write_agenda
from ..ports.ideation_strategies import build_ideation_messages, list_strategies
from ..ports.sufficiency import assess, summarise
from ..runtime.agent import Agent
from ..runtime.envelope import error, failed, ok, partial
from ..runtime.machine import RouterPolicy
from ..runtime.messages import AgentReply, ToolCall


def build_planner(kit: AgentKit, *, agent_id: str) -> Agent:
    scheduler = kit.ports.scheduler

    def plan_candidates(snapshot: dict = None, strategy: str = "chronological",
                        batch_size: int = 1):
        return scheduler.plan(snapshot or {}, strategy=strategy,
                              batch_size=batch_size)

    registry = kit.registry.with_tools([
        ("plan_candidates", "在候选集内按策略生成有序阅读序列",
         {"type": "object",
          "properties": {"snapshot": {"type": "object"},
                         "strategy": {"type": "string"},
                         "batch_size": {"type": "integer"}},
          "required": ["snapshot"], "additionalProperties": False},
         plan_candidates),
    ])
    return Agent(role="reading_planner",
                 system_prompt=kit.prompts.get("scheduler_planner.md"),
                 tools=registry,
                 policy=kit.resolve_policy(agent_id + ".planner", _PlanPolicy()),
                 budget=kit.budget(turns=4),
                 id=agent_id + ".planner")


class _PlanPolicy:
    name = "planner_rule"

    def respond(self, request):
        initial = dict(request.metadata.get("initial") or {})
        last = request.messages[-1] if request.messages else None
        if last is not None and last.role == "tool":
            try:
                return AgentReply(payload=json.loads(last.content))
            except json.JSONDecodeError:
                return AgentReply(payload=None)
        return AgentReply(text="", tool_calls=[ToolCall(
            "plan_candidates", {"snapshot": initial.get("snapshot") or {},
                                "strategy": initial.get("strategy", "chronological"),
                                "batch_size": int(initial.get("batch_size") or 1)})])


def build_scheduler_agent(kit: AgentKit, *, agent_id: str = "scheduler") -> Agent:
    scheduler = kit.ports.scheduler
    planner = build_planner(kit, agent_id=agent_id)

    def next_reading_tasks(strategy: str = "", batch_size: int = 0,
                           context_strategy: str = "",
                           context_budget_tokens: int = 0,
                           reading_strategy_id: str = "",
                           reread_budget: int = -1,
                           max_rereads_per_paper: int = -1,
                           reread_history: Optional[dict] = None,
                           selected: Optional[list] = None,
                           reasons: Optional[dict] = None,
                           reading_depths: Optional[dict] = None,
                           reader_groups: Optional[list] = None):
        """Build the next batch of ReadingTasks from the Scheduler's decision.

        This tool **does not decide what to read**.  It reports the facts the
        Scheduler needs (candidates, gaps, caps, stalled papers) and, when the
        Scheduler passes ``selected``, turns that decision into concrete
        ReadingTask objects.  Calling it with no ``selected`` returns the
        situation only -- that is the "look before you leap" mode for an agent
        that wants to inspect before deciding.
        """
        snapshot = kit.ports.organization.snapshot()
        if not snapshot["ok"]:
            return snapshot
        data = snapshot["data"]
        by_id = {paper["paper_id"]: paper for paper in data.get("papers", [])}

        assessments = []
        for paper_id, paper in by_id.items():
            if paper.get("reading_depth") in (None, "", "unread"):
                continue
            understanding = kit.ports.organization.paper_understanding(paper_id)
            result = understanding["data"] if understanding["ok"] else {}
            assessments.append(assess(paper_id, result or None))
        summary = summarise(assessments)

        caps = _caps(kit, reread_budget, max_rereads_per_paper)
        state_by_paper = _reread_state(kit, reread_history)
        stalled = _stalled(assessments, state_by_paper)
        situation = _situation(by_id, assessments, summary, state_by_paper,
                               stalled, caps,
                               snapshot_id=data.get("snapshot_id"),
                               edges=_graph_edges(data))

        # No decision supplied: return the facts and let the agent decide.
        if selected is None:
            return ok({"snapshot_id": data.get("snapshot_id"),
                       "situation": situation,
                       "tasks": [], "awaiting_decision": True},
                      warnings=["未提供 selected；已返回当前情况供 Scheduler 决策"])

        chosen = [str(item) for item in selected]
        reasons = dict(reasons or {})
        reading_depths = dict(reading_depths or {})
        group_by_paper = _reader_group_assignments(chosen, reader_groups)
        by_id_known = set(by_id)
        unread = {paper_id for paper_id, paper in by_id.items()
                  if paper.get("reading_depth") in (None, "", "unread")}
        rereadable = {paper_id for paper_id, item in situation["papers"].items()
                      if item["reread_allowed"]}

        tasks, warnings, rejected = [], [], []
        budget = int(batch_size or kit.config.batch_size)
        reread_cap = int(caps.get("reread_budget", 0))
        reread_used = 0
        for paper_id in chosen:
            if len(tasks) >= budget:
                rejected.append({"paper_id": paper_id,
                                 "reason": "超出 batch_size={}".format(budget)})
                continue
            if paper_id not in by_id_known:
                rejected.append({"paper_id": paper_id, "reason": "不在快照中"})
                continue
            is_reread = paper_id in rereadable
            if not is_reread and paper_id not in unread:
                rejected.append({"paper_id": paper_id,
                                 "reason": "已读且没有可重读的缺口"})
                continue
            if is_reread and reread_used >= reread_cap:
                # The cap is enforced here, not only in the rule policy: the
                # prompt promises it, and an external agent can select more
                # re-reads than allowed.  A promise the code does not keep is
                # worse than no promise, because it makes audits wrong.
                rejected.append({
                    "paper_id": paper_id,
                    "reason": "重读数量已达上限 {}".format(reread_cap)})
                continue
            if is_reread:
                reread_used += 1
            item = next((entry for entry in assessments
                         if entry.paper_id == paper_id), None)
            context_env = _context_for(
                kit, scheduler, paper_id, context_strategy or
                kit.config.context_strategy,
                context_budget_tokens or kit.config.context_budget_tokens,
                warnings)
            if context_env is None:
                continue
            task_id = "{}_{}_{}".format(
                "reread" if is_reread else "read",
                (data.get("snapshot_id") or "org").replace("org_", ""),
                len(tasks) + 1)
            task = _build_task(
                by_id[paper_id], {"strategy": strategy or
                                  kit.config.planner_strategy,
                                  "snapshot_id": data.get("snapshot_id"),
                                  "reasons": reasons,
                                  "reading_depth": reading_depths.get(paper_id),
                                  "reader_group": group_by_paper[paper_id]},
                context_env, task_id=task_id,
                reading_strategy_id=reading_strategy_id or
                kit.config.reading_strategy_id)
            task["planner_reason"] = reasons.get(paper_id, "")
            if is_reread and item is not None:
                task.update({
                    "task_type": "reread",
                    "reread_reason": reasons.get(paper_id) or "上轮阅读信息不足",
                    "reading_focus": item.focus,
                    "deficiency": [gap.to_dict() for gap in item.gaps],
                    "reread_index": (state_by_paper.get(paper_id) or {}).get(
                        "count", 0) + 1,
                    "deficiency_signature": [list(pair) for pair in
                                             _gap_signature(item.gaps)],
                })
            else:
                task["task_type"] = "first_read"
            tasks.append(task)

        value = {"snapshot_id": data.get("snapshot_id"), "tasks": tasks,
                 "situation": situation, "selected": chosen,
                 "rejected": rejected}
        if rejected:
            warnings.append("{} 项选择未被采纳：{}".format(
                len(rejected), "; ".join(
                    "{}={}".format(item["paper_id"], item["reason"])
                    for item in rejected)))
        if warnings:
            return partial(value, warnings=warnings)
        return ok(value)

    def _context_for(kit_, scheduler_, paper_id, context_strategy,
                     budget, warnings):
        knowledge = kit_.ports.organization.related_knowledge(paper_id)
        items = knowledge["data"] if knowledge["ok"] else []
        if not knowledge["ok"]:
            warnings.append("{} 的相关知识查询失败".format(paper_id))
        selected = scheduler_.select_context(
            paper_id, items, strategy=context_strategy,
            budget_tokens=budget)
        if not selected["ok"]:
            return None
        warnings.extend(selected.get("warnings") or [])
        return selected["data"]

    def request_context(task: dict = None, question: str = "",
                        budget_tokens: int = 1000):
        task = task or {}
        pool = (task.get("reading_context") or {}).get("knowledge_items") or []
        if not pool:
            knowledge = kit.ports.organization.related_knowledge(
                task.get("target_paper", ""))
            pool = knowledge["data"] if knowledge["ok"] else []
        selected = scheduler.filter_context_for_question(
            pool, question, budget_tokens=budget_tokens)
        if selected["ok"]:
            selected["data"]["task_id"] = task.get("task_id")
            selected["data"]["knowledge_snapshot"] = task.get("knowledge_snapshot")
        return selected

    def get_landscape():
        """Project the evidence package into a problem-centric landscape.

        Read-only and deterministic: it is the *shared input* every ideation
        strategy sees, so an experiment varies the strategy, not the input.
        """
        evidence = kit.ports.organization.export_evidence()
        if not evidence["ok"]:
            return evidence
        return ok(build_landscape(evidence["data"]))

    def propose_research_directions(strategy_id: str = "",
                                    research_goal: str = "",
                                    max_proposals: int = 5,
                                    agent_executor=None):
        """Run one ideation turn with the configured (or given) strategy."""
        chosen = strategy_id or kit.config.ideation_strategy
        evidence = kit.ports.organization.export_evidence()
        if not evidence["ok"]:
            return evidence
        landscape = build_landscape(evidence["data"])
        if not landscape["paper_count"]:
            return failed(error("IDEATION_NO_CORPUS",
                                "语料为空，无法提出研究方向",
                                hint="先完成检索与阅读"))
        payload = prepare_payload(
            landscape, research_goal=research_goal or kit.config.research_goal,
            reading_state=_reading_state(kit))
        executor = agent_executor or _ideation_executor(kit)
        if executor is None:
            built = build_ideation_messages(chosen, payload)
            if not built["ok"]:
                return built
            return ok({"awaiting_ideation_agent": True,
                       "strategy_id": built["data"]["strategy_id"],
                       "messages": built["data"]["messages"],
                       "strategy_prompt_sha256": built["data"][
                           "strategy_prompt_sha256"],
                       "payload": payload},
                      warnings=["未注入 ideation 执行器；已生成待处理提示词"])
        outcome = run_ideation(payload, strategy_id=chosen, executor=executor,
                               landscape=landscape,
                               max_proposals=max_proposals)
        if not outcome["ok"]:
            return outcome
        path = write_agenda(os.path.join(kit.store.root, "ideation"),
                            outcome["data"])
        value = dict(outcome["data"])
        value.update({"agenda_path": path,
                      "landscape_summary": {
                          "papers": landscape["paper_count"],
                          "contradictions": landscape["contradiction_count"],
                          "recurring_limitations": len(
                              landscape["recurring_limitations"])}})
        if outcome["status"] == "partial":
            return partial(value, warnings=outcome.get("warnings"))
        return ok(value)

    def list_ideation_strategies():
        """Enumerate the pluggable ideation mechanisms."""
        return list_strategies()

    registry = kit.registry.with_tools([
        ("next_reading_tasks", "生成下一批 ReadingTask（必要时包含重读任务）",
         {"type": "object",
          "properties": {"strategy": {"type": "string"},
                         "batch_size": {"type": "integer"},
                         "context_strategy": {"type": "string"},
                         "context_budget_tokens": {"type": "integer"},
                         "reading_strategy_id": {"type": "string"},
                         "reread_budget": {"type": "integer"},
                         "max_rereads_per_paper": {"type": "integer"},
                         "reread_history": {"type": "object"},
                         "selected": {"type": "array"},
                         "reasons": {"type": "object"},
                         "reading_depths": {"type": "object"},
                         "reader_groups": {"type": "array"}},
          "additionalProperties": False},
         next_reading_tasks),
        ("request_context", "在任务授权池内按问题筛选补充上下文",
         {"type": "object",
          "properties": {"task": {"type": "object"},
                         "question": {"type": "string"},
                         "budget_tokens": {"type": "integer"}},
          "required": ["task", "question"], "additionalProperties": False},
         request_context),
        ("get_snapshot", "读取当前知识快照",
         {"type": "object", "properties": {}, "additionalProperties": False},
         lambda: kit.ports.organization.snapshot()),
        ("get_reread_ledger", "读取权威的重读计数（防止漏传参数绕过上限）",
         {"type": "object", "properties": {}, "additionalProperties": False},
         lambda: read_reread_ledger(kit)),
        ("get_landscape", "把知识库导出为以研究问题为中心的全景（只读）",
         {"type": "object", "properties": {}, "additionalProperties": False},
         get_landscape),
        ("propose_research_directions", "综合已有研究，发现机会并提出可检验的研究方向",
         {"type": "object",
          "properties": {"strategy_id": {"type": "string"},
                         "research_goal": {"type": "string"},
                         "max_proposals": {"type": "integer"}},
          "additionalProperties": False},
         propose_research_directions),
        ("list_ideation_strategies", "列出现有 ideation 策略及其状态",
         {"type": "object", "properties": {}, "additionalProperties": False},
         list_ideation_strategies),
    ])
    kit.register_agent(planner, name="scheduler.plan_reading_order")
    registry.register(agent_tool(
        planner, name="plan_reading_order",
        description="让规划子智能体决定下一批阅读顺序",
        kit=kit, parent_id=agent_id))
    return Agent(role="scheduler",
                 system_prompt=kit.prompts.get("scheduler.md"),
                 tools=registry,
                 policy=kit.resolve_policy(agent_id, RouterPolicy(_DeferPolicy())),
                 budget=kit.budget(),
                 id=agent_id)


class _DeferPolicy:
    """Default policy: deterministic ranking, or delegate to the planner subagent.

    Two jobs, because the Scheduler is asked two kinds of question:

    * ``decision_turn``: "what should we read next?" -> rank from the situation
      with a transparent rule (gaps first, then unread by year, respect caps);
    * anything else: delegate to the planner subagent, which owns the ordering
      algorithm used by the ``plan_reading_order`` tool.
    """

    name = "defer"

    def respond(self, request):
        if request.metadata.get("decision_turn"):
            return self._decide(request)
        initial = dict(request.metadata.get("initial") or {})
        last = request.messages[-1] if request.messages else None
        if last is not None and last.role == "tool":
            try:
                return AgentReply(payload=json.loads(last.content))
            except json.JSONDecodeError:
                return AgentReply(payload=None)
        return AgentReply(text="", tool_calls=[ToolCall(
            "plan_reading_order", {"strategy": initial.get("strategy", "chronological"),
                                   "batch_size": int(initial.get("batch_size") or 1),
                                   "context_strategy": initial.get("context_strategy", "none"),
                                   "context_budget_tokens": int(
                                       initial.get("context_budget_tokens") or 4000),
                                   "reading_strategy_id": initial.get(
                                       "reading_strategy_id", "S0")})])

    @staticmethod
    def _decide(request):
        """Deterministic ranking: explicit, inspectable, and reproducible.

        Order:
          1. papers that may be re-read (a localised gap is cheaper to close
             than opening a new paper), most severe gap first;
          2. unread papers, oldest first (a prerequisite usually precedes).
        Caps are respected here; the tool rejects anything that slips through.
        """
        # The situation arrives through metadata (the supervisor builds the
        # turn), with the raw message kept as a fallback for hand-written turns.
        payload = dict(request.metadata or {})
        if not payload.get("situation"):
            text = request.messages[-1].content if request.messages else "{}"
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                payload = {}
        situation = payload.get("situation") or {}
        papers = situation.get("papers") or {}
        caps = situation.get("caps") or {}
        batch = int(payload.get("batch_size") or 1)
        reread_budget = int(caps.get("reread_budget", 0))
        rereadable, unread = [], []
        for paper_id, item in papers.items():
            if item.get("reread_allowed"):
                severity = max([gap.get("priority", 1)
                                for gap in item.get("gaps") or []] or [0])
                rereadable.append((severity, paper_id))
            elif item.get("unread") and item.get("pdf_available", True):
                unread.append((item.get("year") or 9999, paper_id))
        rereadable.sort(key=lambda pair: (-pair[0], pair[1]))
        unread.sort(key=lambda pair: (pair[0], pair[1]))
        chosen = [paper_id for _, paper_id in rereadable[:reread_budget]]
        for _, paper_id in unread:
            if len(chosen) >= batch:
                break
            chosen.append(paper_id)
        reasons = {}
        for paper_id in chosen:
            item = papers.get(paper_id) or {}
            if item.get("reread_allowed"):
                reasons[paper_id] = "缺口可定向补齐（{} 处）".format(
                    item.get("gap_count", 0))
            else:
                reasons[paper_id] = "未读，按发表年份排序"
        # The note is written into the conversation, so the next cycle can see
        # what this cycle concluded.  An agent that decides silently cannot
        # revise its own judgement later.
        note = ("本轮判断：{} 篇可重读、{} 篇未读；选择 {}（{}）".format(
            len(rereadable), len(unread), chosen[:batch] or "无",
            "先补已定位的缺口" if rereadable else "按发表年份铺开"))
        return AgentReply(text=note,
                          payload={"selected": chosen[:batch],
                                   "reasons": reasons,
                                   "reading_depths": {
                                       paper_id: ("deep" if
                                                  (papers.get(paper_id) or {}).get(
                                                      "pdf_available", True)
                                                  else "summary")
                                       for paper_id in chosen[:batch]},
                                   "reader_groups": [
                                       {"group_id": paper_id,
                                        "paper_ids": [paper_id],
                                        "mode": "independent",
                                        "reason": "规则策略默认独立阅读"}
                                       for paper_id in chosen[:batch]],
                                   "note": note})


def _build_task(target: dict, plan: dict, context: dict, *, task_id: str,
                reading_strategy_id: str) -> dict:
    paper_id = target["paper_id"]
    group = dict(plan.get("reader_group") or {})
    group_id = str(group.get("group_id") or paper_id)
    return {
        "task_id": task_id,
        "task_schema_version": "1.0",
        "target_paper": paper_id,
        "reader_group_id": group_id,
        "reader_agent_id": "read:{}".format(group_id),
        "reader_group_papers": list(group.get("paper_ids") or [paper_id]),
        "reading_mode": str(group.get("mode") or "independent"),
        "reader_group_reason": str(group.get("reason") or ""),
        "target_material": {
            "paper_id": paper_id,
            "title": target.get("title", ""),
            "pdf_ref": "pdf:{}".format(paper_id),
            "pdf_path": target.get("pdf_path"),
            "document_ref": "organization:{}".format(paper_id),
        },
        "reading_context": {
            "prerequisite_papers": [item.get("source_paper") for item in
                                    context.get("knowledge_items", [])],
            "knowledge_items": context.get("knowledge_items", []),
        },
        "reading_focus": [],
        "planner_strategy": plan.get("strategy", "chronological"),
        "context_strategy": context.get("strategy", "none"),
        "reading_strategy_id": reading_strategy_id,
        "requested_reading_depth": (plan.get("reading_depth") or
                                    ("deep" if target.get("pdf_available") else "summary")),
        "context_budget_tokens": context.get("estimated_tokens", 0),
        "knowledge_snapshot": plan.get("snapshot_id"),
        "planner_reason": (plan.get("reasons") or {}).get(paper_id, ""),
        "scheduler_version": "paperscheduler-0.1.0",
    }


def _reader_group_assignments(selected, reader_groups):
    """Map every selected paper to the Reader group chosen by Scheduler.

    Missing or malformed assignments degrade to one stable group per paper, so
    old Scheduler replies remain valid while new replies may deliberately put
    related or contrasting papers in the same persistent Reader Session.
    """
    selected_set = set(selected)
    assigned = {}
    for raw in reader_groups or []:
        if not isinstance(raw, dict):
            continue
        group_id = str(raw.get("group_id") or "").strip()
        paper_ids = [str(item) for item in raw.get("paper_ids") or []]
        valid = [item for item in paper_ids if item in selected_set]
        if not group_id or not valid:
            continue
        group = {"group_id": group_id, "paper_ids": valid,
                 "mode": str(raw.get("mode") or "comparative"),
                 "reason": str(raw.get("reason") or "")}
        for paper_id in valid:
            if paper_id not in assigned:
                assigned[paper_id] = group
    for paper_id in selected:
        assigned.setdefault(paper_id, {"group_id": paper_id,
                                       "paper_ids": [paper_id],
                                       "mode": "independent", "reason": ""})
    return assigned


def ordered_of(plan: dict):
    return list(plan.get("ordered") or [])


def _gap_signature(gaps):
    """Identity of a gap set, used to detect a re-read that changed nothing."""
    return tuple(sorted((gap.field, gap.reason) for gap in gaps))


def _reading_state(kit_):
    """What the Scheduler knows about how the corpus was read.

    This is the part a standalone idea generator cannot have: which papers were
    re-read, which gaps remain open, and how the reading order evolved.
    """
    import os
    runs = kit_.config.runs_dir
    state = {}
    if os.path.isdir(runs):
        for name in sorted(os.listdir(runs)):
            if not name.endswith("-literature"):
                continue
            path = os.path.join(runs, name, "state.json")
            if os.path.isfile(path):
                with open(path, encoding="utf-8") as handle:
                    state = json.load(handle)
    return {
        "cycle": state.get("cycle"),
        "completed_papers": state.get("completed_paper_ids") or [],
        "reread_counts": state.get("reread_counts") or {},
        "relation_review": (state.get("relation_review") or {}).get("status"),
    }


def _ideation_executor(kit_):
    """Resolve the ideation executor, if a model has been attached."""
    return getattr(kit_, "ideation_executor", None)


def compute_situation(kit_, *, reread_history=None, reread_budget=-1,
                      max_rereads_per_paper=-1) -> dict:
    """Public entry: the facts a Scheduler needs, without a model turn.

    Used both by the Scheduler agent (to answer ``next_reading_tasks``) and by
    the literature supervisor (to seed the decision turn).  One implementation,
    so the agent and the supervisor can never disagree about what it saw.
    """
    snapshot = kit_.ports.organization.snapshot()
    if not snapshot["ok"]:
        return snapshot
    data = snapshot["data"]
    by_id = {paper["paper_id"]: paper for paper in data.get("papers", [])}
    assessments = []
    for paper_id, paper in by_id.items():
        if paper.get("reading_depth") in (None, "", "unread"):
            continue
        understanding = kit_.ports.organization.paper_understanding(paper_id)
        assessments.append(assess(paper_id,
                                  understanding["data"] if understanding["ok"]
                                  else None))
    caps = _caps(kit_, reread_budget, max_rereads_per_paper)
    state_by_paper = _reread_state(kit_, reread_history)
    stalled = _stalled(assessments, state_by_paper)
    return ok(_situation(by_id, assessments, summarise(assessments),
                         state_by_paper, stalled, caps,
                         snapshot_id=data.get("snapshot_id"),
                         edges=_graph_edges(data)))


def _caps(kit_, reread_budget, max_rereads_per_paper):
    """Effective limits: explicit argument wins, otherwise the configuration."""
    return {
        "reread_budget": (int(reread_budget) if reread_budget >= 0
                          else int(kit_.config.reread_budget)),
        "max_rereads_per_paper": (
            int(max_rereads_per_paper) if max_rereads_per_paper >= 0
            else int(kit_.config.max_rereads_per_paper)),
    }


def _reread_state(kit_, history):
    """Merge caller history with the authoritative ledger (never downgrade)."""
    merged = dict(history or {})
    ledger = _ledger(kit_)
    for paper_id, record in (ledger.get("data") or {}).get(
            "reread_counts", {}).items():
        current = dict(merged.get(paper_id) or {})
        if int(record.get("count", 0)) > int(current.get("count", 0)):
            value = dict(record)
            value.update({key: item for key, item in current.items()
                          if item is not None})
            merged[paper_id] = value
    return merged


def _stalled(assessments, state_by_paper):
    """Papers whose re-reads have demonstrably stopped converging."""
    stalled = set()
    for item in assessments:
        record = state_by_paper.get(item.paper_id) or {}
        if int(record.get("count", 0)) <= 0:
            continue
        previous = record.get("signature")
        recorded = (record.get("last_outcome") or {}).get("progress")
        same_gaps = (previous is not None
                     and tuple(tuple(pair) for pair in previous)
                     == _gap_signature(item.gaps))
        if same_gaps or recorded is False:
            stalled.add(item.paper_id)
    return stalled


def _situation(by_id, assessments, summary, state_by_paper, stalled, caps,
               snapshot_id=None, edges=None):
    """The facts a Scheduler needs to decide.  No recommendation is included.

    Every entry answers a question the prompt asks the agent to consider, which
    is why this is a report and not a plan.
    """
    assessment_by_paper = {item.paper_id: item for item in assessments}
    papers = {}
    for paper_id, paper in by_id.items():
        depth = paper.get("reading_depth")
        unread = depth in (None, "", "unread")
        record = state_by_paper.get(paper_id) or {}
        count = int(record.get("count", 0))
        item = assessment_by_paper.get(paper_id)
        gaps = [gap.to_dict() for gap in item.gaps] if item else []
        papers[paper_id] = {
            "title": paper.get("title"),
            "year": paper.get("preprint_year") or paper.get("year"),
            "reading_depth": depth,
            "unread": unread,
            "pdf_available": paper.get("pdf_available") is not False,
            "method_family": paper.get("method_family"),
            "gist": paper.get("gist"),
            "research_problem": paper.get("research_problem"),
            "contribution": paper.get("contribution"),
            "gaps": gaps,
            "gap_count": len(gaps),
            "reread_count": count,
            "reread_remaining": max(0, caps["max_rereads_per_paper"] - count),
            "stalled": paper_id in stalled,
            "reread_allowed": bool(item and item.retryable
                                   and paper_id not in stalled
                                   and count < caps["max_rereads_per_paper"]),
            "terminal_gaps": len(item.terminal_gaps) if item else 0,
        }
    # Adjacency, from the real graph.  The prompt tells the agent that graph
    # relations are a clue about prerequisites; without these edges that
    # instruction cannot be followed without a second tool round trip.
    adjacency = {}
    for src, dst, kind in (edges or []):
        if src in papers and dst in papers:
            adjacency.setdefault(src, []).append(
                {"paper_id": dst, "relation": kind, "direction": "out"})
            adjacency.setdefault(dst, []).append(
                {"paper_id": src, "relation": kind, "direction": "in"})
    for paper_id in papers:
        papers[paper_id]["neighbours"] = adjacency.get(paper_id, [])

    return {
        # The snapshot id comes from the caller that read the snapshot, not from
        # the summary (which is an aggregate over gaps and has no identity).
        "snapshot_id": snapshot_id,
        "caps": caps,
        "papers": papers,
        "counts": {
            "total": len(papers),
            "unread": len([p for p in papers.values() if p["unread"]]),
            "read": len([p for p in papers.values() if not p["unread"]]),
            "insufficient": len([p for p in papers.values()
                                 if p["gaps"]]),
            "rereadable": len([p for p in papers.values()
                               if p["reread_allowed"]]),
            "stalled": len([p for p in papers.values() if p["stalled"]]),
        },
    }


def read_reread_ledger(kit_):
    """Authoritative re-read counts, merged from every literature run."""
    return _ledger(kit_)


def _ledger(kit_):
    runs = kit_.config.runs_dir
    ledger = {}
    if os.path.isdir(runs):
        for name in sorted(os.listdir(runs)):
            if not name.endswith("-literature"):
                continue
            state_path = os.path.join(runs, name, "state.json")
            if not os.path.isfile(state_path):
                continue
            with open(state_path, encoding="utf-8") as handle:
                state = json.load(handle)
            ledger.update(state.get("reread_counts") or {})
    return ok({"reread_counts": ledger, "source": "literature state"})


def _graph_edges(snapshot: dict) -> list:
    """Paper-level edges, normalised to ``(src, dst, kind)``.

    Only *internal* citations are useful here: an external citation points
    outside the corpus, so it cannot inform a reading-order decision.
    """
    edges = []
    for edge in snapshot.get("citations") or []:
        if edge.get("internal") and edge.get("to_paper"):
            edges.append((edge.get("from_paper"), edge["to_paper"], "cites"))
    for edge in snapshot.get("paper_relations") or []:
        if edge.get("src_paper") and edge.get("dst_paper"):
            edges.append((edge["src_paper"], edge["dst_paper"],
                          edge.get("kind") or "related"))
    return edges
