#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""openalex.py —— OpenAlex 数据源采集方式

定位：整个检索系统的 **主学术索引 + citation graph**（对应）。
它的数据模型是 Works / Authors / Institutions / Sources / Topics 五大实体。

官方文档：https://docs.openalex.org/    交互式调试：https://api.openalex.org/

─────────────────────────────────────────────────────────────────────────────
关键机制：它是**额度计费**的，不是"无限免费"
─────────────────────────────────────────────────────────────────────────────
带 key 的响应头会直接告诉你用了多少：
    x-ratelimit-limit-usd: 1          免费额度 $1/天
    x-ratelimit-remaining-usd: 0.98   还剩多少
    x-ratelimit-cost-usd: 0.001       本次 search 花了 $0.001
⇒ 约 1000 次检索/天，个人科研够用。
详细调用（filter / 按 ID 取）比 search 便宜得多。

**匿名调用会随机 429**：
    "Anonymous search is temporarily rate-limited while the search cluster is
     under elevated load. Please retry in 30s, or use a free API key..."
规律（实测）：
    检索类端点（search= / title.search / fulltext.search）→ 匿名会被限流
    过滤类端点（cites: / openalex_id: 批量 / works/{id}）→ 匿名完全正常
所以：**带上 OPENALEX_API_KEY，并保留 filter 降级路径。**

两种检索写法，行为和价格不同：
    A) GET /works?search=xxx                    全文模糊检索，最贵，匿名会被限流
    B) GET /works?filter=title_and_abstract.search:xxx   限定字段检索，更精准便宜
    C) GET /works?filter=openalex_id:W1|W2|W3   批量按 ID 取，最便宜，无 429 风险

不带 key 也建议带 mailto（polite pool）：
    加 &mailto=you@example.com 会让请求进入"礼貌池"，
    官方说这样在出问题时能联系到你，不会自动提速但更"守规矩"。

命令行用法：
    python3 openalex.py search "counterfactual regret minimization"
    python3 openalex.py work W2741809807
    python3 openalex.py refs W2741809807         # 参考文献（cite graph 向后）
    python3 openalex.py cites W2741809807        # 被引（cite graph 向前）
    python3 openalex.py entities                 # authors/sources/topics 探测
    python3 openalex.py quota                    # 看今日额度
    python3 openalex.py check                    # 快速自检

─────────────────────────────────────────────────────────────────────────────
函数索引（快速定位）
  [检索]  OpenAlex.search(query, per_page, page, select, extra_filter, sort)
          全文检索（按额度计费 $0.001/次）→ {"status","count","results","quota"}
  [检索]  OpenAlex.search_all(query, limit, per_page, select, extra_filter)
          cursor 翻页批量采集；collected 与实际返回条数一致
  [检索]  OpenAlex.search_by_filter(filter_expr, ...)
          filter 检索（更便宜、匿名也稳）；**cites: / cited_by: 方向见注释**
  [作品]  OpenAlex.get_work(work_id, select)
          单篇详情；normalize_work_id 兼容 ID/URL/DOI 四种写法
  [作品]  OpenAlex.get_works_by_ids(work_ids, select, batch_size)
          批量取（自动分批，返回 missing_ids 与 complete，不静默丢数据）
  [图谱]  OpenAlex.get_citations(work_id, limit)      被引：谁引用了它
  [图谱]  OpenAlex.get_references(work_id, ...)       参考文献：它引用了谁
  [实体]  OpenAlex.search_entity(entity, query)       authors/sources/topics
  [额度]  OpenAlex.quota(hdrs) / quota_now()
          从响应头读 $ 额度与剩余可查次数
  [工具]  normalize_work_id(raw) / reconstruct_abstract(inverted_index)
          ID 规范化 / 倒排索引还原摘要
  [工具]  write_paper_record(work, provenance)
          统一 Paper schema（含摘要还原与 found_by 溯源）
  [自检]  check(oa)  [CLI] main(argv)
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import (http_request, get_json, url_with, section, kv,
                     show_records, get_key, mask)

BASE = "https://api.openalex.org"

# ---- 可调参数 ---------------------------------------------------------------
# per_page 上限 200；批量取 ID 时建议顶到 200，能把请求数压到 1/10
MAX_PER_PAGE = 200
# cursor 分页比 page 分页更稳（page 分页深度有限且慢）。
# 需要翻超过 1 万条时用 cursor，见 search_all()。
DEFAULT_TIMEOUT = 30.0
HTTP_RETRIES = 2

