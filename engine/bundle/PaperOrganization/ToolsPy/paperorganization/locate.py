"""全文定位元件（元件：locate）。

职责：实现的「定位链」：
    media_id → media 表（page_first + bbox）→ PDF 对应页的区域

注意（方法适用边界，必须说清）：
    - figure / table：可用「bbox 区域反提文字 ↔ caption 比对」验证定位精度。
    - formula：PDF 用符号字体，提取字符与 caption 的 Unicode 数学符号编码不同，
      **文字比对不适用**，只能用几何合理性验证。

依赖 pypdf（只在需要读 PDF 时导入，不影响其他元件的纯标准库特性）。

主要函数：
    get_locate_info(cfg, media_id)          取定位信息（页 + bbox + PDF 路径）
    verify_locate(cfg, media_id)            验证该 media 的定位是否可信
    verify_all(cfg)                         批量验证，返回按类型统计
"""
from __future__ import annotations

import os
import re
import sqlite3
import sys
from typing import Any, Dict, List, Optional

from .errors import (ErrorCode, ToolResult, Timer, fail, fail_from_exception, ok)
from .paths import KBConfig
from .schema import connect, safe_id

_VENDOR_WHEEL = os.path.abspath(os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "..", "..", "vendor",
    "pypdf-6.21.4-py3-none-any.whl"))
if os.path.isfile(_VENDOR_WHEEL) and _VENDOR_WHEEL not in sys.path:
    sys.path.insert(0, _VENDOR_WHEEL)


# ---------------------------------------------------------------------------
# 坐标工具
# ---------------------------------------------------------------------------

def parse_bbox(bbox: str) -> Optional[List[float]]:
    """把 "x,y,x2,y2" 解析成 [float]。非法返回 None。"""
    try:
        vals = [float(v) for v in (bbox or "").split(",")]
        return vals if len(vals) == 4 else None
    except Exception:
        return None


def bbox_to_pdf_rect(bbox: List[float], page_height: float) -> List[float]:
    """GROBID 坐标（左上原点、y 向下）→ PDF 坐标（左下原点、y 向上）。

    返回 [x1, y1, x2, y2]（PDF 坐标系），可直接用于在 PDF 上画框。
    """
    x, y, x2, y2 = bbox
    return [x, page_height - y2, x2, page_height - y]


def geom_ok(bbox: List[float]) -> bool:
    """几何合理性：正尺寸、不越界（页面约 612x792，给余量）。"""
    x, y, x2, y2 = bbox
    w, h = x2 - x, y2 - y
    return 0 < w < 620 and 0 < h < 800 and x >= 0 and y >= 0


def _norm(s: str) -> str:
    """去掉所有空白，消除 PDF 提取文字与 caption 的空格差异。"""
    return re.sub(r"\s+", "", s or "")


def caption_keyword(caption: str) -> str:
    """从 caption 取关键词（去 Figure N 前缀 + 去空白），用于文字比对。"""
    body = re.sub(r"^(Figure|Table|Fig\.?)\s*\d+\s*[|:.\-]?\s*", "", caption or "").strip()
    return _norm(body)[:15]


# ---------------------------------------------------------------------------
# 定位信息
# ---------------------------------------------------------------------------

def get_locate_info(cfg: KBConfig, media_id: str) -> ToolResult:
    """取某个 media 的定位信息：论文、PDF 路径、页号、bbox。

    返回 data={"media_id","paper_id","pdf_path","page","bbox","bbox_pdf","kind","caption"}
    Agent 拿到后即可打开 PDF 对应页并高亮 bbox_pdf 区域。
    """
    try:
        conn = connect(cfg)
        row = conn.execute(
            """SELECT m.*, p.title FROM media m
               LEFT JOIN papers p ON m.paper_id=p.paper_id
               WHERE m.media_id=?""", (media_id,)).fetchone()
        conn.close()
    except Exception as exc:
        return fail_from_exception(exc, location="locate.get_locate_info",
                                   code=ErrorCode.DB_ERROR, message=str(exc))
    if not row:
        return fail(ErrorCode.NOT_FOUND, "media 不存在：{}".format(media_id),
                    location="locate.get_locate_info",
                    hint="用 query.get_media(paper_id) 查看可用 media_id")

    d = dict(row)
    bbox = parse_bbox(d.get("bbox"))
    pdf_path = os.path.join(cfg.pdf_dir, safe_id(d["paper_id"]) + ".pdf")
    data = {
        "media_id": media_id,
        "paper_id": d["paper_id"],
        "title": d.get("title") or "",
        "kind": d.get("kind"),
        "caption": d.get("caption") or "",
        "page": d.get("page_first"),
        "page_last": d.get("page_last"),
        "bbox": bbox,
        "pdf_path": pdf_path,
        "pdf_exists": os.path.isfile(pdf_path),
        "coords_n_raw_boxes": d.get("coords_n_raw_boxes"),
        "coords_missing_reason": d.get("coords_missing_reason"),
    }

    if not bbox:
        return fail(ErrorCode.EMPTY,
                    "该 media 没有坐标（无法定位）：{}".format(media_id),
                    location="locate.get_locate_info",
                    hint="原因：{}".format(d.get("coords_missing_reason") or "未知"),
                    context=data)
    if not data["pdf_exists"]:
        return fail(ErrorCode.FILE_NOT_FOUND,
                    "PDF 不存在：{}".format(pdf_path),
                    location="locate.get_locate_info",
                    hint="先获取全文（PaperSearch 步骤）或确认 kb/raw/pdfs 已复制",
                    context=data)

    # 附带 PDF 坐标系下的矩形
    try:
        from pypdf import PdfReader
        ph = float(PdfReader(pdf_path).pages[d["page_first"] - 1].mediabox.height)
        data["bbox_pdf"] = bbox_to_pdf_rect(bbox, ph)
        data["page_height"] = ph
    except Exception as exc:
        data["bbox_pdf"] = None
        data["page_height"] = None

    # ★ 坐标可信度提示（与 query.get_media_detail 的 locate_hint 保持一致）
    #   GROBID 按行给框；只识别到 1 个行框时，往往只框住了图注而非图主体。
    n_raw = d.get("coords_n_raw_boxes")
    if n_raw is None:
        data["locate_hint"] = "原始框数未知，坐标可信度无法判断"
    elif n_raw <= 1:
        data["locate_hint"] = ("坐标可疑：GROBID 只识别到 {} 个行框，"
                               "可能只框住了图注而非图主体，建议人工核对".format(n_raw))
    else:
        data["locate_hint"] = "坐标可用（{} 个原始行框已合并）".format(n_raw)
    return ok(data)


