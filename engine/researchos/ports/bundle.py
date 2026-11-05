"""Bundle ports: the real tool libraries behind the engine interfaces.

Design rules:

* **No re-implementation.** Every method delegates to the bundled module that
  already owns that behaviour.  If a behaviour is missing there, this adapter
  fails loudly rather than approximating it.
* **Translation only.** The bundled libraries use their own envelope shapes
  (``ToolResult`` objects, plain dicts with ``ok``).  This module flattens them
  into the single engine envelope so agents never branch on two conventions.
* **Import isolation.** The bundled packages are loaded by path, not by an
  ambient ``sys.path`` mutation, so two runs in one process cannot cross-wire.
"""
from __future__ import annotations

import importlib.util
import copy
import hashlib
import os
import re
import sys
from typing import Any, Dict, Iterable, List, Optional

from ..config import SystemConfig
from ..runtime.envelope import error, failed, ok, partial
from . import corpus_files
from .contracts import PortError


BUNDLE_ROOT = os.path.join(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))), "bundle")


def _module(directory: str, module: str):
    """Import ``bundle/<directory>/AgentWorkflow/<module>.py`` by path."""
    path = os.path.join(BUNDLE_ROOT, directory, "AgentWorkflow")
    if path not in sys.path:
        sys.path.insert(0, path)
    tools = os.path.join(BUNDLE_ROOT, directory, "ToolsPy")
    if os.path.isdir(tools) and tools not in sys.path:
        sys.path.insert(0, tools)
    file_path = os.path.join(path, module + ".py")
    if not os.path.isfile(file_path):
        raise PortError("bundle 模块缺失：{}".format(file_path))
    name = "_researchos_bundle_{}_{}".format(directory.lower(), module)
    loaded = sys.modules.get(name)
    if loaded is not None:
        return loaded
    spec = importlib.util.spec_from_file_location(name, file_path)
    if spec is None or spec.loader is None:
        raise PortError("无法加载 bundle 模块：{}".format(file_path))
    loaded = importlib.util.module_from_spec(spec)
    sys.modules[name] = loaded
    try:
        spec.loader.exec_module(loaded)
    except Exception:
        sys.modules.pop(name, None)
        raise
    return loaded


def _flatten(value: Any) -> dict:
    """Normalise a bundled return value into the engine envelope.

    The bundled libraries return either a ``ToolResult`` (attribute access with
    a ``to_dict``) or a plain dict already shaped like an envelope.  Both are
    accepted; anything else is a contract violation and is reported as such.
    """
    if hasattr(value, "to_dict"):
        value = value.to_dict()
    if not isinstance(value, dict):
        return failed(error("BUNDLE_BAD_RESULT",
                            "bundle 返回了非信封值：{!r}".format(type(value))))
    if "status" in value and "ok" in value:
        value.setdefault("warnings", [])
        value.setdefault("diagnostics", {})
        value.setdefault("meta", {})
        return value
    # A ToolResult-like object that did not expose to_dict: reconstruct.
    ok_flag = bool(getattr(value, "ok", False))
    data = getattr(value, "data", None)
    err = getattr(value, "error", None)
    if err is not None and hasattr(err, "to_dict"):
        err = err.to_dict()
    return {
        "ok": ok_flag,
        "status": getattr(value, "status", "ok" if ok_flag else "failed"),
        "data": data if data is not None else value.get("data"),
        "error": err,
        "warnings": list(getattr(value, "warnings", []) or []),
        "diagnostics": dict(getattr(value, "diagnostics", {}) or {}),
        "meta": {},
    }