# select 参数：只取需要的字段，响应体积能小一个数量级。
# 这是**省额度、省带宽**的关键技巧：不写 select 会返回几百个字段。
# ★ 必须包含 abstract_inverted_index：
#   OpenAlex **不直接返回摘要字符串**，只给倒排索引；不加这个字段，
#   默认 search/get_work 的结果里就没有摘要，「④ 初步相关性筛选」
#   会因为拿不到文本而完全失效。
#   代价是响应体积变大 —— 只想批量拉索引信息时请显式用 WORK_SELECT_LITE。
WORK_SELECT = ("id,doi,title,display_name,publication_year,publication_date,type,"
               "cited_by_count,referenced_works_count,referenced_works,"
               "authorships,primary_location,open_access,best_oa_location,"
               "topics,keywords,language,is_retracted,abstract_inverted_index")
# 精简版：只要索引信息，用于大批量拉取
WORK_SELECT_LITE = "id,doi,title,publication_year,cited_by_count,referenced_works"

# 固定锚点作品：自检时用它，避免依赖"检索是否成功"
OPENALEX_FALLBACK_WORK = "W2741809807"


def normalize_work_id(raw: str) -> str:
    """把各种写法统一成 OpenAlex 能接受的 ID 形式。

    输入 → 输出：
        'W2741809807'                          → 'W2741809807'
        'https://openalex.org/W2741809807'     → 'W2741809807'
        'https://api.openalex.org/works/W123'  → 'W123'
        'doi:10.1038/nature12373'              → 'doi:10.1038/nature12373'
        'https://doi.org/10.1038/nature12373'  → 'doi:10.1038/nature12373'
        '10.1038/nature12373'（裸 DOI）         → 'doi:10.1038/nature12373'

    ⚠️ 关键点：**DOI 里本身含斜杠**，所以绝对不能简单 `rsplit('/', 1)[-1]` ——
       那会把 10.1038/nature12373 截成 nature12373，请求变成 works/nature12373。
       必须先判断是不是 DOI，再决定怎么规范化。
    """
    s = (raw or "").strip()
    if not s:
        return ""
    if s.startswith("doi:"):
        return s
    if "doi.org/" in s:
        return "doi:" + s.split("doi.org/", 1)[1].strip()
    if s.startswith("10."):
        return "doi:" + s                      # 裸 DOI
    if s.startswith("http"):
        return s.rstrip("/").rsplit("/", 1)[-1]
    return s


