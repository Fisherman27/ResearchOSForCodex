#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fulltext.py —— STEP 7 全文获取 + STEP 8 结构解析

定位
----
STEP 7  按的优先级把 PDF 拿到本地
            arXiv PDF  →  OpenAlex best_oa_location  →  Unpaywall  →  （CORE，V2）
STEP 8  PDF → GROBID → TEI → 结构化论文

成本控制（的核心要求）
------------------------
明确反对"搜到 1000 篇就下 1000 个 PDF"：

    1000 candidates → metadata 筛选 → 200 → citation/abstract 筛选 → 50 → 获取 50 篇全文

所以本模块**只对 pipeline 指定的 N 篇**工作，且每篇都有大小上限与超时。
不做"批量抓全"的接口 —— 那会让成本失控，而且违背阶段的职责划分。

降级链（每级都记进 provenance，失败不抛异常）
--------------------------------------------
    GROBID 可用      → parsed_by="grobid"，sections/图表/参考文献齐全
    GROBID 不可用    → 退回 pypdf 纯文本，parsed_by="pypdf"，**结构字段留空**
                       并在 document_structure.error 写明原因
    两者都不可用      → parsed_by=""，error 记录

★ 这里刻意**不假装** pypdf 的结果等于结构解析：
明确说"全文不要保存成单一字符串"。所以 pypdf 兜底只填
  `text_path`（纯文本旁路），`sections` 保持空 —— 下游能一眼看出
  "这篇没有结构"，而不是拿到一堆被误当成 section 的文本。

函数索引
─────────────────────────────────────────────────────────────────────────────
  [STEP7] candidate_urls(paper)              按优先级列出可尝试的 PDF 地址
  [STEP7] acquire_pdf(paper, store, cfg)     下载并落盘 → dict(ok, source, ...)
  [STEP8] grobid_available(cfg)              服务是否可用（避免每篇都超时）
  [STEP8] parse_with_grobid(paper, store, cfg) 调 GROBID → 填 document_structure
  [STEP8] parse_fallback(paper, store, cfg)  纯文本兜底（明确标记非结构）
  [STEP8] parse_structure(paper, store, cfg) 主入口：自动选 GROBID 或兜底
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import os
import sys
import time
from typing import List, Tuple

# Offline PDF fallback bundled with the Codex plugin.  Pure-Python wheels are
# valid zip import paths, so this does not install or mutate the user's Conda
# environment. GROBID remains the primary structured parser.
_VENDOR_WHEEL = os.path.abspath(os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "..", "vendor",
    "pypdf-6.21.4-py3-none-any.whl"))
if os.path.isfile(_VENDOR_WHEEL) and _VENDOR_WHEEL not in sys.path:
    sys.path.insert(0, _VENDOR_WHEEL)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _bootstrap import ensure_importable          # noqa: E402
ensure_importable()

import grobid as ex_grobid                        # noqa: E402
import _common                                    # noqa: E402

from model import Paper                           # noqa: E402

# 默认请求坐标的元素：只取图表公式（体积小、后续做高亮最常用）。
# 需要段落级回链时在 config 里把 coordinate_elements 加上 "p"/"head"。
DEFAULT_COORD_ELEMENTS = ("figure", "table", "formula")


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


# =============================================================================
# STEP 7：全文获取
# =============================================================================

def candidate_urls(paper: Paper) -> List[Tuple[str, str]]:
    """按的优先级列出可尝试的 PDF 地址。

    返回 [(url, 来源标签), ...]，**已经去重**。

    顺序与理由（来自）：
      1. arXiv PDF —— 免费、稳定、无 paywall，是第一优先级
      2. openalex  —— best_oa_location（仓库副本/出版社 OA/预印本）
      3. s2        —— openAccessPdf（S2 自己认定的 OA）
      4. unpaywall —— 其他来源已在 sources.resolve_oa 里合并进 pdf_url，
                      这里只兜底 alt_locations
    只用能**直接下到 PDF** 的 URL；landing page 不尝试（还要再解析一层 HTML，
    投入产出比低，而且第一版不做爬虫）。
    """
    urls: List[Tuple[str, str]] = []
    seen = set()

    def add(u: str, src: str):
        """收下一个候选地址。

        ★ 这里**不再按文件名猜**是不是 PDF（实测踩到的 P2 缺陷）：
          第一版要求 URL 以 .pdf 结尾或含 /pdf/，结果 `…/download/123`
          这类**明确由来源提供的** PDF 地址被直接丢掉，
          明明能下却被判成"没有可用的 PDF 地址"。
          正确做法是**照单全收、按响应验证** ——
          下载后本来就会检查 `%PDF-` 魔数，猜错了代价只是多一次请求，
          猜漏了代价是拿不到全文（贵得多）。
        """
        u = (u or "").strip()
        if not u or u in seen:
            return
        if not u.lower().startswith("http"):
            return
        seen.add(u)
        urls.append((u, src))

    aid = (paper.identity.get("arxiv_id") or "").strip()
    if aid:
        add("https://arxiv.org/pdf/{}".format(aid), "arxiv")
    add(paper.full_text.get("pdf_url", ""), "openalex/s2")
    for loc in (paper.full_text.get("alt_locations") or []):
        add(loc.get("url", ""), "alt:" + str(loc.get("host") or "?"))
    return urls


