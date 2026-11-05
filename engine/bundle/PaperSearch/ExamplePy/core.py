#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""core.py —— CORE 数据源采集方式

定位：**OA 全文补漏**，V2 阶段才需要，不是核心检索源。
它聚合了全球机构仓库、OA 期刊、预印本服务器的元数据 + 全文。

官方文档：https://api.core.ac.uk/docs/v3
申请 key：https://core.ac.uk/services/api

─────────────────────────────────────────────────────────────────────────────
⚠️ 当前状态：**BLOCKED —— 需要 API key（可选，未申请）**
─────────────────────────────────────────────────────────────────────────────
CORE v3 的所有接口都要 `Authorization: Bearer {key}`。
没有 key 时请求会被重定向/拒绝（实测无 key 访问 /v3/search/works 会被 307 重定向）。

**License 需要认真确认**：CORE 官方提供免费 API，但免费许可有具体条款
（个人研究、部分公共研究机构用途可符合）。实现前建议读一遍：
https://core.ac.uk/services/api 与 https://core.ac.uk/terms

─────────────────────────────────────────────────────────────────────────────
在全文获取链路里的位置
─────────────────────────────────────────────────────────────────────────────
    arXiv（免费 PDF，第一优先）
      ↓ 找不到
    OpenAlex best_oa_location
      ↓ 找不到
    Unpaywall（DOI → OA 位置）
      ↓ 找不到
    CORE（机构仓库补漏）      ← 你在这里
因为它是"最后一道兜底"，调用量小，免费额度基本够用。

命令行用法（需要 key）：
    python3 core.py search "counterfactual regret minimization"
    python3 core.py fulltext 1234567
    python3 core.py check

─────────────────────────────────────────────────────────────────────────────
函数索引（快速定位）
  ★ 本模块当前状态：缺 CORE_API_KEY，会返回 status=0 + missing_credentials
  [底层]  CORE._raw(path, ...)                 原始文本响应，**不 JSON 解码、不截断**
  [底层]  CORE._request(path, ...)             JSON 接口封装（在 _raw 之上）
  [检索]  CORE.search_works(query, limit, offset, fields)
          全文/元数据检索；结果可能直接带 inline fullText
  [作品]  CORE.get_work(core_id, fulltext)     单篇详情
  [全文]  CORE.get_fulltext(core_id)           全文纯文本（★ 走 _raw，不截断）
  [工具]  to_internal_paper(work)              统一 schema；保存 inline_text
  [自检]  check(core)  [CLI] main(argv)        search / fulltext
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import http_request, url_with, section, kv, show_records, get_key, mask

BASE = "https://api.core.ac.uk/v3"

HTTP_TIMEOUT = 30.0
HTTP_RETRIES = 1        # 带 key 时不宜盲目重试，避免消耗配额
MAX_LIMIT = 100         # /search/works 单次上限（受套餐影响，保守取值）


