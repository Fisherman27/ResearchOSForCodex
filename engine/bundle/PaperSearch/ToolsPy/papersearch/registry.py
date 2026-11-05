#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
registry.py —— 工具注册表 + 文档符号索引器

两部分
------
1. **注册表**：集中列出所有工具（元件 + 流水线），给 Agent 一份机器可读的清单
   （含参数签名、返回结构、状态语义）。Agent 不必读源码就能知道有哪些工具、
   怎么调、会返回什么。

2. **索引器**（`build_symbol_index()` / `find_symbol()`）：把 API.md 里的
   条目解析成"符号 → 文件行号"的索引，支持
   **从目录直接跳到 API.md 的指定行**。

为什么要索引器（而不是人工维护跳转链接）
----------------------------------------
目录里写 `[E10 search_arxiv](API.md#e10-search_arxiv)` 这种锚点链接，
在 GitHub/编辑器里能用，但在**终端/Agent 读文本**时没用 —— 它拿到的
还是全文。有了行号索引，Agent 可以：
    line = find_symbol("E10")           # → 该条目在 API.md 的第几行
    然后只读那一段，而不是整份 API.md（省 context）。
这也是"可复用到其他工具库"的部分：换一份 API.md，改解析规则即可。

API.md 的条目格式约定（解析器依赖它）
------------------------------------
    ### [E10] search_arxiv
    ...（正文，直到下一个 ### 或 ## 结束）