def acquire_pdf(paper: Paper, store, cfg) -> dict:
    """下载 PDF 并落盘（STEP 7）。

    参数
    ----
    store : CorpusStore（用它的 pdfs/ 目录，文件名基于 paper_id）
    cfg   : 用 fetch_pdf / max_pdf_mb / net_retries

    返回 {"ok", "source", "url", "bytes", "error"}

    行为要点：
      - 已经下过的（resume 场景）直接返回 ok，不重复下载；
      - 逐个候选 URL 尝试，**任一成功即停**；
      - 校验 `%PDF-` 魔数：很多 OA 链接返回的是 HTML 错误页；
      - 大小上限 max_pdf_mb：防止误抓了几百 MB 的整期期刊合订本。
    """
    if store.has_pdf(paper):
        return {"ok": True, "source": paper.full_text.get("pdf_source", "cached"),
                "url": paper.full_text.get("pdf_url", ""),
                "bytes": paper.full_text.get("pdf_bytes", 0), "error": ""}

    limit = int(max(1, cfg.max_pdf_mb) * 1024 * 1024)
    errors: List[str] = []
    tried_unpaywall = False
    attempted: set = set()          # 已尝试过的 (url, 来源)，回退阶段避免重复请求
    for url, src in candidate_urls(paper):
        attempted.add((url, src))
        status, body, hdrs = _common.http_request(
            url, timeout=60.0, retries=cfg.net_retries, email=_email_of())
        if status != 200:
            errors.append("{} HTTP {}".format(src, status))
            continue
        if body[:5] != b"%PDF-":
            ctype = (hdrs or {}).get("Content-Type", "?")
            errors.append("{} 非 PDF（{}）".format(src, ctype))
            continue
        if len(body) > limit:
            errors.append("{} 超过 {}MB 上限".format(src, cfg.max_pdf_mb))
            continue
        # ★ 完整性校验（G10）：截断文件不写盘、不计入成功
        integ_ok, why = pdf_integrity(body)
        if not integ_ok:
            errors.append("{} 内容不完整：{}".format(src, why))
            continue
        store.save_pdf(paper, body, src)
        return {"ok": True, "source": src, "url": url, "bytes": len(body), "error": ""}

    # ---- 回退：候选都失败时，再用 Unpaywall 找一次其他 OA 位置 ----
    # ★ 为什么必须有这一步（实测踩到的 P2 缺陷）：
    #   一条 pdf_url 存在**不等于**它能下载。常见情形是 OpenAlex 给的
    #   仓库链接已失效（404）。第一版因为在 resolve_oa 里"已有 pdf_url 就跳过"，
    #   而且下载失败后不再回退，导致明明 DOI 有 OA 版本却查都不查一次
    #   （离线测试确认 Unpaywall 调用次数为 0）。
    tries = list(errors)
    if paper.identity.get("doi") and not tried_unpaywall:
        try:
            import sources  # 局部导入：避免模块级循环依赖
            r = sources.resolve_oa([paper], cfg, only_missing=False, limit=1)
            for u2, src2 in candidate_urls(paper):
                # ⚠️ 变量名必须是 attempted：第一版写成了 tried，
                #    而那段是 `tries = list(errors)`（字符串列表，不是 url 集合），
                #    于是抛 NameError，**整条 Unpaywall 回退路径静默失效**，
                #    表现为 PDF 全部下载失败却没有兜底。
                if (u2, src2) in attempted:
                    continue
                attempted.add((u2, src2))
                status2, body2, hdrs2 = _common.http_request(
                    u2, timeout=60.0, retries=cfg.net_retries, email=_email_of())
                if status2 == 200 and body2[:5] == b"%PDF-" and len(body2) <= limit:
                    store.save_pdf(paper, body2, "unpaywall-fallback")
                    paper.add_history("fulltext_fallback",
                                      "原链接失败后经 Unpaywall 回退成功：{}".format(u2))
                    return {"ok": True, "source": "unpaywall-fallback", "url": u2,
                            "bytes": len(body2), "error": ""}
                tries.append("{} HTTP {}".format(src2, status2))
            if r.get("errors"):
                tries.extend(r["errors"][:2])
        except Exception as exc:
            tries.append("unpaywall 回退失败: {}".format(str(exc)[:80]))

    paper.full_text["error"] = "; ".join(tries[:4]) or "没有可用的 PDF 地址"
    return {"ok": False, "source": "", "url": "",
            "bytes": 0, "error": paper.full_text["error"]}