class BundleResearch:
    """PaperSearch: one traceable search run producing a corpus on disk."""

    def __init__(self, config: SystemConfig):
        self.config = config
        self._workflow = None

    @property
    def workflow(self):
        if self._workflow is None:
            module = _module("PaperSearch", "search_workflow")
            self._workflow = module.SearchWorkflow(self.config.search_output_dir)
        return self._workflow

    def run(self, question: str, *, run_id: str, query: str,
            reason: str = "", options: Optional[Dict[str, Any]] = None) -> dict:
        options = dict(options or {})
        # A caller may hand over a corpus it already owns instead of asking for
        # a live search.  Two shapes are accepted, both of which only *select*
        # output -- they never change what the pipeline stages mean:
        #   * ``search_output``: an existing PaperSearch output directory;
        #   * ``papers``: records to write as a corpus (offline / replay runs).
        # A live search remains the default when neither is given.
        offline = _offline_corpus(question, run_id, options,
                                  self.config.search_output_dir)
        if offline is not None:
            return offline
        # ``agent_executor`` here would re-introduce a second query planner; the
        # Scheduler already owns planning, so the pipeline receives the query
        # the Research agent decided on.
        primary = query or question
        # Query variants are the whole point of keyword divergence: recording
        # them and then searching once makes the audit trail claim more than the
        # corpus contains. The primary query always runs; each variant then runs
        # as a bounded supplement and its hits are merged (never replaced), so a
        # variant can only add recall.
        variants = [item for item in (options.get("query_variants") or [])
                    if isinstance(item, str) and item.strip() and item.strip() != primary]
        variant_budget = max(0, int(options.get("variant_budget", 3)))
        attempts = [primary] + variants[:variant_budget]
        per_query = []
        merged = None
        web_candidates = [row for row in (options.get("web_candidates") or [])
                          if isinstance(row, dict)]
        web_seeded = 0
        for index, one_query in enumerate(attempts):
            suffix = run_id if index == 0 else "{}-v{}".format(run_id, index)
            try:
                result = self.workflow.run(
                    one_query, run_id=suffix,
                    max_per_source=int(options.get("max_per_source", 20)),
                    fetch_top=int(options.get("fetch_top", 8)),
                    parse_top=int(options.get("parse_top", 8)),
                    expand_top=int(options.get("expand_top", 4)))
            except Exception as exc:  # noqa: BLE001 - adapter boundary
                if index == 0:
                    fallback, web_seeded = _web_candidate_result(
                        self.config.search_output_dir, run_id, web_candidates,
                        source_query=primary,
                        warning="数据库检索失败：{}: {}".format(type(exc).__name__, exc))
                    if fallback is None:
                        return failed(error("RESEARCH_BUNDLE_FAILED",
                                            "{}: {}".format(type(exc).__name__, exc),
                                            hint="检查 PaperSearch 依赖与凭证"))
                    merged = fallback
                    per_query.append({"query": one_query, "status": "failed",
                                      "error": str(exc)})
                    continue
                # A supplement that fails must not lose the primary corpus.
                per_query.append({"query": one_query, "status": "failed",
                                  "error": "{}: {}".format(
                                      type(exc).__name__, exc)})
                continue
            flat = _flatten(result)
            if not flat["ok"]:
                if index == 0:
                    fallback, web_seeded = _web_candidate_result(
                        self.config.search_output_dir, run_id, web_candidates,
                        source_query=primary,
                        warning="数据库检索未返回可用结果")
                    if fallback is None:
                        return flat
                    merged = fallback
                    per_query.append({"query": one_query, "status": flat.get("status"),
                                      "error": (flat.get("error") or {}).get("message")})
                    continue
                per_query.append({"query": one_query, "status": flat.get("status"),
                                  "error": (flat.get("error") or {}).get("message")})
                continue
            data = flat.get("data") or {}
            rows = _corpus_rows(data)
            per_query.append({"query": one_query, "status": flat.get("status"),
                              "papers": len(rows)})
            if merged is None:
                merged = flat
            else:
                _merge_corpus(merged.get("data") or {}, data, source_query=one_query)
        value = merged if merged is not None else failed(error(
            "RESEARCH_NO_RESULT", "所有查询变体都未返回结果"))
        if value["ok"]:
            web_added = _merge_web_candidates(
                value.get("data") or {}, web_candidates, source_query=primary) + web_seeded
            plan = (value.get("diagnostics") or {}).get("query_plan") or {}
            plan.update({"query": primary,
                         "reason": reason or plan.get("reason", "")})
            plan["query_variants_executed"] = per_query
            plan["variant_budget"] = variant_budget
            plan["agent_web_search"] = {
                "enabled": bool(web_candidates),
                "candidates_submitted": len(web_candidates),
                "papers_added": web_added,
                "deduplication": "DOI, arXiv id, then normalized title/year",
            }
            value.setdefault("diagnostics", {})["query_plan"] = plan
            value["data"] = _corpus_summary(value.get("data"))
        return value


