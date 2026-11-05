"""Independent PaperSearch Agent Workflow."""
from __future__ import annotations

import json
import os
import sys
from typing import Callable, Optional


HERE = os.path.abspath(os.path.dirname(__file__))
TOOLS = os.path.abspath(os.path.join(HERE, "..", "ToolsPy"))
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)

import papersearch  # noqa: E402


class SearchWorkflow:
    def __init__(self, output_root: str):
        self.output_root = os.path.abspath(output_root)
        self.prompt_path = os.path.join(HERE, "prompts", "query_planner.md")

    def run(self, research_question: str, *, run_id: str,
            agent_executor: Optional[Callable] = None,
            max_per_source: int = 20, fetch_top: int = 8,
            parse_top: int = 8, expand_top: int = 4):
        if not research_question or not research_question.strip():
            return self._fail("SEARCH_INPUT_MISSING", "research_question 不能为空")
        query, reason = research_question.strip(), "直接使用研究问题"
        if agent_executor is not None:
            try:
                with open(self.prompt_path, encoding="utf-8") as handle:
                    prompt = handle.read()
                proposal = agent_executor([
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": research_question},
                ])
                if isinstance(proposal, str):
                    proposal = json.loads(proposal)
                query = str(proposal.get("query") or "").strip()
                reason = str(proposal.get("reason") or "")
                if not query:
                    raise ValueError("query planner returned empty query")
            except Exception as exc:
                return self._fail("SEARCH_AGENT_OUTPUT_INVALID", str(exc))
        out_dir = os.path.join(self.output_root, run_id)
        os.makedirs(out_dir, exist_ok=True)
        result = papersearch.full_pipeline(
            query, out_dir=out_dir, max_per_source=max_per_source,
            fetch_top=fetch_top, parse_top=parse_top, expand_top=expand_top)
        result.setdefault("diagnostics", {})["query_plan"] = {
            "research_question": research_question, "query": query, "reason": reason}
        if result.get("data") is not None and isinstance(result["data"], dict):
            result["data"]["search_output"] = out_dir
        return result

    @staticmethod
    def _fail(code, message):
        return {"ok": False, "status": "failed", "data": None,
                "error": {"code": code, "message": message, "retryable": False,
                          "hint": "检查研究问题或 Query Agent 的 JSON 输出"},
                "warnings": [], "diagnostics": {}, "meta": {"tool": "SearchWorkflow"}}