class OpenAlex:
    """OpenAlex 客户端。用类封装是因为 key / mailto / 超时这些配置要贯穿所有方法。

    用法：
        oa = OpenAlex()                       # 自动从 Keys.txt 读 OPENALEX_API_KEY
        r = oa.search("deep cfr", per_page=10)
        w = oa.get_work("W2741809807")
    """

    def __init__(self, api_key: str = "", email: str = "",
                 timeout: float = DEFAULT_TIMEOUT, retries: int = HTTP_RETRIES):
        self.api_key = api_key or get_key("OPENALEX_API_KEY")
        self.email = email or get_key("PAPERSEARCH_EMAIL")
        self.timeout = timeout
        self.retries = retries

    # ---- 底层 ---------------------------------------------------------------
    def _get(self, path: str, params: dict = None):
        """发一个 GET，自动附加 api_key 与 mailto。

        返回 (status, data, raw_text, headers)
        headers 里含额度信息，不要丢掉 —— 它是判断"是否快用完"的唯一来源。
        """
        p = dict(params or {})
        if self.email:
            p["mailto"] = self.email          # polite pool
        if self.api_key:
            p["api_key"] = self.api_key       # 解除匿名限流 + 显示额度
        url = url_with("{}/{}".format(BASE, path.lstrip("/")), p)
        status, body, hdrs = http_request(url, timeout=self.timeout,
                                          retries=self.retries, email=self.email)
        text = body.decode("utf-8", "replace")
        try:
            return status, __import__("json").loads(text), text, hdrs
        except Exception:
            return status, None, text[:300], hdrs

    # ---- 检索 ---------------------------------------------------------------
    def search(self, query: str, per_page: int = 25, page: int = 1,
               select: str = WORK_SELECT, extra_filter: str = "",
               sort: str = ""):
        """方式 A：全文模糊检索 /works?search=xxx

        参数
        ----
        query    : 关键词。**支持 OpenAlex 检索语法**：
                     "deep cfr"                短语（引号）
                     deep AND cfr              布尔
                     deep OR cfr
                     deep NOT cfr
        per_page : 每页多少条，1~200，默认 25
        page     : 第几页（从 1 开始）。**page 分页深度有限**，
                   要拉上万条请改用 cursor 分页（见 search_all）
        select   : 返回哪些字段，逗号分隔。不传会返回全部字段（体积巨大）
        extra_filter : 额外过滤条件，多个用逗号分隔（AND 关系）。常用：
                     is_oa:true                        只看开放获取
                     from_publication_date:2020-01-01  起始年份
                     to_publication_date:2024-12-31
                     type:article                      只要期刊论文
                     language:en                       只要英文
                     可组合："is_oa:true,from_publication_date:2020-01-01"
        sort     : 排序，如 "cited_by_count:desc"、"publication_date:desc"

        返回 {"status", "count"(总命中), "results": [work...], "error", "quota"}
        work 字段含义（选出来的部分）：
          id                    https://openalex.org/W...  ← 内部统一 ID
          doi                   10.xxxx/xxxx（可能为 None）
          title / display_name  标题
          publication_year      年份
          type                  article / preprint / book-chapter ...
          cited_by_count        被引数（可用于"重要性"排序）
          referenced_works      它引用的论文 ID 列表  ← citation graph 向后边
          authorships           作者+机构+是否通讯作者
          primary_location      发表处（source 里有期刊/会议名）
          open_access           {is_oa, oa_status, oa_url}  ← 全文可用性判断
          best_oa_location      最佳免费全文位置（含 pdf_url）← 直接决定能否下载
          topics / keywords     主题标签，可用于领域分类和趋势分析
          is_retracted          是否被撤稿（质检用）

        用途：初始候选池。先用它把候选从几千压到几百，再往下走。
        """
        params = {"search": query, "per-page": min(per_page, MAX_PER_PAGE),
                  "page": page}
        if select:
            params["select"] = select
        if extra_filter:
            params["filter"] = extra_filter
        if sort:
            params["sort"] = sort
        status, data, text, hdrs = self._get("works", params)
        if status != 200 or not data:
            hint = ""
            if status == 429:
                hint = ("（匿名检索被限流，请配置 OPENALEX_API_KEY；"
                        "或改用 filter 类端点）")
            elif status == 401:
                hint = "（API key 无效，检查 Keys.txt）"
            return {"status": status, "count": 0, "results": [],
                    "error": "{}{}".format(text[:200], hint),
                    "quota": self.quota(hdrs)}
        return {"status": 200, "count": data.get("meta", {}).get("count", 0),
                "results": data.get("results", []), "error": "",
                "quota": self.quota(hdrs)}

    def search_all(self, query: str, limit: int = 1000, per_page: int = 200,
                   select: str = WORK_SELECT_LITE, extra_filter: str = ""):
        """方式 A 的翻页封装：用 **cursor 分页** 拉大批量结果。

        为什么不用 page：OpenAlex 的 page 分页在大深度时性能差甚至报错，
        cursor 是官方推荐的大批量方式。响应里 meta.next_cursor 是游标。

        参数
        ----
        limit    : 最多要多少条
        per_page : 每页条数，建议 200（上限），能省额度
        select   : 建议用 LITE 版本，否则 1 万条会很慢

        返回 {"collected", "results", "pages"}；cursor 耗尽会自动停。
        """
        results, cursor, pages, errors = [], "*", 0, []
        while len(results) < limit:
            params = {"per-page": min(per_page, MAX_PER_PAGE), "cursor": cursor}
            if select:
                params["select"] = select
            if extra_filter:
                params["filter"] = extra_filter
            if query:
                params["search"] = query
            status, data, text, _ = self._get("works", params)
            if status != 200 or not data:
                errors.append("page {}: HTTP {} {}".format(pages + 1, status, text[:120]))
                break
            batch = data.get("results", [])
            if not batch:
                break
            results.extend(batch)
            pages += 1
            cursor = (data.get("meta") or {}).get("next_cursor")
            if not cursor:
                break
        # ★ collected 必须等于**实际返回的条数**。
        #   早期实现在裁剪前计数，导致 collected=1000 而 results 只有 limit 条，
        #   调用方按 collected 估算覆盖率会得到错误结论。
        trimmed = results[:limit]
        # ★ complete 的语义在不同方法里天然不同，容易误读，所以这里把
        #   两个"为什么结束"的原因**分开命名**，由调用方按需判断：
        #     requested_limit_reached : 拿到了要的条数（不是你想要的"全采完"）
        #     source_exhausted        : cursor 用尽，服务端确实没有更多了
        #   只有 source_exhausted 才等价于"这个查询采完了"。
        limit_reached = len(results) >= limit
        exhausted = not cursor
        return {"status": 200 if not errors else 207, "ok": not errors,
                "collected": len(trimmed), "results": trimmed, "pages": pages,
                "requested_limit_reached": limit_reached,
                "source_exhausted": exhausted,
                # complete 保留给旧调用方：表示"正常结束且没有报错"
                "complete": not errors and (limit_reached or exhausted),
                "error": "; ".join(errors[:2])}

    def search_by_filter(self, filter_expr: str, per_page: int = 25,
                         select: str = WORK_SELECT_LITE, page: int = 1):
        """方式 B：filter 检索（**比 search 更便宜，且匿名也稳定**）。

        filter_expr 常用值（⚠️ 引用方向极易记反，这里按实测结论标注）：
            openalex_id:W1|W2|W3              批量按 ID 取（管道符是 OR）
            cites:W2741809807                 ★ 引用了 W 的所有论文（= W 的被引，向前扩展）
            cited_by:W2741809807              ★ W 引用了哪些论文（= W 的参考文献，向后追溯）
            referenced_works:W123             W 被哪些作品引用（等价于 cites:）
            title_and_abstract.search:xxx     标题+摘要检索
            title.search:deep cfr             标题检索
            fulltext.search:xxx               全库检索（慢）
            default.search:xxx                等价于 search 参数
            authorships.author.id:A123456     某作者
            primary_location.source.id:S1234  某期刊/会议
            publication_year:2024
            is_oa:true
            from_publication_date:2020-01-01
            多个条件用逗号连接 = AND；同一 key 用 | 连接 = OR

        实测校验（2026-09-28，W2741809807 该文被引 1262、参考文献 54）：
            filter=cites:W2741809807    → count = 1257   ← 引用了它
            filter=cited_by:W2741809807 → count = 44     ← 它引用的
        即 `cites:` 是"谁引用了我"，`cited_by:` 是"我引用了谁"。

        为什么推荐它：search= 参数在服务端走"全文检索集群"，贵且会被限流；
        filter 走索引，便宜、快、匿名也稳。**做 citation expansion 一律用 filter。**
        """
        return self._get("works", {"filter": filter_expr,
                                   "per-page": min(per_page, MAX_PER_PAGE),
                                   "page": page, "select": select})

    # ---- 单作品 -------------------------------------------------------------
    def get_work(self, work_id: str, select: str = WORK_SELECT):
        """方式 C：按 ID 拿单篇作品的完整元数据。

        参数
        ----
        work_id : 支持四种写法，内部用 normalize_work_id() 规范化
            'W2741809807'                         OpenAlex 原生 ID
            'https://openalex.org/W2741809807'    完整 URL
            'doi:10.1038/nature12373'             DOI 前缀形式
            'https://doi.org/10.1038/nature12373' DOI URL
        ⚠️ 踩过的坑：早期实现用 `work_id.rsplit('/', 1)[-1]` 取最后一段，
           DOI 本身含斜杠（10.1038/nature12373），会被截成 'nature12373'，
           请求变成 works/nature12373 → 404。所以必须按前缀分别规范化。

        返回 {"status", "work", "error", "quota"}
        其中 referenced_works 是**引用图的核心**：它给出这篇引用的所有论文 ID。

        用途：候选池里的论文做"详情补全"；拿 referenced_works 做向后扩展。
        """
        wid = normalize_work_id(work_id)
        params = {"select": select} if select else {}
        status, data, text, hdrs = self._get("works/{}".format(wid), params)
        if status != 200 or not data:
            return {"status": status, "work": None, "error": text[:200],
                    "quota": self.quota(hdrs)}
        return {"status": 200, "work": data, "error": "", "quota": self.quota(hdrs)}

    def get_works_by_ids(self, work_ids: list, select: str = WORK_SELECT_LITE,
                         batch_size: int = MAX_PER_PAGE):
        """方式 C 批量：一次取多篇（**自动分批，绝不静默丢数据**）。

        参数
        ----
        work_ids   : ['W123', 'W456', ...] 或完整 URL；数量不限，内部按批切分
        batch_size : 每批多少个 ID，默认 200（OpenAlex 单次 filter 的实际上限）

        返回 {"status", "ok", "works", "found", "requested",
              "missing_ids", "batches", "complete", "error"}
          found       : 实际换回元数据的数量
          missing_ids : 请求了但服务端没返回的 ID（**显式上报，不藏起来**）
          complete    : 是否每个请求的 ID 都有下落（found + missing == requested）
                       且没有批次失败 —— 判断"引用图是否完整"要看它

        ⚠️ 踩过的坑：早期实现直接 `ids[:200]` 截断，超过 200 篇的参考文献会**静默丢失**，
           调用方看到 count=500 却只拿到 200 篇还以为引用图是完整的。
           现在改为分批请求 + 显式 missing_ids，让"不完整"这件事可见。

        用途：citation expansion 拿到一堆 ID 后批量换取标题/年份/被引数。
        """
        ids = [normalize_work_id(w) for w in (work_ids or [])]
        requested = len(ids)
        if not requested:
            # 空输入是**正常情况**（例如论文没有参考文献），不是错误
            return {"status": 200, "ok": True, "works": [], "found": 0,
                    "requested": 0, "missing_ids": [], "batches": 0,
                    "complete": True, "error": ""}

        found, batches, errors = {}, 0, []
        for i in range(0, requested, max(1, batch_size)):
            chunk = ids[i:i + batch_size]
            batches += 1
            status, data, text, _ = self._get("works", {
                "filter": "openalex_id:" + "|".join(chunk),
                "per-page": len(chunk), "select": select})
            if status != 200 or not data:
                errors.append("batch {}: HTTP {} {}".format(batches, status, text[:120]))
                continue
            for w in (data.get("results") or []):
                key = normalize_work_id(w.get("id") or "")
                if key:
                    found[key] = w

        works = [found[k] for k in ids if k in found]
        missing = [k for k in ids if k not in found]
        # ★ all_ids_resolved 才是"每个请求的 ID 都有下落"。
        #   注意：即便 all_ids_resolved=True，也可能有 missing_ids ——
        #   那说明服务端确实没有这些 ID（已合并/删除），不是我们丢了数据。
        #   complete 因此**不能**用来表示"引用图完整"。
        all_resolved = not errors
        return {"status": 200 if not errors else 207, "ok": not errors,
                "works": works, "found": len(works), "requested": requested,
                "missing_ids": missing, "batches": batches,
                "all_ids_resolved": all_resolved,
                "complete": all_resolved,
                "error": "; ".join(errors[:3])}

    # ---- Citation graph -----------------------------------------------------
    def get_citations(self, work_id: str, limit: int = 200, select: str = WORK_SELECT_LITE):
        """被引扩展（向前）：谁引用了这篇论文。

        原理：filter=cites:W123
        返回 {"status", "count", "works"}
          count 是总被引数（不受 limit 限制），可用来判断这篇的影响力。

        用途：从种子论文出发，找到"经典工作之后的后续工作"，
              这是恢复研究方向发展脉络的关键一步。
        注意：被引可能上万篇，务必配合 sort=cited_by_count:desc 取头部。
        """
        wid = work_id.rsplit("/", 1)[-1]
        status, data, text, _ = self._get("works", {
            "filter": "cites:" + wid, "per-page": min(limit, MAX_PER_PAGE),
            "select": select, "sort": "cited_by_count:desc"})
        if status != 200 or not data:
            return {"status": status, "count": 0, "works": [], "error": text[:200]}
        return {"status": 200, "count": data.get("meta", {}).get("count", 0),
                "works": data.get("results", []), "error": ""}

    def get_references(self, work_id: str, select: str = WORK_SELECT_LITE,
                       batch_size: int = MAX_PER_PAGE):
        """参考文献扩展（向后）：这篇论文引用了谁。

        原理：先取 work.referenced_works（只是 ID 列表），
              再用 get_works_by_ids() 分批换成元数据。需要 2 步。

        返回 {"status", "ok", "count", "works", "found", "missing_ids",
              "complete", "error"}
          count     : referenced_works 里的 ID 总数（**注意：这是"声称"的数量**）
          found     : 实际换回元数据的数量
          complete  : find+missing == count 且无批次失败 → 引用图是否完整
        ⚠️ 不要用 count 当作"拿到了多少"！实测一篇 54 条参考文献的论文，
           只换回 44 篇（其余 10 个 ID 在 OpenAlex 里已合并/删除）。
           必须看 found 和 missing_ids。

        ★ 零参考文献是**成功**，不是错误：直接返回空列表，
          不要拿空 ID 列表去调批量接口（那会得到一个 400 的假失败）。

        用途：追溯方法渊源 —— 一篇新论文引用的往往是这个方向的经典工作。
        """
        r = self.get_work(work_id, select="id,referenced_works")
        if r["status"] != 200 or not r["work"]:
            return {"status": r["status"], "ok": False, "count": 0, "works": [],
                    "found": 0, "missing_ids": [], "complete": False,
                    "error": r.get("error", "")}
        ids = r["work"].get("referenced_works") or []
        if not ids:
            # 没有参考文献 = 正常的空结果
            return {"status": 200, "ok": True, "count": 0, "works": [],
                    "found": 0, "missing_ids": [], "complete": True, "error": ""}
        batch = self.get_works_by_ids(ids, select=select, batch_size=batch_size)
        return {"status": batch["status"], "ok": batch["ok"], "count": len(ids),
                "works": batch["works"], "found": batch["found"],
                "missing_ids": batch["missing_ids"], "complete": batch["complete"],
                "error": batch.get("error", "")}

    # ---- 其它实体 -----------------------------------------------------------
    def search_entity(self, entity: str, query: str, per_page: int = 10):
        """查 Authors / Sources / Topics / Institutions 实体。

        entity 取值：'authors' | 'sources' | 'topics' | 'institutions' | 'funders'
        query：关键词，如 'Yoshua Bengio'（authors）、'Nature'（sources）

        返回 (status, data, text)  —— 三元组，便于 `s, d, t = ...` 直接解包
        用途：
          - authors  → 作者分析、找某人的全部工作、识别核心研究者
          - sources  → 判断 venue 层级（期刊/会议）、做 venue 加权排序
          - topics   → 领域分类，比关键词更稳定，适合做趋势统计
        """
        status, data, text, _hdrs = self._get(entity, {
            "search": query, "per-page": min(per_page, MAX_PER_PAGE)})
        return status, data, text

    # ---- 额度 ---------------------------------------------------------------
    @staticmethod
    def quota(hdrs: dict) -> dict:
        """从响应头解析额度。每次请求都带，不需要额外调用。

        返回 {"limit_usd", "remaining_usd", "cost_usd", "limit", "remaining",
              "reset_seconds", "calls_left"}
          calls_left = remaining_usd / cost_usd ≈ 今天还能查多少次
        用途：在长任务里定期打印，避免跑到一半额度耗尽。
        """
        low = {k.lower(): v for k, v in (hdrs or {}).items()}
        out = {
            "limit_usd": low.get("x-ratelimit-limit-usd"),
            "remaining_usd": low.get("x-ratelimit-remaining-usd"),
            "cost_usd": low.get("x-ratelimit-cost-usd"),
            "limit": low.get("x-ratelimit-limit"),
            "remaining": low.get("x-ratelimit-remaining"),
            "reset_seconds": low.get("x-ratelimit-reset"),
            "calls_left": None,
        }
        try:
            cost = float(out["cost_usd"])
            out["calls_left"] = int(float(out["remaining_usd"]) / cost) if cost else None
        except Exception:
            pass
        return out

    def quota_now(self):
        """主动发一个最小请求，读回当前额度。"""
        status, data, text, hdrs = self._get("works", {"search": "test", "per-page": 1})
        return status, self.quota(hdrs)


