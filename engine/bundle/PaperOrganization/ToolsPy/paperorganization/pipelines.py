"""流水线层（自动化工具流）。

把元件层（importers/writers/store/exporters/...）组装成**开箱即用的完整流程**。
Agent 可以直接调这些函数，也可以下探到元件层做精细调整。

包含：
    init_kb(cfg)                       初始化知识库（建表）
    import_search_output(...)           PaperSearch 产物 → 知识库（完整导入流程）
    store_reading_results(...)          批量落库 PaperRead 结果
    store_reading_results_file(...)     从 JSON 文件批量落库
    rebuild_exports(cfg)                重新导出 TSV
    run_import_pipeline(...)            一键：初始化 + 导入 + 导出
"""
from __future__ import annotations

import json
import os
import shutil
from typing import Any, Dict, List, Optional

from . import importers, writers
from .errors import (ErrorCode, ToolResult, Timer, fail, fail_from_exception,
                     ok, partial)
from .paths import KBConfig, resolve_kb
from .schema import clear_tables, connect, ensure_schema, safe_id
from .store import store_claim_relations, store_reading_result
from . import exporters


# ---------------------------------------------------------------------------
# 初始化
# ---------------------------------------------------------------------------

def init_kb(cfg: KBConfig) -> ToolResult:
    """初始化知识库：建目录 + 建表。幂等。"""
    r = ensure_schema(cfg)
    if not r.ok:
        return r
    return ok({"kb_dir": cfg.kb_dir, "tables": r.data["tables"]})


# ---------------------------------------------------------------------------
# 导入
# ---------------------------------------------------------------------------

def _count_reading_results(cfg: KBConfig) -> str:
    """统计已有的阅读结果数量（供清空前的告警用）。返回可读描述，空串=没有。"""
    try:
        conn = connect(cfg)
        n_claims = conn.execute("SELECT COUNT(*) FROM claims").fetchone()[0]
        n_ass = conn.execute("SELECT COUNT(*) FROM assumptions").fetchone()[0]
        n_rel = conn.execute("SELECT COUNT(*) FROM claim_relations").fetchone()[0]
        conn.close()
    except Exception:
        return ""
    if not (n_claims or n_ass or n_rel):
        return ""
    return "claims={}, assumptions={}, claim_relations={}".format(n_claims, n_ass, n_rel)


def _load_jsonl(path: str) -> ToolResult:
    """读 JSONL 文件，返回 data=记录列表。逐行解析，坏行记 warning。"""
    if not os.path.isfile(path):
        return fail(ErrorCode.FILE_NOT_FOUND, "文件不存在：{}".format(path),
                    location="pipelines._load_jsonl",
                    hint="确认 --search-out 指向 PaperSearch 的 out 目录")
    records, warnings = [], []
    try:
        with open(path, encoding="utf-8") as f:
            for lineno, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except Exception as exc:
                    warnings.append("第 {} 行 JSON 解析失败：{}".format(lineno, exc))
    except Exception as exc:
        return fail_from_exception(exc, location="pipelines._load_jsonl",
                                   code=ErrorCode.READ_ERROR,
                                   message="读文件失败：{}".format(exc))
    return ok(records, warnings=warnings)


