"""Host guide: what a work order is asking for, in the host agent's terms.

The engine's order format is deliberately minimal: role, prompt, history, tool
schemas, reply contract.  A fresh DSH subagent can answer from that alone, but
a *human* or a coordinator benefits from a short classification:

    kind      which decision this is (phase advance, reading decision, ...)
    title     one line to show in a list
    guidance  what a correct answer looks like for this kind
    suggested one concrete reply shape, when there is an obvious one

This module is presentation only.  It never decides anything: every rule below
is derived from fields the engine already wrote (agent id, requested tool, the
question payload, the tool grant), and the engine still validates the reply.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional


PHASE_HINTS = {
    "research": "Research 阶段：先确认检索产物，再交给文献闭环。",
    "literature": "Literature 阶段：策划阅读、执行阅读、写回知识库。",
    "finalization": "Finalization 阶段：八个终段依次产出并通过契约校验。",
    "completed": "run 已完成。",
}

TOOL_HINTS = {
    "start_research": "初始化研究并启动 Research 阶段；search_options 可复用已有语料。",
    "advance_phase": "推进一个阶段；完成后按返回状态决定下一步。",
    "run_to_completion": "连续推进；遇到外部回合或关卡会停下。",
    "get_director_state": "只读；用于决定下一步，本身不推进流程。",
    "submit_final_result": "提交某终段的 payload，必须满足该阶段契约。",
    "next_reading_tasks": "把 selected + reasons 装配成 ReadingTask；不带 selected 只返回现状。",
    "propose_research_directions": "一次性的 ideation 问答，按 payload 返回方向、机会与拒绝项。",
    "read_paper": "阅读指定任务的目标论文，返回通过生产契约校验的 ReadingResult。",
    "read_batch": "顺序阅读一批任务。",
    "describe_material": "只读；返回目标论文的本地材料位置。",
    "delegate_reading": "把单篇论文交给隔离上下文的阅读子智能体。",
    "import_search_output": "导入检索语料。",
    "apply_reading_results": "写回已通过契约的阅读结果。",
    "export_evidence": "导出只读证据包。",
    "review_claim_relations": "全语料论点关系审阅（强制关卡）。",
}


def _question(order: Dict[str, Any]) -> Dict[str, Any]:
    raw = order.get("question") or ""
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _tool(order: Dict[str, Any]) -> str:
    return str((order.get("metadata") or {}).get("tool") or "")


def _infer(order: Dict[str, Any]) -> str:
    """The requested tool, or the closest thing the question implies."""
    tool = _tool(order)
    if tool:
        return tool
    question = _question(order)
    turn = str(question.get("turn") or "")
    if turn == "reading_decision":
        return "next_reading_tasks"
    agent = str(order.get("agent_id") or "")
    if agent == "director":
        return "advance_phase"
    if agent.split(".")[0] == "read":
        return "read_paper"
    return ""


def classify(order: Dict[str, Any]) -> str:
    """Coarse kind, used for grouping and for the coordinator's loop."""
    tool = _infer(order)
    agent = str(order.get("agent_id") or "")
    if tool == "propose_research_directions":
        return "ideation"
    if agent == "director":
        return "phase"
    if agent.split(".")[0] == "scheduler":
        return "reading_decision"
    if agent.split(".")[0] == "read":
        return "reading"
    if agent.split(".")[0] == "organization":
        return "knowledge"
    return "think"