def reconstruct_abstract(inverted_index) -> str:
    """把 OpenAlex 的 `abstract_inverted_index` 还原成正常摘要文本。

    为什么需要它：OpenAlex **不直接给摘要字符串**，而是给一个倒排索引：
        {"We": [0, 12], "show": [1], "that": [2, 7], ...}
    键是词，值是这个词在原文中的位置列表。

    还原算法：建一个 (位置 → 词) 的映射，按位置排序后拼起来。
    返回 "" 表示这篇没有摘要（OpenAlex 对约一半文献没有摘要，属正常）。

    ⚠️ 影响：不还原摘要的话，的「④ 初步相关性筛选」就完全没有输入 ——
       OpenAlex 默认 select 里也没有这个字段，必须显式加到 select 才能拿到。
    """
    if not inverted_index or not isinstance(inverted_index, dict):
        return ""
    positions = {}
    for word, idxs in inverted_index.items():
        for i in idxs:
            positions[i] = word
    if not positions:
        return ""
    return " ".join(positions[i] for i in sorted(positions))


# 兼容旧名：WORK_SELECT 现在默认就含摘要
WORK_SELECT_WITH_ABSTRACT = WORK_SELECT


def write_paper_record(work: dict, provenance: dict = None) -> dict:
    """把 OpenAlex work 转成内部 Paper 结构（统一 schema，对应）。

    这样 arXiv / OpenAlex / S2 的结果能直接合并去重。

    参数
    ----
    work       : OpenAlex 的 work 字典
    provenance : 溯源信息，如 {"query": "...", "page": 2, "cursor": "...",
                 "fetched_at": "2026-09-28T12:00:00"}
                 ★明确要求保留"论文是怎么找到的"。
                   固定写死 kind="search" 是不够的 —— 事后无法解释
                   "这条结果来自哪个查询、第几页"，也就无法复现或审计覆盖度。

    统一 schema 的字段落点（跨源保持一致，避免去重时对不上）：
        identity.arxiv_id   ← 从 ids 里取 PMID/DOI 之外的 arXiv 标识
        abstract            ← abstract_inverted_index 还原（**顶层，不是 metadata 里**）
        full_text.pdf_url   ← best_oa_location.pdf_url
    """
    best = work.get("best_oa_location") or {}
    src = (work.get("primary_location") or {}).get("source") or {}
    ids = work.get("ids") or {}
    # OpenAlex 的 ids 里可能直接带 arXiv 标识
    arxiv_id = None
    for key, val in ids.items():
        if "arxiv" in key.lower() and val:
            arxiv_id = str(val).rsplit("/", 1)[-1]
            break
    # DOI 统一：OpenAlex 返回的是 URL 形式，去掉前缀便于与 Crossref 对齐
    doi = work.get("doi") or ""
    if doi.startswith("http"):
        doi = doi.split("doi.org/", 1)[-1]

    found_by = [{"source": "openalex", "kind": "search"}]
    if provenance:
        found_by = [dict({"source": "openalex"}, **provenance)]

    return {
        "identity": {
            "openalex_id": work.get("id"),
            "doi": doi or None,
            "arxiv_id": arxiv_id,
            "s2_id": None, "openreview_id": None,
        },
        "metadata": {
            "title": work.get("title") or work.get("display_name"),
            "authors": [a.get("author", {}).get("display_name")
                        for a in (work.get("authorships") or [])],
            "institutions": [i.get("display_name")
                             for a in (work.get("authorships") or [])
                             for i in (a.get("institutions") or [])],
            "year": work.get("publication_year"),
            "venue": src.get("display_name"),
            "venue_type": src.get("type"),
            "topics": [t.get("display_name") for t in (work.get("topics") or [])],
            "keywords": [k.get("display_name") for k in (work.get("keywords") or [])],
            "citation_count": work.get("cited_by_count"),
            "type": work.get("type"),
            "is_retracted": work.get("is_retracted"),
        },
        "abstract": reconstruct_abstract(work.get("abstract_inverted_index")),
        "open_access": {
            "is_oa": (work.get("open_access") or {}).get("is_oa"),
            "status": (work.get("open_access") or {}).get("oa_status"),
            "pdf_url": best.get("pdf_url"),
            "landing_url": best.get("landing_page_url"),
        },
        "full_text": {
            "pdf_url": best.get("pdf_url"),
            "landing_url": best.get("landing_page_url"),
            "locations": [],
        },
        "citation_graph": {
            "references": work.get("referenced_works") or [],
            "reference_count": work.get("referenced_works_count"),
        },
        "search_meta": {"found_by": found_by},
    }