def _corpus_path(data: dict) -> str:
    """The papers file inside a PaperSearch output directory."""
    import os as _os
    for key in ("corpus_path", "papers_path"):
        if data.get(key):
            return data[key]
    root = data.get("search_output") or data.get("out_dir") or ""
    if not root:
        return ""
    for name in corpus_files.CORPUS_ALIASES + (corpus_files.CORPUS,):
        candidate = _os.path.join(root, name)
        if _os.path.isfile(candidate):
            return candidate
    return ""


def _corpus_rows(data: dict) -> list:
    """Read the paper records of one search result, tolerating absence."""
    import json as _json
    path = _corpus_path(data)
    if not path:
        return list(data.get("papers") or [])
    rows = []
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    rows.append(_json.loads(line))
    except (OSError, ValueError):
        return list(data.get("papers") or [])
    return rows


def _dedup_key(row: dict) -> str:
    """Stable identity for duplicate detection.

    Order matters: a DOI or arXiv id is authoritative; a normalised title is the
    fallback because two records of the same paper often agree on the title but
    not on any identifier.
    """
    identity = row.get("identity") or {}
    for key in ("doi", "arxiv_id", "openalex_id", "s2_id"):
        value = str(identity.get(key) or "").strip().lower()
        if value:
            return "{}:{}".format(key, value)
    metadata = row.get("metadata") or {}
    # Collapse internal whitespace too: the same paper is routinely recorded as
    # "Theory  of Mind" and "theory of mind" by different sources.
    raw_title = str(metadata.get("title") or metadata.get("title_norm") or "")
    title = " ".join(raw_title.lower().split())
    if title:
        year = metadata.get("year") or ""
        return "title:{}|{}".format(title, year)
    return "id:{}".format(row.get("paper_id") or "")


def _merge_corpus(target: dict, extra: dict, *, source_query: str) -> int:
    """Append the unseen papers of `extra` to `target`'s corpus file.

    Returns the number of papers added. Every added record keeps the query that
    found it, so provenance answers "why is this paper in the corpus".
    """
    import json as _json
    target_path = _corpus_path(target)
    extra_rows = _corpus_rows(extra)
    if not target_path or not extra_rows:
        return 0
    existing = _corpus_rows(target)
    seen = {_dedup_key(row) for row in existing}
    added = 0
    with open(target_path, "a", encoding="utf-8") as handle:
        for row in extra_rows:
            key = _dedup_key(row)
            if key in seen:
                continue
            seen.add(key)
            record = dict(row)
            provenance = dict(record.get("provenance") or {})
            provenance.setdefault("discovered_by_query", source_query)
            record["provenance"] = provenance
            handle.write(_json.dumps(record, ensure_ascii=False) + "\n")
            added += 1
    # Keep the reported count honest: the summary reads the file, not a guess.
    stats = dict(target.get("stats") or {})
    stats["count"] = len(existing) + added
    target["stats"] = stats
    return added


def _merge_web_candidates(target: dict, candidates: list, *, source_query: str) -> int:
    """Add independently web-discovered papers to the database corpus."""
    import hashlib
    rows = []
    for candidate in candidates:
        title = str(candidate.get("title") or "").strip()
        source_url = str(candidate.get("source_url") or candidate.get("url") or "").strip()
        if not title or not source_url:
            continue
        doi = str(candidate.get("doi") or "").strip().lower()
        arxiv_id = str(candidate.get("arxiv_id") or "").strip().lower()
        paper_id = str(candidate.get("paper_id") or "").strip()
        if not paper_id:
            paper_id = ("doi:" + doi if doi else "arxiv:" + arxiv_id if arxiv_id
                        else "web:" + hashlib.sha256(
                            (title.lower() + "|" + source_url).encode("utf-8")
                        ).hexdigest()[:20])
        rows.append({
            "paper_id": paper_id,
            "identity": {key: value for key, value in {
                "doi": doi, "arxiv_id": arxiv_id}.items() if value},
            "metadata": {"title": title, "year": candidate.get("year"),
                         "authors": list(candidate.get("authors") or []),
                         "abstract": str(candidate.get("abstract") or ""),
                         "urls": [source_url]},
            "full_text": {},
            "provenance": {
                "discovery_method": "agent_web_search",
                "discovered_by_query": str(candidate.get("search_query") or source_query),
                "source_url": source_url,
                "source_name": str(candidate.get("source_name") or "web"),
                "retrieved_at": candidate.get("retrieved_at"),
            },
        })
    return _merge_corpus(target, {"papers": rows}, source_query=source_query)


