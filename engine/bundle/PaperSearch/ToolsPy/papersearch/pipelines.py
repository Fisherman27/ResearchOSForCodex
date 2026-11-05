#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pipelines.py —— 自动化工具流（把元件整合成一条命令能跑完的流程）

与元件的分工
------------
    元件（elements.py）：一次调用做一件事，无状态，不写盘，适合精细调整
    流水线（本模块）：多步编排 + 断点 + 落盘 + 汇总，适合"一条命令出结果"

三层流水线（由小到大）
----------------------
    P10  search_pipeline      检索 → 去重 → 排序 → 返回论文列表（不抓 PDF）
    P20  acquire_pipeline     在 P10 基础上加：抓 PDF + 结构解析
    P30  full_pipeline        P20 + 引用扩展 + 三层落盘 + 报告（等价于 ProcessPy 的 8 步）

设计契约
--------
1. **每一步都返回信封**，最后汇总成流水线信封（status 取最差的一步，
   但**只要有可用产物就是 ok/partial，绝不因为某一可降级步骤失败而丢弃结果**）；
2. **每步都记进 diagnostics.steps**，含耗时/条目数/状态 —— Agent 据此定位是哪步慢/空；
3. **单步异常不中断流水线**（可降级步骤），但会把异常完整记进该步的 error；
4. **产物落盘可复现**：run.json 记下完整参数快照，同参数重跑结果一致。

函数索引（详见 API.md）
─────────────────────────────────────────────────────────────────────────────
  [P10] search_pipeline(query, ...)     检索+去重+排序
  [P20] acquire_pipeline(query, ...)    检索+全文+解析
  [P30] full_pipeline(query, ...)       完整流程+三层落盘
  [辅助] summarize_result(pipeline_env) 把流水线结果压成一句话（给 Agent 看）
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import os
import sys
import time
from typing import Any, Dict, List, Optional

from ._bootstrap import ensure_importable      # noqa: E402
from . import result as R                      # noqa: E402
from .errors import make_error                 # noqa: E402
from . import elements as E                    # noqa: E402

ensure_importable()

P10, P20, P30 = "P10.search_pipeline", "P20.acquire_pipeline", "P30.full_pipeline"


class _Steps:
    """收集每一步的结果，便于统一汇总。

    为什么要单独一个类：流水线里"某步失败要不要继续"的判断需要看
    **之前所有步**的状态（比如检索全空就没必要继续抓 PDF）。
    把这个判断集中在一处，比在每步里写 if 更不容易出错。
    """

    def __init__(self):
        self.items: List[Dict[str, Any]] = []
        self.t0 = time.time()

    def add(self, name: str, env: dict) -> dict:
        """记录一步（保留完整信封，方便 Agent 溯源到具体错误）。

        ★ `n` 的取法（Agent 实测发现的 bug）：
          第一版写 `len(env["data"])` —— 对 list 是对的，但**对 dict 是错的**：
          `search_all_sources` 的 data 是 `{papers, by_source, source_status, dedup}`，
          `len(dict)` 得到的是**键的个数 4**，不是论文数 24。
          结果摘要里显示 "search n=4" 而 "rank n=24"，
          Agent 会以为"检索只拿到 4 篇、排序却冒出 24 篇" —— 数据不一致的假象，
          足以让 Agent 去查一个根本不存在的问题。

          正确的优先级：
            1. `diagnostics.n_results`（工具自己报的权威数字）
            2. data 是 list → len(data)
            3. data 是 dict 且含常见结果键（papers/works/edges/urls）→ 取该键的长度
            4. 否则 None（**不猜** —— 给个错的数字比不给更糟）
        """
        d = env.get("data")
        diag = env.get("diagnostics") or {}
        n = diag.get("n_results")
        if n is None:
            if isinstance(d, list):
                n = len(d)
            elif isinstance(d, dict):
                for k in ("papers", "works", "edges", "urls", "nodes", "tools",
                          "sources", "candidates", "items"):
                    if isinstance(d.get(k), list):
                        n = len(d[k])
                        break
        self.items.append({
            "step": name,
            "status": env.get("status"),
            "ok": env.get("ok"),
            "n": n,
            "elapsed_sec": diag.get("elapsed_sec"),
            "error": env.get("error"),
            "warnings": env.get("warnings") or [],
        })
        return env

    def worst_status(self) -> str:
        """取最差状态 —— 但有结果时不低于 partial。

        顺序（好→差）：ok < empty < partial < blocked < failed
        ★ empty 比 partial 好：empty 是"干净地没有结果"，
          partial 是"有结果但不全"。两者都不算失败。
        """
        rank = {"ok": 0, "empty": 1, "partial": 2, "blocked": 3, "failed": 4}
        return max((i["status"] for i in self.items),
                   key=lambda s: rank.get(s, 9), default="ok")

    def has_data(self) -> bool:
        return any(i["status"] in ("ok", "partial") for i in self.items)

    def as_dict(self) -> List[dict]:
        return self.items

    def elapsed(self) -> float:
        return round(time.time() - self.t0, 3)

    def all_errors(self) -> List[dict]:
        return [i["error"] for i in self.items if i.get("error")]

    def all_warnings(self) -> List[dict]:
        out = []
        for i in self.items:
            out.extend(i.get("warnings") or [])
        return out

    def failed_steps(self) -> List[str]:
        return [i["step"] for i in self.items if i["status"] in ("failed", "blocked")]


