#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sources.py —— STEP 3 多源检索 + STEP 4 元数据校正 + STEP 6 引用扩展

定位
----
把 ExamplePy 里那批"单接口示例"组装成**流程需要的样子**：
    STEP 3  三个检索源并行捞候选          → search_all()
    STEP 4  Crossref / Unpaywall 补全校正  → enrich_crossref() / resolve_oa()
    STEP 6  顺着引用网络扩展               → expand_citations()

为什么不直接复用 ExamplePy 里的 to_internal_paper / write_paper_record
------------------------------------------------------------------
那些函数产出的是"那个模块自己视角的统一结构"（键名、嵌套都不一样，
例如 OpenAlex 那只用 `open_access` 而不用 `full_text`）。
ProcessPy 有**唯一的** Paper 模型（model.py），必须由本层适配，
否则每接一个源就要在下游加 if。适配的成本集中在这里，收益是全流程只用一套字段。

容错原则
--------
**单源失败不能拖垮整轮检索。** 每个源都是 try/except 包住，失败记进
返回的 errors 列表并继续。原因：S2 无 key 必然失败、OpenAlex 可能限流，
但 arXiv 通常是通的 —— 让一个源挂掉就整体失败，等于主动放弃已有的召回。

函数索引
─────────────────────────────────────────────────────────────────────────────
  [转换]  paper_from_arxiv / paper_from_openalex / paper_from_s2
  [STEP3] search_arxiv / search_openalex / search_semantic_scholar
  [STEP3] search_all(plan, cfg)              三源汇总 → (papers, stats)
  [STEP4] enrich_crossref(papers, cfg)       DOI/出版信息校正
  [STEP4] resolve_oa(papers, cfg)            Unpaywall DOI → OA 位置
  [STEP6] expand_citations(papers, cfg)      引用网络扩展
  [工具]  _openalex_id_of(paper)             从 identity 反推 OpenAlex 短 ID
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import os
import re
import sys
import time
from typing import Dict, List, Optional, Tuple

# 接入 ExamplePy（_bootstrap 在导入时就会把路径挂好）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _bootstrap import ensure_importable          # noqa: E402
ensure_importable()

import arxiv as ex_arxiv                          # noqa: E402
import openalex as ex_openalex                    # noqa: E402
import crossref as ex_crossref                    # noqa: E402
import unpaywall as ex_unpaywall                  # noqa: E402
import semantic_scholar as ex_s2                  # noqa: E402

from model import Paper, new_paper, normalize_doi, normalize_arxiv  # noqa: E402


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


# 引用扩展时向 OpenAlex 要的字段。
# ★ 这个列表必须够宽：扩展是**一次请求返回多条**，把字段列全几乎不增加成本
#   （同一个请求、响应大一点），但漏了字段就得为每条候选再发一次请求补全。
#   实测踩到的坑（第一版只选了 5 个字段）：
#     漏 abstract_inverted_index → 扩展进来的 19 篇**全部没有摘要**，
#                                   而摘要正是 STEP5 评分的输入之一
#     漏 referenced_works         → 扩展进来的论文**没有引用边**，
#                                   引用图只有 23/60 篇有数据，图基本是断的
EXPAND_SELECT = ("id,doi,title,publication_year,publication_date,cited_by_count,"
                 "referenced_works,referenced_works_count,abstract_inverted_index,"
                 "type,authorships,primary_location,open_access,best_oa_location,"
                 "topics,language,is_retracted")