# =============================================================================
# 自检与命令行入口
# =============================================================================

def check(oa: OpenAlex) -> int:
    failed = 0

    section("1) 检索 /works?search=")
    r = oa.search("counterfactual regret minimization", per_page=3)
    kv("状态", "HTTP {}".format(r["status"]))
    kv("命中总数", r.get("count"))
    if r["status"] == 200:
        kv("额度", r["quota"])
        show_records(r["results"], ["id", "publication_year", "cited_by_count", "doi"])
    else:
        kv("错误", r.get("error"))
        failed += 1

    anchor = "W2741809807"   # 固定的锚点作品，保证不依赖检索结果
    section("2) 单作品 works/{}".format(anchor))
    w = oa.get_work(anchor)
    if w["status"] == 200 and w["work"]:
        work = w["work"]
        kv("标题", work.get("title"))
        kv("年份/被引", "{} / {}".format(work.get("publication_year"), work.get("cited_by_count")))
        kv("参考文献数", len(work.get("referenced_works") or []))
        kv("OA 状态", work.get("open_access", {}).get("oa_status"))
        kv("字段完整度", "{} 个顶层字段".format(len(work)))
    else:
        kv("错误", w.get("error"))
        failed += 1

    section("3) 批量按 ID 取（最省额度的方式）")
    refs = oa.get_references(anchor)
    kv("参考文献总数", refs["count"])
    kv("批量换回元数据", "{} 篇".format(len(refs["works"])))
    kv("原理", "referenced_works 只给 ID，再用 openalex_id:W1|W2 批量换元数据")
    if not refs["works"]:
        failed += 1

    section("4) 被引扩展 filter=cites:")
    c = oa.get_citations(anchor, limit=5)
    kv("状态", "HTTP {}".format(c["status"]))
    kv("总被引", c["count"])
    show_records(c["works"], ["id", "publication_year", "cited_by_count"])
    if c["status"] != 200:
        failed += 1

    section("5) 其它实体")
    for ent, q in (("authors", "Yoshua Bengio"), ("sources", "Nature"), ("topics", "game theory")):
        status, data, text = oa.search_entity(ent, q, per_page=1)
        ok = status == 200 and data and data.get("results")
        kv(ent, "HTTP {} | 命中 {}{}".format(
            status, (data or {}).get("meta", {}).get("count", "?") if ok else "-",
            "" if ok else " ← 失败"))
        if not ok:
            failed += 1

    section("6) 额度")
    status, quota = oa.quota_now()
    kv("状态", "HTTP {}".format(status))
    kv("额度", quota)
    kv("说明", "免费 $1/天，search 约 $0.001/次 ⇒ 约 1000 次/天")

    print("\n" + "-" * 78)
    print("OpenAlex 自检：{}".format("全部通过" if failed == 0 else "{} 项失败".format(failed)))
    return failed


