"""写库元件（元件：writers）。

职责：把结构化记录写进知识库（SQLite 表 + raw JSON）。
**只负责写**，不做数据转换（转换在 importers/validators 里）。

包含：
    写 paper 记录          write_paper()
    写 span 记录           write_spans()
    写 media 记录          write_media()
    写引用边               write_citations()
    写 raw JSON 文件       write_raw_json()
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List

from .errors import (ErrorCode, ToolResult, fail, fail_from_exception, ok)
from .paths import KBConfig
from .schema import connect, safe_id


# ---------------------------------------------------------------------------
# SQLite 写入
# ---------------------------------------------------------------------------

def write_paper(conn, paper: Dict[str, Any]) -> ToolResult:
    """写/更新 papers 表的一行（导入阶段用，阅读阶段由 store 更新理解字段）。"""
    meta = paper.get("metadata") or {}
    try:
        conn.execute(
            """INSERT OR REPLACE INTO papers
               (paper_id, title, abstract, year, preprint_year, venue, citation_count,
                reading_depth, in_corpus)
               VALUES (?,?,?,?,?,?,?,?,1)""",
            (paper["paper_id"], meta.get("title") or "",
             paper.get("abstract") or "",          # ★ 摘要必须保留（源数据有）
             meta.get("year"),
             meta.get("preprint_year"), meta.get("venue") or "",
             meta.get("citation_count"), "unread"))
    except Exception as exc:
        return fail_from_exception(exc, location="writers.write_paper",
                                   code=ErrorCode.DB_ERROR,
                                   message="写 papers 失败：{}".format(exc),
                                   context={"paper_id": paper.get("paper_id")})
    return ok({"paper_id": paper["paper_id"]})


def write_spans(conn, spans: List[Dict[str, Any]]) -> ToolResult:
    """批量写 span_map 表。"""
    try:
        for s in spans:
            conn.execute(
                """INSERT OR REPLACE INTO span_map
                   (span_id, paper_id, section, char_start, char_end, text)
                   VALUES (?,?,?,?,?,?)""",
                (s["span_id"], s["paper_id"], s["section"],
                 s["char_start"], s["char_end"], s["text"]))
    except Exception as exc:
        return fail_from_exception(exc, location="writers.write_spans",
                                   code=ErrorCode.DB_ERROR,
                                   message="写 span_map 失败：{}".format(exc),
                                   context={"n": len(spans)})
    return ok({"written": len(spans)})


def write_media(conn, media: List[Dict[str, Any]]) -> ToolResult:
    """批量写 media 表（含坐标：page_first/page_last/bbox）。"""
    try:
        for m in media:
            c = m.get("coords") or {}
            bbox = None
            if c:
                bbox = "{},{},{},{}".format(c.get("x"), c.get("y"),
                                            c.get("x2"), c.get("y2"))
            conn.execute(
                """INSERT OR REPLACE INTO media
                   (media_id, paper_id, kind, label, caption, section_index,
                    page_first, page_last, bbox, bboxes_per_page, coords_raw,
                    coords_n_raw_boxes, coords_source, coords_missing_reason, raw_tei)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (m["media_id"], m["paper_id"], m["kind"], m["label"], m["caption"],
                 m["section_index"], c.get("page_first"), c.get("page_last"), bbox,
                 json.dumps(m["bboxes_per_page"], ensure_ascii=False),
                 json.dumps(m["coords_raw_boxes"], ensure_ascii=False),
                 m["coords_n_raw_boxes"], m["coords_source"],
                 m["coords_missing_reason"], m["raw_tei"]))
    except Exception as exc:
        return fail_from_exception(exc, location="writers.write_media",
                                   code=ErrorCode.DB_ERROR,
                                   message="写 media 失败：{}".format(exc),
                                   context={"n": len(media)})
    return ok({"written": len(media)})


def write_citations(conn, edges: List[Dict[str, Any]]) -> ToolResult:
    """批量写 citations 表。

    edges: [{"from","to","to_raw","internal","source"}, ...]

    ★ 三个字段的分工（对齐 PaperSearch/ProcessPy INDEX.md）：
        to_raw   源里的原始 ID（总是在）—— 用于「这论文引了哪些外部文献」的完整性
        to_paper 解析后的目标：库内=to（paper_id），库外=to_raw —— 用于图遍历
        internal 目标是否在语料库内 —— 让统计一眼看出连通度
    """
    try:
        n = 0
        for e in edges:
            src = e.get("from") or ""
            raw = e.get("to_raw") or ""
            if not src or not raw:
                continue
            # to_paper：库内用解析后的 paper_id，库外用原始 ID
            internal = 1 if e.get("internal") else 0
            to_paper = (e.get("to") or raw) if internal else raw
            conn.execute(
                """INSERT OR REPLACE INTO citations
                   (from_paper, to_paper, to_raw, internal, source)
                   VALUES (?,?,?,?,?)""",
                (src, to_paper, raw, internal, e.get("source") or ""))
            n += 1
    except Exception as exc:
        return fail_from_exception(exc, location="writers.write_citations",
                                   code=ErrorCode.DB_ERROR,
                                   message="写 citations 失败：{}".format(exc))
    return ok({"written": n})


# ---------------------------------------------------------------------------
# raw JSON 文件
# ---------------------------------------------------------------------------

def write_raw_json(cfg: KBConfig, paper_id: str, raw: Dict[str, Any]) -> ToolResult:
    """写 raw/papers/<safe_id>.json（证据存档，只增改对应论文，不动别的）。"""
    path = os.path.join(cfg.raw_dir, safe_id(paper_id) + ".json")
    try:
        os.makedirs(cfg.raw_dir, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(raw, f, ensure_ascii=False, indent=2)
    except Exception as exc:
        return fail_from_exception(exc, location="writers.write_raw_json",
                                   code=ErrorCode.WRITE_ERROR,
                                   message="写 raw JSON 失败：{}".format(exc),
                                   context={"path": path})
    return ok({"path": path})


def load_raw_json(cfg: KBConfig, paper_id: str) -> ToolResult:
    """读 raw/papers/<safe_id>.json。不存在时 ok=False（E_STATE_NOT_FOUND）。"""
    path = os.path.join(cfg.raw_dir, safe_id(paper_id) + ".json")
    if not os.path.isfile(path):
        return fail(ErrorCode.NOT_FOUND, "raw JSON 不存在：{}".format(path),
                    location="writers.load_raw_json",
                    hint="该论文可能未导入；先运行 import 流水线",
                    context={"paper_id": paper_id})
    try:
        with open(path, encoding="utf-8") as f:
            return ok(json.load(f), meta={"path": path})
    except Exception as exc:
        return fail_from_exception(exc, location="writers.load_raw_json",
                                   code=ErrorCode.READ_ERROR,
                                   message="读 raw JSON 失败：{}".format(exc),
                                   context={"path": path})