def import_search_output(search_out: str, cfg: KBConfig,
                         copy_pdf: bool = False,
                         clear_first: bool = True) -> ToolResult:
    """把 PaperSearch 的 out 目录导入知识库。

    流程：
        1. 读 corpus.jsonl → 每篇转 raw JSON + 写 papers/span_map/media 表
        2. 读 graph.jsonl → 写 citations 表
        3. 可选复制 PDF
        4. 导出 TSV

    参数：
        search_out   含 corpus.jsonl / graph.jsonl 的目录
        cfg          知识库配置
        copy_pdf     是否复制 PDF 到 kb/raw/pdfs
        clear_first  是否先清空表（幂等重建）。★ 会清掉已有的阅读结果
                     （claims/assumptions/claim_relations），有数据时给出 warning

    返回 data={"papers","spans","media","citations","pdfs","exports"}
    """
    with Timer() as t:
        corpus = os.path.join(search_out, "corpus.jsonl")
        graph = os.path.join(search_out, "graph.jsonl")
        if not os.path.isfile(corpus):
            return fail(ErrorCode.FILE_NOT_FOUND,
                        "找不到 corpus.jsonl：{}".format(corpus),
                        location="pipelines.import_search_output",
                        hint="确认 search_out 指向 PaperSearch/ProcessPy/out")

        # 确保建表
        r = ensure_schema(cfg)
        if not r.ok:
            return r

        warnings: List[str] = []
        if clear_first:
            # ★ 清空前检查：若已有阅读结果，明确告警（避免静默丢工作成果）
            lost = _count_reading_results(cfg)
            if lost:
                warnings.append(
                    "clear_first 清空了已有阅读结果：{}。"
                    "如需保留，请改用 clear_first=False".format(lost))
            c = clear_tables(cfg)
            if not c.ok:
                return c

        # 读 corpus
        lr = _load_jsonl(corpus)
        if not lr.ok:
            return lr
        papers_raw = lr.data
        warnings.extend(lr.warnings)      # 保留清空告警 + 解析告警

        if not papers_raw:
            return fail(ErrorCode.EMPTY, "corpus.jsonl 没有记录",
                        location="pipelines.import_search_output",
                        hint="确认 PaperSearch 已产出结果")

        stat = {"papers": 0, "spans": 0, "media": 0, "citations": 0, "pdfs": 0}
        conn = connect(cfg)
        try:
            for paper in papers_raw:
                pid = paper.get("paper_id")
                if not pid:
                    warnings.append("跳过一条无 paper_id 的记录")
                    continue

                # 1) 转 raw JSON
                pr = importers.paper_to_raw(paper)
                if not pr.ok:
                    warnings.append("{} 转 raw 失败：{}".format(pid, pr.error.message))
                    continue
                raw = pr.data

                # 2) 写 raw JSON 文件
                wr = writers.write_raw_json(cfg, pid, raw)
                if not wr.ok:
                    warnings.append("{} 写 raw JSON 失败：{}".format(pid, wr.error.message))

                # 3) 写 SQLite（paper / spans / media）
                writers.write_paper(conn, paper)
                # spans 记录需要 paper_id 字段（build_spans 未带，这里补上）
                spans = [dict(s, paper_id=pid) for s in raw["spans"]]
                media = [dict(m, paper_id=pid) for m in raw["media"]]
                if spans:
                    writers.write_spans(conn, spans)
                if media:
                    writers.write_media(conn, media)

                stat["papers"] += 1
                stat["spans"] += len(spans)
                stat["media"] += len(media)

                # 4) 复制 PDF
                if copy_pdf:
                    pdf = (paper.get("full_text") or {}).get("pdf_path") or ""
                    if pdf and not os.path.isabs(pdf):
                        pdf = os.path.join(search_out, pdf)
                    if pdf and os.path.isfile(pdf):
                        try:
                            os.makedirs(cfg.pdf_dir, exist_ok=True)
                            shutil.copy2(pdf, os.path.join(cfg.pdf_dir, safe_id(pid) + ".pdf"))
                            stat["pdfs"] += 1
                        except Exception as exc:
                            warnings.append("{} 复制 PDF 失败：{}".format(pid, exc))

            # 5) 引用边
            if os.path.isfile(graph):
                gr = _load_jsonl(graph)
                if gr.ok:
                    edges = []
                    for e in gr.data:
                        src = e.get("from") or ""
                        raw = e.get("to_raw") or e.get("to") or ""
                        if not src or not raw:
                            continue
                        # ★ 保留 to / to_raw / internal 三个字段（对齐 INDEX.md 分工）
                        edges.append({
                            "from": src,
                            "to": e.get("to") or "",
                            "to_raw": raw,
                            "internal": bool(e.get("internal")),
                            "source": e.get("source") or "",
                        })
                    writers.write_citations(conn, edges)
                    stat["citations"] = len(edges)
                    stat["internal_citations"] = sum(1 for x in edges if x["internal"])
                    warnings.extend(gr.warnings)
                else:
                    warnings.append("引用边导入失败：{}".format(gr.error.message))
            else:
                warnings.append("graph.jsonl 不存在，跳过引用边导入")

            conn.commit()
        except Exception as exc:
            conn.rollback()
            conn.close()
            return fail_from_exception(exc, location="pipelines.import_search_output",
                                       code=ErrorCode.DB_ERROR,
                                       message="导入过程异常（已回滚）：{}".format(exc))
        conn.close()

        # 6) 导出
        er = exporters.export_tsv(cfg)
        stat["exports"] = er.data["files"] if er.ok else {}
        if not er.ok:
            warnings.append("导出失败：{}".format(er.error.message))

    return ok(stat, warnings=warnings, meta={"ms": t.ms, "kb_dir": cfg.kb_dir})