def response_ok(r: dict, source: str) -> Tuple[bool, str]:
    """判断一次检索响应是否**真的**成功，返回 (ok, 原因)。

    ★ 为什么不能只看 HTTP 状态码（实测缺陷 G7）：
      "HTTP 200" 只说明**服务器应答了**，不代表内容是我们要的。
      常见情形：网关/限流页面以 200 返回一段 HTML（如
      `<html>502 Bad Gateway</html>`），或 API 在 200 的 body 里塞
      `{"error": "..."}`。

      第一版 `search_openalex()` 只写 `if r.get("status") != 200`，
      于是这种情况被当成"这篇查询没有结果" —— `errors` 为空、`raw=0`，
      上层 `run()` 据此判该源 `ok`、整体 `status=done`。
      **失败伪装成成功**是最难发现的一类 bug：报告上一切正常。

      这里统一做三层判定：
        1. HTTP 状态码必须 200
        2. 响应里不能带 error 字段（底层模块已把非法 JSON/HTML 记进这里）
        3. 结构必须像样（results/entries 至少是个 list）
    """
    if not isinstance(r, dict):
        return False, "响应不是 dict（{}）".format(type(r).__name__)
    st = r.get("status")
    if st != 200:
        return False, "HTTP {} {}".format(st, str(r.get("error"))[:120])
    # 第 2 层：底层模块把"200 但内容非法"记在 error 里（可能非空字符串）
    err = (r.get("error") or "").strip()
    if err:
        return False, "HTTP 200 但响应体含错误：{}".format(err[:140])
    # 第 3 层：结构校验 —— 找一个"结果列表"字段并确认它是 list。
    # ⚠️ 这里必须容忍**合法的空集**（test.mdE3：零 references 是正常空集）。
    #    第一版把"没有 results/entries/papers/data 这些键"也判成失败，
    #    于是 `get_references()` 返回的 `{"works": []}` 被当成错误 ——
    #    把"这篇论文没有参考文献"错报成"接口失败"。
    #    键名清单要覆盖各端点的实际返回：
    #      results  /works 检索
    #      entries  arXiv Atom
    #      papers   S2
    #      works    OpenAlex get_references / get_citations
    #      data     通用兜底
    LIST_KEYS = ("results", "entries", "papers", "works", "data", "items")
    for key in LIST_KEYS:
        if key in r:
            if isinstance(r[key], list):
                return True, ""       # 空 list 也是**成功**（合法空集）
            return False, "字段 {} 不是列表（{}）".format(key, type(r[key]).__name__)
    # 一个结果列表键都没有：可能是 __init__ 未遍历，但对"已实现的端点"而言
    # 说明响应形状超出预期 —— 如实报错而不是当成空集。
    return False, "响应里没有任何结果列表字段（键：{}）".format(
        ",".join(sorted(r.keys()))[:100])


def _email() -> str:
    """polite pool 邮箱（ExamplePy 的 Keys.txt 里读）。"""
    try:
        import _common
        return _common.get_key("PAPERSEARCH_EMAIL")
    except Exception:
        return ""


# =============================================================================
# 一、各源 → Paper 的转换
# =============================================================================

def paper_from_arxiv(e: dict, query: str = "") -> Paper:
    """arXiv Export API 的一条 entry → Paper。

    字段映射（arXiv → 统一模型）：
        arxiv_id → identity.arxiv_id     （带版本号，存进 comment 保留）
        summary  → abstract
        published/updated → metadata.published_date / 年份
        comment  → metadata.comment       （常含 "Accepted at NeurIPS 2024"）
        journal_ref → metadata.venue      （正式发表信息，有的话很值钱）
        categories → metadata.categories
        pdf_url  → full_text.pdf_url      （arXiv 是全文第一优先级，）
    """
    raw_id = str(e.get("arxiv_id") or "")
    p = new_paper({"arxiv_id": raw_id}, e.get("title", ""))
    p.identity["arxiv_id"] = normalize_arxiv(raw_id) or ""
    # ★ 保留 arXiv 版本号（实测缺陷 G3）
    #   identity.arxiv_id 与 paper_id 都刻意**去掉**版本号（v1/v2 是同一篇，
    #   不去掉会让同一篇在引用图里分裂成两个节点）。但"这是第几版"本身是信息，
    #   做版本演化分析（"某方法在第几版被修正"）时要用。
    #   第一版把版本号直接丢了，只有在 comment 自由文本里可能残留，等于没有。
    m = re.search(r"(v\d+)\s*$", raw_id.strip())
    if m:
        p.metadata["arxiv_version"] = m.group(1)
    p.abstract = (e.get("summary") or "").strip()
    m = p.metadata
    m["authors"] = list(e.get("authors") or [])
    m["categories"] = list(e.get("categories") or [])
    m["published_date"] = (e.get("published") or "")[:10]
    y = (e.get("published") or "")[:4]
    m["year"] = int(y) if y.isdigit() else None
    m["venue"] = (e.get("journal_ref") or "").strip()
    m["comment"] = (e.get("comment") or "").strip()
    m["type"] = "preprint"
    if e.get("doi"):
        p.identity["doi"] = normalize_doi(e["doi"])
    p.full_text["pdf_url"] = e.get("pdf_url") or ""
    p.add_found_by("arxiv", "keyword_search", query, at=_now())
    return p