class CORE:
    """CORE v3 客户端。所有方法都需要 key。

    用法：
        core = CORE()                    # 自动读 Keys.txt 的 CORE_API_KEY
        r = core.search_works("deep cfr", limit=10)
    """

    def __init__(self, api_key: str = "", email: str = "",
                 timeout: float = HTTP_TIMEOUT, retries: int = HTTP_RETRIES):
        self.api_key = api_key or get_key("CORE_API_KEY")
        self.email = email or get_key("PAPERSEARCH_EMAIL")
        self.timeout = timeout
        self.retries = retries

    def available(self) -> bool:
        return bool(self.api_key)

    def _raw(self, path: str, params: dict = None, method: str = "GET",
             body: bytes = None):
        """底层请求：**返回原始文本，不做 JSON 解析、不做截断**。

        返回 (status, text, http_headers)

        ⚠️ 为什么必须和 _request 分开：
           `/works/{id}/fulltext` 返回的是**纯文本全文**（可能几百 KB），
           不是 JSON。早期实现让所有响应都走 `_request` 的 JSON 解码路径，
           解码失败时兜底 `text[:300]` —— 结果 200 响应里 500KB 的全文
           被截成 300 字符，还被当作"成功"返回给调用方，静默丢数据。
           凡是可能返回非 JSON 的端点，一律走这个函数。
        """
        if not self.api_key:
            return 0, ("missing_credentials：未配置 CORE_API_KEY。CORE v3 全部接口需要 "
                       "Authorization: Bearer <key>；申请：https://core.ac.uk/services/api"), {}
        url = url_with(BASE + path, params or {})
        headers = {"Authorization": "Bearer " + self.api_key,
                   "Content-Type": "application/json"}
        status, raw, hdrs = http_request(url, method=method, headers=headers,
                                         data=body, timeout=self.timeout,
                                         retries=self.retries, email=self.email)
        return status, raw.decode("utf-8", "replace"), hdrs

    def _request(self, path: str, params: dict = None, method: str = "GET",
                 body: bytes = None):
        """JSON 接口专用：在 _raw 之上做 JSON 解码。

        返回 (status, data, raw_text)
          status=0   → **缺凭证**（本地判定，没有真的发请求）
          status=401 → 服务器明确拒绝（key 无效或未激活）
          status=429 → 超出套餐配额（免费套餐有明确的次数上限）
          status=200 但 data=None → HTTP 成功但响应不是 JSON（要警惕）
        """
        status, text, _hdrs = self._raw(path, params=params, method=method, body=body)
        if status == 0:
            return 0, None, text
        if status == 401:
            return 401, None, "401 Unauthorized：key 无效或未激活"
        if status == 429:
            return 429, None, "429：超出套餐配额"
        if status != 200:
            return status, None, text[:300]
        try:
            import json
            return status, json.loads(text), text
        except Exception:
            return status, None, "HTTP 200 但响应不是 JSON：{}".format(text[:200])

    # ---- 检索 ---------------------------------------------------------------
    def search_works(self, query: str, limit: int = 10, offset: int = 0,
                     fields: str = ""):
        """全文/元数据检索 /v3/search/works

        参数
        ----
        query  : CORE 的查询语法（支持 AND/OR/NOT、字段限定，类似 Elasticsearch）。
                 例：'counterfactual regret minimization'
                     'title:"deep cfr"'
                     'fullText:"regret minimization"'
        limit  : 返回条数，默认 10，上限随套餐
        offset : 偏移量，用于翻页
        fields : 逗号分隔，限定返回哪些字段。不传返回默认字段集

        返回 {"status", "total", "results": [...], "error"}

        result 关键字段：
          id             CORE 内部 ID（数字或 hash）
          title          标题
          abstract       摘要
          authors        作者
          yearPublished  年份
          doi / arxivId  外部标识
          downloadUrl    ★ 全文下载地址（可能是 PDF、也可能是 landing page）
          fullText       ★ 全文纯文本（部分记录直接带，省去 PDF 解析！）
          publisher / journals / language / documentType
          repositoryDocument  仓库侧信息（机构仓库来源）

        用途：arXiv / Unpaywall 都拿不到全文时的兜底。
        工程提示：**优先用 fullText 字段** —— 它已经是纯文本，
                  可以跳过 PDF 下载和 GROBID 解析，成本低得多。
        """
        params = {"q": query, "limit": min(limit, MAX_LIMIT)}
        if offset:
            params["offset"] = offset
        if fields:
            params["fields"] = fields
        status, data, text = self._request("/search/works", params)
        if status != 200 or not data:
            return {"status": status, "total": 0, "results": [], "error": text}
        return {"status": 200, "total": data.get("totalHits", 0),
                "results": data.get("results", []), "error": ""}

    def get_work(self, core_id: str, fulltext: bool = False):
        """按 ID 取单篇 /v3/works/{id}

        参数
        ----
        core_id  : CORE 数字 ID
        fulltext : 是否包含全文。**全文可能几百 KB，默认关闭**

        返回 {"status", "work", "error"}
        """
        params = {"fullText": "true"} if fulltext else {}
        status, data, text = self._request("/works/{}".format(core_id), params)
        if status != 200 or not data:
            return {"status": status, "work": None, "error": text}
        return {"status": 200, "work": data, "error": ""}

    def get_fulltext(self, core_id: str):
        """只取全文纯文本 /v3/works/{id}/fulltext

        返回 {"status", "text", "length", "error"}
        用途：直接拿文本做论文分析，不需要 PDF 解析链路。
        注意：不是所有记录都有全文（取决于来源仓库是否开放原文）。
        """
        # ★ 走 _raw：这个端点返回纯文本全文，绝不能进 JSON 解码 + [:300] 兜底那条路
        status, text, hdrs = self._raw("/works/{}/fulltext".format(core_id))
        ctype = (hdrs or {}).get("Content-Type", "")
        if status == 200:
            # 少数情况下 CORE 会包一层 JSON（{"fullText": "..."}），这里做兼容
            if "json" in ctype.lower():
                try:
                    import json
                    obj = json.loads(text)
                    if isinstance(obj, dict):
                        text = obj.get("fullText") or obj.get("fulltext") or text
                except Exception:
                    pass
            return {"status": 200, "ok": True, "text": text,
                    "length": len(text), "content_type": ctype, "error": ""}
        if status == 0:
            return {"status": 0, "ok": False, "text": "", "length": 0,
                    "content_type": ctype, "error": text[:300]}
        return {"status": status, "ok": False, "text": "", "length": 0,
                "content_type": ctype, "error": text[:300]}