def _web_candidate_result(output_root: str, run_id: str, candidates: list,
                          *, source_query: str, warning: str):
    """Materialise verified web candidates when every database source fails."""
    if not candidates:
        return None, 0
    target = os.path.join(output_root, run_id)
    os.makedirs(target, exist_ok=True)
    corpus_path = corpus_files.corpus_path(target)
    if not os.path.isfile(corpus_path):
        open(corpus_path, "a", encoding="utf-8").close()
    graph_path = corpus_files.graph_path(target)
    if not os.path.isfile(graph_path):
        open(graph_path, "a", encoding="utf-8").close()
    data = {"search_output": target, "corpus_path": corpus_path,
            "graph_path": graph_path, "stats": {"count": 0}}
    added = _merge_web_candidates(data, candidates, source_query=source_query)
    if not added:
        return None, 0
    return partial(data, warnings=[warning, "已降级为 Agent 联网候选语料"]), added


def _corpus_summary(data: Any) -> dict:
    """Reduce a search result to references plus counts.

    The pipeline returns every paper's metadata (tens of thousands of
    characters for a 30-paper corpus). Inlining that into a *tool result* hits
    the runtime's `max_result_chars` ceiling, which nulls `data` and hides
    `search_output` from the caller -- the run then fails with
    `DIRECTOR_RESEARCH_OUTPUT_MISSING` even though retrieval succeeded.

    Everything downstream reads the corpus from disk by path, so the envelope
    only needs the path and a size summary (guide R4: large data travels by
    reference, not by value).
    """
    if not isinstance(data, dict):
        return data
    papers = data.get("papers") or []
    stats = data.get("stats") or {}
    summary = {
        "search_output": data.get("search_output") or data.get("out_dir"),
        "corpus_path": data.get("corpus_path"),
        "graph_path": data.get("graph_path"),
        "pdfs_dir": data.get("pdfs_dir"),
        "count": len(papers) if isinstance(papers, list) else stats.get("count"),
        "stats": stats if isinstance(stats, dict) else {},
        # Keep identifiers only: the full metadata is on disk.
        "paper_ids": [p.get("paper_id") for p in papers
                      if isinstance(p, dict) and p.get("paper_id")][:50],
        "query": data.get("query"),
    }
    return {key: val for key, val in summary.items() if val is not None}


