"""Independent PaperRead Agent Workflow."""
from __future__ import annotations

import json
import inspect
import os
import sys
from typing import Callable, Optional


HERE = os.path.abspath(os.path.dirname(__file__))
EXAMPLE = os.path.abspath(os.path.join(HERE, "..", "ExamplePy"))
TOOLS = os.path.join(EXAMPLE, "tools")
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)

from paperread import build_read_messages, validate_reading_result  # noqa: E402


class ReadWorkflow:
    """Builds an isolated read package and validates one model-produced result."""

    def prepare(self, reading_task: dict, *, material: Optional[dict] = None):
        strategy = reading_task.get("reading_strategy_id") or "S0"
        messages_result = build_read_messages(strategy)
        if not messages_result["ok"]:
            return messages_result
        task_payload = {
            "reading_task": reading_task,
            "authorized_material": material or reading_task.get("target_material", {}),
            "required_output_schema": "reading_result.schema.json@2.0-draft",
        }
        messages = list(messages_result["data"]["messages"])
        messages.append({"role": "user", "content": json.dumps(
            task_payload, ensure_ascii=False)})
        return {
            "ok": True, "status": "ok",
            "data": {**messages_result["data"], "messages": messages,
                     "task_id": reading_task.get("task_id"),
                     "target_paper": reading_task.get("target_paper")},
            "error": None, "warnings": messages_result.get("warnings") or [],
            "diagnostics": {}, "meta": {"tool": "ReadWorkflow.prepare"},
        }

    def run(self, reading_task: dict, *, agent_executor: Callable,
            material: Optional[dict] = None, request_context: Optional[Callable] = None):
        package = self.prepare(reading_task, material=material)
        if not package["ok"]:
            return package
        try:
            messages = package["data"]["messages"]
            pdf_path = reading_task.get("target_material", {}).get("pdf_path")
            tools = {}
            if request_context is not None:
                tools["request_context"] = request_context
            output = self._invoke_executor(agent_executor, messages, tools,
                                           [pdf_path] if pdf_path else [])
            if isinstance(output, str):
                output = json.loads(output)
        except Exception as exc:
            return self._fail("R_AGENT_EXECUTION_FAILED", str(exc))
        validation = validate_reading_result(output, mode="production")
        if not validation["ok"]:
            return validation
        if output.get("paper_id") != reading_task.get("target_paper"):
            return self._fail("R_TASK_RESULT_MISMATCH",
                              "ReadingResult.paper_id 与 ReadingTask.target_paper 不一致")
        return {"ok": True, "status": validation["status"], "data": output,
                "error": None, "warnings": validation.get("warnings") or [],
                "diagnostics": {"validation": validation["data"],
                                "strategy_prompt_sha256": package["data"].get(
                                    "strategy_prompt_sha256")},
                "meta": {"tool": "ReadWorkflow.run",
                         "task_id": reading_task.get("task_id")}}

    @staticmethod
    def _invoke_executor(executor, messages, tools, attachments):
        """Support simple callables and richer runtimes without binding a vendor SDK.

        Rich executors may implement ``run(messages=..., tools=..., attachments=...)``
        or accept the same keyword arguments as a callable. Simple ``fn(messages)``
        remains supported for offline tests and manual adapters.
        """
        target = executor.run if hasattr(executor, "run") else executor
        try:
            parameters = inspect.signature(target).parameters
        except (TypeError, ValueError):
            parameters = {}
        accepts_kwargs = any(p.kind == inspect.Parameter.VAR_KEYWORD
                             for p in parameters.values())
        if accepts_kwargs or "tools" in parameters or "attachments" in parameters:
            return target(messages=messages, tools=tools, attachments=attachments)
        return target(messages)

    @staticmethod
    def _fail(code, message):
        return {"ok": False, "status": "failed", "data": None,
                "error": {"code": code, "message": message, "retryable": False,
                          "hint": "检查任务绑定、模型输出 JSON 与生产 Schema"},
                "warnings": [], "diagnostics": {}, "meta": {"tool": "ReadWorkflow"}}