def _finalize(steps: _Steps, data: Any, *, tool: str, params: dict,
              extra_diag: Optional[dict] = None) -> Dict[str, Any]:
    """按所有步骤的汇总结果构造流水线信封。

    判定规则（**这是流水线的核心契约**）：
        有产物 + 无失败步        → ok
        有产物 + 有失败/降级步    → partial（**产物仍可用**）
        无产物 + 全部 blocked     → blocked
        无产物 + 有 failed       → failed
        无产物 + 无失败（真的空）  → empty
    """
    st = steps.worst_status()
    diag = {"elapsed_sec": steps.elapsed(), "steps": steps.as_dict(),
            "failed_steps": steps.failed_steps(),
            "n_steps": len(steps.items)}
    if extra_diag:
        diag.update(extra_diag)
    has = steps.has_data() or bool(data)
    if has and st in ("ok", "empty"):
        return R.envelope("ok", data=data, warnings=steps.all_warnings(),
                          diagnostics=diag, tool=tool, params=params)
    if has:
        return R.envelope("partial", data=data, warnings=steps.all_warnings(),
                          diagnostics=diag, tool=tool, params=params)
    if st == "blocked" or (steps.items and all(i["status"] == "blocked"
                                               for i in steps.items)):
        errs = steps.all_errors()
        return R.envelope("blocked",
                          error=(errs[0] if errs else make_error(
                              "SERVICE_UNAVAILABLE", "所有步骤都缺前提")),
                          warnings=steps.all_warnings(), diagnostics=diag,
                          tool=tool, params=params)
    if st == "failed":
        errs = steps.all_errors()
        return R.envelope("failed",
                          error=(errs[0] if errs else make_error(
                              "INTERNAL_ERROR", "流水线失败但没记录错误")),
                          warnings=steps.all_warnings(), diagnostics=diag,
                          tool=tool, params=params)
    return R.envelope("empty", warnings=steps.all_warnings(), diagnostics=diag,
                      tool=tool, params=params)


# =============================================================================
# P10：检索流水线
# =============================================================================