# ---------------------------------------------------------------------------
# 阅读结果落库
# ---------------------------------------------------------------------------

def store_reading_results(results: List[Dict[str, Any]], cfg: KBConfig) -> ToolResult:
    """批量落库阅读结果。

    逐篇落库；失败的记录不阻断其他记录。
    返回 data={"ok_count","fail_count","ok":"[paper_id]","fail":[{"paper_id","error"}]}
    有失败时返回 partial（仍带 data，便于调用方区分边界）。
    """
    with Timer() as t:
        ok_ids, fail_list = [], []
        warnings: List[str] = []
        for r in results:
            res = store_reading_result(r, cfg)
            if res.ok:
                ok_ids.append(r.get("paper_id"))
                warnings.extend(res.warnings)
            else:
                fail_list.append({"paper_id": r.get("paper_id"),
                                  "error": res.error.to_dict() if res.error else None})
        data = {"ok_count": len(ok_ids), "fail_count": len(fail_list),
                "ok": ok_ids, "fail": fail_list}
        meta = {"ms": t.elapsed_ms(), "total": len(results)}   # 块内取耗时

    if fail_list and ok_ids:
        p = partial(data, warnings=warnings,
                    message="部分成功：{} 成功 / {} 失败".format(len(ok_ids), len(fail_list)),
                    location="pipelines.store_reading_results",
                    hint="查看 data['fail'] 逐条修正后重试；已成功的可跳过")
        p.meta = meta
        return p
    if fail_list and not ok_ids:
        return fail(ErrorCode.INVALID_VALUE,
                    "全部 {} 篇落库失败".format(len(fail_list)),
                    location="pipelines.store_reading_results",
                    hint="查看 context.fail 的首条错误",
                    context={"fail": fail_list[:5]},
                    warnings=warnings)
    return ok(data, warnings=warnings, meta=meta)


def store_reading_results_file(path: str, cfg: KBConfig) -> ToolResult:
    """从 JSON 文件批量落库（文件内容为阅读结果数组，或 {"results": [...]}）。"""
    if not os.path.isfile(path):
        return fail(ErrorCode.FILE_NOT_FOUND, "文件不存在：{}".format(path),
                    location="pipelines.store_reading_results_file")
    try:
        with open(path, encoding="utf-8") as f:
            obj = json.load(f)
    except Exception as exc:
        return fail_from_exception(exc, location="pipelines.store_reading_results_file",
                                   code=ErrorCode.PARSE_ERROR,
                                   message="解析 JSON 失败：{}".format(exc))
    if isinstance(obj, dict):
        obj = obj.get("results") or obj.get("data") or []
    if not isinstance(obj, list):
        return fail(ErrorCode.INVALID_TYPE,
                    "文件内容应为数组或 {'results': [...]}",
                    location="pipelines.store_reading_results_file",
                    hint="检查 PaperRead 输出格式")
    return store_reading_results(obj, cfg)


# ---------------------------------------------------------------------------
# 导出
# ---------------------------------------------------------------------------

def rebuild_exports(cfg: KBConfig) -> ToolResult:
    """重新导出 TSV（落库后调用，让 git 能看到变化）。"""
    return exporters.export_tsv(cfg)


# ---------------------------------------------------------------------------
# 一键流程
# ---------------------------------------------------------------------------

def run_import_pipeline(search_out: str, kb: Optional[str] = None,
                        copy_pdf: bool = True) -> ToolResult:
    """一键：初始化知识库 + 导入 PaperSearch 产物 + 导出。

    参数：
        search_out  PaperSearch 的 out 目录
        kb          知识库路径（None=默认 ToolsPy/kb）
        copy_pdf    是否复制 PDF

    返回 data={"config","import"}
    """
    kr = resolve_kb(kb)
    if not kr.ok:
        return kr
    cfg = kr.data

    ir = import_search_output(search_out, cfg, copy_pdf=copy_pdf)
    if not ir.ok:
        return ir
    data = {"config": cfg.to_dict(), "import": ir.data}
    return ok(data, warnings=ir.warnings, meta=ir.meta)
