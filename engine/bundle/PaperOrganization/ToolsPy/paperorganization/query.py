"""查询元件（元件：query）。

职责：从知识库读数据。Agent 消费知识库的主要入口。

设计：
    - 所有函数返回 ToolResult，便于 Agent 统一处理。
    - 空结果不是错误，但会带 warning（区分「查到了 0 条」和「出错了」）。
    - 提供「落点式」查询：按 paper/claim/media 取，而非要求 Agent 写 SQL。

主要函数：
    list_papers(cfg)                  论文清单
    get_paper(cfg, paper_id)          单篇论文（含 understanding）
    get_claims(cfg, paper_id)         某论文的论点
    get_assumptions(cfg, paper_id)    某论文的假设
    get_media(cfg, paper_id, kind)    某论文的图表公式（含坐标）
    get_media_detail(cfg, media_id)   单个 media（含完整坐标，供定位）
    get_spans(cfg, paper_id)          某论文的正文段落
    get_span(cfg, span_id)            单个 span 原文
    find_paper_by_media(cfg, media_id) 由 media 反查论文（定位链用）
    stats(cfg)                        知识库统计
"""
from __future__ import annotations

import hashlib
import json
import os
from typing import Any, Dict, List, Optional

from .errors import (ErrorCode, ToolResult, Timer, empty, fail, fail_from_exception, ok)
from .paths import KBConfig
from .schema import connect, safe_id


def _rows_to_dicts(rows) -> List[Dict[str, Any]]:
    return [dict(r) for r in rows]


def _empty_ok(data, what: str) -> ToolResult:
    """空结果：ok=True 但带 warning，明确区分「没有」和「出错」。"""
    if isinstance(data, list) and not data:
        return empty(data, warnings=["{}：查询到 0 条".format(what)])
    return ok(data)


# ---------------------------------------------------------------------------
# 论文
# ---------------------------------------------------------------------------

def list_papers(cfg: KBConfig, limit: Optional[int] = None) -> ToolResult:
    """列出论文（paper_id/title/year/reading_depth/method_family）。"""
    try:
        conn = connect(cfg)
        sql = ("SELECT paper_id,title,year,preprint_year,venue,reading_depth,"
               "method_family,citation_count,in_corpus FROM papers ORDER BY paper_id")
        if limit:
            sql += " LIMIT {}".format(int(limit))
        rows = _rows_to_dicts(conn.execute(sql))
        conn.close()
    except Exception as exc:
        return fail_from_exception(exc, location="query.list_papers",
                                   code=ErrorCode.DB_ERROR,
                                   message="查询 papers 失败：{}".format(exc))
    return _empty_ok(rows, "论文清单")


def get_paper(cfg: KBConfig, paper_id: str) -> ToolResult:
    """取单篇论文（含 raw JSON 里的 understanding）。"""
    try:
        conn = connect(cfg)
        row = conn.execute("SELECT * FROM papers WHERE paper_id=?", (paper_id,)).fetchone()
        conn.close()
    except Exception as exc:
        return fail_from_exception(exc, location="query.get_paper",
                                   code=ErrorCode.DB_ERROR,
                                   message="查询失败：{}".format(exc))
    if not row:
        return fail(ErrorCode.NOT_FOUND, "论文不存在：{}".format(paper_id),
                    location="query.get_paper",
                    hint="用 list_papers() 查看可用 paper_id")
    data = dict(row)
    # 附带 raw JSON 的 understanding（如果有）
    from . import writers
    r = writers.load_raw_json(cfg, paper_id)
    if r.ok:
        data["understanding"] = (r.data or {}).get("understanding") or {}
        data["metadata"] = (r.data or {}).get("metadata") or {}
    return ok(data)


# ---------------------------------------------------------------------------
# 论点 / 假设
# ---------------------------------------------------------------------------

def get_claims(cfg: KBConfig, paper_id: str) -> ToolResult:
    """取某论文的论点（claims 表）。"""
    try:
        conn = connect(cfg)
        rows = _rows_to_dicts(conn.execute(
            "SELECT claim_id,text,evidence,evidence_type,confidence,generated_by "
            "FROM claims WHERE paper_id=? ORDER BY claim_id", (paper_id,)))
        conn.close()
    except Exception as exc:
        return fail_from_exception(exc, location="query.get_claims",
                                   code=ErrorCode.DB_ERROR, message=str(exc))
    return _empty_ok(rows, "{} 的论点".format(paper_id))