def search_pipeline(query: str, *, max_per_source: int = 20,
                    keep_top: int = 40, year_from: int = 0, year_to: int = 0,
                    concepts: Optional[List[str]] = None,
                    sources: Optional[List[str]] = None) -> Dict[str, Any]:
    """[P10] 检索 → 去重 → 排序（**不抓 PDF**，快）。

    适用：想先看"这个题目有哪些论文"、或只做文献调研时。

    参数
    ----
    query          : 检索式（多词自由文本即可，会按各源语法转换）
    max_per_source : 每源取多少条
    keep_top       : 排序后保留多少篇（0 = 全留）
    year_from/to   : 年份范围（0 = 不限）
    concepts       : 用于打分的概念词；**留空则自动从 query 抽取**
    sources        : 限定源；None = 全部可用

    返回 data = {
        "papers": [Paper...]（已去重、已排序、含 score.why）,
        "by_source", "source_status", "dedup",
        "ranking": {n_in, n_out, dropped, top1}
    }

    示例
    ----
        >>> r = search_pipeline("counterfactual regret minimization", keep_top=20)
        >>> len(r["data"]["papers"])
        20
        >>> r["data"]["papers"][0]["score"]["why"]     # 为什么排第一
        '标题命中 counterfactual regret minimization; 被引 21'
    """
    params = {"query": query, "max_per_source": max_per_source,
              "keep_top": keep_top, "year_from": year_from, "year_to": year_to,
              "sources": sources or [], "concepts": concepts or []}
    steps = _Steps()
    try:
        if not (query or "").strip():
            return R.failed("INPUT_MISSING", message="query 不能为空", tool=P10,
                            component=P10, params=params,
                            context={"missing": "query"})

        # ---- STEP 1：多源检索 ----
        r = E.search_all_sources(query, max_per_source, sources=sources,
                                 year_from=year_from, year_to=year_to)
        steps.add("search", r)
        papers = ((r.get("data") or {}).get("papers") or []) if r.get("data") else []
        if not papers:
            return _finalize(steps, {"papers": [], "ranking": {}}, tool=P10,
                             params=params)

        # ---- STEP 2：抽取概念（用于打分）----
        cs = list(concepts or [])
        if not cs:
            try:
                import query as _q
                plan = _q.build_queries(query)
                cs = plan.get("core_concepts") or []
                steps.add("extract_concepts",
                          R.ok(cs, tool=P10, diagnostics={"n_results": len(cs)}))
            except Exception as exc:
                steps.add("extract_concepts",
                          R.from_exception(exc, tool=P10, component=P10))
                cs = [w for w in query.replace('"', "").split() if len(w) > 3][:6]

        # ---- STEP 3：打分排序 ----
        ranked = papers
        if keep_top and cs:
            try:
                import rank as _rank
                from config import PipelineConfig
                from model import Paper
                cfg = PipelineConfig()
                objs = [Paper.from_dict(p) for p in papers]
                kept, rep = _rank.rank_papers(objs, cs, cfg, stage="initial")
                ranked = [p.to_dict() for p in kept[:keep_top]] if keep_top else \
                    [p.to_dict() for p in kept]
                steps.add("rank", R.ok(
                    ranked, tool=P10,
                    diagnostics={"n_results": len(ranked),
                                 "n_in": rep["in"], "n_out": rep["out"],
                                 "dropped": rep["dropped"]}))
            except Exception as exc:
                steps.add("rank", R.from_exception(exc, tool=P10, component=P10))
                ranked = papers[:keep_top] if keep_top else papers
        else:
            ranked = papers[:keep_top] if keep_top else papers

        top1 = ranked[0] if ranked else {}
        data = {
            "papers": ranked,
            "by_source": (r.get("data") or {}).get("by_source", {}),
            "source_status": (r.get("data") or {}).get("source_status", {}),
            "dedup": (r.get("data") or {}).get("dedup", {}),
            "concepts": cs,
            "ranking": {"n_in": len(papers), "n_out": len(ranked),
                        "top1_title": (top1.get("metadata") or {}).get("title", ""),
                        "top1_score": (top1.get("score") or {}).get("total", 0),
                        "top1_why": (top1.get("score") or {}).get("why", "")},
        }
        return _finalize(steps, data, tool=P10, params=params)
    except Exception as exc:
        return R.from_exception(exc, tool=P10, component=P10, params=params)


# =============================================================================
# P20：检索 + 全文 + 解析
# =============================================================================