def pdf_integrity(data: bytes) -> tuple:
    """检查 PDF 字节流是否**完整可用**，返回 (ok, 原因)。

    ★ 为什么不能只看 `%PDF-` 魔数（实测缺陷 G10）：
      只校验开头 5 字节的话，一个**被网关截断**的响应
      （有头无尾）会一路通过：写盘 → 回填 pdf_path → 计入 `with_pdf`
      → 到 STEP8 才在 GROBID 那里报错。中间所有统计都被污染，
      而"有 PDF"这个数字是下游（PaperOrganization / PaperRead）判断
      "这篇能不能读全文"的依据 —— 假的可用性比没有更糟。

      三层校验：
        1. 开头必须是 `%PDF-`
        2. 结尾附近必须有 `%%EOF`（PDF 规范要求；截断文件没有）
        3. 能被 pypdf 打开且至少 1 页（结构真的可解析）
           —— 第 3 层最贵，但只在有 pypdf 时做，且能挡住"头尾都在、
              中间坏掉"的情况。

    返回 (True, "") 或 (False, "具体原因")
    """
    # ⚠️ 不设"字节数下限"当作完整性判据：合法的最小 PDF 可以只有几百字节
    #    （实测我自己生成的最小有效 PDF 是 584 字节）。
    #    用字节数挡"坏文件"会误杀真文件 —— 完整性要靠**结构**判断，
    #    不靠体积。这里只挡真正的空响应。
    if not data:
        return False, "空响应（0 字节）"
    if not data.startswith(b"%PDF-"):
        return False, "缺少 %PDF- 头"
    # %%EOF 允许出现在最后 2KB 内（有些 PDF 尾部带空白/注释）
    if b"%%EOF" not in data[-2048:]:
        return False, "缺少 %%EOF 结尾（文件被截断）"
    # 结构校验：能让 pypdf 真的打开并数出页数
    try:
        import io as _io
        import pypdf
        reader = pypdf.PdfReader(_io.BytesIO(data))
        n = len(reader.pages)
        if n < 1:
            return False, "PDF 可打开但页数为 0"
    except ImportError:
        # 没有 pypdf 时只做前两层（不因为缺依赖就拒绝所有 PDF）
        return True, ""
    except Exception as exc:
        return False, "PDF 结构损坏（{}）：{}".format(type(exc).__name__, str(exc)[:80])
    return True, ""


def _email_of() -> str:
    try:
        return _common.get_key("PAPERSEARCH_EMAIL")
    except Exception:
        return ""


# =============================================================================
# STEP 8：结构解析
# =============================================================================

def grobid_available(cfg) -> Tuple[bool, str]:
    """GROBID 服务是否可用。

    ★ 必须先探活再逐篇调用：解析请求的超时是 120s，服务没起的话
      每篇都会白等两分钟。一次探活（8 秒）换掉 N 次超时，非常划算。
    返回 (可用, 说明)
    """
    try:
        g = ex_grobid.Grobid(base_url=cfg.grobid_url)
        alive, msg = g.isalive()
        if alive:
            status, ver = g.version()
            return True, str(ver)[:80]
        return False, "服务无响应（{}）".format(cfg.grobid_url)
    except Exception as exc:
        return False, "探活失败：{}".format(str(exc)[:120])