def get_assumptions(cfg: KBConfig, paper_id: str) -> ToolResult:
    """取某论文的假设（assumptions 表）。"""
    try:
        conn = connect(cfg)
        rows = _rows_to_dicts(conn.execute(
            "SELECT assumption_id,text,explicit,evidence,confidence "
            "FROM assumptions WHERE paper_id=? ORDER BY assumption_id", (paper_id,)))
        conn.close()
    except Exception as exc:
        return fail_from_exception(exc, location="query.get_assumptions",
                                   code=ErrorCode.DB_ERROR, message=str(exc))
    return _empty_ok(rows, "{} 的假设".format(paper_id))


def get_claim_relations(cfg: KBConfig, paper_id: Optional[str] = None) -> ToolResult:
    """取论点间关系。paper_id 给定时，取涉及该论文 claims 的边。"""
    try:
        conn = connect(cfg)
        if paper_id:
            rows = _rows_to_dicts(conn.execute(
                """SELECT r.* FROM claim_relations r
                   JOIN claims c ON (r.src_claim=c.claim_id OR r.dst_claim=c.claim_id)
                   WHERE c.paper_id=? ORDER BY r.rel_id""", (paper_id,)))
        else:
            rows = _rows_to_dicts(conn.execute(
                "SELECT * FROM claim_relations ORDER BY rel_id"))
        conn.close()
    except Exception as exc:
        return fail_from_exception(exc, location="query.get_claim_relations",
                                   code=ErrorCode.DB_ERROR, message=str(exc))
    return _empty_ok(rows, "论点关系")


# ---------------------------------------------------------------------------
# media（图表公式，含坐标）
# ---------------------------------------------------------------------------

def get_media(cfg: KBConfig, paper_id: str,
              kind: Optional[str] = None) -> ToolResult:
    """取某论文的图表公式（含坐标摘要）。kind 可选 figure/table/formula。"""
    if kind and kind not in ("figure", "table", "formula"):
        return fail(ErrorCode.INVALID_ENUM, "kind 非法：{}".format(kind),
                    location="query.get_media",
                    hint="kind 只能是 figure / table / formula")
    try:
        conn = connect(cfg)
        if kind:
            rows = _rows_to_dicts(conn.execute(
                """SELECT media_id,kind,label,caption,section_index,page_first,
                          page_last,bbox,coords_n_raw_boxes,coords_missing_reason
                   FROM media WHERE paper_id=? AND kind=? ORDER BY media_id""",
                (paper_id, kind)))
        else:
            rows = _rows_to_dicts(conn.execute(
                """SELECT media_id,kind,label,caption,section_index,page_first,
                          page_last,bbox,coords_n_raw_boxes,coords_missing_reason
                   FROM media WHERE paper_id=? ORDER BY media_id""", (paper_id,)))
        conn.close()
    except Exception as exc:
        return fail_from_exception(exc, location="query.get_media",
                                   code=ErrorCode.DB_ERROR, message=str(exc))
    return _empty_ok(rows, "{} 的媒体对象".format(paper_id))


def get_media_detail(cfg: KBConfig, media_id: str) -> ToolResult:
    """取单个 media 的完整信息（含 bboxes_per_page / coords_raw，供精确定位）。

    这是「定位链」的关键一步：Agent 拿到 media_id 后调这里，
    得到 page_first + bbox，即可打开 PDF 对应页并高亮该区域。
    """
    try:
        conn = connect(cfg)
        row = conn.execute("SELECT * FROM media WHERE media_id=?", (media_id,)).fetchone()
        conn.close()
    except Exception as exc:
        return fail_from_exception(exc, location="query.get_media_detail",
                                   code=ErrorCode.DB_ERROR, message=str(exc))
    if not row:
        return fail(ErrorCode.NOT_FOUND, "media 不存在：{}".format(media_id),
                    location="query.get_media_detail",
                    hint="用 get_media(paper_id) 查看该论文有哪些 media_id")
    d = dict(row)
    # 解析 JSON 字段，方便调用方直接用
    for k in ("bboxes_per_page", "coords_raw"):
        if d.get(k):
            try:
                d[k] = json.loads(d[k])
            except Exception:
                pass
    # 标注可信度（：coords_n_raw_boxes==1 时坐标可疑）
    n = d.get("coords_n_raw_boxes")
    d["locate_hint"] = ("坐标可疑：GROBID 只识别到 1 个行框，可能只框住了图注"
                        if n == 1 else "坐标可用")
    return ok(d)