# ---------------------------------------------------------------------------
# 定位验证
# ---------------------------------------------------------------------------

def _extract_bbox_text(pdf_path: str, page_idx: int, bbox: List[float]) -> str:
    """按 bbox 区域从 PDF 反提文字（仅文本类元素适用）。"""
    from pypdf import PdfReader
    x, y, x2, y2 = bbox
    page = PdfReader(pdf_path).pages[page_idx]
    ph = float(page.mediabox.height)
    top, bottom = ph - y, ph - y2
    hits: List[str] = []

    def visitor(text, cm, tm, font_dict, font_size):
        if x <= tm[4] <= x2 and bottom <= tm[5] <= top:
            hits.append(text)

    page.extract_text(visitor_text=visitor)
    return "".join(hits)


def verify_locate(cfg: KBConfig, media_id: str) -> ToolResult:
    """验证某个 media 的坐标是否可信。

    策略（必须按类型区分，否则会得出错误结论）：
        figure/table → bbox 区域反提文字 ↔ caption 比对（归一化空白）
        formula      → 几何合理性（文字比对不适用）

    返回 data={"media_id","kind","method","passed","evidence"}
    """
    info = get_locate_info(cfg, media_id)
    if not info.ok:
        return info
    d = info.data
    bbox = d["bbox"]
    kind = d["kind"]

    if kind == "formula":
        passed = geom_ok(bbox)
        return ok({"media_id": media_id, "kind": kind, "method": "geometry",
                   "passed": passed,
                   "evidence": "几何{}".format("合理" if passed else "异常：{}".format(bbox))})

    # figure / table：文字比对
    try:
        got = _extract_bbox_text(d["pdf_path"], d["page"] - 1, bbox)
    except Exception as exc:
        return fail_from_exception(exc, location="locate.verify_locate",
                                   code=ErrorCode.READ_ERROR,
                                   message="读 PDF 区域失败：{}".format(exc),
                                   context={"media_id": media_id})
    kw = caption_keyword(d["caption"])
    if not kw:
        return ok({"media_id": media_id, "kind": kind, "method": "text",
                   "passed": None, "evidence": "caption 为空，无法文字比对（非坐标错误）"})
    passed = kw in _norm(got)
    return ok({"media_id": media_id, "kind": kind, "method": "text", "passed": passed,
               "evidence": "关键词 {!r} {}".format(kw, "命中" if passed else "未命中"),
               "extracted_preview": got[:120]})


def verify_all(cfg: KBConfig) -> ToolResult:
    """批量验证所有有坐标的 media，按类型统计。

    返回 data={"by_kind": {...}, "details": [...], "pass_rate": {...}}
    """
    try:
        conn = connect(cfg)
        rows = [dict(r) for r in conn.execute(
            """SELECT media_id, kind FROM media
               WHERE bbox IS NOT NULL AND page_first IS NOT NULL
               ORDER BY kind, media_id""")]
        conn.close()
    except Exception as exc:
        return fail_from_exception(exc, location="locate.verify_all",
                                   code=ErrorCode.DB_ERROR, message=str(exc))

    by_kind: Dict[str, Dict[str, int]] = {}
    details = []
    for r in rows:
        res = verify_locate(cfg, r["media_id"])
        k = r["kind"]
        by_kind.setdefault(k, {"passed": 0, "failed": 0, "undetermined": 0})
        if not res.ok:
            by_kind[k]["failed"] += 1
            details.append({"media_id": r["media_id"], "kind": k,
                            "passed": False, "error": res.error.to_dict()})
            continue
        p = res.data.get("passed")
        if p is True:
            by_kind[k]["passed"] += 1
        elif p is False:
            by_kind[k]["failed"] += 1
        else:
            by_kind[k]["undetermined"] += 1
        details.append({"media_id": r["media_id"], "kind": k, "passed": p,
                        "method": res.data.get("method"),
                        "evidence": res.data.get("evidence")})

    pass_rate = {}
    for k, s in by_kind.items():
        tot = s["passed"] + s["failed"]
        pass_rate[k] = round(s["passed"] / tot * 100, 1) if tot else None

    return ok({"by_kind": by_kind, "pass_rate": pass_rate, "details": details},
              meta={"total": len(rows)})
