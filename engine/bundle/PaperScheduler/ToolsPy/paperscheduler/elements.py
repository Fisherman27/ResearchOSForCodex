"""PaperScheduler 原子能力：规划、上下文选择与任务构建。"""
from __future__ import annotations

import re
from collections import defaultdict
from typing import Dict, Iterable, List, Optional, Tuple

from .errors import from_exception, make_error
from .result import empty, failed, ok, partial


PLANNER_STRATEGIES = ("chronological", "citation_based", "dependency_based")
CONTEXT_STRATEGIES = ("none", "summary", "structured")


def _year(paper: dict) -> int:
    return int(paper.get("preprint_year") or paper.get("year") or 9999)


def _is_read(paper: dict) -> bool:
    return paper.get("reading_depth") not in (None, "", "unread")


def _topological_order(nodes: List[dict], edges: Iterable[Tuple[str, str]]) -> Tuple[List[str], List[str]]:
    """Return order and cyclic leftovers. Edges are prerequisite -> successor."""
    by_id = {p["paper_id"]: p for p in nodes}
    outgoing = defaultdict(set)
    indegree = {pid: 0 for pid in by_id}
    for src, dst in edges:
        if src not in by_id or dst not in by_id or src == dst:
            continue
        if dst not in outgoing[src]:
            outgoing[src].add(dst)
            indegree[dst] += 1
    ready = sorted((pid for pid, deg in indegree.items() if deg == 0),
                   key=lambda pid: (_year(by_id[pid]), pid))
    ordered = []
    while ready:
        current = ready.pop(0)
        ordered.append(current)
        for nxt in sorted(outgoing[current]):
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                ready.append(nxt)
                ready.sort(key=lambda pid: (_year(by_id[pid]), pid))
    cyclic = sorted((pid for pid in by_id if pid not in ordered),
                    key=lambda pid: (_year(by_id[pid]), pid))
    return ordered + cyclic, cyclic


def plan_reading(snapshot: dict, *, strategy: str = "chronological",
                 batch_size: int = 1, completed: Optional[List[str]] = None,
                 require_material: bool = True) -> dict:
    """从一个 Organization 快照生成有解释的下一批阅读候选。"""
    params = {"strategy": strategy, "batch_size": batch_size,
              "require_material": require_material}
    if strategy not in PLANNER_STRATEGIES:
        return failed(make_error("INPUT_INVALID", "未知规划策略：{}".format(strategy),
                                 location="elements.plan_reading"),
                      tool="plan_reading", params=params)
    if not isinstance(batch_size, int) or batch_size < 1:
        return failed(make_error("INPUT_INVALID", "batch_size 必须是正整数",
                                 location="elements.plan_reading"),
                      tool="plan_reading", params=params)
    papers = snapshot.get("papers") if isinstance(snapshot, dict) else None
    if not isinstance(papers, list):
        return failed(make_error("FORMAT_SNAPSHOT", "快照缺少 papers 列表",
                                 location="elements.plan_reading"),
                      tool="plan_reading", params=params)
    if not papers:
        return empty({"ordered": [], "selected": []}, tool="plan_reading",
                     params=params, warnings=["论文图为空"])

    done = set(completed or [])
    done.update(p["paper_id"] for p in papers if _is_read(p))
    unavailable = [p["paper_id"] for p in papers if p.get("paper_id") and
                   p["paper_id"] not in done and p.get("pdf_available") is False]
    candidates = [p for p in papers if p.get("paper_id") and p["paper_id"] not in done
                  and (not require_material or p.get("pdf_available") is not False)]
    if not candidates:
        return empty({"ordered": [], "selected": [], "completed": sorted(done),
                      "unavailable_material": unavailable},
                     tool="plan_reading", params=params,
                     warnings=(["没有材料可用的未读候选；{} 篇论文缺少本地 PDF".format(
                         len(unavailable))] if unavailable else ["当前快照没有未完成候选"]))

    reasons = {}
    cyclic = []
    if strategy == "chronological":
        ordered = [p["paper_id"] for p in sorted(candidates,
                   key=lambda p: (_year(p), p["paper_id"]))]
        reasons = {pid: "按首次公开时间排序；缺失时间时置后" for pid in ordered}
    else:
        edges = []
        if strategy == "citation_based":
            # citing -> cited in storage; cited should be read first.
            for edge in snapshot.get("citations") or []:
                if edge.get("internal") and edge.get("to_paper"):
                    edges.append((edge["to_paper"], edge.get("from_paper")))
            reason = "原始引用边反向为被引论文先读；同层按时间排序"
        else:
            # Organization contract: src=newer/extension, dst=prerequisite.
            for edge in snapshot.get("paper_relations") or []:
                if edge.get("src_paper") and edge.get("dst_paper"):
                    edges.append((edge["dst_paper"], edge["src_paper"]))
            reason = "语义关系按前置论文→后继论文拓扑排序；同层按时间排序"
        ordered, cyclic = _topological_order(candidates, edges)
        reasons = {pid: reason for pid in ordered}
        for pid in cyclic:
            reasons[pid] += "；检测到依赖环，已稳定降级为时间顺序"

    selected = ordered[:batch_size]
    data = {
        "snapshot_id": snapshot.get("snapshot_id"),
        "strategy": strategy,
        "ordered": ordered,
        "selected": selected,
        "reasons": {pid: reasons[pid] for pid in selected},
        "completed": sorted(done),
        "remaining_after_batch": max(0, len(ordered) - len(selected)),
        "unavailable_material": unavailable,
    }
    if cyclic:
        return partial(data, tool="plan_reading", params=params,
                       warnings=["依赖图包含环：{}".format(cyclic)],
                       diagnostics={"cycle_nodes": cyclic})
    warnings = (["{} 篇未读论文缺少本地 PDF，未进入本轮候选".format(len(unavailable))]
                if unavailable and require_material else [])
    return (partial if warnings else ok)(data, tool="plan_reading", params=params,
                                         warnings=warnings,
                                         diagnostics={"unavailable_material": unavailable})


