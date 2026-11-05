"""Independent PaperOrganization Agent Workflow."""
from __future__ import annotations

import json
import os
import sys
from typing import Callable, Iterable, Optional


HERE = os.path.abspath(os.path.dirname(__file__))
TOOLS = os.path.abspath(os.path.join(HERE, "..", "ToolsPy"))
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)

from paperorganization import paths, pipelines, query, semantic, store, visualization  # noqa: E402


def _result(ok, status, data=None, error=None, warnings=None):
    return {"ok": ok, "status": status, "data": data, "error": error,
            "warnings": warnings or []}


class OrganizationWorkflow:
    """Coordinates deterministic storage tools and a replaceable curation Agent."""

    def __init__(self, kb: str, *, visualization_dir: Optional[str] = None):
        cfg_result = paths.resolve_kb(kb)
        if not cfg_result.ok:
            raise ValueError(str(cfg_result.error))
        self.cfg = cfg_result.data
        self.visualization_dir = visualization_dir
        self.prompt_path = os.path.join(HERE, "prompts", "index_curation.md")
        self.claim_relation_prompt_path = os.path.join(
            HERE, "prompts", "claim_relation_review.md")

    def initialize(self, search_output: str, *, agent_executor: Optional[Callable] = None):
        imported = pipelines.run_import_pipeline(search_output, kb=self.cfg.kb_dir)
        if not imported.ok:
            return _result(False, "failed", error=imported.error.to_dict(),
                           warnings=imported.warnings)
        return self._refresh(trigger="search_import", agent_executor=agent_executor,
                             extra={"import": imported.data})

    def apply_reading_results(self, results: Iterable[dict], *,
                              agent_executor: Optional[Callable] = None):
        stored = pipelines.store_reading_results(list(results), self.cfg)
        if not stored.ok:
            return _result(False, "failed", data=stored.data,
                           error=stored.error.to_dict() if stored.error else None,
                           warnings=stored.warnings)
        if stored.status == "partial":
            return _result(False, "failed", data=stored.data,
                           error={"code": "O_PARTIAL_WRITE",
                                  "message": "部分 ReadingResult 未写入，拒绝推进整批任务",
                                  "hint": "检查 data.fail，修正后重放本批任务"},
                           warnings=stored.warnings)
        return self._refresh(trigger="reading_results_written",
                             agent_executor=agent_executor,
                             extra={"store": stored.data})

    def get_research_evidence(self):
        """Export a read-only evidence package through Organization's public API.

        Downstream synthesis must not inspect ``raw/`` or the SQLite schema directly.
        This method keeps storage ownership in Organization while exposing the graph,
        per-paper overview, and validated reading understanding needed by later stages.
        """
        snapshot_result = query.get_graph_snapshot(self.cfg)
        if not snapshot_result.ok:
            return _result(False, "failed", error=snapshot_result.error.to_dict(),
                           warnings=snapshot_result.warnings)
        snapshot = snapshot_result.data
        papers = []
        warnings = list(snapshot_result.warnings)
        for item in snapshot.get("papers", []):
            paper_id = item["paper_id"]
            overview = query.get_paper_overview(self.cfg, paper_id)
            if not overview.ok:
                return _result(False, "failed", error=overview.error.to_dict(),
                               warnings=warnings + overview.warnings)
            understanding = query.get_paper_understanding(self.cfg, paper_id)
            if not understanding.ok:
                return _result(False, "failed", error=understanding.error.to_dict(),
                               warnings=warnings + understanding.warnings)
            papers.append({"overview": overview.data,
                           "understanding": understanding.data})
            warnings.extend(understanding.warnings)
        data = {
            "snapshot_id": snapshot["snapshot_id"],
            "papers": papers,
            "citations": snapshot.get("citations", []),
            "paper_relations": snapshot.get("paper_relations", []),
            "claims": snapshot.get("claims", []),
            "claim_relations": snapshot.get("claim_relations", []),
        }
        return _result(True, "partial" if warnings else "ok", data=data,
                       warnings=warnings)

    def prepare_claim_relation_review(self):
        """Build the mandatory corpus-level claim-relation review package.

        An empty relation list is a valid reviewed result.  The workflow gate
        distinguishes that from never performing the review.
        """
        evidence = self.get_research_evidence()
        if not evidence["ok"]:
            return evidence
        with open(self.claim_relation_prompt_path, encoding="utf-8") as handle:
            instructions = handle.read()
        return _result(True, "ok", data={
            "task_type": "claim_relation_review",
            "snapshot_id": evidence["data"]["snapshot_id"],
            "instructions": instructions,
            "claims": evidence["data"].get("claims", []),
            "existing_relations": evidence["data"].get("claim_relations", []),
            "papers": evidence["data"].get("papers", []),
        }, warnings=evidence.get("warnings"))

    def review_claim_relations(self, *, agent_executor: Callable):
        prepared = self.prepare_claim_relation_review()
        if not prepared["ok"]:
            return prepared
        try:
            output = agent_executor([
                {"role": "system", "content": prepared["data"]["instructions"]},
                {"role": "user", "content": json.dumps(
                    {key: value for key, value in prepared["data"].items()
                     if key != "instructions"}, ensure_ascii=False)},
            ])
            if isinstance(output, str):
                output = json.loads(output)
        except Exception as exc:
            return _result(False, "failed", error={
                "code": "O_CLAIM_RELATION_REVIEW_INVALID", "message": str(exc),
                "hint": "返回含 relations 数组的 JSON 对象；确认无关系时返回空数组"})
        return self.apply_claim_relation_review(output)

    def apply_claim_relation_review(self, review: dict):
        """Validate and store a completed corpus-level relation review."""
        if not isinstance(review, dict) or not isinstance(review.get("relations"), list):
            return _result(False, "failed", error={
                "code": "O_CLAIM_RELATION_REVIEW_INVALID",
                "message": "关系审阅结果必须是含 relations 数组的 JSON 对象",
                "hint": "即使确认没有关系，也要显式提交 {\"relations\": []}"})
        if not str(review.get("review_notes") or "").strip():
            return _result(False, "failed", error={
                "code": "O_CLAIM_RELATION_REVIEW_INVALID",
                "message": "关系审阅结果缺少 review_notes",
                "hint": "说明比较了哪些论点；零条关系时说明为何没有可辩护的边"})
        relations = review["relations"]
        generated_by = str(review.get("generated_by") or "organization_claim_relation_review")
        stored = store.store_claim_relations("", relations, self.cfg,
                                             generated_by=generated_by)
        if not stored.ok:
            return _result(False, "failed", error=stored.error.to_dict(),
                           warnings=stored.warnings)
        refreshed = self._refresh(trigger="claim_relation_reviewed",
                                  agent_executor=None,
                                  extra={"relation_review": {
                                      "reviewed": True,
                                      "relations_submitted": len(relations),
                                      "review_notes": str(review.get("review_notes") or ""),
                                      "generated_by": generated_by,
                                  }})
        if refreshed["ok"]:
            refreshed["warnings"] = [w for w in refreshed.get("warnings", [])
                                     if "未提供语义索引 Agent" not in w]
            refreshed["status"] = "partial" if refreshed["warnings"] else "ok"
        return refreshed

    def _refresh(self, *, trigger: str, agent_executor: Optional[Callable], extra=None):
        snapshot_result = query.get_graph_snapshot(self.cfg)
        if not snapshot_result.ok:
            return _result(False, "failed", error=snapshot_result.error.to_dict())
        snapshot = snapshot_result.data
        warnings = []
        proposals = []
        if agent_executor is not None:
            agent_result = self._invoke_curator(agent_executor, snapshot)
            if not agent_result["ok"]:
                return agent_result
            proposals = agent_result["data"]
            stored = semantic.store_relation_proposals(
                self.cfg, proposals, source_snapshot=snapshot["snapshot_id"])
            if not stored.ok:
                return _result(False, "failed", error=stored.error.to_dict(),
                               warnings=stored.warnings)
        else:
            warnings.append("未提供语义索引 Agent；本轮仅维护原始引用图和已有语义关系")
        version = semantic.create_graph_version(
            self.cfg, source_snapshot=snapshot["snapshot_id"], trigger=trigger,
            notes="{} proposals".format(len(proposals)))
        if not version.ok:
            return _result(False, "failed", error=version.error.to_dict(), warnings=warnings)
        visual = visualization.export_graph_visualization(
            self.cfg, output_dir=self.visualization_dir)
        if not visual.ok:
            return _result(False, "failed", error=visual.error.to_dict(), warnings=warnings)
        data = dict(extra or {})
        data.update({"snapshot_id": snapshot["snapshot_id"],
                     "graph_version": version.data,
                     "visualization": visual.data,
                     "proposals_written": len(proposals)})
        return _result(True, "partial" if warnings else "ok", data=data,
                       warnings=warnings)

    def _invoke_curator(self, executor: Callable, snapshot: dict):
        with open(self.prompt_path, encoding="utf-8") as handle:
            system_prompt = handle.read()
        overviews = []
        for paper in snapshot.get("papers", []):
            value = query.get_paper_overview(self.cfg, paper["paper_id"])
            if value.ok:
                overviews.append(value.data)
        payload = {"snapshot_id": snapshot["snapshot_id"],
                   "papers": overviews,
                   "citations": snapshot.get("citations", []),
                   "existing_relations": snapshot.get("paper_relations", [])}
        try:
            output = executor([
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ])
            if isinstance(output, str):
                output = json.loads(output)
            if isinstance(output, dict) and "proposals" in output:
                output = output["proposals"]
            if not isinstance(output, list):
                raise ValueError("curation agent must return a JSON array")
        except Exception as exc:
            return _result(False, "failed", error={
                "code": "O_AGENT_OUTPUT_INVALID", "message": str(exc),
                "hint": "让执行器返回 RelationProposal JSON 数组"})
        return _result(True, "ok", data=output)