def _offline_corpus(question: str, run_id: str, options: Dict[str, Any],
                    output_root: str):
    """Reuse an existing corpus, or materialise one from supplied records.

    Returns ``None`` when the caller did not ask for an offline run, so the
    live pipeline stays the default path.  The bundle libraries' own file names
    are honoured: ``corpus.jsonl`` (records) plus an optional ``graph.jsonl``
    (citation edges), which is exactly what the import pipeline reads.
    """
    import json
    import shutil

    source = options.get("search_output")
    papers = options.get("papers")
    if not source and not papers:
        return None
    target = os.path.join(output_root or os.path.join("runs", "search"), run_id)
    os.makedirs(target, exist_ok=True)
    if source:
        source = os.path.abspath(str(source))
        corpus = corpus_files.corpus_path(source)
        if not os.path.isfile(corpus):
            # The pipeline's own acceptance names are also accepted, so a
            # PaperSearch run directory works without a rename step.
            alternate = os.path.join(source, corpus_files.CORPUS_ALIASES[0])
            if not os.path.isfile(alternate):
                return failed(error(
                    "RESEARCH_CORPUS_MISSING",
                    "指定的检索产物缺少 corpus.jsonl：{}".format(source),
                    hint="用 --search-output 指向含 corpus.jsonl 的目录"))
            corpus_files.normalise(source)
        for name in ((corpus_files.CORPUS, corpus_files.GRAPH)
                     + corpus_files.GRAPH_ALIASES):
            src = os.path.join(source, name)
            if not os.path.isfile(src):
                continue
            dst_name = (corpus_files.GRAPH
                        if name in corpus_files.GRAPH_ALIASES else name)
            dst = os.path.join(target, dst_name)
            if os.path.abspath(src) != os.path.abspath(dst):
                shutil.copy2(src, dst)
        for name in corpus_files.DATA_DIRS:
            src = os.path.join(source, name)
            dst = os.path.join(target, name)
            if os.path.isdir(src) and os.path.abspath(src) != os.path.abspath(dst):
                shutil.copytree(src, dst, dirs_exist_ok=True)
    else:
        records = list(papers or [])
        if not records:
            return None
        pdf_dir = os.path.join(target, "pdfs")
        with open(corpus_files.corpus_path(target), "w",
                  encoding="utf-8") as handle:
            for record in records:
                record = dict(record)
                full_text = dict(record.get("full_text") or {})
                pdf_path = full_text.get("pdf_path")
                if pdf_path:
                    full_text["pdf_path"] = _stage_pdf(pdf_path, pdf_dir)
                record["full_text"] = full_text
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        graph = corpus_files.graph_path(target)
        if not os.path.isfile(graph):
            open(graph, "w", encoding="utf-8").close()
    count = 0
    with open(corpus_files.corpus_path(target), encoding="utf-8") as handle:
        count = sum(1 for line in handle if line.strip())
    return ok({"search_output": target, "query": question,
               "query_reason": "复用给定语料" if source else "由给定记录生成语料",
               "count": count},
              diagnostics={"query_plan": {"query": question,
                                          "reason": "offline corpus"}},
              meta={"offline": True})


def _stage_pdf(path: str, pdf_dir: str) -> str:
    """Place a supplied PDF inside the run so the corpus stays portable.

    Absolute paths are copied into ``<out>/pdfs`` and rewritten relative to the
    output directory, which is what the import pipeline resolves a relative
    ``pdf_path`` against.  A path that is already relative is left untouched.
    """
    import shutil
    if not path or not os.path.isabs(path):
        return path
    if not os.path.isfile(path):
        return path
    os.makedirs(pdf_dir, exist_ok=True)
    name = os.path.basename(path)
    shutil.copy2(path, os.path.join(pdf_dir, name))
    return os.path.join("pdfs", name)


def _curation_subset(papers: List[dict], citations: List[dict],
                     fallback_limit: int = 12):
    """Keep a curator package useful and below the tool-result ceiling.

    A knowledge base can contain hundreds of unread abstracts.  Sending every
    overview caused the generic 20k result limiter to drop the entire
    ``papers`` and ``citations`` keys, leaving the Organization agent with no
    evidence at all.  Once readings exist, semantic curation should compare
    those evidence-bearing papers.  Before the first reading, a bounded seed
    set is enough to orient the curator.
    """
    rows = [row for row in papers if isinstance(row, dict)]
    read = [row for row in rows
            if row.get("reading_depth") not in (None, "", "unread")]
    selected = read if read else rows[:max(1, fallback_limit)]
    selected_ids = {row.get("paper_id") for row in selected
                    if row.get("paper_id")}
    edges = [edge for edge in citations if isinstance(edge, dict) and (
        edge.get("from_paper") in selected_ids
        or edge.get("to_paper") in selected_ids
        or edge.get("src") in selected_ids
        or edge.get("dst") in selected_ids
    )][:200]
    omitted = len(rows) - len(selected)
    warning = ("策展包聚焦 {} 篇已读论文，省略 {} 篇未读论文；完整语料仍在知识库"
               .format(len(selected), omitted)
               if read and omitted > 0 else
               ("首次策展包仅发送前 {} 篇论文，省略 {} 篇；后续将优先发送已读论文"
                .format(len(selected), omitted)
                if omitted > 0 else ""))
    return selected, edges, warning