def acquire_pipeline(query: str, *, max_per_source: int = 20, keep_top: int = 20,
                     fetch_top: int = 5, parse: bool = True,
                     year_from: int = 0, year_to: int = 0,
                     out_dir: str = "",
                     coordinate_elements: Optional[List[str]] = None) -> Dict[str, Any]:
    """[P20] 检索 → 排序 → **抓 PDF** → **解析结构**。

    适用：除了找到论文，还要拿到全文与结构化元件（图/表/坐标/span）。

    参数
    ----
    fetch_top : 给排序最靠前的多少篇抓 PDF（**成本控制的关键参数**：
                抓 PDF 是最慢的一步，别一次抓几百篇）
    parse     : 是否解析结构（需要 GROBID；不可用时自动降级为纯文本）
    out_dir   : 产物目录（PDF/TEI 落在这里）；留空用临时目录
    coordinate_elements : 要坐标的元素，默认含 p/head（才能给 span 配坐标）

    返回 data = {
        "query", "papers": [...],
        "fulltext": [{paper_id, ok, path, source, bytes, sha256, error}],
        "parsed":   [{paper_id, ok, parsed_by, stats, structure_path}],
        "out_dir": "..."
    }

    ★ 状态语义：PDF 拿不到（闭源）是 **empty 不是 failed** ——
      流水线会继续处理其他论文，最终 status=partial 并说明有几篇没拿到。
    """
    params = {"query": query, "max_per_source": max_per_source,
              "keep_top": keep_top, "fetch_top": fetch_top, "parse": parse,
              "year_from": year_from, "year_to": year_to, "out_dir": out_dir}
    steps = _Steps()
    try:
        if not (query or "").strip():
            return R.failed("INPUT_MISSING", message="query 不能为空", tool=P20,
                            component=P20, params=params,
                            context={"missing": "query"})
        import tempfile
        out = out_dir or tempfile.mkdtemp(prefix="toolspy_acquire_")
        os.makedirs(out, exist_ok=True)

        # ---- STEP 1：检索 ----
        r = search_pipeline(query, max_per_source=max_per_source,
                            keep_top=keep_top, year_from=year_from,
                            year_to=year_to)
        steps.add("search", r)
        papers = ((r.get("data") or {}).get("papers") or []) if r.get("data") else []
        if not papers:
            return _finalize(steps, {"papers": [], "fulltext": [], "parsed": [],
                                     "out_dir": out}, tool=P20, params=params)

        # ---- STEP 2：抓 PDF ----
        targets = papers[:max(0, fetch_top)]
        ft_results, ft_ok = [], 0
        for p in targets:
            pid = p.get("paper_id", "")
            rr = E.download_pdf(p, out_dir=os.path.join(out, "pdfs"))
            ok = rr["ok"]
            ft_ok += 1 if ok else 0
            ft_results.append({
                "paper_id": pid, "ok": ok, "status": rr["status"],
                "path": (rr.get("data") or {}).get("path", "") if isinstance(rr.get("data"), dict) else "",
                "source": (rr.get("data") or {}).get("source", "") if isinstance(rr.get("data"), dict) else "",
                "bytes": (rr.get("data") or {}).get("bytes", 0) if isinstance(rr.get("data"), dict) else 0,
                "sha256": (rr.get("data") or {}).get("sha256", "") if isinstance(rr.get("data"), dict) else "",
                "error": (rr.get("error") or {}).get("hint", "") if rr.get("error") else "",
                "error_code": (rr.get("error") or {}).get("code", "") if rr.get("error") else "",
            })
            # 把 pdf_path 回填到 paper，供解析用
            if ok and isinstance(rr.get("data"), dict):
                p.setdefault("full_text", {})["pdf_path"] = rr["data"]["path"]
        steps.add("fetch_pdf", R.envelope(
            "ok" if ft_ok == len(targets) else ("partial" if ft_ok else "empty"),
            data=[x for x in ft_results if x["ok"]],
            warnings=[{"code": "NOTFOUND_PDF",
                       "message": "{} 篇没有拿到 PDF（闭源属正常）".format(len(targets) - ft_ok),
                       "hint": "这些论文没有开放获取版本，继续处理其他论文即可"}]
            if ft_ok < len(targets) else [],
            diagnostics={"elapsed_sec": 0, "n_results": ft_ok,
                         "n_targets": len(targets), "details": ft_results}))

        # ---- STEP 3：解析结构 ----
        parsed = []
        if parse:
            for p in papers:
                pp = (p.get("full_text") or {}).get("pdf_path", "")
                if not pp or not os.path.isfile(pp):
                    continue
                pr = E.parse_pdf(pp, elements=coordinate_elements,
                                 out_dir=os.path.join(out, "structures"),
                                 paper_id=p.get("paper_id", ""))
                item = {"paper_id": p.get("paper_id", ""), "ok": pr["ok"],
                        "status": pr["status"],
                        "parsed_by": (pr.get("data") or {}).get("parsed_by", "")
                                     if isinstance(pr.get("data"), dict) else "",
                        "stats": (pr.get("data") or {}).get("stats", {})
                                 if isinstance(pr.get("data"), dict) else {},
                        "structure_path": (pr.get("data") or {}).get("structure_path", "")
                                          if isinstance(pr.get("data"), dict) else "",
                        "tei_path": (pr.get("data") or {}).get("tei_path", "")
                                    if isinstance(pr.get("data"), dict) else "",
                        "error": (pr.get("error") or {}).get("hint", "") if pr.get("error") else ""}
                parsed.append(item)
                if pr["ok"] and isinstance(pr.get("data"), dict):
                    # 把结构挂回 paper（就地富化，调用方拿到的是完整对象）
                    p["document_structure"] = {
                        "parsed_by": item["parsed_by"],
                        "sections": pr["data"].get("sections", []),
                        "spans": pr["data"].get("spans", []),
                        "figure_objects": pr["data"].get("figure_objects", []),
                        "table_objects": pr["data"].get("table_objects", []),
                        "formula_objects": pr["data"].get("formula_objects", []),
                        "citation_links": pr["data"].get("citation_links", {}),
                        "coordinates": pr["data"].get("coordinates", {}),
                        "tei_path": item["tei_path"],
                        "structure_path": item["structure_path"],
                    }
            ok_n = sum(1 for x in parsed if x["ok"])
            grobid_n = sum(1 for x in parsed if x["parsed_by"] == "grobid")
            steps.add("parse", R.envelope(
                "ok" if ok_n == len(parsed) and parsed else
                ("partial" if ok_n else "empty"),
                data=[x for x in parsed if x["ok"]],
                warnings=([{"code": "SERVICE_DEGRADED",
                            "message": "{} 篇降级为纯文本（GROBID 不可用）".format(
                                ok_n - grobid_n),
                            "hint": "启动 GROBID 后重跑可拿到完整结构"}]
                          if ok_n > grobid_n else []),
                diagnostics={"elapsed_sec": 0, "n_results": ok_n,
                             "n_targets": len(parsed), "n_grobid": grobid_n,
                             "details": parsed}))

        data = {"query": query, "papers": papers, "fulltext": ft_results,
                "parsed": parsed, "out_dir": out}
        return _finalize(steps, data, tool=P20, params=params,
                         extra_diag={"n_papers": len(papers), "n_pdf": ft_ok,
                                     "n_parsed": sum(1 for x in parsed if x["ok"])})
    except Exception as exc:
        return R.from_exception(exc, tool=P20, component=P20, params=params)