"""
from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
_TOOLSPY = os.path.dirname(_HERE)              # ToolsPy 根（本包的上层）

from . import result as R                      # noqa: E402

# =============================================================================
# 一、工具注册表
# =============================================================================
# 每条：编号 → {name, module, func, kind, summary, params, returns, status_semantics}
# 这是**机器可读的清单**，也是生成 API.md 目录部分的来源。

REGISTRY: Dict[str, Dict[str, Any]] = {
    # ---------------- 依赖 ----------------
    "E00": {"name": "doctor", "module": "papersearch.elements", "kind": "element",
            "summary": "环境自检：依赖 / 凭证 / GROBID 服务 / 各源可达性",
            "params": {},
            "returns": "{dependencies, credentials, grobid, sources, next_actions}",
            "status": "ok | partial(有补救项) | failed(依赖不可用)",
            "when": "**任何工具报错前先跑它** —— 多数'工具不工作'是缺 key 或服务没起"},
    "E01": {"name": "list_sources", "module": "papersearch.elements", "kind": "element",
            "summary": "列出数据源与能力矩阵",
            "params": {},
            "returns": "{sources[], capability_matrix{}}",
            "status": "ok",
            "when": "不确定该用哪个源时"},

    # ---------------- 检索 ----------------
    "E10": {"name": "search_arxiv", "module": "papersearch.elements", "kind": "element",
            "summary": "arXiv 检索（Export API）",
            "params": {"query": "arXiv 语法，支持 all:/ti:/abs:/au:/cat: 与布尔",
                       "max_results": "上限 2000，默认 20",
                       "start": "偏移（翻页）",
                       "sort_by": "relevance|lastUpdatedDate|submittedDate",
                       "year_from/year_to": "本地过滤（API 不支持）"},
            "returns": "[Paper]",
            "status": "ok | empty(0 命中) | failed",
            "when": "需要 arXiv 全文（PDF 直下最稳）或投递时间"},
    "E11": {"name": "search_openalex", "module": "papersearch.elements", "kind": "element",
            "summary": "OpenAlex 检索（主索引：被引数/参考文献/OA/摘要）",
            "params": {"query": "自由文本，支持 AND/OR/NOT 与短语",
                       "max_results": "默认 25，内部自动翻页",
                       "sort": "如 publication_date:asc、cited_by_count:desc",
                       "year_from/year_to": "服务端过滤"},
            "returns": "[Paper]（含 citation_count/topics/abstract）",
            "status": "ok | empty | failed | failed(RATE_LIMITED)",
            "when": "几乎总是 —— 信息最全的源"},
    "E12": {"name": "search_s2", "module": "papersearch.elements", "kind": "element",
            "summary": "Semantic Scholar 检索（**需 key**；独有 citation context）",
            "params": {"query": "自由文本", "max_results": "上限 100"},
            "returns": "[Paper]",
            "status": "**blocked**(无 key) | ok | empty | failed",
            "when": "需要引用上下文（引用它的原句）时"},
    "E13": {"name": "search_all_sources", "module": "papersearch.elements", "kind": "element",
            "summary": "多源检索 + 跨源去重",
            "params": {"query": "", "max_per_source": "每源条数",
                       "sources": "限定源；None=全部可用",
                       "dedup": "是否去重（默认 True）"},
            "returns": "{papers, by_source, source_status, dedup}",
            "status": "ok | partial(某源失败但有结果) | blocked(全缺凭证) | empty",
            "when": "常规入口：一次拿到多源合并结果"},
    "E14": {"name": "search_crossref", "module": "papersearch.elements", "kind": "element",
            "summary": "Crossref DOI 出版物补召回（无需 key）",
            "params": {"query": "自由文本", "max_results": "上限 20"},
            "returns": "[Paper]（正式出版信息、参考 DOI、可用 PDF 链接）",
            "status": "ok | empty | failed",
            "when": "补充未进入预印本索引的正式出版物"},

    # ---------------- 标识符 ----------------
    "E20": {"name": "normalize_ids", "module": "papersearch.elements", "kind": "element",
            "summary": "标识符归一化 + 生成确定性 paper_id",
            "params": {"identity 或 doi/arxiv_id/openalex_id/s2_id/title": ""},
            "returns": "{paper_id, identity, arxiv_version, id_kind}",
            "status": "ok | failed(无任何标识符)",
            "when": "跨源对齐、验证主键规则时"},
    "E21": {"name": "resolve_doi", "module": "papersearch.elements", "kind": "element",
            "summary": "DOI → Crossref + Unpaywall 记录",
            "params": {"doi": "支持 URL/裸写"},
            "returns": "{crossref, unpaywall, merged}",
            "status": "ok | partial(某源无记录) | empty",
            "when": "拿到 DOI 后补书目与 OA 位置"},
    "E22": {"name": "match_title", "module": "papersearch.elements", "kind": "element",
            "summary": "标题 → Crossref 候选（给缺 DOI 的论文补 DOI）",
            "params": {"title": "", "author": "首作者姓氏（提高准确率）",
                       "min_similarity": "采纳门槛，默认 0.92（保守）"},
            "returns": "{accepted, candidates, accepted_rule}",
            "status": "ok(有达标候选) | partial(有候选但都未达标) | empty",
            "when": "论文缺 DOI 且想补全时。**保守**：达不到门槛不给 accepted"},

    # ---------------- 全文 ----------------
    "E30": {"name": "find_pdf_urls", "module": "papersearch.elements", "kind": "element",
            "summary": "列出候选 PDF 地址（不下载，按优先级排序）",
            "params": {"paper 或 doi/arxiv_id": ""},
            "returns": "{urls[], n}",
            "status": "ok | empty(闭源，正常情形)",
            "when": "下载失败时先看有哪些候选"},
    "E31": {"name": "download_pdf", "module": "papersearch.elements", "kind": "element",
            "summary": "下载 PDF 并校验完整性（头/尾/可打开）",
            "params": {"paper 或 doi/arxiv_id": "", "out_dir": "",
                       "max_mb": "默认 30", "verify": "默认 True，别关"},
            "returns": "{path, bytes, sha256, source, url, verified}",
            "status": "ok | empty(无 OA 地址) | failed",
            "when": "需要全文时。失败会依次试所有候选地址"},
    "E32": {"name": "resolve_oa", "module": "papersearch.elements", "kind": "element",
            "summary": "Unpaywall：DOI → OA 位置",
            "params": {"doi": "", "only_missing": "已有链接时跳过"},
            "returns": "{best_pdf, is_oa, oa_status, version, license, locations}",
            "status": "ok | empty(无 OA 版本，正常) | failed",
            "when": "想单独查某 DOI 有没有免费全文"},

    # ---------------- 解析 ----------------
    "E40": {"name": "grobid_available", "module": "papersearch.elements", "kind": "element",
            "summary": "GROBID 服务是否可用（解析前先调）",
            "params": {"url": "默认 http://localhost:8070"},
            "returns": "{available, version, url, note}",
            "status": "ok | blocked(服务没起，hint 给启动命令)",
            "when": "**解析前必调** —— 避免每篇白等 120s 超时"},
    "E41": {"name": "parse_pdf", "module": "papersearch.elements", "kind": "element",
            "summary": "PDF → 结构化元件（章节/段落/span/图表/坐标/引文连接）",
            "params": {"pdf_path": "", "elements": "要坐标的元素，默认含 p/head",
                       "consolidate_citations": "**保持 0**（设 1 会慢 140 倍）",
                       "keep_tei": "默认 True"},
            "returns": "{parsed_by, sections, spans, figure_objects, table_objects, "
                       "formula_objects, references, citation_links, coordinates, stats}",
            "status": "ok | partial(有降级/空字段) | empty(无内容) | failed",
            "when": "**核心元件** —— 需要论文内部结构时"},
    "E42": {"name": "extract_text", "module": "papersearch.elements", "kind": "element",
            "summary": "纯文本兜底（不依赖 GROBID，**无结构**）",
            "params": {"pdf_path": ""},
            "returns": "{text, n_chars, n_pages, method}",
            "status": "ok | empty(扫描版抽不出字) | failed",
            "when": "只要文本、不需要结构时"},

    # ---------------- 引用 ----------------
    "E50": {"name": "get_references", "module": "papersearch.elements", "kind": "element",
            "summary": "取某论文的参考文献（它引用了谁）",
            "params": {"work_id": "OpenAlex ID", "limit": "默认 50"},
            "returns": "{works, claimed_count, resolved_count, missing_ids}",
            "status": "ok | partial(有声明的引用但取不到元数据) | empty",
            "when": "向后追溯方法渊源"},
    "E51": {"name": "get_citations", "module": "papersearch.elements", "kind": "element",
            "summary": "取某论文的被引（谁引用了它）",
            "params": {"work_id": "", "limit": "默认 50"},
            "returns": "{works, total_count}",
            "status": "ok | empty(新论文常见) | failed",
            "when": "向前找后续工作、识别经典文献"},
    "E53": {"name": "get_works_by_ids", "module": "papersearch.elements", "kind": "element",
            "summary": "**按 ID 批量取元数据**（把 OpenAlex/DOI/arXiv ID 变成完整记录）",
            "params": {"ids": "混合列表，支持 W123 / DOI / arXiv ID",
                       "batch_size": "每次请求合并多少（默认 50）"},
            "returns": "{works, requested, resolved, missing, missing_ids, id_map}",
            "status": "ok | partial(部分 ID 取不到) | empty | failed",
            "when": "引用边里的外部 ID 想反查是什么论文时 —— 调研高频需求"},
    "E52": {"name": "get_citation_edges", "module": "papersearch.elements", "kind": "element",
            "summary": "把一批论文抽成统一引用边（citing→cited）",
            "params": {"papers": "[Paper]", "include_unresolved": "默认 True"},
            "returns": "{edges, nodes, stats}",
            "status": "ok | empty(无引用关系)",
            "when": "要分析论文间关系时"},

    # ---------------- 流水线 ----------------
    "P10": {"name": "search_pipeline", "module": "papersearch.pipelines", "kind": "pipeline",
            "summary": "检索 → 去重 → 排序（不抓 PDF，快）",
            "params": {"query": "", "max_per_source": "默认 20",
                       "keep_top": "排序后保留，默认 40",
                       "year_from/year_to": "", "concepts": "留空自动抽取"},
            "returns": "{papers, by_source, source_status, dedup, ranking}",
            "status": "ok | partial | empty | blocked | failed",
            "when": "只需文献列表、做调研时"},
    "P20": {"name": "acquire_pipeline", "module": "papersearch.pipelines", "kind": "pipeline",
            "summary": "检索 → 排序 → 抓 PDF → 解析结构",
            "params": {"query": "", "fetch_top": "抓几篇（**成本关键**）",
                       "parse": "默认 True", "out_dir": ""},
            "returns": "{papers, fulltext, parsed, out_dir}",
            "status": "ok | partial | empty | failed",
            "when": "既要列表又要全文与元件时"},
    "P30": {"name": "full_pipeline", "module": "papersearch.pipelines", "kind": "pipeline",
            "summary": "完整流程：检索→去重→排序→引用扩展→抓PDF→解析→三层落盘",
            "params": {"query": "", "out_dir": "**建议显式指定**",
                       "expand_top": "扩展种子数", "fetch_top": "", "parse_top": ""},
            "returns": "{papers, corpus_path, graph_path, out_dir, stats}",
            "status": "ok | partial | empty | blocked | failed",
            "when": "一条命令产出可交给下游的语料库"},
}

# 常用组合（给 Agent 的"菜谱"）
RECIPES: List[Dict[str, str]] = [
    {"goal": "先看这个题目有哪些论文", "call": "search_pipeline(query, keep_top=20)",
     "note": "不抓 PDF，几秒返回"},
    {"goal": "拿到论文全文与结构元件", "call": "acquire_pipeline(query, fetch_top=5)",
     "note": "fetch_top 控制成本：抓 PDF 最慢"},
    {"goal": "产出一份可交给下游的语料库", "call": "full_pipeline(query, out_dir=..., fetch_top=8)",
     "note": "三层落盘 + run.json 可复现"},
    {"goal": "工具报错了，先查环境", "call": "doctor()",
     "note": "看 next_actions；多数问题是缺 key 或 GROBID 没起"},
    {"goal": "只想解析本地一个 PDF", "call": "parse_pdf('/path/to.pdf')",
     "note": "先 grobid_available() 确认服务在"},
    {"goal": "只要纯文本、不要结构", "call": "extract_text('/path/to.pdf')",
     "note": "不需要 GROBID"},
    {"goal": "给一篇没 DOI 的论文补 DOI", "call": "match_title(title, author='smith')",
     "note": "保守：达不到门槛不给 accepted，别硬塞"},
]


def list_tools(kind: str = "") -> Dict[str, Any]:
    """列出全部工具（可按 kind 过滤：element / pipeline）。

    返回 data = {"tools": [{id, name, kind, summary, when}], "n": n,
                 "recipes": [...]}
    给 Agent 的用法：先 list_tools() 看有什么，再 find_symbol(id) 拿文档行号，
    只读那一段。
    """
    try:
        items = []
        for tid, t in REGISTRY.items():
            if kind and t["kind"] != kind:
                continue
            items.append({"id": tid, "name": t["name"], "kind": t["kind"],
                          "module": t["module"], "summary": t["summary"],
                          "when": t.get("when", "")})
        items.sort(key=lambda x: (x["kind"], x["id"]))
        return R.ok({"tools": items, "n": len(items), "recipes": RECIPES},
                    tool="registry.list_tools",
                    diagnostics={"n_results": len(items)})
    except Exception as exc:
        return R.from_exception(exc, tool="registry.list_tools")


def describe_tool(tool_id: str) -> Dict[str, Any]:
    """取单个工具的完整说明（含参数、返回、状态语义、文档行号）。

    比 list_tools 详细：用于 Agent 决定"这个工具能不能解决我的问题"。
    """
    try:
        tid = (tool_id or "").strip().upper()
        t = REGISTRY.get(tid)
        if not t:
            return R.failed("INPUT_NOT_FOUND",
                            message="未知工具编号：{!r}".format(tool_id),
                            tool="registry.describe_tool", component="registry",
                            hint="用 list_tools() 看全部编号（形如 E10 / P30）",
                            context={"tool_id": tool_id})
        out = dict(t)
        out["id"] = tid
        sym = find_symbol(tid)
        if sym.get("ok"):
            out["api_md"] = sym["data"]
        return R.ok(out, tool="registry.describe_tool")
    except Exception as exc:
        return R.from_exception(exc, tool="registry.describe_tool")


# =============================================================================
# 二、API.md 符号索引器
# =============================================================================

# 条目形如：### [E10] search_arxiv
_ENTRY_RE = re.compile(r"^###\s+\[([A-Za-z]\d+)\]\s+(\S+)", re.M)


def _api_md_path(path: str = "") -> str:
    # API.md 位于 ToolsPy 根（包的上层），不是包内
    return path or os.path.join(_TOOLSPY, "API.md")


def build_symbol_index(api_md: str = "") -> Dict[str, Any]:
    """解析 API.md，建立 **符号 → 行号区间** 的索引。

    返回 data = {
        "path": "…/API.md",
        "entries": {"E10": {"name", "line_start", "line_end", "title", "anchor"}},
        "n": n
    }

    ★ 这个索引器是**可复用的**：换一份遵循同样 `### [ID] name` 约定的
      API.md（比如给别的工具库用），改 `_ENTRY_RE` 即可。

    行号是 1-based（与编辑器/`read --offset` 一致）。
    """
    try:
        p = _api_md_path(api_md)
        if not os.path.isfile(p):
            return R.failed("INPUT_NOT_FOUND",
                            message="API.md 不存在：{}".format(p),
                            tool="registry.build_symbol_index",
                            hint="先生成 API.md（用 gen_api_md.py）")
        with open(p, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
        hits = []
        for i, ln in enumerate(lines, 1):
            m = _ENTRY_RE.match(ln)
            if m:
                hits.append((m.group(1).upper(), m.group(2), i))
        entries = {}
        for idx, (tid, name, start) in enumerate(hits):
            # 结束行 = 下一个条目的前一行（或文件末）
            end = (hits[idx + 1][2] - 1) if idx + 1 < len(hits) else len(lines)
            entries[tid] = {"name": name, "line_start": start, "line_end": end,
                            "n_lines": end - start + 1,
                            "title": lines[start - 1].strip(),
                            "anchor": "#{}-{}".format(tid.lower(), name.lower())}
        return R.ok({"path": p, "entries": entries, "n": len(entries)},
                    tool="registry.build_symbol_index",
                    diagnostics={"n_results": len(entries)})
    except Exception as exc:
        return R.from_exception(exc, tool="registry.build_symbol_index")


def find_symbol(symbol: str, api_md: str = "") -> Dict[str, Any]:
    """**从工具编号直接定位到 API.md 的行号区间**（本库要求 1 的那个自动化）。

    用法（Agent 侧）：
        r = find_symbol("E10")
        d = r["data"]
        # 然后只读 API.md 的 d["line_start"]..d["line_end"]，而不是整份文档
        # → 省 context，且拿到的就是该工具的完整说明

    参数
    ----
    symbol : 工具编号（E10 / P30，大小写不敏感）或函数名（search_arxiv）

    返回 data = {"id", "name", "line_start", "line_end", "title",
                 "path", "read_hint"}
      read_hint 是给 Agent 的现成读法提示（含 read 工具的参数形式）。

    示例
    ----
        >>> r = find_symbol("search_arxiv")
        >>> r["data"]["line_start"], r["data"]["line_end"]
        (123, 178)
    """
    try:
        idx = build_symbol_index(api_md)
        if not idx["ok"]:
            return idx
        entries = idx["data"]["entries"]
        key = (symbol or "").strip()
        k = key.upper()
        hit = entries.get(k)
        if not hit:
            # 也允许按函数名查
            for tid, e in entries.items():
                if e["name"].lower() == key.lower():
                    k, hit = tid, e
                    break
        if not hit:
            return R.failed("INPUT_NOT_FOUND",
                            message="API.md 里没有条目：{!r}".format(symbol),
                            tool="registry.find_symbol", component="registry",
                            hint="用 list_tools() 看可用编号；"
                                 "或先跑 build_symbol_index() 看索引里有什么")
        data = {"id": k, "name": hit["name"], "line_start": hit["line_start"],
                "line_end": hit["line_end"], "title": hit["title"],
                "path": idx["data"]["path"],
                "read_hint": "read('{}', offset={}, limit={})".format(
                    idx["data"]["path"], hit["line_start"],
                    hit["line_end"] - hit["line_start"] + 1)}
        return R.ok(data, tool="registry.find_symbol",
                    diagnostics={"n_results": 1, "n_lines": hit["n_lines"]})
    except Exception as exc:
        return R.from_exception(exc, tool="registry.find_symbol")


def read_symbol(symbol: str, api_md: str = "") -> Dict[str, Any]:
    """直接返回 API.md 里该条目的**正文**（省去调用方自己切片）。

    返回 data = {"id", "name", "line_start", "line_end", "markdown"}
    """
    try:
        loc = find_symbol(symbol, api_md)
        if not loc["ok"]:
            return loc
        d = loc["data"]
        with open(d["path"], "r", encoding="utf-8") as fh:
            lines = fh.readlines()
        md = "".join(lines[d["line_start"] - 1: d["line_end"]])
        out = dict(d)
        out["markdown"] = md
        return R.ok(out, tool="registry.read_symbol",
                    diagnostics={"n_results": 1, "n_chars": len(md)})
    except Exception as exc:
        return R.from_exception(exc, tool="registry.read_symbol")
