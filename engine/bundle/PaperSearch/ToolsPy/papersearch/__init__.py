#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
papersearch —— PaperSearch 工具库统一入口（推荐只从这里导入）

用法
----
    from papersearch import search_arxiv, full_pipeline, doctor

    doctor()                             # 环境自检
    full_pipeline("deep cfr")            # 流水线
    parse_pdf("paper.pdf")               # 元件
    list_tools()                         # 有哪些工具

为什么要有统一入口
------------------
Agent 只需要记住一个导入点，不必知道 `search_arxiv` 在 elements.py、
`full_pipeline` 在 pipelines.py。**降低调用方的认知负担** ——
Agent 的 context 很宝贵，不该花在记"这个函数在哪个文件"上。

所有函数都是直接引用（不是包装），所以签名、文档、行为与各自模块完全一致。

包内模块（ToolsPyRules.md 约定）
--------------------------------
    result.py       返回信封
    errors.py       错误码
    registry.py     工具注册表 + 文档符号索引
    elements.py     元件（原子能力）
    pipelines.py    流水线（自动化工具流）
    _bootstrap.py   外部依赖接入层（内部）
"""
from __future__ import annotations

# ---- 元件（原子能力） ----
from .elements import (                             # noqa: F401
    doctor, list_sources,
    search_arxiv, search_openalex, search_s2, search_crossref, search_all_sources,
    normalize_ids, resolve_doi, match_title,
    find_pdf_urls, download_pdf, resolve_oa,
    grobid_available, parse_pdf, extract_text,
    get_references, get_citations, get_citation_edges, get_works_by_ids,
)
# ---- 流水线（自动化工具流） ----
from .pipelines import (                            # noqa: F401
    search_pipeline, acquire_pipeline, full_pipeline, summarize_result,
)
# ---- 基础设施（Agent 也常用） ----
from .registry import (                             # noqa: F401
    list_tools, describe_tool, find_symbol, read_symbol, build_symbol_index,
)
from .result import pretty, is_ok, is_usable        # noqa: F401
from ._bootstrap import describe as describe_env, check_keys  # noqa: F401

__all__ = [
    # 依赖
    "doctor", "list_sources",
    # 检索
    "search_arxiv", "search_openalex", "search_s2", "search_crossref",
    "search_all_sources",
    # 标识符
    "normalize_ids", "resolve_doi", "match_title",
    # 全文
    "find_pdf_urls", "download_pdf", "resolve_oa",
    # 解析
    "grobid_available", "parse_pdf", "extract_text",
    # 引用
    "get_references", "get_citations", "get_citation_edges", "get_works_by_ids",
    # 流水线
    "search_pipeline", "acquire_pipeline", "full_pipeline", "summarize_result",
    # 文档与自检
    "list_tools", "describe_tool", "find_symbol", "read_symbol",
    "build_symbol_index", "describe_env", "check_keys",
    "pretty", "is_ok", "is_usable",
]

__version__ = "1.0"