def paper_from_openalex(w: dict, query: str = "") -> Paper:
    """OpenAlex 的一条 work → Paper。

    ★ OpenAlex 的摘要不是字符串，而是 `abstract_inverted_index`（倒排索引），
      必须用 ex_openalex.reconstruct_abstract 还原；不还原的话后面 STEP 5 的
      "摘要相关度"这一项就永远是 0。
    """
    wid = (w.get("id") or "").rstrip("/").rsplit("/", 1)[-1]
    p = new_paper({"openalex_id": wid}, w.get("title") or w.get("display_name") or "")
    p.identity["openalex_id"] = wid
    if w.get("doi"):
        p.identity["doi"] = normalize_doi(w["doi"])
    p.abstract = ex_openalex.reconstruct_abstract(w.get("abstract_inverted_index"))

    src = (w.get("primary_location") or {}).get("source") or {}
    m = p.metadata
    m["authors"] = [a.get("author", {}).get("display_name")
                    for a in (w.get("authorships") or []) if a.get("author")]
    m["affiliations"] = [i.get("display_name")
                         for a in (w.get("authorships") or [])
                         for i in (a.get("institutions") or [])]
    m["year"] = w.get("publication_year")
    m["published_date"] = (w.get("publication_date") or "")[:10]
    m["venue"] = src.get("display_name") or ""
    m["venue_type"] = src.get("type") or ""
    m["citation_count"] = w.get("cited_by_count")
    m["topics"] = [t.get("display_name") for t in (w.get("topics") or []) if t.get("display_name")]
    m["keywords"] = [k.get("display_name") for k in (w.get("keywords") or [])
                     if k.get("display_name")]
    m["type"] = w.get("type") or ""
    m["language"] = w.get("language") or ""
    m["is_retracted"] = w.get("is_retracted")

    best = w.get("best_oa_location") or {}
    p.full_text["pdf_url"] = best.get("pdf_url") or ""
    p.full_text["oa_status"] = (w.get("open_access") or {}).get("oa_status") or ""
    p.full_text["version"] = best.get("version") or ""
    p.full_text["license"] = best.get("license") or ""
    p.full_text["alt_locations"] = [
        {"url": l.get("pdf_url") or l.get("landing_page_url"),
         "host": l.get("host_type"), "version": l.get("version")}
        for l in (w.get("locations") or []) if l.get("pdf_url")]

    refs = w.get("referenced_works") or []
    p.citation_graph["references"] = [r.rstrip("/").rsplit("/", 1)[-1] for r in refs]
    p.citation_graph["reference_count"] = len(refs)
    p.citation_graph["source"] = "openalex"
    p.add_found_by("openalex", "keyword_search", query, at=_now())
    return p


def paper_from_s2(sp: dict, query: str = "") -> Paper:
    """Semantic Scholar 的一条 paper → Paper。

    ★ S2 的 `externalIds` 是**跨库对齐的桥梁**：它同时带 DOI / ArXiv / CorpusId，
      所以拿它能把 S2 的引用关系挂到 arXiv/OpenAlex 记录上。
      没有这一步，S2 的 citation context 就没法归到语料库里的论文上。
    """
    ext = sp.get("externalIds") or {}
    arx = ext.get("ArXiv") or ""
    p = new_paper({"doi": ext.get("DOI") or "", "arxiv_id": arx,
                   "s2_id": sp.get("paperId") or ""}, sp.get("title") or "")
    p.identity["s2_id"] = sp.get("paperId") or ""
    if ext.get("DOI"):
        p.identity["doi"] = normalize_doi(ext["DOI"])
    if arx:
        p.identity["arxiv_id"] = normalize_arxiv(arx)
    p.abstract = sp.get("abstract") or ""
    m = p.metadata
    m["authors"] = [a.get("name") for a in (sp.get("authors") or []) if a.get("name")]
    m["year"] = sp.get("year")
    m["venue"] = sp.get("venue") or ""
    m["citation_count"] = sp.get("citationCount")
    m["influential_citation_count"] = sp.get("influentialCitationCount")
    oa = sp.get("openAccessPdf") or {}
    if oa.get("url"):
        p.full_text["pdf_url"] = p.full_text.get("pdf_url") or oa["url"]
    p.add_found_by("semantic_scholar", "keyword_search", query, at=_now())
    return p


# =============================================================================
# 二、STEP 3：三个检索源
# =============================================================================