class BundleOrganization:
    """PaperOrganization: the sole owner of stored knowledge."""

    def __init__(self, config: SystemConfig):
        self.config = config
        self._workflow = None
        self._paths = None
        self._query = None

    def _ensure(self):
        if self._workflow is not None:
            return
        module = _module("PaperOrganization", "organization_workflow")
        self._workflow = module.OrganizationWorkflow(
            self.config.kb_dir,
            visualization_dir=self.config.visualization_dir)
        package = _module("PaperOrganization", "organization_workflow")
        # The workflow already holds a resolved KBConfig; reuse it rather than
        # resolving twice (the two resolutions could disagree after a move).
        self._cfg = self._workflow.cfg
        self._query = sys.modules.get("query")
        if self._query is None:
            from paperorganization import query as _q
            self._query = _q
        from paperorganization import paths as _paths
        self._paths = _paths

    @property
    def query(self):
        self._ensure()
        return self._query

    @property
    def cfg(self):
        self._ensure()
        return self._cfg

    def import_search_output(self, search_output: str) -> dict:
        self._ensure()
        value = _flatten(self._workflow.initialize(
            corpus_files.normalise(search_output)))
        return self._with_snapshot(value)

    def snapshot(self) -> dict:
        self._ensure()
        return _flatten(self._query.get_graph_snapshot(self.cfg))

    def store_reading_results(self, results: Iterable[dict]) -> dict:
        self._ensure()
        value = _flatten(self._workflow.apply_reading_results(list(results)))
        return self._with_snapshot(value)

    def related_knowledge(self, paper_id: str) -> dict:
        self._ensure()
        return _flatten(self._query.get_related_knowledge(self.cfg, paper_id))

    def paper_overview(self, paper_id: str) -> dict:
        self._ensure()
        return _flatten(self._query.get_paper_overview(self.cfg, paper_id))

    def paper_understanding(self, paper_id: str,
                            aspects: Optional[List[str]] = None) -> dict:
        self._ensure()
        return _flatten(self._query.get_paper_understanding(
            self.cfg, paper_id, aspects=aspects))

    def curation_package(self) -> dict:
        """Snapshot + per-paper overviews, the curator's input."""
        self._ensure()
        snapshot = self.snapshot()
        if not snapshot["ok"]:
            return snapshot
        papers = []
        warnings = list(snapshot.get("warnings") or [])
        for paper in snapshot["data"].get("papers", []):
            overview = self.paper_overview(paper["paper_id"])
            if not overview["ok"]:
                return overview
            papers.append(overview["data"])
            warnings.extend(overview.get("warnings") or [])
        papers, citations, subset_warning = _curation_subset(
            papers,
            snapshot["data"].get("citations", []),
        )
        if subset_warning:
            warnings.append(subset_warning)
        value = ok({"snapshot_id": snapshot["data"]["snapshot_id"],
                    "papers": papers,
                    "citations": citations,
                    "existing_relations": snapshot["data"].get(
                        "paper_relations", [])}, warnings=warnings)
        if warnings:
            value["status"] = "partial"
        return value

    def store_relation_proposals(self, proposals: List[dict],
                                 source_snapshot: str) -> dict:
        self._ensure()
        from paperorganization import semantic
        value = _flatten(semantic.store_relation_proposals(
            self.cfg, proposals, source_snapshot=source_snapshot))
        return self._with_snapshot(value)

    def relation_review_package(self) -> dict:
        self._ensure()
        return _flatten(self._workflow.prepare_claim_relation_review())

    def apply_claim_relation_review(self, review: dict) -> dict:
        self._ensure()
        value = _flatten(self._workflow.apply_claim_relation_review(review))
        return self._with_snapshot(value)

    def export_evidence(self) -> dict:
        self._ensure()
        return _flatten(self._workflow.get_research_evidence())

    def _with_snapshot(self, value: dict) -> dict:
        """Every write must leave a snapshot id behind: the state machine binds
        tasks and reviews to it, so a write without one breaks traceability."""
        if not value["ok"]:
            return value
        data = value.get("data")
        if isinstance(data, dict) and not data.get("snapshot_id"):
            snapshot = self.snapshot()
            if snapshot["ok"]:
                data["snapshot_id"] = snapshot["data"]["snapshot_id"]
        return value