def parse_with_grobid(paper: Paper, store, cfg) -> dict:
    """调用 GROBID 解析全文并填 document_structure（STEP 8 主路径）。

    落盘内容（Layer 3）：
        structures/<id>.json     sections / references / 图表公式计数
        structures/<id>.tei.xml  原始 TEI（cfg.keep_tei 时；证据留存）

    ⚠️ consolidateCitations 默认 0（在 ExamplePy/grobid.py 里改过）：
       开启时会对每条参考文献查 Crossref，实测同一篇论文 1.6s → 220s。
       这里也显式传 0，避免有人改了默认值把整轮检索拖垮。
    """
    pdf = paper.full_text.get("pdf_path") or ""
    if not pdf or not os.path.isfile(pdf):
        return {"ok": False, "error": "没有本地 PDF"}
    g = ex_grobid.Grobid(base_url=cfg.grobid_url)
    # ★ 坐标不是默认输出，必须显式请求 teiCoordinates（否则 TEI 里没有 coords）。
    #   请求哪些元素由 cfg.coordinate_elements 决定：
    #   默认只取图表公式（省带宽），需要段落/标题级回链时再打开更多。
    coord_elems = list(getattr(cfg, "coordinate_elements", None)
                       or DEFAULT_COORD_ELEMENTS)
    want_coords = bool(getattr(cfg, "tei_coordinates", True))
    r = g.process_fulltext(pdf, consolidate_citations=0,
                           tei_coordinates=coord_elems if want_coords else False)
    if not r.get("ok"):
        return {"ok": False, "error": str(r.get("error") or "解析失败")[:200]}

    ds = paper.document_structure
    ds["parsed_by"] = "grobid"
    ds["sections"] = r.get("sections") or []
    ds["paragraphs_total"] = r.get("paragraphs_total") or 0
    ds["figures"] = r.get("figures") or 0
    ds["tables"] = r.get("tables") or 0
    ds["equations"] = r.get("equations") or 0
    ds["references_parsed"] = r.get("references") or []
    ds["error"] = ""

    # ---- PDF 坐标（图表公式 + 可选段落）----
    # 只有请求了 tei_coordinates 才有；没请求时 parse_tei_coordinates 会返回
    # count=0 并在 note 里说明原因（不假装成功）。
    tei_bytes = (r.get("tei") or "").encode("utf-8")
    if tei_bytes:
        # ---- 正文引文标记 ↔ 参考文献 xml:id 的连接（G12）----
        # 底层 parse_tei_citation_links() 早已实现并验证（连接率 100%），
        # 但第一版 parse_structure() **没有调用它** —— 结果这个能力只存在于
        # 验收脚本里，Layer 3 产物中拿不到。下游要做句子级证据链时才发现缺。
        links = ex_grobid.parse_tei_citation_links(tei_bytes)
        ds["citation_links"] = {
            "links": links.get("links") or [],
            "unlinked": links.get("unlinked") or [],
            "object_links": links.get("object_links") or {},
            "n_refs": links.get("n_refs", 0),
            "n_bibr_refs": links.get("n_bibr_refs", 0),
            "n_linked": len(links.get("links") or []),
            "n_unlinked": len(links.get("unlinked") or []),
            "n_bib": links.get("n_bib", 0),
            "n_bib_with_id": links.get("n_bib_with_id", 0),
            "link_rate": (round(len(links.get("links") or []) /
                                links["n_bibr_refs"], 3)
                          if links.get("n_bibr_refs") else None),
            "note": "",
        }

        # ---- 句子级 span（带段落坐标）----
        # ★ 坐标精度如实标注 coords_level="paragraph" —— GROBID 只给段落级框，
        #   句子级框需要逐 token 坐标（GROBID 不提供）。硬估会给出**假精度**，
        #   下游按假句子框高亮会指错位置。
        _coords_for_spans = ex_grobid.parse_tei_coordinates(
            tei_bytes, elements=("p", "paragraph"))
        ds["spans"] = ex_grobid.build_spans(tei_bytes, _coords_for_spans)

        coords = ex_grobid.parse_tei_coordinates(
            tei_bytes, elements=coord_elems if want_coords else None)
        # 把坐标按 xml:id 贴回图表公式对象
        objs = ex_grobid.parse_tei_figures_tables_formulas(tei_bytes)
        ex_grobid.attach_coordinates(
            objs, coords,
            elements=tuple(e for e in ("figure", "table", "formula")
                           if e in (coord_elems or ())))
        ds["figure_objects"] = objs.get("figures") or []
        ds["table_objects"] = objs.get("tables") or []
        ds["formula_objects"] = objs.get("formulas") or []
        ds["coordinates"] = {
            "count": coords.get("count", 0),
            "by_element": coords.get("by_element", {}),
            "coordinate_space": coords.get("coordinate_space", ""),
            "note": coords.get("note", ""),
            "requested_elements": list(coord_elems) if want_coords else [],
            # 段落/标题级坐标单独存：数量大，混进 figure_objects 会很难用
            "elements": [it for it in (coords.get("elements") or [])
                         if it.get("element") in ("p", "paragraph", "head", "title", "s")],
        }
        paper.add_history("coordinates",
                          "抽取 {} 个元素的 PDF 坐标（{}）".format(
                              coords.get("count", 0),
                              ",".join("{}:{}".format(k, v) for k, v in
                                       (coords.get("by_element") or {}).items())))
    else:
        ds["coordinates"] = {"count": 0, "note": "无 TEI 内容", "elements": []}

    store.save_structure(paper, tei=(r.get("tei") or "") if cfg.keep_tei else "")
    # 结构里的参考文献（带 DOI 的）可以作为引用边的补充
    extra = []
    for ref in ds["references_parsed"]:
        if ref.get("doi"):
            extra.append("doi:" + str(ref["doi"]).lower())
    if extra:
        cur = paper.citation_graph.get("references") or []
        add = [x for x in extra if x not in cur]
        if add:
            paper.citation_graph["references"] = cur + add
            paper.citation_graph["source"] = (paper.citation_graph.get("source") or "") + "+grobid"
    return {"ok": True, "sections": len(ds["sections"]),
            "paragraphs": ds["paragraphs_total"],
            "figures": ds["figures"], "tables": ds["tables"],
            "equations": ds["equations"], "refs": len(ds["references_parsed"]),
            "coords": (ds.get("coordinates") or {}).get("count", 0),
            "coords_by_element": (ds.get("coordinates") or {}).get("by_element", {}),
            "citation_links": (ds.get("citation_links") or {}).get("n_linked", 0),
            "citation_link_rate": (ds.get("citation_links") or {}).get("link_rate"),
            "spans": len(ds.get("spans") or [])}


