"""Read agent: understands exactly one authorised paper.

Responsibilities:

* build the read package for a task and strategy;
* hand the model the PDF plus the authorised context only;
* expose a ``request_context`` tool that goes back through the Scheduler;
* validate the result against the production contract before anything else
  may see it;
* reject a result whose ``paper_id`` does not match the task.

The Reader may run reader subagents (one per paper) so that
multi-paper batches keep isolated context windows.  The parent Reader never
merges their evidence; it returns a list of validated results.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from .base import AgentKit, agent_tool
from ..runtime.agent import Agent
from ..runtime.envelope import blocked, error, failed, ok
from ..runtime.machine import RouterPolicy
from ..runtime.messages import AgentReply, ToolCall


def _attachments(value) -> list:
    """Resolve the PDF for a task or for turn metadata.

    Accepts either a task dict or a metadata dict (``{"initial": {"task": ...}}``)
    so one helper serves both the tool path and the handoff path.
    """
    task = value or {}
    if isinstance(task, dict) and "initial" in task:
        task = (task.get("initial") or {}).get("task") or {}
    material = (task or {}).get("target_material") or {}
    if not material.get("pdf_path"):
        return []
    return [{"kind": "pdf", "path": material["pdf_path"],
             "paper_id": task.get("target_paper")}]


def build_reader_subagent(kit: AgentKit, *, agent_id: str) -> Agent:
    """One paper, one context window, one validated ReadingResult."""

    def read_package(task: dict = None):
        package = kit.ports.read.prepare(task or {})
        if package["ok"]:
            # A re-read is a *targeted* pass: the Scheduler already said what
            # was missing, so the reader gets that list explicitly instead of
            # being asked to read the paper again from scratch.
            package["data"]["task_type"] = (task or {}).get("task_type",
                                                            "first_read")
            package["data"]["reading_focus"] = (task or {}).get(
                "reading_focus") or []
            package["data"]["deficiency"] = (task or {}).get("deficiency") or []
            package["data"]["reread_reason"] = (task or {}).get("reread_reason")
        return package

    registry = kit.registry.with_tools([
        ("prepare_reading_task", "构建单篇论文的阅读包（含授权材料与策略）",
         {"type": "object", "properties": {"task": {"type": "object"}},
          "required": ["task"], "additionalProperties": False},
         read_package),
    ])
    return Agent(role="reader",
                 system_prompt=kit.prompts.get("read.md"),
                 tools=registry,
                 policy=kit.resolve_policy(agent_id, _ReaderRulePolicy()),
                 budget=kit.budget(),
                 id=agent_id)


class _ReaderRulePolicy:
    """Deterministic stand-in: the real reading is done by an LLM policy.

    It deliberately does *not* fabricate a ReadingResult.  Without a model the
    agent returns a contract-shaped "not read" report so the orchestrator's
    pending/awaiting path is exercised as designed.
    """

    name = "reader_rule"

    def respond(self, request):
        initial = dict(request.metadata.get("initial") or {})
        last = request.messages[-1] if request.messages else None
        task = initial.get("task") or {}
        if last is not None and last.role == "tool":
            try:
                value = json.loads(last.content)
            except json.JSONDecodeError:
                value = {}
            return AgentReply(payload={
                "needs_agent": True,
                "reason": "未注入语言模型策略；阅读需要真实模型",
                "task_id": task.get("task_id"),
                "target_paper": task.get("target_paper", ""),
            })
        return AgentReply(text="", tool_calls=[ToolCall(
            "prepare_reading_task", {"task": task})])


def build_read_agent(kit: AgentKit, *, agent_id: str = "read") -> Agent:
    reader = build_reader_subagent(kit, agent_id=agent_id + ".paper")

    def read_one(task: dict = None):
        task = task or {}
        # Two execution modes, one tool:
        #  * rule readers delegate to the reader subagent (isolated context);
        #  * handoff readers answer directly, because the *Read agent itself* is
        #    the external worker.  Delegating in that mode would publish an order
        #    addressed to ``read`` and then have ``read`` try to answer its own
        #    order, which never terminates.
        self_agent = kit.agent_index.get(agent_id)
        handoff = (self_agent is not None
                   and type(self_agent.policy).__name__ == "HandoffPolicy")
        if handoff:
            context = kit.context(agent_id, parent_id="literature")
            outcome = kit.agent_index[agent_id].one_shot(
                context, json.dumps({"task": task}, ensure_ascii=False),
                metadata={"initial": {"task": task},
                          "attachments": _attachments(task)})
            if outcome.stopped_because == "awaiting_external_agent":
                return blocked(error(
                    "E2_AWAITING_EXTERNAL_AGENT",
                    "阅读 {} 需要外部 Agent 回合".format(task.get("target_paper")),
                    hint="宿主 Agent 需处理 handoff outbox 后重新 resume"),
                    data={"task_id": task.get("task_id")})
            payload = outcome.payload
            if not isinstance(payload, dict):
                return failed(error("READ_AGENT_EMPTY", "阅读智能体未返回结果"))
            validated = kit.ports.read.validate(payload, task)
            if not validated["ok"]:
                return validated
            return ok(validated["data"],
                      diagnostics=dict(validated.get("diagnostics") or {}))
        context = kit.context(reader.id, parent_id=agent_id)
        # In handoff mode this agent's policy publishes a work order instead of
        # answering.  The order must tell the external reader *where the paper
        # is*, because a model cannot read a PDF from a task id alone.
        outcome = reader.one_shot(
            context, json.dumps({"task_id": task.get("task_id")},
                                ensure_ascii=False),
            metadata={"initial": {"task": task},
                      "attachments": _attachments(task)})
        if outcome.stopped_because == "awaiting_external_agent":
            # The reader is an external agent and has published a work order.
            # Surfacing this as ``blocked`` keeps the call outstanding (the
            # supervisor must not record a tool result that looks like a
            # reading), and it is what makes the handoff loop terminate.
            return blocked(error(
                "E2_AWAITING_EXTERNAL_AGENT",
                "阅读 {} 需要外部 Agent 回合".format(task.get("target_paper")),
                hint="宿主 Agent 需处理 handoff outbox 后重新 resume"),
                data={"child_agent": reader.id, "task_id": task.get("task_id"),
                      "order": outcome.payload})
        if outcome.payload is None:
            return failed(error("READ_AGENT_EMPTY", "阅读子智能体未返回结果"))
        if isinstance(outcome.payload, dict) and outcome.payload.get("needs_agent"):
            return blocked(error(
                "READ_AGENT_REQUIRED",
                "任务 {} 需要可执行阅读策略".format(task.get("task_id")),
                hint="注入 read.paper 的策略，或用 reading-submit 提交外部结果",
                context={"task_id": task.get("task_id")}),
                data={"task": task, "package": kit.ports.read.prepare(task)})
        validated = kit.ports.read.validate(outcome.payload, task)
        if not validated["ok"]:
            return validated
        # ``validated["data"]`` is the reading itself (port contract), so this
        # is what gets stored and returned -- not the contract report.
        diagnostics = dict(validated.get("diagnostics") or {})
        diagnostics.update({"child_agent": reader.id,
                            "child_trace": outcome.trace_path})
        value = ok(validated["data"], diagnostics=diagnostics)
        if validated.get("status") == "partial":
            value["status"] = "partial"
            value["warnings"] = list(validated.get("warnings") or [])
        return value

    def read_batch(tasks: list = None):
        results, failures = [], []
        for task in tasks or []:
            outcome = read_one(task)
            if not outcome["ok"]:
                failures.append({"task_id": task.get("task_id"),
                                 "error": outcome.get("error")})
                continue
            results.append(outcome["data"])
        if failures:
            return failed(error("READ_BATCH_PARTIAL",
                                "{} 个阅读任务未通过校验".format(len(failures)),
                                hint="修正后重放失败任务",
                                context={"failures": failures}),
                          data={"results": results, "failures": failures})
        return ok({"results": results})

    def describe_material(task: dict = None):
        """Tell an external reader where the paper physically is.

        A handoff reader receives a task id and nothing else; without this the
        model can only guess at a filename.  The path is metadata, not content:
        the host agent has filesystem access and reads the PDF itself.
        """
        return ok({"task_id": (task or {}).get("task_id"),
                   "attachments": _attachments(task or {}),
                   "target_material": (task or {}).get("target_material") or {}})

    registry = kit.registry.with_tools([
        ("describe_material", "返回某任务目标论文的本地材料位置（只读）",
         {"type": "object", "properties": {"task": {"type": "object"}},
          "required": ["task"], "additionalProperties": False},
         describe_material),
        ("read_paper", "阅读一篇论文并返回通过契约校验的 ReadingResult",
         {"type": "object", "properties": {"task": {"type": "object"}},
          "required": ["task"], "additionalProperties": False},
         read_one),
        ("read_batch", "顺序阅读一批任务",
         {"type": "object", "properties": {"tasks": {"type": "array"}},
          "required": ["tasks"], "additionalProperties": False},
         read_batch),
    ])
    kit.register_agent(reader, name="read.delegate_reading")
    kit.register_agent(reader, name="read.read_paper")
    registry.register(agent_tool(
        reader, name="delegate_reading",
        description="把单篇论文交给阅读子智能体（隔离上下文）",
        kit=kit, parent_id=agent_id))
    agent = Agent(role="reader",
                  system_prompt=kit.prompts.get("read.md"),
                  tools=registry,
                  policy=kit.resolve_policy(agent_id, RouterPolicy(_BatchPolicy())),
                  budget=kit.budget(),
                  id=agent_id,
                  attachment_resolver=_attachments)
    return agent



class _BatchPolicy:
    """Default rule policy: turn one invoke into one read call."""

    name = "read_batch_rule"

    def respond(self, request):
        initial = dict(request.metadata.get("initial") or {})
        last = request.messages[-1] if request.messages else None
        if last is not None and last.role == "tool":
            try:
                return AgentReply(payload=json.loads(last.content))
            except json.JSONDecodeError:
                return AgentReply(payload=None)
        tasks = initial.get("tasks") or []
        if len(tasks) == 1:
            return AgentReply(text="", tool_calls=[ToolCall(
                "read_paper", {"task": tasks[0]})])
        return AgentReply(text="", tool_calls=[ToolCall("read_batch",
                                                        {"tasks": tasks})])