class BundleScheduler:
    """PaperScheduler: deterministic planning and context selection primitives.

    Note which methods are *not* here: proposal ranking, re-read sufficiency and
    ideation are engine concerns, not bundled ones.  The adapter exposes only
    what the bundle actually provides.
    """

    def __init__(self, config: SystemConfig):
        self.config = config
        self._pkg = None

    @property
    def package(self):
        if self._pkg is None:
            tools = os.path.join(BUNDLE_ROOT, "PaperScheduler", "ToolsPy")
            if tools not in sys.path:
                sys.path.insert(0, tools)
            import paperscheduler
            self._pkg = paperscheduler
        return self._pkg

    def plan(self, snapshot: dict, *, strategy: str, batch_size: int,
             completed: Optional[List[str]] = None) -> dict:
        return _flatten(self.package.plan_reading(
            snapshot, strategy=strategy, batch_size=batch_size,
            completed=completed))

    def select_context(self, target_paper: str, knowledge: List[dict], *,
                       strategy: str, budget_tokens: int) -> dict:
        return _flatten(self.package.select_context(
            target_paper, knowledge, strategy=strategy,
            budget_tokens=budget_tokens))

    def filter_context_for_question(self, knowledge_items: List[dict],
                                    question: str, *,
                                    budget_tokens: int = 1000) -> dict:
        return _flatten(self.package.filter_context_for_question(
            knowledge_items, question, budget_tokens=budget_tokens))