def find_paper_by_media(cfg: KBConfig, media_id: str) -> ToolResult:
    """由 media_id 反查所属论文（定位链用）。"""
    try:
        conn = connect(cfg)
        row = conn.execute(
            """SELECT m.media_id, m.paper_id, p.title, m.page_first, m.bbox
               FROM media m LEFT JOIN papers p ON m.paper_id=p.paper_id
               WHERE m.media_id=?""", (media_id,)).fetchone()
        conn.close()
    except Exception as exc:
        return fail_from_exception(exc, location="query.find_paper_by_media",
                                   code=ErrorCode.DB_ERROR, message=str(exc))
    if not row:
        return fail(ErrorCode.NOT_FOUND, "media 不存在：{}".format(media_id),
                    location="query.find_paper_by_media")
    return ok(dict(row))


# ---------------------------------------------------------------------------
# spans（正文段落）
# ---------------------------------------------------------------------------

def get_spans(cfg: KBConfig, paper_id: str, limit: Optional[int] = None) -> ToolResult:
    """取某论文的正文段落（span_map）。text 较长，注意上下文预算。"""
    try:
        conn = connect(cfg)
        sql = ("SELECT span_id,section,char_start,char_end,text FROM span_map "
               "WHERE paper_id=? ORDER BY char_start")
        if limit:
            sql += " LIMIT {}".format(int(limit))
        rows = _rows_to_dicts(conn.execute(sql, (paper_id,)))
        conn.close()
    except Exception as exc:
        return fail_from_exception(exc, location="query.get_spans",
                                   code=ErrorCode.DB_ERROR, message=str(exc))
    return _empty_ok(rows, "{} 的段落".format(paper_id))


def get_span(cfg: KBConfig, span_id: str) -> ToolResult:
    """取单个 span 的原文（Agent 决定细读时用）。"""
    try:
        conn = connect(cfg)
        row = conn.execute("SELECT * FROM span_map WHERE span_id=?", (span_id,)).fetchone()
        conn.close()
    except Exception as exc:
        return fail_from_exception(exc, location="query.get_span",
                                   code=ErrorCode.DB_ERROR, message=str(exc))
    if not row:
        return fail(ErrorCode.NOT_FOUND, "span 不存在：{}".format(span_id),
                    location="query.get_span")
    return ok(dict(row))


# ---------------------------------------------------------------------------
# Scheduler-facing snapshot API
# ---------------------------------------------------------------------------

def get_reading_state(cfg: KBConfig,
                      paper_ids: Optional[List[str]] = None) -> ToolResult:
    """返回调度所需的阅读状态，不暴露 Scheduler 对 SQLite 的依赖。

    ``completed`` 表示已经有非 ``unread`` 的阅读产物；它与工作流任务状态
    （pending/running/failed）是两个概念，后者由 Scheduler 自己维护。
    """
    try:
        conn = connect(cfg)
        sql = ("SELECT paper_id,reading_depth,gist,method_family "
               "FROM papers")
        params = []
        if paper_ids:
            marks = ",".join("?" for _ in paper_ids)
            sql += " WHERE paper_id IN ({})".format(marks)
            params = list(paper_ids)
        sql += " ORDER BY paper_id"
        rows = _rows_to_dicts(conn.execute(sql, params))
        conn.close()
    except Exception as exc:
        return fail_from_exception(exc, location="query.get_reading_state",
                                   code=ErrorCode.DB_ERROR, message=str(exc))
    for row in rows:
        row["completed"] = row.get("reading_depth") not in (None, "", "unread")
        row["knowledge_available"] = bool(row.get("gist"))
    return _empty_ok(rows, "阅读状态")