def to_internal_paper(work: dict) -> dict:
    """把 CORE 结果并入内部 Paper 结构（主要贡献是 full_text 兜底）。

    ⚠️ 两个易错点：
      1. CORE 的摘要字段叫 `abstract`，和 arXiv 的 `summary`、OpenAlex 的
         `abstract_inverted_index` 都不同 —— 统一 schema 里必须落到同一处
         （本模块统一放 metadata.abstract）。
      2. `magId` 是 **Microsoft Academic Graph** 的 ID，不是 arXiv ID，
         早期版本把它命名成 `arxiv_id_alt` 是错的，会误导实体对齐。
    """
    return {
        "identity": {"doi": work.get("doi"), "arxiv_id": work.get("arxivId"),
                     "mag_id": work.get("magId"),      # MAG ID，别当 arXiv ID
                     "core_id": work.get("id"),
                     "openalex_id": None, "s2_id": None, "openreview_id": None},
        "metadata": {
            "title": work.get("title"),
            "year": work.get("yearPublished"),
            "publisher": work.get("publisher"),
            "language": (work.get("language") or {}).get("code"),
        },
        "abstract": work.get("abstract"),
        "full_text": {
            "core_id": work.get("id"),
            "download_url": work.get("downloadUrl"),
            "has_inline_fulltext": bool(work.get("fullText")),
            # ★ 有全文就存下来：这是 CORE 相对其它源最大的价值 ——
            #   拿到的是纯文本，可以跳过 PDF 下载 + GROBID 解析整条链路
            "inline_text": work.get("fullText") or None,
        },
        "search_meta": {"found_by": [{"source": "core", "kind": "fulltext_fallback"}]},
    }


# =============================================================================
# 自检与命令行入口
# =============================================================================

def check(core: CORE) -> int:
    if not core.available():
        section("CORE —— BLOCKED：缺少 API key（可选，V2 阶段）")
        kv("状态", "未配置 CORE_API_KEY")
        kv("影响", "search_works / get_work / get_fulltext 全部无法验证")
        kv("申请", "https://core.ac.uk/services/api")
        kv("注意", "免费许可有具体条款，个人研究/公共研究机构用途需自行确认")
        kv("定位", "V1 不接也能跑通；它是 arXiv/Unpaywall 之后的 OA 全文兜底")
        return 1

    failed = 0
    section("1) 全文检索 /v3/search/works")
    r = core.search_works("counterfactual regret minimization", limit=3)
    kv("状态", "HTTP {}".format(r["status"]))
    kv("命中总数", r["total"])
    if r["results"]:
        show_records(r["results"], ["id", "yearPublished", "doi", "downloadUrl"])
        with_ft = [x for x in r["results"] if x.get("fullText")]
        kv("带 inline fullText 的", "{} / {}".format(len(with_ft), len(r["results"])))
    else:
        kv("错误", r["error"])
        failed += 1

    print("\n" + "-" * 78)
    print("CORE 自检：{}".format("全部通过" if failed == 0 else "{} 项失败".format(failed)))
    return failed


def main(argv) -> int:
    core = CORE()
    print("凭证: CORE_API_KEY={}".format(mask(core.api_key)))
    if not argv or argv[0] == "check":
        return check(core)

    cmd = argv[0]

    if cmd == "search":
        q = argv[1] if len(argv) > 1 else "counterfactual regret minimization"
        r = core.search_works(q, limit=5)
        section("CORE 检索: {}".format(q))
        kv("状态", "HTTP {}".format(r["status"]))
        kv("命中总数", r["total"])
        if r["status"] == 200:
            for w in r["results"]:
                print("\n  ▶ {}".format(w.get("title")))
                print("    CORE ID : {}   年份: {}   类型: {}".format(
                    w.get("id"), w.get("yearPublished"), w.get("documentType")))
                print("    DOI     : {}".format(w.get("doi") or "（无）"))
                print("    下载    : {}".format(w.get("downloadUrl") or "（无）"))
                print("    有全文  : {}".format(bool(w.get("fullText"))))
        else:
            kv("错误", r.get("error"))
            return 1
        return 0

    if cmd == "fulltext":
        if len(argv) < 2:
            print("用法: python3 core.py fulltext {core_id}")
            return 2
        r = core.get_fulltext(argv[1])
        section("CORE 全文")
        kv("状态", "HTTP {}".format(r["status"]))
        kv("长度", r["length"])
        print("\n  预览: {}…".format(r["text"][:400]))
        return 0

    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