def title(order: Dict[str, Any]) -> str:
    kind = classify(order)
    agent = str(order.get("agent_id") or "")
    question = _question(order)
    if kind == "ideation":
        return "Scheduler: 提出研究方向（一次性 ideation）"
    if kind == "reading_decision":
        cycle = question.get("cycle")
        counts = (question.get("situation") or {}).get("counts") or {}
        return "Scheduler: 第 {} 轮阅读决策（未读 {} / 共 {}）".format(
            cycle, counts.get("unread", "?"), counts.get("total", "?"))
    if kind == "reading":
        task = (order.get("metadata") or {}).get("task") or \
            question.get("task") or {}
        if isinstance(task, str):
            # A delegated sub-agent (`read.paper`) is asked in prose, not JSON:
            # its `task` is the instruction itself. Assuming a mapping here
            # raised AttributeError, and because `pending`/`step` both route
            # through `_orders() -> describe()`, one prose order took the whole
            # CLI down — the run became unreadable and therefore unrepairable.
            headline = task.strip().splitlines()[0][:80] if task.strip() else ""
            return "Read: {}".format(headline or "一篇论文")
        if not isinstance(task, dict):
            task = {}
        return "Read: 阅读 {}".format(task.get("target_paper") or "一篇论文")
    if kind == "phase":
        phase = question.get("phase") or "?"
        stage = question.get("current_stage")
        return "Director: 推进 {} 阶段{}".format(
            phase, "（{}）".format(stage) if stage else "")
    return "{}: {}".format(agent, _infer(order) or "决策")


def guidance(order: Dict[str, Any]) -> List[str]:
    """Compact routing help; the order already carries the full role contract.

    Codex children receive prompt, messages, tools and attachments directly.
    Repeating their entire protocol here wastes the Lead context and creates a
    second, easily stale source of instructions.
    """
    kind = classify(order)
    tool = _infer(order)
    lines = [TOOL_HINTS.get(tool, "按工单内的 prompt、messages 与契约作答。")]
    if kind == "reading":
        attachments = [str(item.get("path")) for item in
                       (order.get("attachments") or []) if item.get("path")]
        if attachments:
            lines.append("材料：" + ", ".join(attachments))
    lines.append("只返回一个 JSON：tool_calls 或 payload。")
    return lines


def suggested(order: Dict[str, Any],
              state: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """A concrete reply when the situation leaves no real choice.

    Only used for orders whose answer is mechanically implied by the *current*
    engine state -- it is a convenience for a host, never a substitute for the
    model's judgement on decisions.

    ``state`` is the live run state, which the caller obtains because the order's
    own ``question`` is a snapshot taken when the turn began; by the time an
    order is published the engine may already have advanced past it (a director
    turn that started the research phase and then paused in literature is the
    common case).  Without ``state`` only the question is available, and the
    answer is deliberately conservative.
    """
    kind = classify(order)
    live = dict(state or {})
    snapshot = _question(order)
    phase = live.get("phase") or snapshot.get("phase")
    corpus = live.get("research_output")
    if corpus is None:
        corpus = snapshot.get("research_output")
    if kind == "phase":
        # Starting the research phase is never mechanical: the host chooses the
        # corpus (live retrieval, an existing search output, or a paper list).
        # Suggesting it would let a caller loop on "start" without ever deciding.
        if not corpus:
            return None
        # Once a corpus exists, advancing is deterministic orchestration: the
        # engine picks the next transition from its own state, and `reply`
        # revalidates the request.
        if live.get("status") == "awaiting_stage_agent":
            return None
        return {"tool_calls": [{"name": "advance_phase", "arguments": {}}]}
    return None


def describe(order: Dict[str, Any],
             state: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Everything a host needs to route one order.

    Never raises. This module is presentation only, but `cli.py pending` and
    `cli.py step` both call it while listing orders — so an unexpected field
    shape here does not merely render a bad label, it takes the control path
    down and leaves the run unreadable and unrepairable. Each field degrades
    on its own instead.
    """
    def safe(fn, fallback):
        try:
            return fn()
        except Exception:  # noqa: BLE001 - presentation must never fail a run
            return fallback

    return {
        "kind": safe(lambda: classify(order), "unknown"),
        "title": safe(lambda: title(order), "工单（无法识别其标题）"),
        "requested_tool": safe(lambda: _infer(order), ""),
        "guidance": safe(lambda: guidance(order), []),
        "suggested_reply": safe(lambda: suggested(order, state), None),
    }