def search_arxiv(queries: List[str], cfg) -> Tuple[List[Paper], dict]:
    """arXiv Export API 多检索式检索。

    注意（来自 ExamplePy 实测）：
      - 官方要求请求间隔 ~3 秒，arxiv.py 内部已强制；
      - 深分页在约 10000 处会 500，这里每式只取 per_source_limit（≤60），不会碰到；
      - 该端点偶发 406，arxiv.py 已内置 curl 兜底。
    """
    out, errs, total_hits = [], [], 0
    for q in queries:
        # ★ per-query 异常隔离（实测缺陷 G9）：
        #   第一版循环体没有 try —— 第 2 条查询抛 ParseError 时，
        #   第 1 条查询**已经拿到的 papers 随异常一起丢失**
        #   （它们还在局部变量 out 里，但函数没返回）。
        #   test.mdE5 明确要求"已有结果保留"。
        #   逐条兜住异常，坏的那条记进 errors，其余照常返回。
        try:
            r = ex_arxiv.search(q, max_results=cfg.per_source_limit, email=_email())
            _ok, _why = response_ok(r, "arxiv")
            if not _ok or not r.get("ok", True):
                errs.append("arxiv[{}]: {}".format(q[:40], _why or str(r.get("error"))[:120]))
                continue
            total_hits += int(r.get("total") or 0)
            for e in r.get("entries") or []:
                try:
                    out.append(paper_from_arxiv(e, q))
                except Exception as exc:
                    errs.append("arxiv[{}] 单条记录转换失败：{}: {}".format(
                        q[:30], type(exc).__name__, str(exc)[:80]))
        except Exception as exc:
            errs.append("arxiv[{}]: {}: {}".format(q[:40], type(exc).__name__, str(exc)[:120]))
        if cfg.sleep_between_calls:
            time.sleep(cfg.sleep_between_calls)
    return out, {"source": "arxiv", "queries": len(queries), "raw": len(out),
                 "total_hits": total_hits, "errors": errs,
                 "partial": bool(errs and out)}


def search_openalex(queries: List[str], cfg) -> Tuple[List[Paper], dict]:
    """OpenAlex 多检索式检索（主索引，）。

    ⚠️ 匿名调用 search 会随机 429（Retry-After 30~35s），所以必须配
       OPENALEX_API_KEY（Keys.txt）。没有 key 时这里会记录错误并返回空，
       由 pipeline 决定是否需要降级到 filter 端点。
    """
    out, errs, total_hits = [], [], 0
    oa = ex_openalex.OpenAlex(email=_email())
    for q in queries:
        try:                                  # 同上：per-query 异常隔离（G9）
            r = oa.search(q, per_page=cfg.per_source_limit)
            _ok, _why = response_ok(r, "openalex")
            if not _ok:
                errs.append("openalex[{}]: {}".format(q[:40], _why))
                continue
            total_hits += int(r.get("count") or 0)
            for w in r.get("results") or []:
                try:
                    out.append(paper_from_openalex(w, q))
                except Exception as exc:
                    errs.append("openalex[{}] 单条记录转换失败：{}: {}".format(
                        q[:30], type(exc).__name__, str(exc)[:80]))
        except Exception as exc:
            errs.append("openalex[{}]: {}: {}".format(q[:40], type(exc).__name__, str(exc)[:120]))
        if cfg.sleep_between_calls:
            time.sleep(cfg.sleep_between_calls)
    return out, {"source": "openalex", "queries": len(queries), "raw": len(out),
                 "total_hits": total_hits, "errors": errs,
                 "partial": bool(errs and out)}


def search_semantic_scholar(queries: List[str], cfg) -> Tuple[List[Paper], dict]:
    """Semantic Scholar 多检索式检索。

    **无 key 时不会发请求**（semantic_scholar.py 会直接返回 status=0 +
    missing_credentials），这里据此把该源标记为 unavailable 而不是 failed ——
    两者对使用者的含义完全不同：前者是"没配 key"，后者是"服务出问题了"。
    """
    out, errs = [], []
    s2 = ex_s2.SemanticScholar(email=_email())
    if not s2.available():
        return out, {"source": "semantic_scholar", "raw": 0,
                     "available": False,
                     "note": "未配置 S2_API_KEY，已跳过（该源的 citation context 暂不可用）",
                     "errors": []}
    for q in queries:
        try:                                  # 同上：per-query 异常隔离（G9）
            r = s2.search_papers(q, limit=min(cfg.per_source_limit, 100))
            _ok, _why = response_ok(r, "s2")
            if not _ok:
                errs.append("s2[{}]: {}".format(q[:40], _why))
                continue
            for sp in r.get("papers") or []:
                try:
                    out.append(paper_from_s2(sp, q))
                except Exception as exc:
                    errs.append("s2[{}] 单条记录转换失败：{}: {}".format(
                        q[:30], type(exc).__name__, str(exc)[:80]))
        except Exception as exc:
            errs.append("s2[{}]: {}: {}".format(q[:40], type(exc).__name__, str(exc)[:120]))
        if cfg.sleep_between_calls:
            time.sleep(cfg.sleep_between_calls)   # S2 限 1 req/s，内部已节流
    return out, {"source": "semantic_scholar", "queries": len(queries),
                 "raw": len(out), "available": True, "errors": errs,
                 "partial": bool(errs and out)}