def _estimate_tokens(value: str) -> int:
    return max(1, (len(value or "") + 3) // 4)


def select_context(target_paper: str, knowledge_candidates: List[dict], *,
                   strategy: str = "none", budget_tokens: int = 4000) -> dict:
    """从 Organization 提供的已读知识候选中选择上下文。"""
    params = {"target_paper": target_paper, "strategy": strategy,
              "budget_tokens": budget_tokens}
    if strategy not in CONTEXT_STRATEGIES:
        return failed(make_error("INPUT_INVALID", "未知上下文策略：{}".format(strategy),
                                 location="elements.select_context"),
                      tool="select_context", params=params)
    if not isinstance(budget_tokens, int) or budget_tokens < 0:
        return failed(make_error("INPUT_INVALID", "budget_tokens 必须是非负整数",
                                 location="elements.select_context"),
                      tool="select_context", params=params)
    if strategy == "none" or budget_tokens == 0:
        return ok({"knowledge_items": [], "estimated_tokens": 0,
                   "strategy": strategy}, tool="select_context", params=params)

    items = []
    used = 0
    omitted = 0
    for paper in knowledge_candidates or []:
        if strategy == "summary":
            content = "\n".join(x for x in (
                paper.get("gist"), paper.get("contribution")) if x)
            refs = []
            level = "L1"
        else:
            content_parts = [x for x in (
                paper.get("gist"), paper.get("research_problem"),
                paper.get("contribution")) if x]
            refs = []
            for claim in paper.get("claims") or []:
                if claim.get("text"):
                    content_parts.append("Claim: " + claim["text"])
                    refs.append(claim.get("claim_id"))
            content = "\n".join(content_parts)
            level = "L2"
        if not content:
            omitted += 1
            continue
        cost = _estimate_tokens(content)
        if used + cost > budget_tokens:
            omitted += 1
            continue
        items.append({
            "source_paper": paper.get("paper_id"),
            "item_refs": [r for r in refs if r],
            "content": content,
            "knowledge_level": level,
            "source_type": "paper_assertion",
            "verification_status": "not_checked",
            "selection_reason": "与目标论文在 Organization 图中相邻且已有阅读结果",
            "estimated_tokens": cost,
        })
        used += cost
    status_fn = partial if omitted else ok
    return status_fn({"knowledge_items": items, "estimated_tokens": used,
                      "strategy": strategy}, tool="select_context", params=params,
                     warnings=(["{} 个候选因无内容或预算不足未纳入".format(omitted)]
                               if omitted else []),
                     diagnostics={"omitted": omitted})


def build_reading_task(target: dict, plan: dict, context: dict, *,
                       task_id: str, reading_strategy_id: str = "S0",
                       reading_focus: Optional[List[str]] = None) -> dict:
    """把一次规划决定和上下文组装成稳定 ReadingTask。"""
    if not task_id or not target.get("paper_id"):
        return failed(make_error("INPUT_MISSING", "task_id 和 target.paper_id 必填",
                                 location="elements.build_reading_task"),
                      tool="build_reading_task")
    pid = target["paper_id"]
    data = {
        "task_id": task_id,
        "task_schema_version": "1.0",
        "target_paper": pid,
        "target_material": {
            "paper_id": pid,
            "title": target.get("title", ""),
            "pdf_ref": "pdf:{}".format(pid),
            "pdf_path": target.get("pdf_path"),
            "document_ref": "organization:{}".format(pid),
        },
        "reading_context": {
            "prerequisite_papers": [x.get("source_paper") for x in
                                    context.get("knowledge_items", [])],
            "knowledge_items": context.get("knowledge_items", []),
        },
        "reading_focus": reading_focus or [],
        "planner_strategy": plan.get("strategy", "chronological"),
        "context_strategy": context.get("strategy", "none"),
        "reading_strategy_id": reading_strategy_id,
        "context_budget_tokens": context.get("estimated_tokens", 0),
        "knowledge_snapshot": plan.get("snapshot_id"),
        "planner_reason": (plan.get("reasons") or {}).get(pid, ""),
        "scheduler_version": "paperscheduler-0.1.0",
    }
    return ok(data, tool="build_reading_task")


def filter_context_for_question(knowledge_items: List[dict], question: str, *,
                                budget_tokens: int = 1000) -> dict:
    """在同一任务的已授权知识池内处理 Reader 的增量上下文请求。"""
    terms = {x.lower() for x in re.findall(r"[\w\u4e00-\u9fff]{2,}", question or "")}
    ranked = sorted(knowledge_items or [], key=lambda item: sum(
        term in (item.get("content") or "").lower() for term in terms), reverse=True)
    chosen, used = [], 0
    for item in ranked:
        cost = int(item.get("estimated_tokens") or _estimate_tokens(item.get("content", "")))
        if used + cost <= budget_tokens:
            chosen.append(item)
            used += cost
    return ok({"knowledge_items": chosen, "estimated_tokens": used,
               "question": question}, tool="filter_context_for_question")
