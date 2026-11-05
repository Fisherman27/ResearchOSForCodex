#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""semantic_scholar.py —— Semantic Scholar 数据源采集方式

定位：**citation expansion、citation context 和相似论文推荐**。
它和 OpenAlex 不是重复关系：
    OpenAlex  → 覆盖广、元数据规范、便宜，适合做"全局索引"
    S2        → 引用上下文 / 引用意图 / 影响力引用 / 推荐，这些是 OpenAlex 没有的

官方文档：https://api.semanticscholar.org/api-docs/
申请 key：https://www.semanticscholar.org/product/api#api-key-form

─────────────────────────────────────────────────────────────────────────────
⚠️ 当前状态：**BLOCKED —— 需要 API key（申请中）**
─────────────────────────────────────────────────────────────────────────────
实测（2026-09-28）：无 key 时所有请求返回
    HTTP 429 {"message": "Too Many Requests. Please wait and try again or apply
              for a key for higher rate limits."}
这不是"你打太快"，而是匿名共享池已被耗尽 —— 无 key 基本不可用。
拿到 key 后填进 Keys.txt 的 S2_API_KEY 即可。

拿到 key 后的速率：官方口径约 **1 request/s**，比 arXiv 还严。所以：
    - 每个请求都要尽量多带 fields（一次拿全，别分多次）
    - 批量接口（/paper/batch）优先于循环单篇
    - 必须实现串行 + 退避，别并发

─────────────────────────────────────────────────────────────────────────────
Paper ID 的写法（很关键，写错就 404）
─────────────────────────────────────────────────────────────────────────────
    '649def34f8be52c8b66281af98ae884c09aef38b'   S2 自己的 40 位 sha
    'CorpusId:215416146'                          S2 语料库数字 ID
    'DOI:10.1145/1234567.1234568'                 DOI 前缀
    'ARXIV:1603.01121'                            arXiv ID 前缀
    'URL:https://arxiv.org/abs/1603.01121'        URL 前缀
    'ACL:W12-3903' / 'MAG:112218234' / 'PMID:19872477'
**建议一律用 'ARXIV:xxx' 或 'DOI:xxx'** —— 这样不需要先查 S2 ID 就能直接调用。

─────────────────────────────────────────────────────────────────────────────
fields 参数（必填，不写只返回极少字段）
─────────────────────────────────────────────────────────────────────────────
标题类：title, abstract, tldr, year, publicationDate, venue, publicationVenue,
        externalIds, url, openAccessPdf, fieldsOfStudy, s2FieldsOfStudy
影响类：citationCount, influentialCitationCount, referenceCount
关系类：references.title, citations.title, citations.contexts,
        citations.intents, citations.isInfluential, citations.influentialCitationCount
作者类：authors.name, authors.authorId, authors.affiliations, authors.hIndex
用点号可以取嵌套字段（如 citations.title），能一次拿到关联论文的标题。
**字段写得越多，单次响应越大但请求数越少 —— 在 1 req/s 的限制下这是最优策略。**

命令行用法（需要 key）：
    python3 semantic_scholar.py search "counterfactual regret minimization"
    python3 semantic_scholar.py paper ARXIV:1603.01121
    python3 semantic_scholar.py refs ARXIV:1603.01121
    python3 semantic_scholar.py cites ARXIV:1603.01121     # 含 citation context
    python3 semantic_scholar.py recommend ARXIV:1603.01121
    python3 semantic_scholar.py check

─────────────────────────────────────────────────────────────────────────────
函数索引（快速定位）
  ★ 本模块当前状态：缺 S2_API_KEY，所有方法会返回 status=0 + missing_credentials
  [凭证]  SemanticScholar.available()
          key 是否已配置；未配置时**不发请求**，状态码 0 而非伪造 429
  [检索]  .search_papers(query, limit, fields, year, venue, open_access_only)
          关键词检索（单次 ≤100）
  [检索]  .search_bulk(query, fields, sort, year, limit)
          批量检索（单次 ≤1000，token 翻页，推荐大规模采集用）
  [作品]  .get_paper(paper_id, fields)
          单篇详情；externalIds 是跨库实体对齐的关键
  [作品]  .get_papers_batch(paper_ids, fields, batch_size)
          批量取详情（自动分批）；★ fields 必须随 POST 一起发送
  [图谱]  .get_references(paper_id, limit)     参考文献
  [图谱]  .get_citations(paper_id, limit)      被引 + **contexts / intents**
  [推荐]  .get_recommendations(paper_id, limit)
          相似论文（无 key 时可能 200 但结果为空）
  [工具]  to_internal_paper(paper)             统一 Paper schema
  [自检]  check(s2)  [CLI] main(argv)
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import (http_request, url_with, section, kv, show_records,
                     get_key, mask)

BASE = "https://api.semanticscholar.org"

# ---- 可调参数 ---------------------------------------------------------------
# 官方约 1 request/s。调小必被 429；配合 _request 内部的最小间隔使用。
MIN_INTERVAL = 1.2          # 两次请求之间的最小间隔（秒）
HTTP_TIMEOUT = 30.0
HTTP_RETRIES = 2
BATCH_LIMIT = 500           # /paper/batch 单次最多 500 个 ID
SEARCH_LIMIT = 100          # /paper/search 单次最多 100 条

# 常用 fields 组合，直接抄
FIELDS_PAPER = ("title,abstract,year,venue,publicationDate,citationCount,"
                "influentialCitationCount,referenceCount,externalIds,"
                "openAccessPdf,authors.name,fieldsOfStudy,url")
FIELDS_SEARCH = ("title,year,venue,citationCount,externalIds,openAccessPdf,"
                 "abstract,authors.name")
FIELDS_CONTEXT = ("contexts,intents,isInfluential,influentialCitationCount,"
                  "title,year,venue,citationCount,externalIds")


class SemanticScholar:
    """Semantic Scholar 客户端。

    特点：内置最小请求间隔（令牌桶的简化版），避免因为 1 req/s 限制而被打回 429。

    用法：
        s2 = SemanticScholar()          # 自动读 Keys.txt 的 S2_API_KEY
        r = s2.search_papers("deep cfr")
        p = s2.get_paper("ARXIV:1603.01121")
    """

    def __init__(self, api_key: str = "", email: str = "",
                 timeout: float = HTTP_TIMEOUT, min_interval: float = MIN_INTERVAL):
        self.api_key = api_key or get_key("S2_API_KEY")
        self.email = email or get_key("PAPERSEARCH_EMAIL")
        self.timeout = timeout
        self.min_interval = min_interval
        self._last_call = 0.0
        self.rate_limited = False      # 一旦撞 429 就置位，调用方可以据此降级

    # ---- 底层 ---------------------------------------------------------------
    def _throttle(self):
        """保证两次请求间隔不小于 min_interval。"""
        wait = self.min_interval - (time.time() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.time()

    def _request(self, path: str, params: dict = None, method: str = "GET",
                 body: bytes = None):
        """发一个请求。GET 和 POST 都把 params 编进 query string，POST 额外带 JSON body。

        返回 (status, data, raw_text)
          status=429 → self.rate_limited 会被置位，且 data 为 None
          status=0   → **本地判定为「缺凭证」，没有真的发出请求**

        ⚠️ 踩过的坑：POST 分支早期写成 `url = BASE + path`，**把 params 丢了**，
           于是 /paper/batch 的 `fields` 参数根本不生效 —— 即使拿到 key，
           批量返回也只有默认字段（没有摘要、作者、externalIds）。
           正确做法是两种 method 都拼 query string，POST 只是多带一个 body。
        """
        if not self.api_key:
            # ★ 刻意返回 status=0 而不是伪造 429：
            #   「没配 key」和「服务器限流」是两件不同的事，
            #   混成同一个状态码会让排错方向完全跑偏（去查限流，实际问题在配置）。
            return 0, None, ("missing_credentials：未配置 S2_API_KEY。"
                             "Semantic Scholar 匿名调用实测 429，"
                             "请到 https://www.semanticscholar.org/product/api "
                             "申请免费 key 并填入 Keys.txt")

        url = url_with(BASE + path, params or {})   # GET / POST 都要拼
        data_bytes = body if method == "POST" else None

        self._throttle()
        headers = {"x-api-key": self.api_key, "Content-Type": "application/json"}
        status, raw, _ = http_request(url, method=method, headers=headers,
                                      data=data_bytes, timeout=self.timeout,
                                      retries=HTTP_RETRIES, backoff=3.0,
                                      email=self.email)
        text = raw.decode("utf-8", "replace")
        if status == 429:
            self.rate_limited = True
            return 429, None, "429 Too Many Requests（触及 1 req/s 限制，已自动重试仍失败）"
        if status != 200:
            return status, None, text[:300]
        try:
            import json
            return status, json.loads(text), text
        except Exception:
            return status, None, text[:300]

    def available(self) -> bool:
        """key 是否已配置。

        没 key 时所有方法**不会发请求**，直接返回 status=0 +
        `missing_credentials`，因此不能用 status 判断"服务是否可用"，
        只能用它判断"本地是否具备调用条件"。
        """
        return bool(self.api_key)

    # ---- 检索 ---------------------------------------------------------------
    def search_papers(self, query: str, limit: int = 20, fields: str = FIELDS_SEARCH,
                      year: str = "", venue: str = "", open_access_only: bool = False):
        """关键词检索 /graph/v1/paper/search

        参数
        ----
        query   : 关键词。**S2 对宽泛 query 常返回 total>0 但 data 为空**，
                  所以查询要具体（加引号、加领域词），或改用推荐接口
        limit   : 返回条数，1~100（硬上限，要更多请翻页 offset）
        fields  : 见文件头说明。写得越全越省请求
        year    : 年份过滤，'2020'、'2018-2022'、'2020-'（2020 至今）
        venue   : venue 过滤，如 'NeurIPS'、'ICLR'（模糊匹配）
        open_access_only : 只要开放获取的

        返回 {"status", "total", "papers": [...], "error"}
        paper 字段：paperId(S2 ID) / title / abstract / year / venue /
          citationCount / influentialCitationCount / externalIds(含 DOI、ArXiv) /
          openAccessPdf(含 url，可直接下载全文) / authors

        用途：和 arXiv/OpenAlex 互补的候选发现；
              externalIds 里的 ArXiv 字段是**跨库实体对齐的桥梁**。
        """
        params = {"query": query, "limit": min(limit, SEARCH_LIMIT), "fields": fields}
        if year:
            params["year"] = year
        if venue:
            params["venue"] = venue
        if open_access_only:
            params["openAccessPdf"] = ""     # S2 用"传空参数"表示只要 OA
        status, data, text = self._request("/graph/v1/paper/search", params)
        if status != 200 or not data:
            return {"status": status, "total": 0, "papers": [], "error": text}
        return {"status": 200, "total": data.get("total", 0),
                "papers": data.get("data", []), "error": ""}

    def search_bulk(self, query: str, fields: str = FIELDS_SEARCH,
                    sort: str = "", year: str = "", limit: int = 1000):
        """批量检索 /graph/v1/paper/search/bulk（**推荐用于大规模采集**）。

        与 search 的区别：
          search      单次 ≤100 条，按相关性；适合"看看有什么"
          search/bulk 单次最多 1000 条，支持排序和 token 翻页；适合"系统采集"
          另外还有 /paper/search/match（按标题精确匹配，做去重很好用）

        参数
        ----
        sort  : 'citationCount:desc'（高被引优先）/ 'publicationDate:desc'（最新优先）
                'paperId'（按 ID 顺序，适合稳定翻页）
        year  : 同 search_papers
        limit : 最多取多少条

        返回 {"status", "total", "papers", "error"}
        注意 bulk 返回的字段结构和 search 略有不同（是 {"total","token","data"}）。
        """
        papers, token, total = [], "", 0
        while len(papers) < limit:
            params = {"query": query, "fields": fields}
            if token:
                params["token"] = token
            else:
                if sort:
                    params["sort"] = sort
                if year:
                    params["year"] = year
            status, data, text = self._request("/graph/v1/paper/search/bulk", params)
            if status != 200 or not data:
                return {"status": status, "total": total, "papers": papers, "error": text}
            total = data.get("total", total)
            batch = data.get("data") or []
            if not batch:
                break
            papers.extend(batch)
            token = data.get("token") or ""
            if not token:
                break
        return {"status": 200, "total": total, "papers": papers[:limit], "error": ""}

    # ---- 单篇 ---------------------------------------------------------------
    def get_paper(self, paper_id: str, fields: str = FIELDS_PAPER):
        """单篇详情 /graph/v1/paper/{id}

        paper_id 写法见文件头（推荐 'ARXIV:1603.01121' 或 'DOI:...'）。

        返回 {"status", "paper", "error"}
        关键字段：
          externalIds          {'ArXiv': '1603.01121', 'DOI': '...', 'CorpusId': ...}
                               ← **实体统一的核心**，一次拿到所有别名
          openAccessPdf        {'url': '...', 'status': 'GREEN'} 可直接下载
          influentialCitationCount  有影响力的引用数，比 citationCount 更能指示重要性
          tldr                 模型生成的摘要（需要时加进 fields）
        """
        status, data, text = self._request(
            "/graph/v1/paper/{}".format(paper_id), {"fields": fields})
        if status != 200 or not data:
            return {"status": status, "paper": None, "error": text}
        return {"status": 200, "paper": data, "error": ""}

    def get_papers_batch(self, paper_ids: list, fields: str = FIELDS_PAPER,
                         batch_size: int = BATCH_LIMIT):
        """批量取详情 /graph/v1/paper/batch （POST）

        参数
        ----
        paper_ids  : ID 列表，写法见文件头；**数量不限**，内部按批切分
        batch_size : 每批多少个，默认 500（S2 单次上限）
        fields     : 要返回的字段。★ 必须传，否则只有默认字段
                     （早期 bug：POST 分支把 params 丢了，fields 静默失效）

        返回 {"status", "ok", "papers", "requested", "found", "batches",
              "failed_batches", "error"}

        **强烈推荐**：1 req/s 的限制下，一次 batch 胜过 500 次单篇。
        用途：候选池批量补全元数据。
        """
        ids = [p for p in (paper_ids or []) if p]
        if not ids:
            return {"status": 200, "ok": True, "papers": [], "requested": 0,
                    "found": 0, "batches": 0, "failed_batches": 0, "error": ""}
        import json as _json
        papers, batches, failed, errors = [], 0, 0, []
        for i in range(0, len(ids), max(1, batch_size)):
            chunk = ids[i:i + batch_size]
            batches += 1
            status, data, text = self._request(
                "/graph/v1/paper/batch",
                method="POST",
                body=_json.dumps({"ids": chunk}).encode("utf-8"),
                params={"fields": fields},      # ← 必须带上，否则 fields 失效
            )
            if status != 200 or not isinstance(data, list):
                failed += 1
                errors.append("batch {}: HTTP {} {}".format(batches, status, text[:100]))
                continue
            papers.extend([p for p in data if p])
        return {"status": 200 if not failed else 207, "ok": not failed,
                "papers": papers, "requested": len(ids), "found": len(papers),
                "batches": batches, "failed_batches": failed,
                "error": "; ".join(errors[:2])}

    # ---- Citation graph -----------------------------------------------------
    def get_references(self, paper_id: str, limit: int = 100, fields: str = FIELDS_SEARCH):
        """参考文献 /graph/v1/paper/{id}/references

        返回 {"status", "references": [{...paper 字段..., 'citedPaper': {...}}]}
        注意：每条是一个"引用边"对象，被引论文在 citedPaper 字段里。

        用途：追溯方法渊源。
        """
        return self._paged("/graph/v1/paper/{}/references".format(paper_id),
                           "citedPaper", limit, fields)

    def get_citations(self, paper_id: str, limit: int = 100, fields: str = FIELDS_CONTEXT):
        """被引 /graph/v1/paper/{id}/citations  ← **这是 S2 最不可替代的接口**

        返回 {"status", "citations": [{'citingPaper': {...}, 'contexts': [...],
                'intents': [...], 'isInfluential': bool}]}

        三个独有字段（要的正是这些）：
          contexts      引用上下文原文片段，例如
                        ["Previous CFR methods require traversal of the entire game tree..."]
                        ← 原文切片，是**证据**：为什么引用、在什么语境下引用
          intents       引用意图标签，S2 的取值只有三类：
                        background（背景）/ methodology（用了其方法）/ result（比较了结果）
          isInfluential 是否"有影响力的引用"（S2 用模型判定，比单纯计数更有信息量）

        用途：给引用边附上**证据与粗粒度意图**，供后续分析使用。
        ⚠️ 边界（别过度解读）：
          - `intents` 是 S2 预训练的**三分类标签**，不是细粒度语义关系。
            它**不能**直接读成"criticizes"、"identifies limitation of" 这类结论。
          - "A 指出了 B 的局限"这种判断，必须由**下游的论文理解模块**读
            `contexts` 原文后得出，不能由 intents 标签自动升级。
          - 这与的边界一致：**检索阶段不强行判断语义关系**，
            检索阶段只负责把证据（contexts）完整地带出来。

        建议 fields 用 FIELDS_CONTEXT（已含 contexts/intents）。
        """
        return self._paged("/graph/v1/paper/{}/citations".format(paper_id),
                           "citingPaper", limit, fields)

    def _paged(self, path: str, wrapper_key: str, limit: int, fields: str):
        """references / citations 通用翻页（用 offset，单页上限 1000）。"""
        out, offset = [], 0
        while len(out) < limit:
            status, data, text = self._request(path, {
                "fields": fields, "limit": min(1000, limit - len(out)),
                "offset": offset})
            if status != 200 or not data:
                return {"status": status, "count": len(out), "data": out, "error": text}
            batch = data.get("data") or []
            if not batch:
                break
            out.extend(batch)
            offset += len(batch)
            nxt = data.get("next")
            if nxt in (None, 0):
                break
        return {"status": 200, "count": len(out), "data": out[:limit], "error": ""}

    # ---- 推荐 ---------------------------------------------------------------
    def get_recommendations(self, paper_id: str, limit: int = 20,
                            fields: str = FIELDS_SEARCH):
        """相似/推荐论文 /recommendations/v1/papers/forpaper/{id}

        参数
        ----
        paper_id : 种子论文，写法见文件头
        limit    : 最多 100

        返回 {"status", "papers", "note", "error"}

        实现细节：底层是 SPECTER 向量相似度，返回的是"内容相近"而非"引用相关"，
        因此能发现**引用图里没有关联、但主题相关**的论文 —— 补漏很有用。

        ⚠️ 实测异常：无 key 时该接口返回 HTTP 200 但 recommendedPapers 为空
        （不是 429）。所以"空结果"要区分是"真没有"还是"没权限"。
        拿到 key 后建议先跑 check() 确认这一项非空。
        """
        status, data, text = self._request(
            "/recommendations/v1/papers/forpaper/{}".format(paper_id),
            {"limit": min(limit, 100), "fields": fields})
        if status != 200 or data is None:
            return {"status": status, "papers": [], "error": text}
        papers = data.get("recommendedPapers") or []
        return {"status": 200, "papers": papers,
                "note": "" if papers else
                        "接口 200 但结果为空：可能是无 key/限流，也可能是真的没有相似论文",
                "error": ""}


def to_internal_paper(paper: dict) -> dict:
    """把 S2 paper 转成内部统一 Paper 结构。

    S2 的价值在于 externalIds —— 它同时带着 DOI / ArXiv / CorpusId，
    是跨库实体对齐最省事的一环。
    """
    ext = paper.get("externalIds") or {}
    return {
        "identity": {
            "s2_id": paper.get("paperId"),
            "doi": ext.get("DOI"),
            "arxiv_id": ext.get("ArXiv"),
            "openalex_id": None, "openreview_id": None,
        },
        "metadata": {
            "title": paper.get("title"),
            "authors": [a.get("name") for a in (paper.get("authors") or [])],
            "year": paper.get("year"),
            "venue": paper.get("venue"),
            "citation_count": paper.get("citationCount"),
            "influential_citation_count": paper.get("influentialCitationCount"),
        },
        "abstract": paper.get("abstract"),
        "full_text": {"pdf_url": (paper.get("openAccessPdf") or {}).get("url")},
        "search_meta": {"found_by": [{"source": "semantic_scholar", "kind": "search"}]},
    }


# =============================================================================
# 自检与命令行入口
# =============================================================================

def check(s2: SemanticScholar) -> int:
    if not s2.available():
        section("Semantic Scholar —— BLOCKED：缺少 API key")
        kv("状态", "未配置 S2_API_KEY")
        kv("影响", "以下 5 个接口全部无法验证，实测匿名返回 429")
        for x in ("paper/search", "paper/{id}", "paper/{id}/citations（含 context）",
                  "paper/{id}/references", "recommendations"):
            print("      ✗ {}".format(x))
        kv("申请", "https://www.semanticscholar.org/product/api#api-key-form")
        kv("填入", "Keys.txt 的 S2_API_KEY= 一行")
        kv("注意", "无 key 时不会发请求，直接返回 429（省得白等）")
        return 1

    failed = 0
    section("1) 关键词检索")
    r = s2.search_papers("counterfactual regret minimization", limit=3)
    kv("状态", "HTTP {}".format(r["status"]))
    kv("命中总数", r["total"])
    if r["papers"]:
        show_records(r["papers"], ["paperId", "year", "citationCount", "externalIds"])
    else:
        kv("错误/说明", r.get("error") or "返回空（S2 对宽泛 query 会返回空）")
        failed += 1

    section("2) 单篇详情（externalIds 是跨库对齐的关键）")
    p = s2.get_paper("ARXIV:1603.01121")
    if p["paper"]:
        kv("标题", p["paper"].get("title"))
        kv("externalIds", p["paper"].get("externalIds"))
        kv("被引/影响力被引", "{}/{}".format(p["paper"].get("citationCount"),
                                             p["paper"].get("influentialCitationCount")))
        kv("OA PDF", (p["paper"].get("openAccessPdf") or {}).get("url"))
    else:
        kv("错误", p.get("error"))
        failed += 1

    section("3) 被引 + citation context（S2 独有）")
    c = s2.get_citations("ARXIV:1603.01121", limit=5)
    kv("条数", c["count"])
    if c["data"]:
        first = c["data"][0]
        kv("intents", first.get("intents"))
        kv("isInfluential", first.get("isInfluential"))
        ctx = first.get("contexts") or []
        kv("context 示例", (ctx[0][:120] if ctx else "（本条无 context）"))
        kv("用途", "给引用边附证据（contexts 是原文切片）；intents 只是三分类标签，"
                   "不能自动当成「指出局限」的结论")
    else:
        kv("错误", c.get("error"))
        failed += 1

    section("4) 参考文献")
    refs = s2.get_references("ARXIV:1603.01121", limit=5)
    kv("条数", refs["count"])
    if not refs["data"]:
        kv("错误", refs.get("error"))
        failed += 1

    section("5) 相似论文推荐")
    rec = s2.get_recommendations("ARXIV:1603.01121", limit=5)
    kv("状态", "HTTP {}".format(rec["status"]))
    kv("条数", len(rec["papers"]))
    if rec.get("note"):
        kv("说明", rec["note"])
    if not rec["papers"]:
        failed += 1

    print("\n" + "-" * 78)
    print("Semantic Scholar 自检：{}".format("全部通过" if failed == 0 else "{} 项失败".format(failed)))
    return failed


def main(argv) -> int:
    s2 = SemanticScholar()
    print("凭证: S2_API_KEY={} | mailto={}".format(
        mask(s2.api_key), s2.email or "（未设置）"))
    print("限速: 两次请求至少间隔 {}s（官方约 1 req/s）".format(s2.min_interval))

    if not argv or argv[0] == "check":
        return check(s2)

    cmd = argv[0]

    if cmd == "search":
        q = argv[1] if len(argv) > 1 else "deep cfr"
        r = s2.search_papers(q, limit=5)
        section("S2 检索: {}".format(q))
        kv("状态", "HTTP {}".format(r["status"]))
        kv("命中总数", r.get("total"))
        if r["status"] == 200:
            for p in r["papers"]:
                print("\n  ▶ {}".format(p.get("title")))
                print("    paperId  : {}   年份: {}   被引: {}".format(
                    p.get("paperId"), p.get("year"), p.get("citationCount")))
                print("    external : {}".format(p.get("externalIds")))
                print("    OA PDF   : {}".format(
                    (p.get("openAccessPdf") or {}).get("url") or "（无）"))
        else:
            kv("错误", r.get("error"))
            return 1
        return 0

    if cmd in ("paper", "refs", "cites", "recommend"):
        pid = argv[1] if len(argv) > 1 else "ARXIV:1603.01121"
        if cmd == "paper":
            r = s2.get_paper(pid)
            section("S2 paper {}".format(pid))
            if r["paper"]:
                import json
                print(json.dumps(r["paper"], ensure_ascii=False, indent=2)[:2500])
            else:
                kv("错误", r.get("error"))
                return 1
        elif cmd == "refs":
            r = s2.get_references(pid, limit=10)
            section("S2 references of {}".format(pid))
            kv("条数", r["count"])
            show_records([x.get("citedPaper") or {} for x in r["data"]],
                         ["title", "year", "citationCount"])
        elif cmd == "cites":
            r = s2.get_citations(pid, limit=10)
            section("S2 citations of {}（含引用上下文）".format(pid))
            kv("条数", r["count"])
            for item in r["data"][:5]:
                cp = item.get("citingPaper") or {}
                print("\n  ▶ {}".format(cp.get("title")))
                print("    intents={} isInfluential={}".format(
                    item.get("intents"), item.get("isInfluential")))
                for ctx in (item.get("contexts") or [])[:1]:
                    print("    context: {}…".format(ctx[:150]))
        else:
            r = s2.get_recommendations(pid, limit=10)
            section("S2 相似论文推荐（种子 {}）".format(pid))
            kv("条数", len(r["papers"]))
            if r.get("note"):
                kv("说明", r["note"])
            show_records(r["papers"], ["title", "year", "citationCount"])
        return 0

    if cmd == "batch":
        ids = argv[1:]
        r = s2.get_papers_batch(ids)
        section("S2 批量取详情")
        kv("请求/返回", "{}/{}".format(len(ids), len(r["papers"])))
        show_records(r["papers"], ["paperId", "title", "year"])
        return 0

    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