def paper_from_crossref(work: dict, query: str = "") -> Paper:
    """Crossref work → canonical Paper; keeps source provenance and references."""
    title = (work.get("title") or [""])[0]
    p = new_paper({"doi": work.get("DOI") or ""}, title)
    p.abstract = re.sub(r"<[^>]+>", " ", work.get("abstract") or "").strip()
    issued = (work.get("issued") or {}).get("date-parts") or [[None]]
    p.metadata.update({
        "authors": [" ".join(filter(None, (a.get("given"), a.get("family"))))
                    for a in (work.get("author") or [])],
        "year": issued[0][0],
        "venue": (work.get("container-title") or [""])[0],
        "publisher": work.get("publisher") or "",
        "type": work.get("type") or "",
        "citation_count": work.get("is-referenced-by-count"),
    })
    p.citation_graph["references"] = [
        normalize_doi(row.get("DOI")) for row in (work.get("reference") or [])
        if row.get("DOI")]
    p.citation_graph["reference_count"] = work.get("references-count") or len(
        p.citation_graph["references"])
    p.citation_graph["source"] = "crossref"
    for link in work.get("link") or []:
        if link.get("content-type") == "application/pdf" and link.get("URL"):
            p.full_text["pdf_url"] = link["URL"]
            break
    p.add_found_by("crossref", "keyword_search", query, at=_now())
    return p


def search_crossref(queries: List[str], cfg) -> Tuple[List[Paper], dict]:
    """Bounded free Crossref discovery for DOI literature missed by preprint indexes."""
    out, errs, total_hits = [], [], 0
    cr = ex_crossref.Crossref(email=_email())
    limit = max(0, int(getattr(cfg, "crossref_query_limit", 3)))
    for q in queries[:limit]:
        try:
            result = cr.query_works(q.replace('"', ""),
                                    rows=min(int(cfg.per_source_limit), 20),
                                    select=ex_crossref.DEFAULT_SELECT)
            if result.get("status") != 200:
                errs.append("crossref[{}]: {}".format(
                    q[:40], str(result.get("error") or result.get("status"))[:120]))
                continue
            total_hits += int(result.get("total") or 0)
            for work in result.get("items") or []:
                try:
                    out.append(paper_from_crossref(work, q))
                except Exception as exc:
                    errs.append("crossref 单条记录转换失败：{}".format(str(exc)[:100]))
        except Exception as exc:
            errs.append("crossref[{}]: {}: {}".format(
                q[:40], type(exc).__name__, str(exc)[:120]))
    return out, {"source": "crossref", "queries": min(len(queries), limit),
                 "raw": len(out), "total_hits": total_hits, "errors": errs,
                 "partial": bool(errs and out)}


def search_all(plan: dict, cfg) -> Tuple[List[Paper], List[dict]]:
    """STEP 3 主入口：按配置跑所有可用的源，汇总候选。

    参数
    ----
    plan : query.build_queries() 的产物
    cfg  : PipelineConfig

    返回 (papers, per_source_stats)

    ★ 单源失败不影响其它源；每个源的结果与错误都单独汇报，
      这样报告里能明确写出"本轮 S2 没参与"，而不是让人误以为都搜了。
    """
    q = plan["queries"]
    papers: List[Paper] = []
    stats: List[dict] = []

    if cfg.use_arxiv:
        try:
            ps, st = search_arxiv(q.get("arxiv") or [], cfg)
            papers += ps
            stats.append(st)
        except Exception as exc:
            stats.append({"source": "arxiv", "raw": 0, "errors": [str(exc)[:200]]})
    if cfg.use_openalex:
        try:
            ps, st = search_openalex(q.get("openalex") or [], cfg)
            papers += ps
            stats.append(st)
        except Exception as exc:
            stats.append({"source": "openalex", "raw": 0, "errors": [str(exc)[:200]]})
    if cfg.use_semantic_scholar:
        try:
            ps, st = search_semantic_scholar(q.get("s2") or [], cfg)
            papers += ps
            stats.append(st)
        except Exception as exc:
            stats.append({"source": "semantic_scholar", "raw": 0,
                          "errors": [str(exc)[:200]]})
    if getattr(cfg, "use_crossref_search", False):
        try:
            ps, st = search_crossref(q.get("openalex") or [], cfg)
            papers += ps
            stats.append(st)
        except Exception as exc:
            stats.append({"source": "crossref", "raw": 0,
                          "errors": [str(exc)[:200]]})
    return papers, stats


# =============================================================================
# 三、STEP 4：Crossref 元数据校正
# =============================================================================