def main(argv) -> int:
    oa = OpenAlex()
    if not argv or argv[0] == "check":
        print("凭证: OPENALEX_API_KEY={} | mailto={}".format(
            mask(oa.api_key), oa.email or "（未设置）"))
        return check(oa)

    cmd = argv[0]

    if cmd == "search":
        q = argv[1] if len(argv) > 1 else "counterfactual regret minimization"
        n = int(argv[argv.index("--max") + 1]) if "--max" in argv else 5
        r = oa.search(q, per_page=n)
        section("OpenAlex 检索: {}".format(q))
        kv("状态", "HTTP {}".format(r["status"]))
        kv("命中总数", r.get("count"))
        kv("额度", r.get("quota"))
        if r["status"] == 200:
            for w in r["results"]:
                src = (w.get("primary_location") or {}).get("source") or {}
                print("\n  ▶ {}".format(w.get("title")))
                print("    OpenAlex : {}   年份: {}   被引: {}".format(
                    w.get("id"), w.get("publication_year"), w.get("cited_by_count")))
                print("    DOI      : {}".format(w.get("doi") or "（无）"))
                print("    venue    : {}".format(src.get("display_name") or "（无）"))
                oa_info = w.get("open_access") or {}
                print("    OA       : {} ({})".format(
                    oa_info.get("is_oa"), oa_info.get("oa_status")))
                print("    参考文献 : {} 篇".format(len(w.get("referenced_works") or [])))
        else:
            kv("错误", r["error"])
            return 1
        return 0

    if cmd in ("work", "refs", "cites"):
        wid = argv[1] if len(argv) > 1 else "W2741809807"
        if cmd == "work":
            r = oa.get_work(wid)
            section("works/{}".format(wid))
            if r["status"] == 200:
                import json
                print(json.dumps(r["work"], ensure_ascii=False, indent=2)[:2500])
            else:
                kv("错误", r["error"])
                return 1
        elif cmd == "refs":
            r = oa.get_references(wid)
            section("references of {}".format(wid))
            kv("总数", r["count"])
            show_records(r["works"], ["id", "publication_year", "cited_by_count"])
        else:
            r = oa.get_citations(wid, limit=10)
            section("citations of {}".format(wid))
            kv("总被引", r["count"])
            show_records(r["works"], ["id", "publication_year", "cited_by_count"])
        return 0

    if cmd == "entities":
        section("OpenAlex 实体")
        for ent, q in (("authors", "Yoshua Bengio"), ("sources", "Nature"),
                       ("topics", "game theory"), ("institutions", "Tsinghua")):
            status, data, text = oa.search_entity(ent, q, per_page=2)
            print("\n  【{}】HTTP {}".format(ent, status))
            if status == 200 and data:
                for item in (data.get("results") or [])[:2]:
                    print("    - {} ({} 命中)".format(
                        item.get("display_name"),
                        data.get("meta", {}).get("count")))
            else:
                print("    失败: {}".format(text[:120]))
        return 0

    if cmd == "quota":
        section("OpenAlex 额度")
        status, q = oa.quota_now()
        kv("状态", "HTTP {}".format(status))
        for k, v in q.items():
            kv(k, v)
        return 0

    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
