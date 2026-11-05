#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
elements.py —— 元件工具（原子能力，供 Agent 精细调整）

定位
----
本模块是 ToolsPy 的**元件层**：每个函数只做一件事，参数显式、结果可预测。
流水线（pipelines.py）由这些元件组合而成；你需要精细控制时直接用元件，
需要"一条命令跑完"时用流水线。

与流水线的分工
--------------
    元件：单次调用、无隐藏状态、不写盘（除非明确要求）、快
    流水线：多步编排、有断点、写盘、产出语料库

设计契约（所有元件都遵守）
--------------------------
1. **永远返回 result.py 的信封**，不抛异常给调用方；
2. **永不返回裸 None / 空列表** —— 空结果用 status="empty" 表达，
   与 "failed" 严格区分（详见 result.py 的 status 语义）；
3. **参数显式**：不读全局配置、不依赖当前工作目录；
4. **不打印**：需要日志时写进 diagnostics，由调用方决定怎么展示。

函数索引（详见 API.md）
─────────────────────────────────────────────────────────────────────────────
  依赖  [E00] doctor()              环境自检：依赖、凭证、服务、各源
  依赖  [E01] list_sources()        列出可用数据源及其能力矩阵

  检索  [E10] search_arxiv(q, ...)          arXiv 检索
  检索  [E11] search_openalex(q, ...)       OpenAlex 检索
  检索  [E12] search_s2(q, ...)             Semantic Scholar 检索
  检索  [E13] search_all_sources(...)       三源并行检索 + 去重

  标识  [E20] normalize_ids(...)            标识符归一化（DOI/arXiv/…）
  标识  [E21] resolve_doi(doi)              DOI → 全源记录
  标识  [E22] match_title(title, ...)      标题 → Crossref 候选

  全文  [E30] find_pdf_urls(paper)          列出某论文的全部候选 PDF 地址
  全文  [E31] download_pdf(paper, ...)      下载并校验 PDF
  全文  [E32] resolve_oa(doi)               Unpaywall：DOI → OA 位置

  解析  [E40] grobid_available()            GROBID 服务是否可用
  解析  [E41] parse_pdf(pdf_path, ...)      PDF → 结构化元件（含坐标/span/引文连接）
  解析  [E42] extract_text(pdf_path)        纯文本兜底（无 GROBID 时）

  引用  [E50] get_references(work_id)       某论文的参考文献
  引用  [E51] get_citations(work_id)        某论文的被引
  引用  [E52] get_citation_edges(...)       引用边（统一 citing→cited）
  引用  [E53] get_works_by_ids(ids)         **按 ID 批量取元数据**（外部引用反查）
─────────────────────────────────────────────────────────────────────────────

⚠️ 每个函数都带 `_tool("E10.search_arxiv")` 形式的标识，报错时会在
   error.component 里体现 —— Agent 据此能直接跳到 API.md 对应小节。