def enrich_crossref(papers: List[Paper], cfg, only_missing: bool = True,
                    limit: int = 40) -> dict:
    """用 Crossref 校正出版信息。

    参数
    ----
    only_missing : True = 只处理"缺 DOI 或缺 venue 或缺年份"的论文（省调用）
    limit        : 最多处理多少篇。**Crossref 是逐篇查询，必须限量** ——
的定位是"身份确认"，不是批量补数据。

    补什么：
      - 没有 DOI 的 → 用 title(+首作者) 反查，**只在标题高度一致时采纳**
      - 有 DOI 的   → 取正式标题/年份/venue/license

    ⚠️ 关键取舍：**绝不因为"分数高"就自动合并**。
       Crossref 的 title 匹配返回的是候选列表，这里只采纳
       "归一化标题完全相同"或"相似度 ≥ cfg.title_similarity"且年份一致的候选。
       合并错了比不合并危害大得多（见 dedup.py 的说明）。
    """
    cr = ex_crossref.Crossref(email=_email())
    done = adopted = 0
    errs = []
    for p in papers:
        if done >= limit:
            break
        has_doi = bool(p.identity.get("doi"))
        if only_missing and has_doi and p.metadata.get("venue") and p.metadata.get("year"):
            continue
        done += 1
        try:
            if has_doi:
                r = cr.get_work_by_doi(p.identity["doi"])
                w = r.get("work")
                if r.get("status") == 200 and w:
                    _apply_crossref(p, w)
                    adopted += 1
            else:
                title = p.metadata.get("title") or ""
                if not title:
                    continue
                authors = p.metadata.get("authors") or []
                r = cr.match_title(title, author=(authors[0].split()[-1] if authors else ""),
                                   rows=3)
                for item in (r.get("items") or []):
                    if _crossref_is_same(p, item, cfg):
                        _apply_crossref(p, item)
                        adopted += 1
                        break
        except Exception as exc:
            errs.append("crossref[{}]: {}".format(p.paper_id[:40], str(exc)[:120]))
        if cfg.sleep_between_calls:
            time.sleep(cfg.sleep_between_calls)
    return {"source": "crossref", "checked": done, "adopted": adopted, "errors": errs}


def _crossref_is_same(paper: Paper, item: dict, cfg) -> bool:
    """判断 Crossref 候选与本地记录是否**同一篇**（保守规则）。

    三条必须同时成立（缺一不可）：
      1. 标题相似度 >= cfg.title_similarity
      2. 年份一致（两边都有时）
      3. 首作者姓氏一致（两边都有时）
    """
    from model import title_similarity, normalize_author
    ct = (item.get("title") or [""])[0]
    if title_similarity(paper.metadata.get("title", ""), ct) < cfg.title_similarity:
        return False
    cy = ((item.get("issued") or {}).get("date-parts") or [[None]])[0][0]
    py = paper.metadata.get("year")
    if cy and py and int(cy) != int(py):
        return False
    ca = item.get("author") or []
    pa = paper.metadata.get("authors") or []
    if ca and pa:
        cn = normalize_author("{} {}".format(ca[0].get("given", ""), ca[0].get("family", "")))
        if cn and cn != normalize_author(pa[0]):
            return False
    return True


def _apply_crossref(p: Paper, w: dict) -> None:
    """把 Crossref 的字段填进 Paper（只补空 / 只做规范化，不覆盖已有值）。"""
    if w.get("DOI") and not p.identity.get("doi"):
        p.identity["doi"] = normalize_doi(w["DOI"])
    m = p.metadata
    title = (w.get("title") or [""])[0]
    if title and not m.get("title"):
        m["title"] = title
    m["venue"] = m.get("venue") or (w.get("container-title") or [""])[0]
    y = ((w.get("issued") or {}).get("date-parts") or [[None]])[0][0]
    if y and not m.get("year"):
        m["year"] = int(y)
    m["type"] = m.get("type") or (w.get("type") or "")
    if not m.get("authors"):
        m["authors"] = [" ".join(filter(None, [a.get("given"), a.get("family")]))
                        for a in (w.get("author") or [])]
    lic = [l.get("URL") for l in (w.get("license") or []) if l.get("URL")]
    if lic and not p.full_text.get("license"):
        p.full_text["license"] = lic[0]
    p.add_found_by("crossref", "entity_resolution", at=_now())


# =============================================================================
# 四、STEP 4：Unpaywall OA 定位
# =============================================================================