def list_reading_candidates(cfg: KBConfig, *, unread_only: bool = True,
                            in_corpus_only: bool = True) -> ToolResult:
    """列出 Scheduler 可以选择的论文及其稳定排序字段。"""
    try:
        conn = connect(cfg)
        clauses = []
        if unread_only:
            clauses.append("(reading_depth IS NULL OR reading_depth='' OR reading_depth='unread')")
        if in_corpus_only:
            clauses.append("in_corpus=1")
        sql = ("SELECT paper_id,title,year,preprint_year,venue,reading_depth,"
               "method_family,citation_count,role FROM papers")
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY COALESCE(preprint_year,year,9999),paper_id"
        rows = _rows_to_dicts(conn.execute(sql))
        conn.close()
    except Exception as exc:
        return fail_from_exception(exc, location="query.list_reading_candidates",
                                   code=ErrorCode.DB_ERROR, message=str(exc))
    return _empty_ok(rows, "阅读候选")


def get_paper_overview(cfg: KBConfig, paper_id: str) -> ToolResult:
    """返回适合 Scheduler 使用的 L0/L1 论文概览。"""
    paper = get_paper(cfg, paper_id)
    if not paper.ok:
        return paper
    data = paper.data
    return ok({
        "paper_id": data.get("paper_id"),
        "title": data.get("title"),
        "abstract": data.get("abstract"),
        "year": data.get("year"),
        "preprint_year": data.get("preprint_year"),
        "venue": data.get("venue"),
        "role": data.get("role"),
        "reading_depth": data.get("reading_depth"),
        "gist": data.get("gist"),
        "research_problem": data.get("research_problem"),
        "contribution": data.get("contribution"),
        "method_family": data.get("method_family"),
    })


def get_paper_understanding(cfg: KBConfig, paper_id: str,
                            aspects: Optional[List[str]] = None) -> ToolResult:
    """按方面读取已经落库的 L1/L2 知识。

    空 understanding 是成功的空结果，调用方不得把它解释为已经理解。
    """
    paper = get_paper(cfg, paper_id)
    if not paper.ok:
        return paper
    understanding = paper.data.get("understanding") or {}
    if aspects:
        understanding = {key: understanding.get(key) for key in aspects
                         if key in understanding}
    if not understanding:
        return ok({}, warnings=["论文尚无可用的结构化理解：{}".format(paper_id)])
    return ok(understanding)


def get_graph_snapshot(cfg: KBConfig) -> ToolResult:
    """返回一次一致读取的论文节点、原始引用边和语义关系边。

    ``snapshot_id`` 由规范化内容计算；相同知识状态得到相同 ID，可用于
    ReadingTask、PlanningDecision 和运行日志之间的一致性绑定。
    """
    conn = None
    try:
        conn = connect(cfg)
        conn.execute("BEGIN")
        papers = _rows_to_dicts(conn.execute(
            "SELECT paper_id,title,year,preprint_year,reading_depth,method_family,role "
            "FROM papers ORDER BY paper_id"))
        citations = _rows_to_dicts(conn.execute(
            "SELECT from_paper,to_paper,to_raw,internal,source FROM citations "
            "ORDER BY from_paper,to_raw"))
        relations = _rows_to_dicts(conn.execute(
            "SELECT rel_id,src_paper,dst_paper,kind,axis,evidence,confidence,generated_by "
            "FROM paper_relations ORDER BY rel_id"))
        claims = _rows_to_dicts(conn.execute(
            "SELECT claim_id,paper_id,text,evidence,evidence_type,confidence,generated_by "
            "FROM claims ORDER BY claim_id"))
        claim_relations = _rows_to_dicts(conn.execute(
            "SELECT rel_id,src_claim,dst_claim,kind,polarity,evidence,confidence,generated_by "
            "FROM claim_relations ORDER BY rel_id"))
        conn.rollback()  # read transaction only
    except Exception as exc:
        return fail_from_exception(exc, location="query.get_graph_snapshot",
                                   code=ErrorCode.DB_ERROR, message=str(exc))
    finally:
        # Missing/corrupt schemas fail after sqlite has opened successfully.
        # Without a finally block those common diagnostic paths leaked the
        # connection until GC, retaining descriptors and sometimes file locks.
        if conn is not None:
            conn.close()
    payload = {"papers": papers, "citations": citations,
               "paper_relations": relations, "claims": claims,
               "claim_relations": claim_relations}
    for paper in payload["papers"]:
        pdf_path = os.path.join(cfg.pdf_dir, safe_id(paper["paper_id"]) + ".pdf")
        paper["pdf_available"] = os.path.isfile(pdf_path)
        paper["pdf_path"] = pdf_path if paper["pdf_available"] else None
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"))
    payload["snapshot_id"] = "org_" + hashlib.sha256(
        canonical.encode("utf-8")).hexdigest()[:16]
    return ok(payload)


