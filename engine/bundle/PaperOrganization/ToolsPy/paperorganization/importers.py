"""数据转换元件（元件：importers）。

职责：把外部数据（PaperSearch 的 corpus.jsonl 记录）**纯粹地**转换成
知识库需要的结构化记录。**不做 IO、不碰数据库**——这样才好单测、好被 Agent 精细调整。

包含：
    build_spans(paper)      → span 记录列表
    build_media(paper)      → media 记录列表
    paper_to_raw(paper)     →的完整 raw JSON 结构
"""
from __future__ import annotations

from typing import Any, Dict, List

from .errors import (ErrorCode, ToolResult, fail, ok)
from .ids import media_id, span_id
from .schema import safe_id

# raw JSON 里 understanding 段的空模板（PaperRead 落库时填充）
EMPTY_UNDERSTANDING = {
    "gist": "", "research_problem": "", "contribution": "",
    "method": {"what": "", "design_rationale": ""},
    "core_claims": [], "key_results": [], "assumptions": [],
    "limitations": {"acknowledged": [], "unacknowledged": []},
    "method_family": "", "tags": [],
    "reading_depth": "unread", "generated_by": "", "generated_at": "",
}


# ---------------------------------------------------------------------------
# spans
# ---------------------------------------------------------------------------

def build_spans(paper: Dict[str, Any]) -> List[Dict[str, Any]]:
    """从 document_structure.sections[].paragraph_list 抽取正文段落 → span 记录。

    ★ char_start/char_end 的语义（必须说清，否则下游会误用）：
        以「本文所有段落按顺序拼接后的字符串」为基准的字符偏移（0-based，半开区间）。
        不是 PDF 字节偏移，也不是 TEI 偏移。同章节内的相对顺序准确。

    返回 [{"span_id","section","char_start","char_end","text"}, ...]
    """
    ds = paper.get("document_structure") or {}
    pid = paper.get("paper_id", "")
    spans: List[Dict[str, Any]] = []
    cursor = 0
    n = 0
    for sec in (ds.get("sections") or []):
        sec_title = sec.get("title") or ""
        for para in (sec.get("paragraph_list") or []):
            text = para or ""
            if not text.strip():
                continue
            n += 1
            spans.append({
                "span_id": span_id(pid, n),
                "section": sec_title,
                "char_start": cursor,
                "char_end": cursor + len(text),
                "text": text,
            })
            cursor += len(text) + 1     # +1 表示段间换行分隔
    return spans


# ---------------------------------------------------------------------------
# media
# ---------------------------------------------------------------------------

def build_media(paper: Dict[str, Any]) -> List[Dict[str, Any]]:
    """从 document_structure 的 figure/table/formula_objects 抽取媒体对象。

    返回 [{"media_id","kind","label","caption","section_index","coords",
           "bboxes_per_page","coords_raw_boxes","coords_n_raw_boxes",
           "coords_source","coords_missing_reason","raw_tei"}, ...]
    """
    ds = paper.get("document_structure") or {}
    pid = paper.get("paper_id", "")
    media: List[Dict[str, Any]] = []
    for kind, bucket in (("figure", "figure_objects"),
                         ("table", "table_objects"),
                         ("formula", "formula_objects")):
        for i, obj in enumerate(ds.get(bucket) or []):
            media.append({
                "media_id": media_id(pid, kind, i),
                "kind": kind,
                "label": obj.get("label") or "",
                "caption": obj.get("text") or "",
                "section_index": obj.get("section_index"),
                "coords": obj.get("coords") or {},
                "bboxes_per_page": obj.get("coords_boxes_per_page") or [],
                "coords_raw_boxes": obj.get("coords_raw_boxes") or [],
                "coords_n_raw_boxes": obj.get("coords_n_raw_boxes"),
                "coords_source": obj.get("coords_source") or "",
                "coords_missing_reason": obj.get("coords_missing_reason"),
                "raw_tei": obj.get("raw_tag") or "",
            })
    return media


# ---------------------------------------------------------------------------
# raw JSON
# ---------------------------------------------------------------------------

def paper_to_raw(paper: Dict[str, Any]) -> ToolResult:
    """把一条 PaperSearch Paper 记录转成的 raw JSON（A/B/B2/C 四段）。

    返回 ToolResult，data 为 raw dict。
    缺 paper_id 时失败（E_INPUT_MISSING_FIELD）。
    """
    if not paper.get("paper_id"):
        return fail(ErrorCode.MISSING_FIELD, "paper 记录缺少 paper_id",
                    location="importers.paper_to_raw",
                    hint="检查 corpus.jsonl 的记录结构，每行必须有 paper_id")
    meta = paper.get("metadata") or {}
    ident = paper.get("identity") or {}
    ft = paper.get("full_text") or {}
    ds = paper.get("document_structure") or {}
    cg = paper.get("citation_graph") or {}

    raw = {
        "paper_id": paper["paper_id"],

        # ★ 摘要（源数据顶层字段，必须保留 —— 曾是导入时被丢弃的字段）
        "abstract": paper.get("abstract") or "",

        # ── A 段：元数据 ──
        "metadata": {
            "title_variants": [t for t in {meta.get("title"), meta.get("title_norm")} if t],
            "authors": meta.get("authors") or [],
            "affiliations": meta.get("affiliations") or [],
            "year": meta.get("year"),
            "preprint_year": meta.get("preprint_year"),
            "published_date": meta.get("published_date"),
            "venue": meta.get("venue") or "",
            "venue_type": meta.get("venue_type") or "",
            #：source_ids 必须在 metadata 内部
            "source_ids": {k: v for k, v in ident.items() if v},
            "categories": meta.get("categories") or [],
            "keywords": meta.get("keywords") or [],
            "topics": meta.get("topics") or [],
            "type": meta.get("type") or "",
            "arxiv_version": meta.get("arxiv_version"),
            "citation_count": meta.get("citation_count"),
            "citation_source": "",
            "citation_fetched_at": "",
        },

        # ── B 段：正文证据 ──
        "spans": build_spans(paper),

        # ── B2 段：图表公式 ──
        "media": build_media(paper),

        # ── C 段：理解摘要（PaperRead 落库时填充）──
        "understanding": dict(EMPTY_UNDERSTANDING,
                              method=dict(EMPTY_UNDERSTANDING["method"]),
                              limitations=dict(EMPTY_UNDERSTANDING["limitations"])),

        # 出处
        "provenance": {
            "found_by": (paper.get("provenance") or {}).get("found_by", []),
            "pdf_path": ft.get("pdf_path", ""),
            "tei_path": ds.get("tei_path", ""),
            "citation_source": cg.get("source", ""),
        },
    }
    return ok(raw)