def parse_fallback(paper: Paper, store, cfg) -> dict:
    """GROBID 不可用时的纯文本兜底。

    ★ 明确标记 `parsed_by="pypdf"` 且 **sections 保持为空**。
要求保留文档结构；pypdf 给不了结构，所以不能装作给了。
      纯文本写到 structures/<id>.txt，由下游自行决定是否接受这种降级。
    """
    pdf = paper.full_text.get("pdf_path") or ""
    if not pdf or not os.path.isfile(pdf):
        return {"ok": False, "error": "没有本地 PDF"}
    r = ex_grobid.extract_text_fallback(pdf)
    if not r.get("method"):
        return {"ok": False, "error": str(r.get("error") or "无可用解析器")[:200]}
    tp = os.path.join(store.structures_dir,
                      ex_grobid_ext_safe(paper.paper_id) + ".txt")
    with open(tp, "w", encoding="utf-8") as fh:
        fh.write(r.get("text") or "")
    ds = paper.document_structure
    ds["parsed_by"] = "pypdf"
    ds["sections"] = []                    # ← 不假装有结构
    ds["paragraphs_total"] = 0
    ds["text_path"] = tp
    ds["error"] = ""
    paper.full_text["text_path"] = tp
    return {"ok": True, "method": r.get("method"), "chars": len(r.get("text") or ""),
            "sections": 0, "note": "纯文本兜底，无章节结构"}


def ex_grobid_ext_safe(paper_id: str) -> str:
    """复用 store.safe_name，避免两处命名规则不一致导致找不到文件。"""
    from store import safe_name
    return safe_name(paper_id)


def parse_structure(paper: Paper, store, cfg, grobid_ok: bool = True) -> dict:
    """STEP 8 主入口：自动选择 GROBID 或纯文本兜底。

    参数
    ----
    grobid_ok : 由 pipeline 统一探活一次后传入（避免每篇都探活）

    返回 {"ok", "parsed_by", "sections", ..., "error"}
    """
    if grobid_ok:
        r = parse_with_grobid(paper, store, cfg)
        if r.get("ok"):
            r["parsed_by"] = "grobid"
            return r
        # GROBID 单篇失败（例如 PDF 损坏）→ 落到兜底，但记录原因
        fb = parse_fallback(paper, store, cfg)
        fb["error"] = "GROBID: {} | 兜底: {}".format(
            str(r.get("error"))[:120], str(fb.get("error"))[:80])
        fb["parsed_by"] = fb.get("method") and "pypdf" or ""
        return fb
    r = parse_fallback(paper, store, cfg)
    r["parsed_by"] = r.get("method") and "pypdf" or ""
    if not r.get("ok"):
        paper.document_structure["error"] = str(r.get("error"))[:200]
    return r