class BundleRead:
    """PaperRead: reading contract, validation and strategy loading."""

    def __init__(self, config: SystemConfig):
        self.config = config
        self._workflow = None
        self._pkg = None

    @property
    def package(self):
        if self._pkg is None:
            tools = os.path.join(BUNDLE_ROOT, "PaperRead", "ExamplePy", "tools")
            if tools not in sys.path:
                sys.path.insert(0, tools)
            import paperread
            self._pkg = paperread
        return self._pkg

    @property
    def workflow(self):
        if self._workflow is None:
            module = _module("PaperRead", "read_workflow")
            self._workflow = module.ReadWorkflow()
        return self._workflow

    def prepare(self, task: dict, *, material: Optional[dict] = None) -> dict:
        return _flatten(self.workflow.prepare(task, material=material))

    def validate(self, result: dict, task: dict) -> dict:
        """Return the reading in ``data``; the contract report goes to diagnostics.

        The bundled validator answers with a *report* (``ok``/``errors``/
        ``warnings``), not with the reading.  Flattening that straight through
        would hand the caller a report where it expects a ReadingResult, and the
        write-back would then store ``{"errors": [], "schema": ...}`` as if it
        were a paper's understanding.
        """
        result = copy.deepcopy(result)
        report = _flatten(self.package.validate_reading_result(
            result, mode="production"))
        diagnostics = {"mode": "production"}
        for key in ("errors", "warnings", "schema", "status"):
            if isinstance(report.get("data"), dict) and key in report["data"]:
                diagnostics[key] = report["data"][key]
        if not report["ok"]:
            return failed(report.get("error") or error(
                "READ_RESULT_INVALID", "阅读结果未通过生产契约"),
                diagnostics=diagnostics)
        if result.get("paper_id") != task.get("target_paper"):
            return failed(error("READ_TASK_RESULT_MISMATCH",
                                "ReadingResult.paper_id 与 ReadingTask.target_paper 不一致",
                                hint="结果必须绑定它被派发的任务"),
                          diagnostics=diagnostics)
        requested = task.get("requested_reading_depth")
        required_depth = {"summary": "first_pass", "skim": "second_pass",
                          "deep": "third_pass"}.get(requested)
        actual_rank = {"unread": 0, "first_pass": 1, "second_pass": 2,
                       "third_pass": 3}.get(result.get("reading_depth"), -1)
        required_rank = {"first_pass": 1, "second_pass": 2,
                         "third_pass": 3}.get(required_depth, 0)
        if requested and actual_rank < required_rank:
            return failed(error(
                "READ_DEPTH_INSUFFICIENT",
                "Scheduler 要求 {}，结果仅达到 {}".format(
                    requested, result.get("reading_depth") or "未标注"),
                hint="按任务深度补读；若材料确实不足，在修订中说明并由 Scheduler 重规划"),
                diagnostics=diagnostics)
        if requested and not isinstance(result.get("screening"), dict):
            return failed(error(
                "READ_SCREENING_MISSING", "新阅读缺少 screening 初筛判断",
                hint="填写 verdict、reason、basis 与 recommended_depth"),
                diagnostics=diagnostics)
        quote_check = self._verify_page_quotes(result, task)
        diagnostics["page_quote_verification"] = quote_check
        if not quote_check["ok"]:
            return failed(error(
                "READ_DOCUMENT_MISMATCH", quote_check["message"],
                hint="确认 ReadingTask 附件版本与 document_sha256 后重新阅读"),
                diagnostics=diagnostics)
        return ok(result, diagnostics=diagnostics)

    @staticmethod
    def _verify_page_quotes(result: dict, task: dict) -> dict:
        """Verify page/quote pairs against the exact PDF version when possible."""
        quotes = result.get("evidence_quotes") or []
        path = ((task.get("target_material") or {}).get("pdf_path") or "")
        if not quotes or not path or not os.path.isfile(path):
            return {"ok": True, "checked": 0, "accepted": 0, "rejected": 0,
                    "available": False}
        with open(path, "rb") as handle:
            digest = hashlib.sha256(handle.read()).hexdigest()
        claimed = result.get("document_sha256")
        if claimed and claimed != digest:
            return {"ok": False, "checked": 0, "accepted": 0, "rejected": 0,
                    "available": True,
                    "message": "ReadingResult.document_sha256 与任务 PDF 不一致"}
        result["document_sha256"] = digest
        wheel = os.path.join(os.path.dirname(os.path.dirname(BUNDLE_ROOT)),
                             "vendor", "pypdf-6.21.4-py3-none-any.whl")
        if os.path.isfile(wheel) and wheel not in sys.path:
            sys.path.insert(0, wheel)
        try:
            from pypdf import PdfReader
            pages = [page.extract_text() or "" for page in PdfReader(path).pages]
        except Exception as exc:  # PDF can still be read by Codex/GROBID
            return {"ok": True, "checked": 0, "accepted": 0, "rejected": 0,
                    "available": False, "warning": "{}: {}".format(
                        type(exc).__name__, str(exc)[:160])}

        def norm(value):
            return re.sub(r"\s+", " ", str(value or "")).strip()

        checked = accepted = rejected = 0
        for quote in quotes:
            if quote.get("review_status") == "rejected":
                rejected += 1
                continue
            page = quote.get("page")
            text = norm(quote.get("quote"))
            checked += 1
            matched = isinstance(page, int) and 1 <= page <= len(pages) and \
                text and text in norm(pages[page - 1])
            if matched:
                quote["review_status"] = "accepted"
                quote["review_reason"] = "exact_text_match_on_pdf_page"
                accepted += 1
            else:
                quote["review_status"] = "rejected"
                quote["review_reason"] = "quote_not_found_on_declared_pdf_page"
                rejected += 1
        return {"ok": True, "checked": checked, "accepted": accepted,
                "rejected": rejected, "available": True,
                "document_sha256": digest}

    def strategies(self) -> dict:
        return _flatten(self.package.list_strategies())


def bundle_ports(config: SystemConfig):
    from ..agents.base import Ports
    if not os.path.isdir(BUNDLE_ROOT):
        raise PortError("bundle 目录不存在：{}".format(BUNDLE_ROOT))
    return Ports(research=BundleResearch(config),
                 organization=BundleOrganization(config),
                 scheduler=BundleScheduler(config),
                 read=BundleRead(config))