def get_related_knowledge(cfg: KBConfig, paper_id: str, *,
                          max_papers: int = 5,
                          include_claims: bool = True) -> ToolResult:
    """返回与目标相邻、且已经实际读过的知识候选。

    本函数只负责可追溯检索；是否把某条知识交给 Reader 由 Scheduler 的
    ContextSelector 决定。
    """
    try:
        conn = connect(cfg)
        neighbors = _rows_to_dicts(conn.execute(
            """SELECT DISTINCT p.paper_id,p.title,p.reading_depth,p.gist,
                              p.research_problem,p.contribution,p.method_family
               FROM papers p
               JOIN (
                 SELECT to_paper AS neighbor FROM citations
                  WHERE from_paper=? AND internal=1
                 UNION
                 SELECT from_paper AS neighbor FROM citations
                  WHERE to_paper=? AND internal=1
                 UNION
                 SELECT dst_paper AS neighbor FROM paper_relations WHERE src_paper=?
                 UNION
                 SELECT src_paper AS neighbor FROM paper_relations WHERE dst_paper=?
               ) n ON n.neighbor=p.paper_id
               WHERE p.reading_depth IS NOT NULL AND p.reading_depth!='unread'
               ORDER BY p.paper_id LIMIT ?""",
            (paper_id, paper_id, paper_id, paper_id, int(max_papers))))
        for item in neighbors:
            if include_claims:
                item["claims"] = _rows_to_dicts(conn.execute(
                    "SELECT claim_id,text,evidence,evidence_type,confidence "
                    "FROM claims WHERE paper_id=? ORDER BY claim_id",
                    (item["paper_id"],)))
        conn.close()
    except Exception as exc:
        return fail_from_exception(exc, location="query.get_related_knowledge",
                                   code=ErrorCode.DB_ERROR, message=str(exc))
    return _empty_ok(neighbors, "相关已读知识")


# ---------------------------------------------------------------------------
# 统计
# ---------------------------------------------------------------------------

def stats(cfg: KBConfig) -> ToolResult:
    """知识库统计（各表行数 + 质量指标），供 Agent 判断库的健康度。"""
    try:
        conn = connect(cfg)
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")]
        counts = {t: conn.execute("SELECT COUNT(*) FROM {}".format(t)).fetchone()[0]
                  for t in tables}
        # 质量指标
        quality = {
            # ★ 摘要覆盖（曾是硬编码 None 的占位，现按 papers.abstract 真值统计）
            "papers_with_abstract": conn.execute(
                "SELECT COUNT(*) FROM papers WHERE abstract IS NOT NULL AND abstract != ''").fetchone()[0],
            "papers_read": conn.execute(
                "SELECT COUNT(*) FROM papers WHERE reading_depth != 'unread'").fetchone()[0],
            "media_with_coords": conn.execute(
                "SELECT COUNT(*) FROM media WHERE bbox IS NOT NULL").fetchone()[0],
            "media_total": counts.get("media", 0),
            "claims_with_evidence": conn.execute(
                "SELECT COUNT(*) FROM claims WHERE evidence IS NOT NULL AND evidence != ''").fetchone()[0],
            "internal_citations": conn.execute(
                "SELECT COUNT(*) FROM citations WHERE internal=1").fetchone()[0],
        }
        conn.close()
    except Exception as exc:
        return fail_from_exception(exc, location="query.stats",
                                   code=ErrorCode.DB_ERROR, message=str(exc))
    return ok({"counts": counts, "quality": quality})
