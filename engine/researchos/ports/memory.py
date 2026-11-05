"""In-memory ports: enough behaviour to exercise the whole loop offline.

These are not a second implementation of the modules.  They are a *test double*
for the ports, with the minimum semantics the orchestrators rely on:

* the Organization port owns an in-memory paper graph and applies the same
  "replace semantics" for a paper's claims that the real store uses;
* the Scheduler port implements the same three deterministic planner
  strategies and the same context strategies;
* the Read port applies the same "paper_id must match the task" check;
* the Research port writes a corpus manifest so downstream code has a path.

Every port switch is deliberate: the real libraries will be dropped in behind
these interfaces later, and the tests that drive the loop will keep passing.
"""
from __future__ import annotations

import hashlib
import json
import os
from collections import defaultdict
from typing import Any, Dict, Iterable, List, Optional

from ..runtime.envelope import empty, error, failed, ok, partial


class InMemoryOrganization:
    def __init__(self, kb_dir: str):
        self.kb_dir = os.path.abspath(kb_dir)
        os.makedirs(self.kb_dir, exist_ok=True)
        self.papers: Dict[str, dict] = {}
        self.citations: List[dict] = []
        self.paper_relations: List[dict] = []
        self.claims: Dict[str, dict] = {}
        self.claim_relations: Dict[str, dict] = {}
        self.understandings: Dict[str, dict] = {}
        self.graph_versions: List[dict] = []
        self.snapshot_id = "org_empty"

    # -- research import -------------------------------------------------
    def import_search_output(self, search_output: str) -> dict:
        # Same naming contract as the bundle port: one definition, two backends.
        from . import corpus_files
        search_output = corpus_files.normalise(search_output)
        corpus = corpus_files.corpus_path(search_output)
        if not os.path.isfile(corpus):
            return failed(error("ORG_CORPUS_MISSING",
                                "找不到 corpus.jsonl：{}".format(search_output),
                                hint="先运行 Research 阶段"))
        imported = 0
        with open(corpus, encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                row = json.loads(line)
                paper_id = row.get("paper_id") or row.get("id")
                if not paper_id:
                    continue
                self.papers[paper_id] = {
                    "paper_id": paper_id,
                    "title": row.get("title", ""),
                    "abstract": row.get("abstract", ""),
                    "year": row.get("year"),
                    "preprint_year": row.get("preprint_year") or row.get("year"),
                    "venue": row.get("venue", ""),
                    "gist": "", "research_problem": "", "contribution": "",
                    "method_family": "", "reading_depth": "unread",
                    "citation_count": row.get("citation_count", 0),
                    "role": row.get("role", ""),
                    "pdf_path": (row.get("full_text") or {}).get("pdf_path"),
                }
                imported += 1
        self._refresh("search_import")
        return ok({"imported": imported, "snapshot_id": self.snapshot_id,
                   "graph_version": self._version("search_import")})

    # -- snapshots -------------------------------------------------------
    def snapshot(self) -> dict:
        payload = {
            "snapshot_id": self.snapshot_id,
            "papers": [dict(p, pdf_available=self._pdf_available(p["paper_id"]))
                       for p in self.papers.values()],
            "citations": list(self.citations),
            "paper_relations": list(self.paper_relations),
            "claims": list(self.claims.values()),
            "claim_relations": list(self.claim_relations.values()),
        }
        return ok(payload)

    def related_knowledge(self, paper_id: str) -> dict:
        neighbours = self._neighbours(paper_id)
        items = []
        for other in neighbours:
            paper = self.papers.get(other)
            if not paper or paper["reading_depth"] in (None, "", "unread"):
                continue
            claims = [c for c in self.claims.values() if c["paper_id"] == other]
            items.append({
                "paper_id": other,
                "gist": paper.get("gist"),
                "research_problem": paper.get("research_problem"),
                "contribution": paper.get("contribution"),
                "claims": claims,
            })
        return ok(items)

    def paper_overview(self, paper_id: str) -> dict:
        paper = self.papers.get(paper_id)
        if not paper:
            return failed(error("ORG_PAPER_NOT_FOUND", paper_id))
        keep = ("paper_id", "title", "abstract", "year", "preprint_year",
                "venue", "role", "reading_depth", "gist", "research_problem",
                "contribution", "method_family")
        return ok({key: paper.get(key) for key in keep})

    def paper_understanding(self, paper_id: str,
                            aspects: Optional[List[str]] = None) -> dict:
        understanding = self.understandings.get(paper_id) or {}
        if aspects:
            understanding = {k: v for k, v in understanding.items() if k in aspects}
        if not understanding:
            return ok({}, warnings=["论文尚无可用的结构化理解：{}".format(paper_id)])
        return ok(understanding)

    # -- writes ----------------------------------------------------------
    def store_reading_results(self, results: Iterable[dict]) -> dict:
        written, failures = [], []
        for result in results:
            paper_id = result.get("paper_id")
            if paper_id not in self.papers:
                failures.append({"paper_id": paper_id,
                                 "code": "ORG_PAPER_NOT_FOUND"})
                continue
            self.understandings[paper_id] = dict(result)
            paper = self.papers[paper_id]
            paper["reading_depth"] = result.get("reading_depth", "third_pass")
            paper["gist"] = result.get("gist", "")
            paper["research_problem"] = result.get("research_problem", "")
            contribution = result.get("contribution") or []
            paper["contribution"] = ("; ".join(contribution)
                                     if isinstance(contribution, list)
                                     else str(contribution))
            paper["method_family"] = result.get("method_family", "")
            for index, claim in enumerate(result.get("core_claims") or [], 1):
                claim_id = "c_{}_{}".format(paper_id.replace(":", "_"), index)
                self.claims[claim_id] = {
                    "claim_id": claim_id, "paper_id": paper_id,
                    "text": claim.get("text", ""),
                    "evidence": claim.get("evidence"),
                    "evidence_type": claim.get("evidence_type", ""),
                    "confidence": claim.get("confidence"),
                    "generated_by": result.get("generated_by", ""),
                }
            written.append(paper_id)
        if failures:
            return failed(error("ORG_PARTIAL_WRITE",
                                "{} 篇阅读结果未写入".format(len(failures)),
                                hint="修正后重放本批任务",
                                context={"failures": failures}),
                          data={"written": written, "fail": failures})
        self._refresh("reading_results_written")
        return ok({"written": written, "snapshot_id": self.snapshot_id,
                   "graph_version": self._version("reading_results_written")})

    def curation_package(self) -> dict:
        snapshot = self.snapshot()["data"]
        overviews = [self.paper_overview(p["paper_id"])["data"]
                     for p in snapshot["papers"]]
        return ok({"snapshot_id": snapshot["snapshot_id"],
                   "papers": overviews,
                   "citations": snapshot["citations"],
                   "existing_relations": snapshot["paper_relations"]})

    def store_relation_proposals(self, proposals: List[dict],
                                 source_snapshot: str) -> dict:
        stored = 0
        for item in proposals or []:
            src, dst = item.get("src_paper"), item.get("dst_paper")
            if not src or not dst or src not in self.papers or dst not in self.papers:
                continue
            rel_id = "r_" + hashlib.sha256(
                "{}{}{}".format(src, dst, item.get("kind", "")).encode()
            ).hexdigest()[:12]
            self.paper_relations.append({
                "rel_id": rel_id, "src_paper": src, "dst_paper": dst,
                "kind": item.get("kind", ""), "axis": item.get("axis", ""),
                "evidence": item.get("evidence"),
                "confidence": item.get("confidence"),
                "generated_by": item.get("generated_by", "curator"),
            })
            stored += 1
        self._refresh("relation_proposals")
        return ok({"stored": stored, "snapshot_id": self.snapshot_id,
                   "graph_version": self._version("relation_proposals")})

    def relation_review_package(self) -> dict:
        snapshot = self.snapshot()["data"]
        return ok({"task_type": "claim_relation_review",
                   "snapshot_id": snapshot["snapshot_id"],
                   "claims": snapshot["claims"],
                   "existing_relations": snapshot["claim_relations"],
                   "papers": snapshot["papers"]})

    def apply_claim_relation_review(self, review: dict) -> dict:
        if not isinstance(review, dict) or not isinstance(
                review.get("relations"), list):
            return failed(error("ORG_RELATION_REVIEW_INVALID",
                                "关系审阅结果必须是含 relations 数组的 JSON 对象",
                                hint="即使没有关系也要显式提交 {\"relations\": []}"))
        if not str(review.get("review_notes") or "").strip():
            return failed(error("ORG_RELATION_REVIEW_INVALID",
                                "关系审阅结果缺少 review_notes",
                                hint="说明比较了哪些论点"))
        for index, relation in enumerate(review["relations"]):
            rel_id = "cr_" + hashlib.sha256(
                json.dumps(relation, sort_keys=True, ensure_ascii=False).encode()
            ).hexdigest()[:12]
            self.claim_relations[rel_id] = {
                "rel_id": rel_id,
                "src_claim": relation.get("src_claim"),
                "dst_claim": relation.get("dst_claim"),
                "kind": relation.get("kind"),
                "polarity": relation.get("polarity"),
                "evidence": relation.get("evidence"),
                "confidence": relation.get("confidence"),
                "generated_by": review.get("generated_by",
                                           "organization_claim_relation_review"),
            }
        self._refresh("claim_relation_reviewed")
        return ok({"relations_submitted": len(review["relations"]),
                   "review_notes": review["review_notes"],
                   "snapshot_id": self.snapshot_id,
                   "graph_version": self._version("claim_relation_reviewed")})

    def export_evidence(self) -> dict:
        snapshot = self.snapshot()["data"]
        papers = []
        for paper in snapshot["papers"]:
            paper_id = paper["paper_id"]
            papers.append({"overview": self.paper_overview(paper_id)["data"],
                           "understanding": self.understandings.get(paper_id, {})})
        return ok({"snapshot_id": snapshot["snapshot_id"], "papers": papers,
                   "citations": snapshot["citations"],
                   "paper_relations": snapshot["paper_relations"],
                   "claims": snapshot["claims"],
                   "claim_relations": snapshot["claim_relations"]})

    # -- internals -------------------------------------------------------
    def _pdf_available(self, paper_id: str) -> bool:
        path = self.papers.get(paper_id, {}).get("pdf_path")
        return bool(path and os.path.isfile(path))

    def _neighbours(self, paper_id: str) -> List[str]:
        found = []
        for edge in self.citations:
            if not edge.get("internal"):
                continue
            if edge.get("from_paper") == paper_id and edge.get("to_paper"):
                found.append(edge["to_paper"])
            elif edge.get("to_paper") == paper_id:
                found.append(edge["from_paper"])
        for edge in self.paper_relations:
            if edge.get("src_paper") == paper_id and edge.get("dst_paper"):
                found.append(edge["dst_paper"])
            elif edge.get("dst_paper") == paper_id:
                found.append(edge["src_paper"])
        return sorted(set(found))

    def _refresh(self, trigger: str) -> None:
        payload = {
            "papers": [dict(p) for p in self.papers.values()],
            "citations": list(self.citations),
            "paper_relations": list(self.paper_relations),
            "claims": list(self.claims.values()),
            "claim_relations": list(self.claim_relations.values()),
        }
        canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False,
                               separators=(",", ":"))
        self.snapshot_id = "org_" + hashlib.sha256(
            canonical.encode("utf-8")).hexdigest()[:16]
        if not self.graph_versions or self.graph_versions[-1]["snapshot_id"] != \
                self.snapshot_id:
            self.graph_versions.append(self._version(trigger))

    def _version(self, trigger: str) -> dict:
        return {"version_id": "v{}".format(len(self.graph_versions) + 1),
                "snapshot_id": self.snapshot_id, "trigger": trigger}