# =============================================================================
# P30：完整流水线（等价于 ProcessPy 的 8 步）
# =============================================================================

def full_pipeline(query: str, *, out_dir: str = "", max_per_source: int = 20,
                  keep_top: int = 30, expand_top: int = 4, fetch_top: int = 8,
                  parse_top: int = 8, year_from: int = 0, year_to: int = 0,
                  expand: bool = True, fetch_pdf: bool = True,
                  parse: bool = True) -> Dict[str, Any]:
    """[P30] 完整流程：检索 → 去重 → 排序 → 引用扩展 → 抓 PDF → 解析 → 三层落盘。

    等价于 `ProcessPy` 的 8 步流水线，但**返回值是信封**（不打印、不依赖 CLI），
    并且把每一步的状态都放进 diagnostics —— Agent 能精确定位是哪步出了问题。

    参数
    ----
    out_dir    : 产物目录。留空则用 `<临时目录>/toolspy_full_<时间>`。
                 **建议显式指定** —— 产物要留着给下游用。
    expand_top : 对排序最靠前的多少篇做引用扩展（0 = 不扩展）
    fetch_top  : 给多少篇抓 PDF
    parse_top  : 解析多少篇（应为 fetch_top 的子集）
    expand     : 是否做引用网络扩展

    返回 data = {
        "papers": [Paper...]（含 document_structure）,
        "corpus_path", "graph_path", "report_path",
        "run_manifest": {...},
        "stats": {n_papers, n_edges, n_pdf, n_parsed, ...}
    }

    产物落盘（三层结构）：
        <out>/papers.jsonl       Layer 1 论文（含 spans / 图表对象 / 引文连接）
        <out>/citation_edges.jsonl Layer 2 引用边（统一 citing→cited）
        <out>/structures/        Layer 3 结构文档 + TEI
        <out>/pdfs/              原始 PDF
        <out>/run.json           参数快照 + 逐步状态（**复现只需这个文件**）

    示例
    ----
        >>> r = full_pipeline("deep cfr", out_dir="/tmp/kb", fetch_top=3)
        >>> r["status"], r["data"]["stats"]["n_papers"]
        ('partial', 30)
    """
    params = {"query": query, "max_per_source": max_per_source,
              "keep_top": keep_top, "expand_top": expand_top,
              "fetch_top": fetch_top, "parse_top": parse_top,
              "year_from": year_from, "year_to": year_to,
              "expand": expand, "fetch_pdf": fetch_pdf, "parse": parse}
    steps = _Steps()
    try:
        if not (query or "").strip():
            return R.failed("INPUT_MISSING", message="query 不能为空", tool=P30,
                            component=P30, params=params,
                            context={"missing": "query"})
        import json
        out = out_dir or os.path.join(
            __import__("tempfile").mkdtemp(prefix="toolspy_full_"), "")
        os.makedirs(out, exist_ok=True)

        # ---- STEP 1-5：检索 + 去重 + 排序 ----
        r = search_pipeline(query, max_per_source=max_per_source,
                            keep_top=keep_top, year_from=year_from,
                            year_to=year_to)
        steps.add("search", r)
        papers = ((r.get("data") or {}).get("papers") or []) if r.get("data") else []
        concepts = ((r.get("data") or {}).get("concepts") or []) if r.get("data") else []
        if not papers:
            _dump_run(out, params, steps, status="empty")
            return _finalize(steps, {"papers": [], "out_dir": out}, tool=P30,
                             params=params)

        # ---- STEP 6：引用扩展 ----
        if expand and expand_top > 0:
            try:
                import sources as _src
                from config import PipelineConfig
                from model import Paper
                import dedup as _dd
                import rank as _rank
                cfg = PipelineConfig()
                cfg.expand_top_n = expand_top
                seeds = [Paper.from_dict(p) for p in papers[:expand_top]]
                new, est = _src.expand_citations(seeds, cfg)
                if new:
                    objs = [Paper.from_dict(p) for p in papers] + new
                    merged, rep = _dd.resolve(objs, cfg)
                    if rep.get("alias"):
                        _dd.apply_alias(merged, rep["alias"])
                    _dd.rekey(merged)
                    kept, _ = _rank.rank_papers(merged, concepts, cfg, stage="expanded")
                    papers = [p.to_dict() for p in kept]
                steps.add("expand", R.envelope(
                    "ok" if new else "empty",
                    data=[p.to_dict() for p in new],
                    warnings=[{"code": "AUTH_MISSING_KEY", "message": str(e)[:120],
                               "hint": "补 key 后该源即可参与扩展"}
                              for e in (est.get("errors") or [])[:2]],
                    diagnostics={"elapsed_sec": 0, "n_results": len(new),
                                 "refs": est.get("refs", 0), "cites": est.get("cites", 0),
                                 "errors": (est.get("errors") or [])[:5]}))
            except Exception as exc:
                steps.add("expand", R.from_exception(exc, tool=P30, component=P30))

        # ---- STEP 7：抓 PDF ----
        ft_results, ft_ok = [], 0
        if fetch_pdf and fetch_top > 0:
            for p in papers[:fetch_top]:
                rr = E.download_pdf(p, out_dir=os.path.join(out, "pdfs"))
                ok = rr["ok"]
                ft_ok += 1 if ok else 0
                d = rr.get("data") if isinstance(rr.get("data"), dict) else {}
                ft_results.append({"paper_id": p.get("paper_id", ""), "ok": ok,
                                   "status": rr["status"], "path": d.get("path", ""),
                                   "source": d.get("source", ""), "bytes": d.get("bytes", 0),
                                   "sha256": d.get("sha256", ""),
                                   "error_code": (rr.get("error") or {}).get("code", "") if rr.get("error") else ""})
                if ok:
                    p.setdefault("full_text", {})["pdf_path"] = d.get("path", "")
            steps.add("fetch_pdf", R.envelope(
                "ok" if ft_ok == min(fetch_top, len(papers)) else ("partial" if ft_ok else "empty"),
                data=[x for x in ft_results if x["ok"]],
                warnings=[{"code": "NOTFOUND_PDF",
                           "message": "{} 篇未获取到 PDF".format(len(ft_results) - ft_ok),
                           "hint": "闭源论文没有 OA 版本，属正常；继续处理其他论文"}]
                if ft_ok < len(ft_results) else [],
                diagnostics={"n_results": ft_ok, "n_targets": len(ft_results),
                             "details": ft_results}))

        # ---- STEP 8：解析结构 ----
        parsed = []
        if parse and parse_top > 0:
            n = 0
            for p in papers:
                pp = (p.get("full_text") or {}).get("pdf_path", "")
                if not pp or not os.path.isfile(pp):
                    continue
                if n >= parse_top:
                    break
                n += 1
                pr = E.parse_pdf(pp, out_dir=os.path.join(out, "structures"),
                                 paper_id=p.get("paper_id", ""))
                d = pr.get("data") if isinstance(pr.get("data"), dict) else {}
                parsed.append({"paper_id": p.get("paper_id", ""), "ok": pr["ok"],
                               "status": pr["status"], "parsed_by": d.get("parsed_by", ""),
                               "stats": d.get("stats", {}),
                               "structure_path": d.get("structure_path", ""),
                               "tei_path": d.get("tei_path", "")})
                if pr["ok"]:
                    p["document_structure"] = {
                        "parsed_by": d.get("parsed_by", ""),
                        "sections": d.get("sections", []),
                        "spans": d.get("spans", []),
                        "figure_objects": d.get("figure_objects", []),
                        "table_objects": d.get("table_objects", []),
                        "formula_objects": d.get("formula_objects", []),
                        "citation_links": d.get("citation_links", {}),
                        "coordinates": d.get("coordinates", {}),
                        "tei_path": d.get("tei_path", ""),
                        "structure_path": d.get("structure_path", ""),
                    }
            ok_n = sum(1 for x in parsed if x["ok"])
            steps.add("parse", R.envelope(
                "ok" if ok_n == len(parsed) and parsed else ("partial" if ok_n else "empty"),
                data=[x for x in parsed if x["ok"]],
                diagnostics={"n_results": ok_n, "n_targets": len(parsed),
                             "details": parsed}))

        # ---- STEP 9：三层落盘 ----
        try:
            from model import Paper
            from store import CorpusStore, summarize
            st = CorpusStore(out, papers_name="papers.jsonl",
                             graph_name="citation_edges.jsonl")
            st.pdf_dir = os.path.join(out, "pdfs")
            st.structures_dir = os.path.join(out, "structures")
            # 补全缺失的 document_structure（否则下游字段不全）
            from model import empty_structure
            for p in papers:
                p.setdefault("document_structure", empty_structure())
            n_papers = st.save_papers([Paper.from_dict(p) for p in papers])
            n_edges = st.save_graph([Paper.from_dict(p) for p in papers])
            edges = st.load_graph()
            summary = summarize([Paper.from_dict(p) for p in papers], edges)
            data = {
                "papers": papers,
                "corpus_path": os.path.join(out, "papers.jsonl"),
                "graph_path": os.path.join(out, "citation_edges.jsonl"),
                "structures_dir": os.path.join(out, "structures"),
                "pdfs_dir": os.path.join(out, "pdfs"),
                "out_dir": out,
                "stats": {"n_papers": len(papers), "n_edges": n_edges,
                          "n_pdf": ft_ok, "n_parsed": sum(1 for x in parsed if x["ok"]),
                          "n_with_abstract": summary.get("with_abstract", 0),
                          "n_with_structure": summary.get("with_structure", 0),
                          "n_with_citation_graph": summary.get("with_citation_graph", 0),
                          "edges_internal": summary.get("edges_internal", 0)},
            }
            steps.add("persist", R.ok(
                {"papers": n_papers, "edges": n_edges}, tool=P30,
                diagnostics={"n_results": n_papers, "n_edges": n_edges}))
            _dump_run(out, params, steps, status=steps.worst_status(),
                      data_stats=data["stats"])
            return _finalize(steps, data, tool=P30, params=params,
                             extra_diag={"out_dir": out, **data["stats"]})
        except Exception as exc:
            steps.add("persist", R.from_exception(exc, tool=P30, component=P30))
            return _finalize(steps, {"papers": papers, "out_dir": out}, tool=P30,
                             params=params)
    except Exception as exc:
        return R.from_exception(exc, tool=P30, component=P30, params=params)