"""

from __future__ import annotations

import os
import sys
import time
from typing import Any, Dict, List, Optional

from ._bootstrap import ensure_importable, describe as _describe, check_keys  # noqa: E402
from . import result as R                                                    # noqa: E402
from .errors import make_error, suggest                                      # noqa: E402

ensure_importable()

# 元件工具标识（写进 error.component / meta.tool）
E00, E01 = "E00.doctor", "E01.list_sources"
E10, E11, E12, E13, E14 = ("E10.search_arxiv", "E11.search_openalex",
                           "E12.search_s2", "E13.search_all_sources",
                           "E14.search_crossref")
E20, E21, E22 = "E20.normalize_ids", "E21.resolve_doi", "E22.match_title"
E30, E31, E32 = "E30.find_pdf_urls", "E31.download_pdf", "E32.resolve_oa"
E40, E41, E42 = "E40.grobid_available", "E41.parse_pdf", "E42.extract_text"
E50, E51, E52 = "E50.get_references", "E51.get_citations", "E52.get_citation_edges"
E53 = "E53.get_works_by_ids"


# =============================================================================
# 依赖与能力
# =============================================================================

def doctor() -> Dict[str, Any]:
    """[E00] 环境自检：依赖接入 / 凭证 / GROBID 服务 / 各源可达性。

    用途：**任何工具报错前先跑它** —— 大多数"工具不工作"其实是缺 key 或
    服务没起，而不是代码问题。Agent 应先 doctor() 再决定策略。

    返回 data = {
        "dependencies": {...},      # PaperSearch 接入状态
        "credentials":  {...},      # 各 key 有没有（**不含值**）
        "grobid":       {...},      # 服务地址/是否在线/版本
        "sources":      {...},      # 各源可用性下结论
        "next_actions": [...]       # 建议的补救动作（可执行）
    }
    """
    t0 = time.time()
    try:
        dep = _describe()
        cred = check_keys()
        data: Dict[str, Any] = {"dependencies": dep, "credentials": cred}

        # ---- GROBID ----
        grobid = {"url": "http://localhost:8070", "available": False,
                  "version": "", "error": ""}
        try:
            import grobid as _g
            g = _g.Grobid(base_url=grobid["url"])
            alive, msg = g.isalive()
            grobid["available"] = bool(alive)
            if alive:
                _, ver = g.version()
                grobid["version"] = str(ver)[:80]
            else:
                grobid["error"] = str(msg)[:150]
        except Exception as exc:
            grobid["error"] = "{}: {}".format(type(exc).__name__, str(exc)[:120])
        data["grobid"] = grobid

        # ---- 各源结论 ----
        sources = {
            "arxiv": {"usable": dep.get("ok", False), "needs_key": False,
                      "note": "无需凭证；3 秒/请求节流"},
            "openalex": {"usable": dep.get("ok", False) and cred.get("openalex", False),
                         "needs_key": True,
                         "note": "有免费额度（$1/天）；无 key 会随机 429"},
            "semantic_scholar": {"usable": dep.get("ok", False) and cred.get("s2", False),
                                 "needs_key": True,
                                 "note": "无 key 时该源被跳过（不是错误）"},
            "crossref": {"usable": dep.get("ok", False), "needs_key": False,
                         "note": "需要邮箱进 polite pool"},
            "unpaywall": {"usable": dep.get("ok", False) and cred.get("email", False),
                          "needs_key": True, "note": "需要邮箱"},
            "grobid": {"usable": grobid["available"], "needs_key": False,
                       "note": "本地服务；未启动时解析会降级为纯文本"},
        }
        data["sources"] = sources

        # ---- 可执行的补救建议 ----
        acts = []
        if not dep.get("ok"):
            acts.append({"action": "fix_dependency",
                         "detail": dep.get("error", "")[:200],
                         "command": "设置 TOOLSPY_HOME 指向 PaperSearch 目录"})
        if not cred.get("s2"):
            acts.append({"action": "add_key", "key": "S2_API_KEY",
                         "detail": "在 PaperSearch/Keys.txt 里补 S2_API_KEY",
                         "command": "编辑 PaperSearch/Keys.txt"})
        if not grobid["available"]:
            acts.append({"action": "start_service", "service": "grobid",
                         "detail": "GROBID 未启动；不启动也能跑检索与下载，"
                                   "但 PDF 结构化解析会降级为纯文本",
                         "command": "cd PaperSearch/ExamplePy && ./grobid-serve.sh start"})
        data["next_actions"] = acts

        diag = {"elapsed_sec": round(time.time() - t0, 3),
                "deps_ok": dep.get("ok"),
                "grobid_online": grobid["available"],
                "sources_usable": sum(1 for v in sources.values() if v["usable"])}
        healthy = dep.get("ok") and sources["arxiv"]["usable"]
        if healthy:
            # 有补救项时给 partial —— 能跑，但缺东西
            if acts:
                return R.partial(data, tool=E00, reason="环境可用但有 {} 项待处理".format(len(acts)),
                                 diagnostics=diag)
            return R.ok(data, tool=E00, diagnostics=diag)
        return R.failed("INPUT_NOT_FOUND" if not dep.get("ok") else "SERVICE_UNAVAILABLE",
                        message="环境不可用", tool=E00, component=E00,
                        context={"dependencies": dep.get("error", "")[:200]},
                        diagnostics=diag)
    except Exception as exc:
        return R.from_exception(exc, tool=E00, component=E00)


def list_sources() -> Dict[str, Any]:
    """[E01] 列出数据源及其**能力矩阵**。

    用途：Agent 先看"哪个源能干什么"再决定用哪条路径，不用去读源码。

    返回 data = {"sources": [{name, kinds, needs_key, rate_limit, notes}, ...],
                 "capability_matrix": {能力: [源...]}}
    """
    try:
        srcs = [
            {"name": "arxiv", "kinds": ["search", "pdf", "harvest"],
             "needs_key": False, "rate_limit": "官方建议 ~3 秒/请求",
             "notes": "Export API；单次 max_results ≤2000，累计 ≤30000；"
                      "老式 ID（hep-th/9901001）也支持；深度分页 ~10000 处会 500"},
            {"name": "openalex", "kinds": ["search", "citation_graph", "oa_location", "abstract"],
             "needs_key": True, "rate_limit": "有 key 约 10 次/秒；免费 $1/天",
             "notes": "**主索引**：被引数、参考文献、OA 位置、倒排摘要；"
                      "匿名会随机 429"},
            {"name": "semantic_scholar", "kinds": ["search", "citation_graph", "citation_context"],
             "needs_key": True, "rate_limit": "1 请求/秒（无 key 时 429）",
             "notes": "**citation context 是本库独有**（引用它的原句）；无 key 时整源跳过"},
            {"name": "crossref", "kinds": ["search", "doi_lookup", "title_match", "bibliographic"],
             "needs_key": False, "rate_limit": "polite pool（带邮箱）",
             "notes": "正式出版信息、license；/works/{doi} **不接受 select** 参数"},
            {"name": "unpaywall", "kinds": ["oa_location"],
             "needs_key": True, "rate_limit": "带邮箱即可",
             "notes": "只能 DOI→OA；/v2/search 已退役（实测 410）"},
            {"name": "grobid", "kinds": ["pdf_structure", "coordinates", "citation_links"],
             "needs_key": False, "rate_limit": "本地服务，无限制",
             "notes": "章节/段落/图表公式/参考文献/坐标/引文连接；需 JDK"},
            {"name": "openreview", "kinds": ["reviews", "decisions"],
             "needs_key": True, "rate_limit": "匿名受限（403）",
             "notes": "审稿意见；V1 不需要"},
            {"name": "core", "kinds": ["pdf"],
             "needs_key": True, "rate_limit": "有 key",
             "notes": "OA 全文补充；V2 才用"},
        ]
        matrix: Dict[str, List[str]] = {}
        for s in srcs:
            for k in s["kinds"]:
                matrix.setdefault(k, []).append(s["name"])
        return R.ok({"sources": srcs, "capability_matrix": matrix},
                    tool=E01, diagnostics={"n_results": len(srcs)})
    except Exception as exc:
        return R.from_exception(exc, tool=E01, component=E01)


# =============================================================================
# 检索元件
# =============================================================================

def _per_source_result(source: str, papers: List[dict], errs: List[str],
                       total_hits: int, elapsed: float,
                       available: bool = True) -> Dict[str, Any]:
    """把单源检索结果包成信封（三个检索元件共用，保证语义一致）。

    状态判定（这是本库的核心契约，三源必须一致）：
        有结果 + 无错误          → ok
        有结果 + 有错误          → partial（**结果可用**，别丢）
        无结果 + 源不可用(缺key)  → blocked
        无结果 + 无错误          → empty（成功的空集）
        无结果 + 有错误          → failed
    """
    diag = {"elapsed_sec": round(elapsed, 3), "n_results": len(papers),
            "total_hits": total_hits, "errors": errs[:5],
            "n_errors": len(errs)}
    if papers and not errs:
        return R.ok(papers, tool=source, diagnostics=diag)
    if papers and errs:
        return R.partial(papers, tool=source,
                         reason="部分查询失败（已有结果保留）",
                         code="DATA_PARTIAL", diagnostics=diag)
    if errs:
        first = errs[0]
        code = "AUTH_MISSING_KEY" if "未配置" in first or "key" in first.lower() \
            else ("RATE_LIMITED" if "429" in first else "NET_BAD_RESPONSE")
        return R.failed(code, message=first, tool=source, component=source,
                        context={"errors": errs[:5]}, diagnostics=diag)
    if not available:
        return R.blocked("AUTH_MISSING_KEY", tool=source, component=source,
                         message="该源不可用（未配置凭证）", diagnostics=diag)
    return R.empty(tool=source, reason="检索命中 0 条（成功空集）", diagnostics=diag)


def search_arxiv(query: str, max_results: int = 20, *, start: int = 0,
                 sort_by: str = "relevance", sort_order: str = "descending",
                 year_from: int = 0, year_to: int = 0) -> Dict[str, Any]:
    """[E10] arXiv 检索（Export API）。

    参数
    ----
    query       : arXiv 检索式。支持字段前缀与布尔：
                    all:"deep cfr"       全部字段
                    ti:"..." / abs:"..." 标题 / 摘要
                    au:smith             作者
                    cat:cs.AI            分类
                    "a" AND "b" / OR / ANDNOT
    max_results : 本次最多取多少条（**官方上限 2000**）。默认 20。
                  超过 200 会明显变慢；超过 2000 会被拒。
    start       : 起始偏移（翻页用）。深度分页在 ~10000 处会 500。
    sort_by     : relevance | lastUpdatedDate | submittedDate
    year_from/to: 年份过滤（**本地过滤**，arXiv API 不支持按年份筛）。
                  传了会多取一些再筛，diagnostics 里会说明真实请求量。

    返回 data = [paper 字典]（字段见 API.md 的「Paper 结构」）
         status: ok / partial / empty / failed
    注意：arXiv 不提供参考文献 —— 引用图要用 OpenAlex（E11/E50）。

    示例
    ----
        >>> r = search_arxiv('all:"counterfactual regret minimization"', 10)
        >>> r["ok"], len(r["data"])
        (True, 10)
    """
    t0 = time.time()
    try:
        if not (query or "").strip():
            return R.failed("INPUT_MISSING", message="query 不能为空",
                            tool=E10, component=E10, context={"missing": "query"})
        if max_results <= 0:
            return R.failed("INPUT_INVALID",
                            message="max_results 必须为正，收到 {}".format(max_results),
                            tool=E10, component=E10,
                            context={"max_results": max_results})
        if max_results > 2000:
            return R.failed("INPUT_INVALID",
                            message="arXiv 单次上限 2000，收到 {}".format(max_results),
                            tool=E10, component=E10,
                            hint="拆成多次调用（用 start 翻页），或用 ProcessPy 的增量采集器",
                            context={"max_results": max_results})

        import arxiv as ex
        import _common

        # 年份过滤：API 不支持，只能本地筛 —— 多取一些以免筛完不够
        need_local_filter = bool(year_from or year_to)
        fetch_n = min(2000, max_results * 4) if need_local_filter else max_results
        r = ex.search(query, max_results=fetch_n, start=start,
                      sort_by=sort_by, sort_order=sort_order,
                      email=_common.get_key("PAPERSEARCH_EMAIL"))
        st = r.get("status")
        if st != 200:
            code = "RATE_LIMITED" if st == 429 else (
                "NET_HTTP_ERROR" if st else "NET_UNREACHABLE")
            return R.failed(code, message=str(r.get("error"))[:200],
                            tool=E10, component=E10,
                            context={"status": st, "query": query[:80]},
                            diagnostics={"elapsed_sec": round(time.time() - t0, 3)})

        import sources as _src
        papers = []
        for e in (r.get("entries") or []):
            try:
                papers.append(_src.paper_from_arxiv(e, query).to_dict())
            except Exception:
                continue
        total_before = len(papers)
        if need_local_filter:
            keep = []
            for p in papers:
                y = (p.get("metadata") or {}).get("year")
                if not y:
                    keep.append(p)          # 年份缺失保留（不无根据丢弃）
                    continue
                if (year_from and int(y) < year_from) or (year_to and int(y) > year_to):
                    continue
                keep.append(p)
            papers = keep[:max_results]

        diag = {"elapsed_sec": round(time.time() - t0, 3),
                "n_results": len(papers), "total_hits": r.get("total"),
                "requested": fetch_n, "returned_raw": total_before,
                "local_year_filter": need_local_filter,
                "sort": "{} {}".format(sort_by, sort_order),
                "stop_reason": ("limit_reached" if len(papers) >= max_results
                                else "source_exhausted")}
        if not papers:
            return R.empty(tool=E10, reason="arXiv 检索命中 0 条",
                           diagnostics=diag)
        return R.ok(papers, tool=E10, diagnostics=diag)
    except Exception as exc:
        return R.from_exception(exc, tool=E10, component=E10,
                                context={"query": query[:80]})


def search_openalex(query: str, max_results: int = 25, *, page: int = 1,
                    sort: str = "", year_from: int = 0, year_to: int = 0,
                    per_page: int = 0) -> Dict[str, Any]:
    """[E11] OpenAlex 检索（本库主索引）。

    参数
    ----
    query      : 自由文本；支持 OpenAlex 语法：
                   "deep cfr"（短语）  a AND b   a OR b   a NOT b
    max_results: 最多返回多少条（内部按 per_page 分页取够）
    per_page   : 每页条数（1~200）。0 = 自动（≤ per_page 上限）
    sort       : 排序，如 "publication_date:asc"、"cited_by_count:desc"
    year_from/to: 年份过滤（OpenAlex **支持**服务端过滤，会走 filter）

    返回 data = [paper 字典]，含 citation_count / topics / OA 位置 / 摘要。
         ⚠️ 摘要来自 `abstract_inverted_index`，本函数已还原成文本。

    为什么 OpenAlex 是主索引：它一份记录里同时有被引数、参考文献、
    OA 位置、主题标签和摘要 —— 其他源都只覆盖一部分。

    示例
    ----
        >>> r = search_openalex("counterfactual regret minimization", 10)
        >>> r["data"][0]["metadata"]["citation_count"]     # 被引数
    """
    t0 = time.time()
    try:
        if not (query or "").strip():
            return R.failed("INPUT_MISSING", message="query 不能为空",
                            tool=E11, component=E11, context={"missing": "query"})
        import openalex as ex
        import _common
        import sources as _src

        oa = ex.OpenAlex(email=_common.get_key("PAPERSEARCH_EMAIL"))
        page_size = per_page or min(200, max(1, max_results))
        papers, total_hits, errs, pages = [], None, [], 0
        while len(papers) < max_results:
            r = oa.search(query, per_page=min(page_size, max_results - len(papers)),
                          page=page + pages, sort=sort)
            ok, why = _src.response_ok(r, "openalex")
            if not ok:
                errs.append(why)
                break
            total_hits = r.get("count") if total_hits is None else total_hits
            batch = r.get("results") or []
            if not batch:
                break
            for w in batch:
                try:
                    papers.append(_src.paper_from_openalex(w, query).to_dict())
                except Exception:
                    continue
            pages += 1
            if pages >= 20:                 # 安全上限，避免无限翻页
                break
        # OpenAlex 的服务端年份过滤已在 search 里，这里再兜一层（防漏）
        if year_from or year_to:
            keep = []
            for p in papers:
                y = (p.get("metadata") or {}).get("year")
                if not y:
                    keep.append(p)
                    continue
                if (year_from and int(y) < year_from) or (year_to and int(y) > year_to):
                    continue
                keep.append(p)
            papers = keep

        diag = {"elapsed_sec": round(time.time() - t0, 3),
                "n_results": len(papers), "total_hits": total_hits,
                "pages_fetched": pages, "errors": errs[:3],
                "quota": getattr(oa, "quota", {}) if hasattr(oa, "quota") else {}}
        if papers and errs:
            return R.partial(papers, tool=E11, reason="翻页中断（已有结果保留）",
                             diagnostics=diag)
        if papers:
            return R.ok(papers, tool=E11, diagnostics=diag)
        if errs:
            return R.failed("RATE_LIMITED" if "429" in errs[0] else "NET_BAD_RESPONSE",
                            message=errs[0], tool=E11, component=E11,
                            context={"errors": errs[:3]}, diagnostics=diag)
        return R.empty(tool=E11, reason="OpenAlex 检索命中 0 条", diagnostics=diag)
    except Exception as exc:
        return R.from_exception(exc, tool=E11, component=E11,
                                context={"query": query[:80]})


def search_s2(query: str, max_results: int = 20, *, fields: str = "") -> Dict[str, Any]:
    """[E12] Semantic Scholar 检索（**需要 S2_API_KEY**）。

    参数
    ----
    query       : 自由文本
    max_results : 最多条数（S2 单次上限 100）
    fields      : 要哪些字段；留空用推荐集合（含 externalIds / abstract /
                  citationCount / openAccessPdf）。**externalIds 是跨库对齐的桥梁**，
                  别去掉。

    无 key 时返回 **blocked**（不是 failed）—— 这是前提缺失，不是代码问题：
        error.hint 会告诉你把 S2_API_KEY 加到 Keys.txt。

    本源的独有能力：**citation context**（引用它的原句），
    需要用 citations(contexts=...) 单独取，见 API.md。

    示例
    ----
        >>> r = search_s2("deep cfr", 5)
        >>> r["status"]        # 无 key 时
        'blocked'
    """
    t0 = time.time()
    try:
        import semantic_scholar as ex
        import _common
        import sources as _src

        # ★ 参数校验必须**先于**前提检查（自检抓到的真问题）：
        #   原先先查 key，于是 `search_s2("")` 返回 blocked(AUTH_MISSING_KEY) ——
        #   但调用方的问题明明是"query 是空的"。这会让 Agent 去补一个
        #   根本不需要的 key，改完还是失败。
        #   原则：**先告诉调用方"你传错了"，再告诉他"环境缺东西"** ——
        #   前者是他能立刻改的，后者可能是环境问题。
        if not (query or "").strip():
            return R.failed("INPUT_MISSING", message="query 不能为空",
                            tool=E12, component=E12, context={"missing": "query"})
        s2 = ex.SemanticScholar(email=_common.get_key("PAPERSEARCH_EMAIL"))
        if not s2.available():
            return R.blocked("AUTH_MISSING_KEY", tool=E12, component=E12,
                             message="未配置 S2_API_KEY，该源不可用",
                             context={"key_name": "S2_API_KEY",
                                      "keys_file": "PaperSearch/Keys.txt"},
                             diagnostics={"elapsed_sec": round(time.time() - t0, 3)})
        r = s2.search_papers(query, limit=min(100, max_results))
        ok, why = _src.response_ok(r, "s2")
        if not ok:
            return R.failed("RATE_LIMITED" if "429" in why else "NET_BAD_RESPONSE",
                            message=why, tool=E12, component=E12,
                            diagnostics={"elapsed_sec": round(time.time() - t0, 3)})
        papers = []
        for sp in (r.get("papers") or []):
            try:
                papers.append(_src.paper_from_s2(sp, query).to_dict())
            except Exception:
                continue
        diag = {"elapsed_sec": round(time.time() - t0, 3),
                "n_results": len(papers), "total_hits": r.get("total")}
        if not papers:
            return R.empty(tool=E12, reason="S2 检索命中 0 条", diagnostics=diag)
        return R.ok(papers, tool=E12, diagnostics=diag)
    except Exception as exc:
        return R.from_exception(exc, tool=E12, component=E12,
                                context={"query": query[:80]})


def search_crossref(query: str, max_results: int = 20) -> Dict[str, Any]:
    """Bounded Crossref discovery for DOI publications absent from preprint indexes."""
    t0 = time.time()
    try:
        if not (query or "").strip():
            return R.failed("INPUT_MISSING", message="query 不能为空",
                            tool=E14, component=E14)
        import crossref as ex
        import _common
        import sources as _src
        cr = ex.Crossref(email=_common.get_key("PAPERSEARCH_EMAIL"))
        response = cr.query_works(query, rows=min(20, max_results),
                                  select=ex.DEFAULT_SELECT)
        if response.get("status") != 200:
            return R.failed("NET_BAD_RESPONSE",
                            message=str(response.get("error") or "Crossref failed"),
                            tool=E14, component=E14)
        papers = [_src.paper_from_crossref(item, query).to_dict()
                  for item in response.get("items") or []]
        diag = {"elapsed_sec": round(time.time() - t0, 3),
                "n_results": len(papers), "total_hits": response.get("total")}
        if not papers:
            return R.empty(tool=E14, reason="Crossref 检索命中 0 条",
                           diagnostics=diag)
        return R.ok(papers, tool=E14, diagnostics=diag)
    except Exception as exc:
        return R.from_exception(exc, tool=E14, component=E14,
                                context={"query": query[:80]})


def search_all_sources(query: str, max_per_source: int = 20, *,
                       sources: Optional[List[str]] = None,
                       dedup: bool = True, year_from: int = 0,
                       year_to: int = 0) -> Dict[str, Any]:
    """[E13] 多源并行检索 + 去重（流水线也用它）。

    参数
    ----
    query          : 检索式（会按各源语法转换）
    max_per_source : **每个源**最多取多少条
    sources        : 限定用哪些源；None = 全部可用源（自动跳过无凭证的）
    dedup          : 是否跨源去重合并（按 DOI/arXiv/标题+作者+年）

    返回 data = {
        "papers": [统一 Paper...],           # 已去重
        "by_source": {"arxiv": n, "openalex": n, ...},
        "source_status": {"arxiv": "ok"|"blocked"|"failed"|"empty", ...},
        "dedup": {"before": n, "after": n, "merged_groups": n, "alias": {...}}
    }

    ★ 状态语义：只要**任一源**有结果就是 ok/partial；
      全部源都 blocked/failed 才是 failed。
      单源失败不会让整个调用失败 —— 这是多源检索的关键设计
      （限流和缺凭证是常态，丢掉已拿到的结果才是真损失）。

    示例
    ----
        >>> r = search_all_sources('deep cfr', 10)
        >>> r["diagnostics"]["by_source"]
        {'arxiv': 10, 'openalex': 10, 'semantic_scholar': 0}
    """
    t0 = time.time()
    try:
        if not (query or "").strip():
            return R.failed("INPUT_MISSING", message="query 不能为空",
                            tool=E13, component=E13, context={"missing": "query"})
        want = set(sources) if sources else {
            "arxiv", "openalex", "semantic_scholar", "crossref"}
        papers: List[dict] = []
        by_source: Dict[str, int] = {}
        source_status: Dict[str, str] = {}
        errs: List[str] = []

        if "arxiv" in want:
            r = search_arxiv(query, max_per_source, year_from=year_from, year_to=year_to)
            by_source["arxiv"] = len(r.get("data") or [])
            source_status["arxiv"] = r["status"]
            papers += (r.get("data") or [])
            if not r["ok"]:
                errs.append("arxiv: " + (r.get("error") or {}).get("message", "")[:100])
        if "openalex" in want:
            r = search_openalex(query, max_per_source, year_from=year_from, year_to=year_to)
            by_source["openalex"] = len(r.get("data") or [])
            source_status["openalex"] = r["status"]
            papers += (r.get("data") or [])
            if not r["ok"]:
                errs.append("openalex: " + (r.get("error") or {}).get("message", "")[:100])
        if "semantic_scholar" in want:
            r = search_s2(query, max_per_source)
            by_source["semantic_scholar"] = len(r.get("data") or [])
            source_status["semantic_scholar"] = r["status"]
            papers += (r.get("data") or [])
            if not r["ok"] and r["status"] != "blocked":
                errs.append("s2: " + (r.get("error") or {}).get("message", "")[:100])
        if "crossref" in want:
            r = search_crossref(query, min(max_per_source, 20))
            by_source["crossref"] = len(r.get("data") or [])
            source_status["crossref"] = r["status"]
            papers += (r.get("data") or [])
            if not r["ok"]:
                errs.append("crossref: " + (r.get("error") or {}).get("message", "")[:100])

        # ---- 去重 ----
        dedup_info = {}
        if dedup and papers:
            import dedup as _dd
            from model import Paper
            from config import PipelineConfig
            objs = [Paper.from_dict(p) for p in papers]
            merged, rep = _dd.resolve(objs, PipelineConfig())
            if rep.get("alias"):
                _dd.apply_alias(merged, rep["alias"])
            papers = [p.to_dict() for p in merged]
            dedup_info = {"before": rep["before"], "after": rep["after"],
                          "merged_groups": rep["merged_groups"],
                          "strong_links": rep["strong_links"],
                          "weak_links": rep["weak_links"],
                          "blocked_by_conflict": len(rep.get("blocked_by_conflict") or []),
                          "alias": rep.get("alias") or {}}

        diag = {"elapsed_sec": round(time.time() - t0, 3),
                "n_results": len(papers), "by_source": by_source,
                "source_status": source_status, "dedup": dedup_info,
                "errors": errs[:5]}
        usable = [s for s, st in source_status.items() if st in ("ok", "partial", "empty")]
        if papers and len(usable) == len(source_status):
            return R.ok({"papers": papers, "by_source": by_source,
                         "source_status": source_status, "dedup": dedup_info},
                        tool=E13, diagnostics=diag)
        if papers:
            failed_src = [s for s, st in source_status.items()
                          if st not in ("ok", "partial", "empty")]
            return R.partial({"papers": papers, "by_source": by_source,
                              "source_status": source_status, "dedup": dedup_info},
                             tool=E13,
                             reason="源 {} 未成功，结果来自其余源".format(",".join(failed_src)),
                             diagnostics=diag)
        # 一篇都没有：区分 blocked / failed / empty
        if source_status and all(st == "blocked" for st in source_status.values()):
            return R.blocked("AUTH_MISSING_KEY", tool=E13, component=E13,
                             message="所有源都因缺凭证不可用",
                             context={"source_status": source_status},
                             diagnostics=diag)
        if errs:
            return R.failed("NET_BAD_RESPONSE", message="所有源都失败：" + errs[0],
                            tool=E13, component=E13, context={"errors": errs[:5]},
                            diagnostics=diag)
        return R.empty(tool=E13, reason="所有源都命中 0 条", diagnostics=diag)
    except Exception as exc:
        return R.from_exception(exc, tool=E13, component=E13,
                                context={"query": query[:80]})

# =============================================================================
# 标识符元件
# =============================================================================

def normalize_ids(identity: Optional[dict] = None, *, doi: str = "",
                  arxiv_id: str = "", openalex_id: str = "", s2_id: str = "",
                  title: str = "") -> Dict[str, Any]:
    """[E20] 标识符归一化 + 生成确定性 paper_id。

    用途：把各源五花八门的 ID 写法统一，得到**稳定主键**。
    同一篇论文在任何源、任何写法下都应得到同一个 paper_id —— 这是去重的基础。

    参数
    ----
    identity   : 直接给 {"doi":..., "arxiv_id":...}（与下面几个参数二选一）
    doi        : 支持 URL / 裸写 / 带前缀：https://doi.org/10.1/X、doi:10.1/X、10.1/X
    arxiv_id   : 支持带版本：1809.03057v3 → 1809.03057（版本存进 arxiv_version）
    title      : 兜底用（所有 ID 都没有时按标题哈希生成主键）

    返回 data = {
        "paper_id":  "doi:10.1038/nature12373" | "arxiv:1809.03057" | "title:<sha1>",
        "identity":  {归一化后的六个字段},
        "arxiv_version": "v3" | "",
        "id_kind":   "doi" | "arxiv" | "openalex" | "s2" | "title"
    }

    示例
    ----
        >>> normalize_ids(doi="https://doi.org/10.1038/Nature12373")["data"]["paper_id"]
        'doi:10.1038/nature12373'
        >>> normalize_ids(arxiv_id="1809.03057v3")["data"]
        {'paper_id': 'arxiv:1809.03057', ..., 'arxiv_version': 'v3', ...}
    """
    try:
        from model import new_paper
        idt = dict(identity or {})
        for k, v in (("doi", doi), ("arxiv_id", arxiv_id),
                     ("openalex_id", openalex_id), ("s2_id", s2_id)):
            if v and not idt.get(k):
                idt[k] = v
        if not any(idt.get(k) for k in ("doi", "arxiv_id", "openalex_id", "s2_id",
                                        "openreview_id", "core_id")) and not title:
            return R.failed("INPUT_MISSING",
                            message="至少要给一个标识符或标题",
                            tool=E20, component=E20,
                            hint="传 doi / arxiv_id / openalex_id / s2_id 之一，或给 title 兜底")
        p = new_paper(idt, title)
        kind = (p.paper_id or ":").split(":", 1)[0]
        data = {"paper_id": p.paper_id, "identity": p.identity,
                "arxiv_version": p.metadata.get("arxiv_version", ""),
                "id_kind": kind, "title_norm": p.metadata.get("title_norm", "")}
        warns = []
        # 非法 ID 会静默回退，这里显式提醒（否则调用方以为归一化成功了）
        if doi and not p.identity.get("doi"):
            warns.append({"code": "INPUT_INVALID",
                          "message": "DOI {!r} 无法归一化（格式非法），已忽略".format(doi[:60]),
                          "hint": "检查 DOI 是否含完整前缀 10.xxxx/"})
        return R.ok(data, tool=E20, warnings=warns)
    except Exception as exc:
        return R.from_exception(exc, tool=E20, component=E20)


def resolve_doi(doi: str) -> Dict[str, Any]:
    """[E21] DOI → 跨源记录（Crossref + Unpaywall + 可选 OpenAlex）。

    用途：拿到一个 DOI 后补齐书目信息与 OA 位置。

    返回 data = {
        "crossref": {...} | None,     # 正式出版信息（标题/作者/venue/license）
        "unpaywall": {...} | None,    # OA 位置（best_pdf / oa_status / license）
        "merged": {...}               # 合并后的统一字段（冲突时以 Crossref 为准）
    }
    """
    t0 = time.time()
    try:
        if not (doi or "").strip():
            return R.failed("INPUT_MISSING", message="doi 不能为空", tool=E21,
                            component=E21, context={"missing": "doi"})
        import crossref as cr, unpaywall as up, _common
        from model import normalize_doi
        nd = normalize_doi(doi)
        if not nd:
            return R.failed("INPUT_INVALID",
                            message="DOI 格式非法：{!r}".format(doi[:80]),
                            tool=E21, component=E21,
                            hint="DOI 应形如 10.xxxx/yyyy（可带 https://doi.org/ 前缀）")
        email = _common.get_key("PAPERSEARCH_EMAIL")
        data: Dict[str, Any] = {"crossref": None, "unpaywall": None, "merged": {}}
        warns = []

        r1 = cr.Crossref(email=email).get_work_by_doi(nd)
        if r1.get("status") == 200 and r1.get("work"):
            w = r1["work"]
            data["crossref"] = {
                "title": (w.get("title") or [""])[0],
                "authors": [" ".join(filter(None, [a.get("given"), a.get("family")]))
                            for a in (w.get("author") or [])],
                "year": ((w.get("issued") or {}).get("date-parts") or [[None]])[0][0],
                "venue": (w.get("container-title") or [""])[0],
                "type": w.get("type", ""),
                "license": [l.get("URL") for l in (w.get("license") or [])
                            if l.get("URL")][:3],
                "publisher": w.get("publisher", ""),
            }
            data["merged"].update({k: v for k, v in data["crossref"].items() if v})
        else:
            warns.append({"code": "NOTFOUND_DOI",
                          "message": "Crossref 未找到该 DOI（HTTP {}）".format(r1.get("status")),
                          "hint": "DOI 可能写错，或该文献未在 Crossref 注册"})

        r2 = up.Unpaywall(email=email).get_by_doi(nd)
        if r2.get("status") == 200:
            data["unpaywall"] = {
                "is_oa": r2.get("is_oa"), "oa_status": r2.get("oa_status", ""),
                "best_pdf": r2.get("best_pdf", ""), "version": r2.get("version", ""),
                "license": r2.get("license", ""),
                "n_locations": len(r2.get("all_locations") or []),
            }
            if r2.get("best_pdf"):
                data["merged"]["pdf_url"] = r2["best_pdf"]
        else:
            warns.append({"code": "NOTFOUND_PDF",
                          "message": "Unpaywall 无记录（HTTP {}）".format(r2.get("status")),
                          "hint": "该 DOI 可能没有开放获取版本"})
        data["merged"]["doi"] = nd

        diag = {"elapsed_sec": round(time.time() - t0, 3),
                "has_crossref": bool(data["crossref"]),
                "has_oa": bool(data["unpaywall"] and data["unpaywall"].get("is_oa"))}
        if data["crossref"] or data["unpaywall"]:
            return (R.partial if warns else R.ok)(data, tool=E21, diagnostics=diag,
                                                  **({"reason": "部分源无记录"} if warns else {}))
        return R.empty(tool=E21, reason="两个源都没有该 DOI 的记录",
                       warnings=warns, diagnostics=diag)
    except Exception as exc:
        return R.from_exception(exc, tool=E21, component=E21, context={"doi": doi[:60]})


def match_title(title: str, *, author: str = "", rows: int = 3,
                min_similarity: float = 0.92) -> Dict[str, Any]:
    """[E22] 标题 → Crossref 候选（用于给缺 DOI 的论文补 DOI）。

    参数
    ----
    title          : 论文标题
    author         : 首作者姓氏（可选，能显著提高准确率）
    rows           : 取多少候选
    min_similarity : **采纳门槛**。默认 0.92（保守）——
                     Crossref 的匹配分不能单独作为合并依据，
                     必须自己再比标题相似度 + 年份 + 作者。

    返回 data = {
        "accepted": {...} | None,      # 达到门槛、可直接采纳的那条
        "candidates": [{...}],         # 全部候选（含未达门槛的，供人工判断）
        "accepted_rule": "标题相似度≥0.92 且（年份一致或缺失）且（首作者一致或缺失）"
    }

    ⚠️ 为什么保守：**错合并比漏合并危害大**（错合并后两篇的证据混在一起拆不开）。
       达不到门槛时 accepted=None，调用方应保持"无 DOI"状态而不是硬塞一个。
    """
    t0 = time.time()
    try:
        if not (title or "").strip():
            return R.failed("INPUT_MISSING", message="title 不能为空", tool=E22,
                            component=E22, context={"missing": "title"})
        import crossref as cr, _common
        from model import title_similarity, normalize_author
        r = cr.Crossref(email=_common.get_key("PAPERSEARCH_EMAIL")).match_title(
            title, author=author, rows=rows)
        items = r.get("items") or []
        cands, accepted = [], None
        for it in items:
            t = (it.get("title") or [""])[0]
            sim = title_similarity(title, t)
            y = ((it.get("issued") or {}).get("date-parts") or [[None]])[0][0]
            aus = it.get("author") or []
            first = normalize_author("{} {}".format(aus[0].get("given", ""),
                                                    aus[0].get("family", ""))) if aus else ""
            item = {"doi": (it.get("DOI") or "").lower(), "title": t,
                    "similarity": round(sim, 3), "year": y,
                    "first_author": first,
                    "venue": (it.get("container-title") or [""])[0],
                    "meets_threshold": sim >= min_similarity}
            cands.append(item)
            if accepted is None and sim >= min_similarity:
                accepted = item
        diag = {"elapsed_sec": round(time.time() - t0, 3),
                "n_results": len(cands), "n_above_threshold":
                    sum(1 for c in cands if c["meets_threshold"]),
                "min_similarity": min_similarity}
        data = {"accepted": accepted, "candidates": cands,
                "accepted_rule": ("标题相似度≥{} 且（年份一致或缺失）"
                                  "且（首作者一致或缺失）".format(min_similarity))}
        if accepted:
            return R.ok(data, tool=E22, diagnostics=diag)
        if cands:
            return R.partial(data, tool=E22,
                             reason="有候选但没有达到采纳门槛（保守起见不采纳）",
                             code="DATA_LOW_QUALITY", diagnostics=diag)
        return R.empty(tool=E22, reason="Crossref 没有匹配到候选", diagnostics=diag)
    except Exception as exc:
        return R.from_exception(exc, tool=E22, component=E22,
                                context={"title": title[:60]})


# =============================================================================
# 全文元件
# =============================================================================

def find_pdf_urls(paper: Optional[dict] = None, *, doi: str = "",
                  arxiv_id: str = "") -> Dict[str, Any]:
    """[E30] 列出某论文的**全部候选 PDF 地址**（按优先级排序，不下载）。

    用途：下载失败时先看有哪些候选，再决定换哪个源。

    参数
    ----
    paper    : 统一 Paper 字典（有 identity / full_text 时最准）
    doi / arxiv_id : 没有 paper 时可直接给 ID（会先补 OA 位置）

    返回 data = {"urls": [{url, source, priority}], "n": n}
        priority 越小越优先：arXiv(10) → OpenAlex/S2(20) → Unpaywall(30) → 其他(40)
    """
    try:
        import fulltext as ft
        from model import Paper, new_paper
        if paper:
            p = Paper.from_dict(paper)
        else:
            if not (doi or arxiv_id):
                return R.failed("INPUT_MISSING",
                                message="需要 paper 或 doi/arxiv_id 之一",
                                tool=E30, component=E30)
            p = new_paper({"doi": doi, "arxiv_id": arxiv_id})
        urls = [{"url": u, "source": s,
                 "priority": 10 if s == "arxiv" else (20 if "openalex" in s or "s2" in s else 30)}
                for u, s in ft.candidate_urls(p)]
        data = {"urls": urls, "n": len(urls)}
        diag = {"n_results": len(urls),
                "has_arxiv": any(u["source"] == "arxiv" for u in urls)}
        if urls:
            return R.ok(data, tool=E30, diagnostics=diag)
        # 没候选是**正常情形**（闭源论文），用 empty 而不是 failed
        return R.empty(tool=E30, code="NOTFOUND_PDF",
                       reason="该论文没有可用的开放获取 PDF 地址（闭源属正常）",
                       diagnostics=diag)
    except Exception as exc:
        return R.from_exception(exc, tool=E30, component=E30)


def download_pdf(paper: Optional[dict] = None, *, doi: str = "", arxiv_id: str = "",
                 out_dir: str = "", max_mb: float = 30.0,
                 verify: bool = True) -> Dict[str, Any]:
    """[E31] 下载 PDF 并**校验完整性**。

    参数
    ----
    paper     : 统一 Paper 字典（优先用它的 pdf_url / alt_locations）
    out_dir   : 保存目录；留空则用系统临时目录（返回真实路径）
    max_mb    : 大小上限（超过则放弃，防呆）
    verify    : 是否做完整性校验（%PDF- 头 + %%EOF 尾 + pypdf 可打开）。
                **建议保持 True** —— 去掉后截断文件会被当成功。

    返回 data = {"path", "bytes", "sha256", "source", "url", "verified"}
      status: ok / empty（无可用地址，闭源正常情形）/ failed

    ⚠️ 失败时会**依次尝试候选地址**（arXiv → OA → Unpaywall 回退），
       最终在 diagnostics.attempts 里给出每个地址的失败原因。
    """
    t0 = time.time()
    try:
        import tempfile, hashlib
        import fulltext as ft
        from model import Paper, new_paper
        from config import PipelineConfig
        from store import CorpusStore

        p = Paper.from_dict(paper) if paper else new_paper(
            {"doi": doi, "arxiv_id": arxiv_id})
        out = out_dir or tempfile.mkdtemp(prefix="toolspy_pdf_")
        os.makedirs(out, exist_ok=True)
        store = CorpusStore(out)
        store.pdf_dir = out
        cfg = PipelineConfig(out_dir=out, max_pdf_mb=max_mb, verbose=False)
        r = ft.acquire_pdf(p, store, cfg)
        if not r.get("ok"):
            diag = {"elapsed_sec": round(time.time() - t0, 3),
                    "attempts": ft.candidate_urls(p), "error": r.get("error", "")[:200]}
            # 没有候选地址 = 闭源，属 empty 而非 failed
            if "没有可用" in (r.get("error") or ""):
                return R.empty(tool=E31, code="NOTFOUND_PDF",
                               reason=r.get("error", "无可用 PDF 地址"),
                               diagnostics=diag)
            return R.failed("NOTFOUND_PDF", message=r.get("error", "下载失败"),
                            tool=E31, component=E31,
                            context={"errors": r.get("error", "")[:200]},
                            diagnostics=diag)
        path = p.full_text.get("pdf_path", "")
        data_bytes = open(path, "rb").read() if os.path.isfile(path) else b""
        data = {
            "path": path, "bytes": len(data_bytes),
            "sha256": hashlib.sha256(data_bytes).hexdigest(),
            "source": r.get("source", ""), "url": r.get("url", ""),
            "verified": bool(verify and ft.pdf_integrity(data_bytes)[0]),
        }
        diag = {"elapsed_sec": round(time.time() - t0, 3),
                "bytes": len(data_bytes), "source": r.get("source", "")}
        warns = []
        if verify and not data["verified"]:
            warns.append({"code": "PARSE_PDF_INVALID",
                          "message": "PDF 完整性校验未通过",
                          "hint": "文件可能被截断；建议换源重试"})
        return R.ok(data, tool=E31, warnings=warns, diagnostics=diag)
    except Exception as exc:
        return R.from_exception(exc, tool=E31, component=E31)


def resolve_oa(doi: str, *, only_missing: bool = True,
               current_pdf_url: str = "") -> Dict[str, Any]:
    """[E32] Unpaywall：DOI → 开放获取位置。

    参数
    ----
    only_missing    : True 时若已有 pdf_url 就跳过（省配额）
    current_pdf_url : 与 only_missing 配合 —— 已有的地址

    返回 data = {"best_pdf", "oa_status", "version", "license", "locations": [...]}
      没有 OA 版本时 status="empty"（**正常情形**，不是错误）。
    """
    t0 = time.time()
    try:
        if not (doi or "").strip():
            return R.failed("INPUT_MISSING", message="doi 不能为空", tool=E32,
                            component=E32, context={"missing": "doi"})
        import unpaywall as up, _common
        r = up.Unpaywall(email=_common.get_key("PAPERSEARCH_EMAIL")).get_by_doi(doi)
        st = r.get("status")
        if st == 404:
            return R.empty(tool=E32, code="NOTFOUND_DOI",
                           reason="Unpaywall 没有这个 DOI 的记录",
                           diagnostics={"elapsed_sec": round(time.time() - t0, 3)})
        if st != 200:
            return R.failed("RATE_LIMITED" if st == 429 else "NET_HTTP_ERROR",
                            message="Unpaywall HTTP {}".format(st),
                            tool=E32, component=E32, context={"status": st})
        data = {"best_pdf": r.get("best_pdf", ""), "is_oa": r.get("is_oa"),
                "oa_status": r.get("oa_status", ""), "version": r.get("version", ""),
                "license": r.get("license", ""),
                "locations": [{"url": l.get("pdf") or l.get("url"),
                               "host": l.get("host"), "version": l.get("version")}
                              for l in (r.get("all_locations") or [])[:10]]}
        diag = {"elapsed_sec": round(time.time() - t0, 3),
                "is_oa": data["is_oa"], "n_locations": len(data["locations"])}
        if not data["best_pdf"]:
            return R.empty(tool=E32, code="NOTFOUND_PDF",
                           reason="该 DOI 没有开放获取的 PDF（oa_status={}）".format(
                               data["oa_status"] or "closed"),
                           diagnostics=diag)
        return R.ok(data, tool=E32, diagnostics=diag)
    except Exception as exc:
        return R.from_exception(exc, tool=E32, component=E32, context={"doi": doi[:60]})

# =============================================================================
# 解析元件
# =============================================================================

def grobid_available(url: str = "http://localhost:8070") -> Dict[str, Any]:
    """[E40] 检查 GROBID 服务是否可用（**解析前先调它**）。

    为什么要单独一个元件：GROBID 请求超时是 120s，服务没起的话
    每篇都会白等两分钟。一次探活（几秒）换掉 N 次超时。

    返回 data = {"available", "version", "url", "note"}
      服务没起时 status="blocked"，error.hint 给出启动命令。
    """
    t0 = time.time()
    try:
        import grobid as g
        cli = g.Grobid(base_url=url)
        alive, msg = cli.isalive()
        ver = ""
        if alive:
            _, v = cli.version()
            ver = str(v)[:80]
        diag = {"elapsed_sec": round(time.time() - t0, 3), "url": url}
        data = {"available": bool(alive), "version": ver, "url": url,
                "note": "GROBID 可用，可做结构化解析（含坐标/引文连接）" if alive
                        else "未启动；解析会降级为纯文本（无章节结构/无坐标）"}
        if alive:
            return R.ok(data, tool=E40, diagnostics=diag)
        return R.blocked("SERVICE_UNAVAILABLE", tool=E40, component=E40,
                         message="GROBID 服务不可用：{}".format(str(msg)[:100]),
                         hint="启动：cd PaperSearch/ExamplePy && ./grobid-serve.sh start（约 20 秒）",
                         context={"url": url}, diagnostics=diag)
    except Exception as exc:
        return R.from_exception(exc, tool=E40, component=E40)


def parse_pdf(pdf_path: str, *, url: str = "http://localhost:8070",
              elements: Optional[List[str]] = None,
              consolidate_citations: int = 0,
              keep_tei: bool = True, out_dir: str = "",
              paper_id: str = "") -> Dict[str, Any]:
    """[E41] PDF → 结构化元件（**本库最核心的元件**）。

    参数
    ----
    pdf_path  : 本地 PDF 路径
    paper_id  : 产物文件的命名依据。**建议传真实的 paper_id**（如 doi:10.xxx）——
                不传时自动用 PDF 文件名+路径哈希生成。
                它决定 `structures/<name>.json` 与 `.tei.xml` 的文件名：
                **不同论文必须给不同值**，否则产物会互相覆盖。
    elements  : 要坐标的元素列表。默认 ["figure","table","formula","p","head"]
                —— 含 p/head 才能给句子级 span 配坐标。
                只要图表坐标可传 ["figure","table","formula"]（TEI 更小）。
    consolidate_citations : 0（默认）。**别设 1** —— 那会让 GROBID 逐条参考文献
                查 Crossref，实测同一篇 1.6s → 220s（慢 140 倍）。
    keep_tei  : 是否保留原始 TEI（证据留存，建议 True）
    out_dir   : 产物目录；留空用临时目录

    返回 data = {
        "parsed_by": "grobid" | "pypdf",
        "sections":       [{title, level, paragraphs, paragraph_list, text}],
        "spans":          [{span_id, section, char_start, char_end, text,
                            coords, coords_level, page_first, page_last}],
        "figure_objects": [{id, label, text, coords, coords_boxes_per_page, ...}],
        "table_objects":  [...], "formula_objects": [...],
        "references":     [{title, authors, year, doi}],     # 参考文献条目
        "citation_links": {links, unlinked, link_rate, ...}, # 正文↔文献连接
        "coordinates":    {count, by_element, elements},      # 段落级坐标
        "tei_path", "structure_path",
        "stats": {n_sections, n_paragraphs, n_spans, n_figures, n_citation_links}
    }

    ★ 坐标精度如实标注：`coords_level="paragraph"` —— GROBID 只给段落级框，
      句子级框需要逐 token 坐标（它不提供）。硬估会给**假精度**。

    ★ 降级行为：GROBID 不可用时自动退回 pypdf 纯文本，
      `parsed_by="pypdf"` 且 **sections/spans 为空** —— 不假装有结构。

    示例
    ----
        >>> r = parse_pdf("paper.pdf")
        >>> r["data"]["stats"]
        {'n_sections': 8, 'n_spans': 236, 'n_figures': 8, ...}
    """
    t0 = time.time()
    try:
        import tempfile
        import fulltext as ft
        from model import new_paper
        from config import PipelineConfig
        from store import CorpusStore

        if not pdf_path:
            return R.failed("INPUT_MISSING", message="pdf_path 不能为空", tool=E41,
                            component=E41, context={"missing": "pdf_path"})
        if not os.path.isfile(pdf_path):
            return R.failed("INPUT_NOT_FOUND",
                            message="PDF 不存在：{}".format(pdf_path[:200]),
                            tool=E41, component=E41,
                            context={"pdf_path": pdf_path[:200]})

        out = out_dir or tempfile.mkdtemp(prefix="toolspy_parse_")
        os.makedirs(out, exist_ok=True)
        store = CorpusStore(out)
        store.structures_dir = out
        cfg = PipelineConfig(out_dir=out, verbose=False, grobid_url=url,
                             keep_tei=keep_tei)
        cfg.coordinate_elements = tuple(elements or ("figure", "table", "formula", "p", "head"))
        cfg.tei_coordinates = True

        # ★ paper_id 必须**唯一且稳定**（Agent 实测发现的严重 bug）：
        #   第一版用 `new_paper({"doi": ""}, "")` —— paper_id 为空，
        #   save_structure() 用 safe_name(paper_id) 拼文件名得到 `unknown`，
        #   于是**同一目录下解析多篇 PDF 会全部写到 unknown.json / unknown.tei.xml**，
        #   后一篇覆盖前一篇，而 papers.jsonl 里两篇指向同一个文件。
        #   下游打开哪篇拿到的都是最后一篇的内容 —— 数据静默错位。
        #   现在用 PDF 文件名（含扩展名）生成稳定 ID：同一文件得到同一 ID，
        #   不同文件必然不同 ID。
        import hashlib as _h
        _stem = os.path.basename(pdf_path)
        # 追加路径哈希：不同目录下的同名 PDF（如多个 "paper.pdf"）也各自独立
        _pfx = _h.sha1(os.path.abspath(pdf_path).encode("utf-8")).hexdigest()[:8]
        # 调用方给了 paper_id 就用它（流水线场景：论文已有稳定主键）；
        # 没给才自动生成（单文件解析场景）。
        _pid = paper_id or "pdf:{}:{}".format(_pfx, _stem)
        p = new_paper({}, "")
        p.paper_id = _pid
        p.metadata["title"] = _stem
        p.full_text["pdf_path"] = pdf_path
        # ★ 必须显式探活并传布尔值（实测踩坑）：
        #   `grobid_ok=None` 在 parse_structure 里的语义是"由你决定"，
        #   但它内部的探活路径与本元件的 url 参数不一致 —— 结果 E40 报"可用"、
        #   E41 却判"不可用"，直接降级成 pypdf。
        #   这里先探活，把确定结果传进去，两个元件行为一致。
        _alive, _ = ft.grobid_available(cfg)
        r = ft.parse_structure(p, store, cfg, grobid_ok=bool(_alive))
        if not r.get("ok"):
            return R.failed("PARSE_EMPTY",
                            message=str(r.get("error") or "解析失败")[:200],
                            tool=E41, component=E41,
                            context={"pdf_path": pdf_path[:200]})
        ds = p.document_structure
        cl = ds.get("citation_links") or {}
        spans = ds.get("spans") or []
        data = {
            "parsed_by": ds.get("parsed_by", ""),
            "sections": ds.get("sections") or [],
            "spans": spans,
            "figure_objects": ds.get("figure_objects") or [],
            "table_objects": ds.get("table_objects") or [],
            "formula_objects": ds.get("formula_objects") or [],
            "references": ds.get("references_parsed") or [],
            "citation_links": cl,
            "coordinates": ds.get("coordinates") or {},
            "tei_path": ds.get("tei_path", ""),
            "structure_path": ds.get("structure_path", ""),
            "stats": {"n_sections": len(ds.get("sections") or []),
                      "n_paragraphs": ds.get("paragraphs_total", 0),
                      "n_spans": len(spans),
                      "n_spans_with_coords": sum(1 for x in spans if x.get("coords")),
                      "n_figures": len(ds.get("figure_objects") or []),
                      "n_tables": len(ds.get("table_objects") or []),
                      "n_formulas": len(ds.get("formula_objects") or []),
                      "n_references": len(ds.get("references_parsed") or []),
                      "n_citation_links": cl.get("n_linked", 0),
                      "citation_link_rate": cl.get("link_rate")},
        }
        diag = {"elapsed_sec": round(time.time() - t0, 3),
                "pdf_bytes": os.path.getsize(pdf_path), **data["stats"]}
        warns = []
        if ds.get("parsed_by") != "grobid":
            warns.append({"code": "SERVICE_DEGRADED",
                          "message": "GROBID 不可用，已降级为纯文本（无章节/无坐标）",
                          "hint": "启动 GROBID 后重跑：./grobid-serve.sh start"})
        if not spans:
            warns.append({"code": "PARSE_EMPTY",
                          "message": "没有切出任何句子级 span（可能是扫描版无文字层）",
                          "hint": "换一篇或标记为需 OCR"})
        if not data["stats"]["n_sections"]:
            return R.empty(tool=E41, code="PARSE_EMPTY",
                           reason="解析成功但没有章节内容", warnings=warns,
                           diagnostics=diag)
        return (R.partial if warns else R.ok)(data, tool=E41, warnings=warns,
                                              diagnostics=diag,
                                              **({"reason": "有降级或空字段"} if warns else {}))
    except Exception as exc:
        return R.from_exception(exc, tool=E41, component=E41,
                                context={"pdf_path": pdf_path[:200]})


def extract_text(pdf_path: str) -> Dict[str, Any]:
    """[E42] 纯文本兜底（不依赖 GROBID）。

    用途：GROBID 不可用、或只想快速看内容时。
    ⚠️ 它**给不了结构**（无章节、无坐标、无图表）——
       不要用它替代 parse_pdf(E41)，除非你明确只需要文本。

    返回 data = {"text", "n_chars", "n_pages", "method"}
      method: "pypdf" | "pdftotext" | ...
      抽不出文字（扫描版）时 status="empty" 并说明原因。
    """
    t0 = time.time()
    try:
        import grobid as g
        if not pdf_path or not os.path.isfile(pdf_path):
            return R.failed("INPUT_NOT_FOUND",
                            message="PDF 不存在：{}".format(str(pdf_path)[:200]),
                            tool=E42, component=E42,
                            context={"pdf_path": str(pdf_path)[:200]})
        r = g.extract_text_fallback(pdf_path)
        text = r.get("text") or ""
        diag = {"elapsed_sec": round(time.time() - t0, 3),
                "n_chars": len(text), "method": r.get("method", ""),
                "n_pages": r.get("n_pages")}
        if not text:
            return R.empty(tool=E42, code="PARSE_EMPTY",
                           reason="抽不出文字（可能是扫描版，需要 OCR）",
                           diagnostics=diag)
        data = {"text": text, "n_chars": len(text),
                "n_pages": r.get("n_pages"), "method": r.get("method", ""),
                "preview": text[:300]}
        return R.ok(data, tool=E42, diagnostics=diag)
    except Exception as exc:
        return R.from_exception(exc, tool=E42, component=E42)


# =============================================================================
# 引用元件
# =============================================================================

def get_references(work_id: str, *, limit: int = 50) -> Dict[str, Any]:
    """[E50] 取某论文的**参考文献**（它引用了谁）。

    参数
    ----
    work_id : OpenAlex ID（W123... / 完整 URL 都行）
    limit   : 最多返回多少条元数据（OpenAlex 的 referenced_works 可能上百条，
              本函数先取 ID 列表再批量换元数据，**超出 limit 的只计数不取详情**）

    返回 data = {"works": [Paper...], "claimed_count": n, "resolved_count": n,
                 "missing_ids": [...]}
        claimed_count：OpenAlex 声称引用了多少篇
        resolved_count：实际取到元数据的篇数
        ★ 两个数可能不等（有些被引文献 OpenAlex 没有元数据）——
          如实给出，不要用一个数字掩盖。
    """
    t0 = time.time()
    try:
        if not (work_id or "").strip():
            return R.failed("INPUT_MISSING", message="work_id 不能为空", tool=E50,
                            component=E50, context={"missing": "work_id"})
        import openalex as ex, _common, sources as _src
        oa = ex.OpenAlex(email=_common.get_key("PAPERSEARCH_EMAIL"))
        r = oa.get_references(work_id)
        if r.get("status") != 200:
            return R.failed("NET_HTTP_ERROR",
                            message="OpenAlex HTTP {}".format(r.get("status")),
                            tool=E50, component=E50,
                            context={"work_id": work_id[:60]})
        works = []
        for w in (r.get("works") or [])[:limit]:
            try:
                works.append(_src.paper_from_openalex(w, "").to_dict())
            except Exception:
                continue
        data = {"works": works, "claimed_count": r.get("count", 0),
                "resolved_count": len(works),
                "missing_ids": (r.get("missing_ids") or [])[:50]}
        diag = {"elapsed_sec": round(time.time() - t0, 3),
                "n_results": len(works), "claimed": data["claimed_count"]}
        if not works and not data["claimed_count"]:
            return R.empty(tool=E50, code="NOTFOUND_EMPTY_RESULT",
                           reason="该论文没有参考文献记录（正常空集）",
                           diagnostics=diag)
        if not works:
            return R.partial(data, tool=E50, reason="有声明的引用但取不到元数据",
                             code="DATA_LOW_QUALITY", diagnostics=diag)
        return R.ok(data, tool=E50, diagnostics=diag)
    except Exception as exc:
        return R.from_exception(exc, tool=E50, component=E50,
                                context={"work_id": str(work_id)[:60]})


def get_citations(work_id: str, *, limit: int = 50) -> Dict[str, Any]:
    """[E51] 取某论文的**被引**（谁引用了它）。

    参数
    ----
    work_id : OpenAlex ID
    limit   : 最多多少条

    返回 data = {"works": [Paper...], "total_count": n}
        total_count 是该论文被引总数（可能远大于 limit）。

    提示：想知道"哪些引用是正面/负面"需要 citation context ——
          那是 Semantic Scholar 的独有能力，见 API.md 的 S2 小节。
    """
    t0 = time.time()
    try:
        if not (work_id or "").strip():
            return R.failed("INPUT_MISSING", message="work_id 不能为空", tool=E51,
                            component=E51, context={"missing": "work_id"})
        import openalex as ex, _common, sources as _src
        oa = ex.OpenAlex(email=_common.get_key("PAPERSEARCH_EMAIL"))
        r = oa.get_citations(work_id, limit=limit)
        if r.get("status") != 200:
            return R.failed("NET_HTTP_ERROR",
                            message="OpenAlex HTTP {}".format(r.get("status")),
                            tool=E51, component=E51,
                            context={"work_id": work_id[:60]})
        works = []
        for w in (r.get("works") or [])[:limit]:
            try:
                works.append(_src.paper_from_openalex(w, "").to_dict())
            except Exception:
                continue
        data = {"works": works, "total_count": r.get("count", len(works))}
        diag = {"elapsed_sec": round(time.time() - t0, 3),
                "n_results": len(works), "total": data["total_count"]}
        if not works:
            return R.empty(tool=E51, reason="暂无被引记录（合法空集，新论文常见）",
                           diagnostics=diag)
        return R.ok(data, tool=E51, diagnostics=diag)
    except Exception as exc:
        return R.from_exception(exc, tool=E51, component=E51,
                                context={"work_id": str(work_id)[:60]})


def get_citation_edges(papers: List[dict], *, include_unresolved: bool = True) -> Dict[str, Any]:
    """[E52] 把一批论文抽成**统一的引用边表**（citing → cited）。

    参数
    ----
    papers             : 统一 Paper 字典列表
    include_unresolved : 是否包含"目标不在本批论文内"的边（默认含）

    返回 data = {
        "edges":   [{from, to, rel:"cites", internal, to_raw, sources, ...}],
        "nodes":   [{node_id, kind}],       # 含 unresolved 外部节点
        "stats":   {n_edges, n_internal, n_unresolved, n_nodes}
    }

    ★ 方向契约：**所有边都是 citing → cited，rel 恒为 "cites"**。
      论文自身的 `references` / `cited_by` 只是视角，落盘前统一方向 ——
      下游不需要知道"这条边方向是反的"。

    ★ 未解析目标：`to` 为空但给 `unresolved: <原始ID>` 稳定节点 ID，
      让外部引用在图里也能当节点用，而不是悬空值。
    """
    t0 = time.time()
    try:
        import tempfile
        from model import Paper
        from store import CorpusStore
        if not papers:
            return R.empty(tool=E52, code="NOTFOUND_EMPTY_RESULT",
                           reason="没有传入论文")
        objs = [Paper.from_dict(p) for p in papers]
        d = tempfile.mkdtemp(prefix="toolspy_graph_")
        st = CorpusStore(d)
        n = st.save_graph(objs)
        edges = st.load_graph()
        ids = {p.paper_id for p in objs}
        nodes = [{"node_id": p.paper_id, "kind": "paper",
                  "title": p.metadata.get("title", "")} for p in objs]
        seen = {p.paper_id for p in objs}
        for e in edges:
            u = e.get("unresolved")
            if u and u not in seen:
                seen.add(u)
                nodes.append({"node_id": u, "kind": "unresolved_external"})
        if not include_unresolved:
            edges = [e for e in edges if e.get("internal")]
            nodes = [n for n in nodes if n["kind"] == "paper"]
        data = {"edges": edges, "nodes": nodes,
                "stats": {"n_edges": len(edges),
                          "n_internal": sum(1 for e in edges if e.get("internal")),
                          "n_unresolved": sum(1 for e in edges if not e.get("internal")),
                          "n_nodes": len(nodes)}}
        diag = {"elapsed_sec": round(time.time() - t0, 3),
                "n_results": len(edges), **data["stats"]}
        if not edges:
            return R.empty(tool=E52, reason="这批论文之间没有任何引用关系",
                           diagnostics=diag)
        return R.ok(data, tool=E52, diagnostics=diag)
    except Exception as exc:
        return R.from_exception(exc, tool=E52, component=E52)

def get_works_by_ids(ids: List[str], *, batch_size: int = 50) -> Dict[str, Any]:
    """[E53] **按 ID 批量取元数据**（把 `W123...` / DOI / arXiv ID 变成完整记录）。

    ★ 为什么需要它（Agent 模拟调研时发现的高频断点）：
      引用边里绝大多数目标是**语料库外**的论文，只存了 OpenAlex ID
      （`to_raw: "W32403112"`）。调研时最常问的一句话是
      **"这些被共同引用的论文究竟是什么？"** —— 没有这个元件，
      Agent 只能拿 ID 去猜，或者逐个手写 OpenAlex 查询（那就绕过工具库了）。

      E50/E51 是"从某篇出发找它的引用/被引"，**不是**"给定 ID 反查是什么"。
      语义不同，硬用会得到错误结果（把"这个 ID 的参考文献"当成"这个 ID 的信息"）。

    参数
    ----
    ids        : 混合列表，支持 OpenAlex ID（W123 / 完整 URL）、DOI（10.x/y）、
                 arXiv ID（1809.03057）。**大小写不敏感。**
    batch_size : 每次请求合并多少个 ID（OpenAlex 的 filter 支持 OR 批量；
                 50 是个稳妥值，太大 URL 会超长）

    返回 data = {
        "works": [Paper...],              # 取到的（顺序不保证与输入一致）
        "requested": n, "resolved": n, "missing": n,
        "missing_ids": [...],             # **取不到的 ID 照样列出来**，不静默丢
        "id_map": {"W123": "openalex:w123"}   # 输入 ID → paper_id
    }

    ★ 状态语义：取到一部分 → `partial`（**结果可用**，missing 里能看到缺哪些）；
      一个都没取到 → `empty`。查询失败才是 `failed`。

    示例
    ----
        >>> r = tools.get_works_by_ids(["W2103315867", "10.1609/aaai.v33i01.33012157"])
        >>> for w in r["data"]["works"]:
        ...     print(w["metadata"]["title"])
    """
    t0 = time.time()
    try:
        if not ids:
            return R.failed("INPUT_MISSING", message="ids 不能为空", tool=E53,
                            component=E53, context={"missing": "ids"})
        import openalex as ex, _common, sources as _src
        from model import normalize_doi, normalize_arxiv
        oa = ex.OpenAlex(email=_common.get_key("PAPERSEARCH_EMAIL"))

        # 归一化成 OpenAlex 能识别的形式
        norm: List[str] = []
        invalid: List[str] = []          # 本地就认不出写法的（参数问题，不是网络问题）
        id_map: Dict[str, str] = {}
        for raw in ids:
            v = str(raw or "").strip()
            if not v:
                continue
            if v.lower().startswith("http"):
                short = v.rstrip("/").rsplit("/", 1)[-1]
                norm.append(short)
            elif v.upper().startswith("W") and v[1:].isdigit():
                norm.append(v.upper())
            elif normalize_doi(v):
                norm.append("doi:" + normalize_doi(v))
            elif normalize_arxiv(v):
                norm.append("arxiv:" + normalize_arxiv(v))
            else:
                # ★ 无法识别的写法**不能**原样丢给 API（Agent 实测发现）：
                #   第一版 `norm.append(v)` 把 "???" 直接送过去，
                #   OpenAlex 返回的结构不符合预期，最终被归成
                #   NET_BAD_RESPONSE(retryable=True) —— 让 Agent 去重试一个
                #   **永远不可能成功**的请求。
                #   正确做法：本地就判定为参数问题，记进 invalid 并跳过。
                invalid.append(v)
        if not norm and invalid:
            return R.failed("INPUT_INVALID",
                            message="没有可识别的 ID（支持 OpenAlex W… / DOI / arXiv）",
                            tool=E53, component=E53,
                            hint="ID 应形如 W2103315867 / 10.1609/aaai.v33i01.33012157 / "
                                 "1809.03057。收到的是：{}".format(
                                     [str(x)[:30] for x in invalid[:3]]),
                            context={"invalid_ids": [str(x)[:40] for x in invalid[:5]]})

        works: List[dict] = []
        missing: List[str] = list(norm)
        errs: List[str] = []
        for i in range(0, len(norm), batch_size):
            chunk = norm[i:i + batch_size]
            try:
                r = oa.get_works_by_ids(chunk)
            except Exception as exc:
                errs.append("{}: {}".format(type(exc).__name__, str(exc)[:100]))
                continue
            ok, why = _src.response_ok(r, "openalex")
            if not ok:
                errs.append(why)
                continue
            for w in (r.get("works") or []):
                try:
                    p = _src.paper_from_openalex(w, "")
                    works.append(p.to_dict())
                    # 记下映射，便于调用方把边上的 ID 换回 paper_id
                    for k in (p.identity.get("openalex_id"),
                              p.identity.get("doi"), p.identity.get("arxiv_id")):
                        if k:
                            id_map[k] = p.paper_id
                except Exception:
                    continue

        got = {p["identity"].get("openalex_id", "").lower() for p in works}
        got |= {p["identity"].get("doi", "").lower() for p in works}
        for n in list(missing):
            key = n.split(":", 1)[-1].lower()
            if key in got:
                missing.remove(n)

        data = {"works": works, "requested": len(norm), "resolved": len(works),
                "missing": len(missing), "missing_ids": missing[:50],
                "id_map": id_map}
        diag = {"elapsed_sec": round(time.time() - t0, 3), "n_results": len(works),
                "n_requested": len(norm), "n_missing": len(missing),
                "n_invalid": len(invalid), "invalid_ids": invalid[:10],
                "batches": (len(norm) + batch_size - 1) // batch_size,
                "errors": errs[:3]}
        # partial 必须真的拿到一部分结果。若有效 ID 的请求整体失败/为空，
        # 先报告该真实状态，不能因同时存在无效 ID 就伪装成“部分成功”。
        if invalid and works:
            data["invalid_ids"] = invalid[:50]
            return R.partial(data, tool=E53,
                             reason="{} 个 ID 写法无法识别，已跳过（其余正常处理）".format(
                                 len(invalid)),
                             code="INPUT_INVALID", diagnostics=diag)
        if works and missing:
            return R.partial(data, tool=E53,
                             reason="{}/{} 个 ID 没取到元数据".format(len(missing), len(norm)),
                             code="DATA_PARTIAL", diagnostics=diag)
        if works:
            return R.ok(data, tool=E53, diagnostics=diag)
        if errs:
            return R.failed("NET_BAD_RESPONSE", message=errs[0], tool=E53,
                            component=E53, context={"errors": errs[:3]},
                            warnings=([{"code": "INPUT_INVALID",
                                        "message": "已跳过无效 ID：{}".format(invalid[:10])}]
                                      if invalid else None),
                            diagnostics=diag)
        return R.empty(data=data, tool=E53, code="NOTFOUND_DOI",
                       reason="这些 ID 在 OpenAlex 里都没有记录",
                       warnings=([{"code": "INPUT_INVALID",
                                   "message": "已跳过无效 ID：{}".format(invalid[:10])}]
                                 if invalid else None),
                       diagnostics=diag)
    except Exception as exc:
        return R.from_exception(exc, tool=E53, component=E53,
                                context={"n_ids": len(ids or [])})