def resolve_oa(papers: List[Paper], cfg, limit: int = 60,
               only_missing: bool = True) -> dict:
    """对有 DOI 但还没有 PDF 直链的论文查 Unpaywall。

    only_missing : True（默认）= 跳过已有 pdf_url 的论文，省配额。
                   False = **连已有直链的也查**。
                   什么时候需要 False：已有直链**下载失败**了。
                   "有 URL" ≠ "能下载"（实测常见：OpenAlex 给的仓库链接 404）。
                   fulltext.acquire_pdf 在候选全灭后会用 False 再查一次作回退。

    ⚠️ Unpaywall 的 search 接口已退役（实测 410），只能 DOI→OA，
       所以没有 DOI 的论文这里直接跳过（这是接口能力边界，不是 bug）。
    """
    up = ex_unpaywall.Unpaywall(email=_email())
    checked = found = 0
    errs = []
    for p in papers:
        if checked >= limit:
            break
        doi = p.identity.get("doi")
        if not doi:
            continue
        if only_missing and p.full_text.get("pdf_url"):
            continue
        checked += 1
        r = up.get_by_doi(doi)
        if r.get("status") != 200:
            if r.get("status") not in (404,):
                errs.append("unpaywall[{}]: {}".format(doi[:40], str(r.get("error"))[:100]))
            continue
        if r.get("best_pdf"):
            p.full_text["pdf_url"] = r["best_pdf"]
            found += 1
        p.full_text["oa_status"] = p.full_text.get("oa_status") or (r.get("oa_status") or "")
        p.full_text["version"] = p.full_text.get("version") or (r.get("version") or "")
        p.full_text["license"] = p.full_text.get("license") or (r.get("license") or "")
        for loc in (r.get("all_locations") or [])[:5]:
            p.full_text.setdefault("alt_locations", []).append(
                {"url": loc.get("pdf") or loc.get("url"), "host": loc.get("host"),
                 "version": loc.get("version")})
        p.add_found_by("unpaywall", "fulltext_resolution", at=_now())
        if cfg.sleep_between_calls:
            time.sleep(cfg.sleep_between_calls)
    return {"source": "unpaywall", "checked": checked, "found_pdf": found, "errors": errs}


# =============================================================================
# 五、STEP 6：引用网络扩展
# =============================================================================

def _openalex_id_of(p: Paper) -> str:
    """从 identity 取 OpenAlex 短 ID（W...）。没有就返回 ""。"""
    v = (p.identity.get("openalex_id") or "").strip()
    return v.rstrip("/").rsplit("/", 1)[-1] if v else ""