def _dump_run(out: str, params: dict, steps: "_Steps", *, status: str,
              data_stats: Optional[dict] = None) -> None:
    """写 run.json —— 参数快照 + 逐步状态。

    ★ 为什么必须有它：**复现一次运行只需要这个文件**。
      出问题时 Agent/人可以拿它对照参数、看是哪一步的什么状态，
      不用去猜当时传了什么。
    """
    import json
    try:
        payload = {
            "tool": P30, "params": params, "status": status,
            "elapsed_sec": steps.elapsed(),
            "steps": steps.as_dict(),
            "stats": data_stats or {},
        }
        with open(os.path.join(out, "run.json"), "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
    except Exception:
        # run.json 写不出来不该让整个流水线失败 —— 它是诊断产物不是主产物
        pass


# =============================================================================
# 辅助
# =============================================================================

def summarize_result(env: dict) -> str:
    """把流水线信封压成**给人/Agent 看的一段话**。

    比 result.pretty() 详细：列出每一步的状态与关键数字，
    让 Agent 一眼看出"哪步成了、哪步没成、缺什么"。

    示例
    ----
        [partial] P30.full_pipeline 耗时 42.1s ｜ 4 步
          ✓ search     ok       30 项
          ✓ expand     ok       12 项
          ~ fetch_pdf  partial  3/8
          ✓ parse      ok       3 项
        ⚠ 5 篇未获取到 PDF（闭源属正常） ｜ 继续处理其他论文
    """
    if not env:
        return "空信封"
    lines = []
    d = env.get("diagnostics") or {}
    lines.append("[{}] {} 耗时 {}s ｜ {} 步".format(
        env.get("status"), (env.get("meta") or {}).get("tool", "?"),
        d.get("elapsed_sec", "?"), d.get("n_steps", 0)))
    for s in (d.get("steps") or []):
        mark = {"ok": "✓", "partial": "~", "empty": "○",
                "blocked": "⛔", "failed": "✗"}.get(s.get("status"), "?")
        lines.append("  {} {:<12} {:<8} {}".format(
            mark, s.get("step", ""), s.get("status", ""),
            "{} 项".format(s["n"]) if s.get("n") is not None else ""))
    for w in (env.get("warnings") or [])[:3]:
        # ★ 防御：warning 必须是 dict。若上游误传了 list/str，
        #   这里**不能崩** —— summarize_result 是 Agent 读结果的主要入口，
        #   它挂了会让"有结果但看不到"变成"什么都看不到"（比原始问题更糟）。
        if isinstance(w, dict):
            lines.append("⚠ {} ｜ {}".format((w.get("message") or "")[:70],
                                            (w.get("hint") or "")[:60]))
        else:
            lines.append("⚠ {}".format(str(w)[:110]))
    if env.get("error"):
        lines.append("✗ {} ｜ {}".format(env["error"].get("code"),
                                        env["error"].get("hint", "")[:70]))
    return "\n".join(lines)