class InMemoryScheduler:
    PLANNERS = ("chronological", "citation_based", "dependency_based")

    def plan(self, snapshot: dict, *, strategy: str, batch_size: int,
             completed: Optional[List[str]] = None) -> dict:
        if strategy not in self.PLANNERS:
            return failed(error("SCHED_INPUT_INVALID",
                                "未知规划策略：{}".format(strategy)))
        papers = snapshot.get("papers") or []
        done = set(completed or [])
        done.update(p["paper_id"] for p in papers
                    if p.get("reading_depth") not in (None, "", "unread"))
        unavailable = [p["paper_id"] for p in papers
                       if p["paper_id"] not in done and p.get("pdf_available") is False]
        candidates = [p for p in papers if p["paper_id"] not in done
                      and p.get("pdf_available") is not False]
        if not candidates:
            return empty({"ordered": [], "selected": [],
                          "unavailable_material": unavailable,
                          "snapshot_id": snapshot.get("snapshot_id")},
                         warnings=["没有材料可用的未读候选"])
        if strategy == "chronological":
            ordered = [p["paper_id"] for p in sorted(
                candidates, key=lambda p: (_year(p), p["paper_id"]))]
            reasons = {pid: "按首次公开时间排序" for pid in ordered}
        else:
            edges = []
            if strategy == "citation_based":
                for edge in snapshot.get("citations") or []:
                    if edge.get("internal") and edge.get("to_paper"):
                        edges.append((edge["to_paper"], edge["from_paper"]))
            else:
                for edge in snapshot.get("paper_relations") or []:
                    if edge.get("src_paper") and edge.get("dst_paper"):
                        edges.append((edge["dst_paper"], edge["src_paper"]))
            ordered = _topological(candidates, edges)
            reasons = {pid: "前置论文优先的拓扑序" for pid in ordered}
        selected = ordered[:batch_size]
        return ok({"snapshot_id": snapshot.get("snapshot_id"),
                   "strategy": strategy, "ordered": ordered,
                   "selected": selected,
                   "reasons": {pid: reasons[pid] for pid in selected},
                   "unavailable_material": unavailable})

    def select_context(self, target_paper: str, knowledge: List[dict], *,
                       strategy: str, budget_tokens: int) -> dict:
        if strategy == "none" or budget_tokens == 0:
            return ok({"knowledge_items": [], "estimated_tokens": 0,
                       "strategy": strategy})
        items, used, omitted = [], 0, 0
        for paper in knowledge or []:
            parts = [paper.get("gist"), paper.get("research_problem"),
                     paper.get("contribution")]
            refs = []
            if strategy == "structured":
                for claim in paper.get("claims") or []:
                    if claim.get("text"):
                        parts.append("Claim: " + claim["text"])
                        refs.append(claim.get("claim_id"))
            content = "\n".join(part for part in parts if part)
            if not content:
                omitted += 1
                continue
            cost = max(1, (len(content) + 3) // 4)
            if used + cost > budget_tokens:
                omitted += 1
                continue
            items.append({"source_paper": paper.get("paper_id"),
                          "item_refs": [r for r in refs if r],
                          "content": content,
                          "knowledge_level": "L2" if strategy == "structured" else "L1",
                          "source_type": "paper_assertion",
                          "verification_status": "not_checked",
                          "selection_reason": "与目标论文相邻且已读",
                          "estimated_tokens": cost})
            used += cost
        value = {"knowledge_items": items, "estimated_tokens": used,
                 "strategy": strategy}
        if omitted:
            return partial(value, warnings=[
                "{} 个候选因无内容或预算不足未纳入".format(omitted)])
        return ok(value)

    def filter_context_for_question(self, knowledge_items: List[dict],
                                    question: str, *,
                                    budget_tokens: int = 1000) -> dict:
        terms = {term.lower() for term in _terms(question)}
        ranked = sorted(knowledge_items or [],
                        key=lambda item: sum(term in (item.get("content") or "").lower()
                                             for term in terms), reverse=True)
        chosen, used = [], 0
        for item in ranked:
            cost = int(item.get("estimated_tokens") or
                       max(1, (len(item.get("content") or "") + 3) // 4))
            if used + cost <= budget_tokens:
                chosen.append(item)
                used += cost
        return ok({"knowledge_items": chosen, "estimated_tokens": used,
                   "question": question})


class InMemoryRead:
    def prepare(self, task: dict, *, material: Optional[dict] = None) -> dict:
        if not task.get("task_id") or not task.get("target_paper"):
            return failed(error("READ_INPUT_MISSING", "task_id 与 target_paper 必填"))
        return ok({"task_id": task["task_id"],
                   "target_paper": task["target_paper"],
                   "reading_task": task,
                   "strategy": task.get("reading_strategy_id"),
                   "authorized_material": material or task.get("target_material", {}),
                   "required_output_schema": "reading_result.schema.json@2.0-draft"})

    def validate(self, result: dict, task: dict) -> dict:
        if not isinstance(result, dict):
            return failed(error("READ_RESULT_INVALID", "ReadingResult 必须是 JSON object"))
        required = ("schema_version", "paper_id", "reading_depth", "gist",
                    "core_claims", "limitations", "understanding_quality")
        missing = [field for field in required if field not in result]
        if missing:
            return failed(error("READ_RESULT_INVALID",
                                "缺少字段：{}".format(", ".join(missing))))
        if result.get("paper_id") != task.get("target_paper"):
            return failed(error("READ_TASK_RESULT_MISMATCH",
                                "ReadingResult.paper_id 与 ReadingTask.target_paper 不一致"))
        # Keep the port contract identical to the bundle adapter: ``data`` is
        # the reading, the report rides in diagnostics.
        return ok(result, diagnostics={"mode": "production", "errors": [],
                                       "warnings": []})


class InMemoryResearch:
    def __init__(self, output_root: str = ""):
        self.output_root = os.path.abspath(output_root) if output_root else ""

    def run(self, question: str, *, run_id: str, query: str,
            reason: str = "", options: Optional[Dict[str, Any]] = None) -> dict:
        records = (options or {}).get("papers") or []
        output = (options or {}).get("search_output") or os.path.join(
            self.output_root or os.path.join("runs", "search"), run_id)
        os.makedirs(output, exist_ok=True)
        corpus = os.path.join(output, "corpus.jsonl")
        with open(corpus, "w", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        if not records:
            return empty({"search_output": output, "query": query},
                         warnings=["检索结果为空"])
        return ok({"search_output": output, "query": query, "query_reason": reason,
                   "count": len(records)})


def _year(paper: dict) -> int:
    return int(paper.get("preprint_year") or paper.get("year") or 9999)


def _terms(text: str) -> List[str]:
    import re
    return re.findall(r"[\w\u4e00-\u9fff]{2,}", text or "")


def _topological(nodes: List[dict], edges) -> List[str]:
    by_id = {node["paper_id"]: node for node in nodes}
    outgoing = defaultdict(set)
    indegree = {pid: 0 for pid in by_id}
    for src, dst in edges:
        if src not in by_id or dst not in by_id or src == dst:
            continue
        if dst not in outgoing[src]:
            outgoing[src].add(dst)
            indegree[dst] += 1
    ready = sorted((pid for pid, degree in indegree.items() if degree == 0),
                   key=lambda pid: (_year(by_id[pid]), pid))
    ordered: List[str] = []
    while ready:
        current = ready.pop(0)
        ordered.append(current)
        for nxt in sorted(outgoing[current]):
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                ready.append(nxt)
                ready.sort(key=lambda pid: (_year(by_id[pid]), pid))
    ordered.extend(sorted((pid for pid in by_id if pid not in ordered),
                          key=lambda pid: (_year(by_id[pid]), pid)))
    return ordered