def expand_citations(papers: List[Paper], cfg) -> Tuple[List[Paper], dict]:
    """STEP 6：对排序最高的若干篇做引用网络扩展（test.md STEP4）。

    参数
    ----
    papers : 已过 STEP 5 排序的论文（取前 cfg.expand_top_n 篇作种子）
    cfg    : expand_refs_per_paper / expand_cites_per_paper / use_* / expansion_rounds

    返回 (新增候选, 统计 dict)

    ★ 设计要点

    1. 只取"排序最高"的少数几篇做种子。
的例子说明扩散是**指数**的（一篇经典工作可能被引上万次），
       不加约束会瞬间淹没候选池并引入大量低相关内容。

    2. **遵守源开关**（实测 P2 缺陷）
       第一版无论 `use_openalex` 为何值都实例化并请求 OpenAlex，开关形同虚设。

    3. **检查响应状态，而不只是捕获异常**（实测 P2 缺陷）
       OpenAlex 返回 503 时是一个正常的 dict、不抛异常。第一版因此把
       "服务不可用"当成"这篇没有参考文献"，`errors` 为空 ——
       使用者完全看不出扩展其实失败了。

    4. **OpenAlex 与 S2 两个源都做**（test.md STEP4 明确要求两源扩展）
       第一版只调 OpenAlex，只有 S2 ID 的种子被跳过；即使配了 S2 key
       也不会启用 —— 这是实现缺口，与"没配 key"是两回事。

    5. 轮数：V1 只实现 1 轮。配了别的值会**明确记一条 error 而不是静默忽略**
       （静默忽略是最坏的选择：使用者以为跑了 2 轮）。
    """
    seeds = papers[:max(0, cfg.expand_top_n)]
    new: List[Paper] = []
    stats = {"seeds": len(seeds), "refs": 0, "cites": 0, "errors": [],
             "sources": [], "rounds": 1}
    if not seeds:
        return new, stats

    use_oa = bool(getattr(cfg, "use_openalex", True))
    use_s2 = bool(getattr(cfg, "use_semantic_scholar", True))
    rounds = int(getattr(cfg, "expansion_rounds", 1) or 1)
    if rounds != 1:
        stats["errors"].append(
            "expansion_rounds={} 不受支持（V1 只实现 1 轮），已按 1 轮执行".format(rounds))

    oa = None
    if use_oa:
        try:
            oa = ex_openalex.OpenAlex(email=_email())
            stats["sources"].append("openalex")
        except Exception as exc:
            stats["errors"].append("openalex init: {}".format(str(exc)[:150]))

    s2 = None
    if use_s2:
        try:
            cand = ex_s2.SemanticScholar(email=_email())
            if cand.available():
                s2 = cand
                stats["sources"].append("semantic_scholar")
            else:
                stats["errors"].append("semantic_scholar 不可用（未配置 S2_API_KEY）")
        except Exception as exc:
            stats["errors"].append("s2 init: {}".format(str(exc)[:150]))

    def _ok(resp, tag) -> bool:
        """响应级成功判定；失败记进 errors 并返回 False。

        复用 response_ok()，保证**检索与扩展两个阶段判据一致** ——
        第一版这里只看 status，扩展阶段同样会把"200 + 错误页"当成功。
        """
        ok, why = response_ok(resp, tag)
        if not ok:
            stats["errors"].append("{}: {}".format(tag, why))
        return ok

    def _add(items, src_name, kind, seed, key, extractor):
        n = 0
        for it in (items or [])[:None]:
            raw = extractor(it)
            if not isinstance(raw, dict) or not raw:
                continue
            np = paper_from_s2(raw, query="") if src_name == "semantic_scholar" \
                else paper_from_openalex(raw, query="")
            np.provenance["found_by"] = []
            np.add_found_by(src_name, kind, query=seed.paper_id, at=_now())
            new.append(np)
            n += 1
            if n >= key:
                break
        return n

    for seed in seeds:
        # ---------- OpenAlex ----------
        wid = _openalex_id_of(seed)
        if oa is not None:
            if not wid:
                stats["errors"].append(
                    "种子 {} 无 OpenAlex ID，跳过 OpenAlex 扩展".format(seed.paper_id[:40]))
            else:
                if cfg.expand_refs_per_paper:
                    try:
                        r = oa.get_references(wid, select=EXPAND_SELECT)
                        if _ok(r, "openalex.refs[{}]".format(wid)):
                            stats["refs"] += _add(
                                r.get("works"), "openalex", "reference_expansion",
                                seed, cfg.expand_refs_per_paper, lambda x: x)
                    except Exception as exc:
                        stats["errors"].append("openalex.refs[{}]: {}: {}".format(
                            wid, type(exc).__name__, str(exc)[:100]))
                if cfg.expand_cites_per_paper:
                    try:
                        r = oa.get_citations(wid, limit=cfg.expand_cites_per_paper,
                                             select=EXPAND_SELECT)
                        if _ok(r, "openalex.cites[{}]".format(wid)):
                            stats["cites"] += _add(
                                r.get("works"), "openalex", "citation_expansion",
                                seed, cfg.expand_cites_per_paper, lambda x: x)
                    except Exception as exc:
                        stats["errors"].append("openalex.cites[{}]: {}: {}".format(
                            wid, type(exc).__name__, str(exc)[:100]))

        # ---------- Semantic Scholar ----------
        sid = (seed.identity.get("s2_id") or "").strip()
        if s2 is not None:
            if not sid:
                stats["errors"].append(
                    "种子 {} 无 S2 ID，跳过 S2 扩展".format(seed.paper_id[:40]))
            else:
                if cfg.expand_refs_per_paper:
                    try:
                        r = s2.get_references(sid, limit=cfg.expand_refs_per_paper)
                        if _ok(r, "s2.refs[{}]".format(sid[:20])):
                            stats["refs"] += _add(
                                r.get("papers") or r.get("data"), "semantic_scholar",
                                "reference_expansion", seed, cfg.expand_refs_per_paper,
                                lambda x: (x.get("citedPaper") if isinstance(x, dict) else None) or x)
                    except Exception as exc:
                        stats["errors"].append("s2.refs[{}]: {}: {}".format(
                            sid[:20], type(exc).__name__, str(exc)[:100]))
                if cfg.expand_cites_per_paper:
                    try:
                        r = s2.get_citations(sid, limit=cfg.expand_cites_per_paper)
                        if _ok(r, "s2.cites[{}]".format(sid[:20])):
                            stats["cites"] += _add(
                                r.get("papers") or r.get("data"), "semantic_scholar",
                                "citation_expansion", seed, cfg.expand_cites_per_paper,
                                lambda x: (x.get("citingPaper") if isinstance(x, dict) else None) or x)
                    except Exception as exc:
                        stats["errors"].append("s2.cites[{}]: {}: {}".format(
                            sid[:20], type(exc).__name__, str(exc)[:100]))

        if cfg.sleep_between_calls:
            time.sleep(cfg.sleep_between_calls)

    return new, stats
