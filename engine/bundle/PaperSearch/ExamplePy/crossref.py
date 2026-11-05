#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""crossref.py —— Crossref 数据源采集方式

定位：**DOI 权威解析 + 标准出版信息校正 + 去重匹配**。
Crossref 不是用来"发现论文"的，而是用来回答"这篇论文的官方身份是什么"。

官方文档：https://api.crossref.org/swagger-ui/index.html
**完全公开，不需要任何 key**，带上 email 进 polite pool 即可。

─────────────────────────────────────────────────────────────────────────────
为什么需要它：同一个东西有多个 ID
─────────────────────────────────────────────────────────────────────────────
    arXiv:            2310.12345
    OpenAlex:         W123456
    Semantic Scholar: abcde123
    Crossref(DOI):    10.xxxx/xxxxx     ← **最权威、最稳定、跨库通用**
它们可能全指向同一篇论文。Crossref 能给出：
    - 正式标题（arXiv 预印本标题可能和正式版不同）
    - 正式出版年份、期刊/会议名、出版社、卷期页
    - license、funding、reference 列表
优先匹配顺序：DOI → arXiv ID → title + authors

─────────────────────────────────────────────────────────────────────────────
polite pool：带 mailto 的礼貌池
─────────────────────────────────────────────────────────────────────────────
    https://api.crossref.org/works/{doi}?mailto=you@example.com
不带也能用，但会被归入"普通池"，官方可能降低优先级。
**email 不是 key，不涉及计费。**

命令行用法：
    python3 crossref.py doi 10.1038/nature12373
    python3 crossref.py match "Deep Counterfactual Regret Minimization"
    python3 crossref.py query "counterfactual regret minimization"
    python3 crossref.py check

─────────────────────────────────────────────────────────────────────────────
函数索引（快速定位）
  [DOI]   Crossref.get_work_by_doi(doi)
          DOI → 权威元数据；★ 该路由**不支持 select**，传了会 400
  [匹配]  Crossref.match_title(title, author, rows, select)
          标题(+作者) → DOI 候选；★★ **只返回候选，不做合并判定**
  [检索]  Crossref.query_works(query, rows, filter_expr, select)
          通用检索（不推荐做论文发现，仅用于按已知信息反查）
  [工具]  to_internal_paper(work)              统一 Paper schema（规范化出版信息）
  [自检]  check(cr)  [CLI] main(argv)          doi / match / query
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import os
import sys
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import http_request, url_with, section, kv, show_records, get_key

BASE = "https://api.crossref.org"

# ---- 可调参数 ---------------------------------------------------------------
HTTP_TIMEOUT = 30.0
HTTP_RETRIES = 2
# select 参数：只取需要的字段。Crossref 单条 work 有 100+ 字段，
# 用 select 能把响应从 50KB 压到 3KB —— 批量处理时差别巨大。
#
# ⚠️ 两个坑（实测踩过）：
#   1. select 的字段名必须是官方白名单里的，写错一个就整个请求 400
#      select-not-available："Select 'xxx' specified but there is no such select"
#   2. **/works/{doi} 单篇路由完全不支持 select**，传了会 400
#      所以下面的 SELECT 只用于 match_title / query_works 这类列表路由
#
# 合法字段名以官方报错信息为准（故意写错一个字段就能拿到完整白名单）：
#   abstract URL resource member posted score created degree update-policy
#   short-title license ISSN container-title issued update-to issue prefix
#   approved indexed article-number clinical-trial-number accepted author
#   group-title DOI is-referenced-by-count updated-by event chair
#   standards-body original-title funder translator published archive
#   published-print alternative-id subject subtitle published-online
#   publisher-location content-domain reference title link type publisher
#   volume references-count ISBN issn-type assertion deposited page
#   contributor content-created short-container-title relation editor
# 注意是 references-count（复数）而不是 reference-count；language 不在白名单里。
DEFAULT_SELECT = ("DOI,title,author,issued,published,container-title,"
                  "short-container-title,publisher,type,volume,issue,page,"
                  "ISSN,ISBN,license,URL,abstract,is-referenced-by-count,"
                  "references-count,reference,subject,link,funder,score")


class Crossref:
    """Crossref 客户端。无需 key，只需 mailto 进 polite pool。

    用法：
        cr = Crossref()
        r = cr.get_work_by_doi("10.1038/nature12373")
        m = cr.match_title("Deep Counterfactual Regret Minimization")
    """

    def __init__(self, email: str = "", timeout: float = HTTP_TIMEOUT,
                 retries: int = HTTP_RETRIES):
        self.email = email or get_key("PAPERSEARCH_EMAIL")
        self.timeout = timeout
        self.retries = retries

    def _get(self, path: str, params: dict = None):
        p = dict(params or {})
        if self.email:
            p["mailto"] = self.email
        url = url_with("{}/{}".format(BASE, path.lstrip("/")), p)
        status, body, _ = http_request(url, timeout=self.timeout,
                                       retries=self.retries, email=self.email)
        text = body.decode("utf-8", "replace")
        try:
            import json
            return status, json.loads(text), text, url
        except Exception:
            return status, None, text[:300], url

    # ---- 方式 1：DOI → 完整元数据 -------------------------------------------
    def get_work_by_doi(self, doi: str):
        """按 DOI 取权威元数据 /works/{doi}

        参数
        ----
        doi : '10.1038/nature12373'、完整 URL 'https://doi.org/10.1038/...'
              都会被自动清洗

        ⚠️ 注意：**/works/{doi} 这条路由不支持 select 参数**
           实测传 select 会返回 400 parameter-not-allowed：
           {"status":"failed","message":"This route does not support select"}
           所以本方法返回**全部字段**。要精简字段请用 match_title / query_works。

        返回 {"status", "work": {...}, "error"}

        work 关键字段及用途：
          DOI                  权威唯一标识 → 作为内部 paper 的主键之一
          title                正式标题（列表形式）→ 与 arXiv 标题比对做去重
          author               [{given, family, ORCID, affiliation}] → 作者消歧
          issued / published   正式出版日期 → 趋势分析的时间基准（≠ arXiv 提交时间）
          container-title      期刊/会议全名 → venue 分析
          short-container-title 缩写（如 'NeurIPS'）
          publisher            出版社
          type                 journal-article / proceedings-article / posted-content
                               ⚠️ 'posted-content' 通常表示"预印本"（含 arXiv）
          volume/issue/page    卷期页 → 精确引用格式
          license              许可协议 → **判断能否合法下载全文**
          is-referenced-by-count  被引数（Crossref 口径，通常低于 Google Scholar）
          reference            参考文献列表（含 DOI/unstructured）→ 引用图补充
          abstract             部分出版商会提供（多数为 None）
          link                 出版商全文链接（多为付费墙）
          subject              学科分类

        用途：实体统一时补全 DOI/年份/venue；判断 license；
              为"论文排序"提供正式出版信息。
        """
        clean = doi.strip()
        if clean.lower().startswith("http"):
            clean = clean.split("doi.org/")[-1]
        clean = clean.replace("doi:", "").strip()
        # 不传 select —— 该路由不支持
        status, data, text, url = self._get(
            "works/{}".format(urllib.parse.quote(clean)), None)
        if status != 200 or not data:
            return {"status": status, "work": None, "error": text[:200], "url": url}
        return {"status": 200, "work": data.get("message", data), "error": "", "url": url}

    # ---- 方式 2：标题+作者 → DOI（去重匹配） --------------------------------
    def match_title(self, title: str, author: str = "", rows: int = 5,
                    select: str = DEFAULT_SELECT):
        """用标题（可加作者）反查 DOI /works?query.bibliographic=

        参数
        ----
        title  : 论文标题。**注意**：纯 arXiv 预印本可能查不到（未被出版商注册），
                 这时 Crossref 返回 0 条属于正常现象，不要当成 bug
        author : 第一作者姓，可显著提高准确率
        rows   : 返回几条候选，默认 5。返回多条时按 score 从高到低

        返回 {"status", "items": [{...,'score': 匹配分}], "error"}
          score 是 Crossref 的相关性打分，**不是相似度百分比**，
          也不是跨查询可比的值 —— 只能用于**同一次返回内部的相对比较**。

        用途：实体统一的第一步 —— 把 arXiv/OpenAlex 的标题对齐到正式 DOI。
        ⚠️ 本方法**只返回候选，不做判定**。合并决策必须另写逻辑，且不能只靠 score：
           score 是检索相关性，无法区分这些情况：
             - 同一篇的预印本 vs 正式版   （应当合并，但标题可能不同）
             - 同名的不同论文 / 综述 vs 原始工作（不应合并）
             - 标题极其相似的系列工作     （不应合并）
           可靠做法（按优先级）：
             1. DOI 完全一致            → 直接合并，无需 score
             2. arXiv ID / S2 externalIds 交叉命中 → 合并
             3. 标题归一化（小写、去标点、去空格）+ 首作者姓 + 年份 三者全同 → 合并
             4. 只有 score 高 → **人工/模型复核**，不要自动合并
           任何"score > 某个阈值就认为是同一篇"的规则都过于粗糙，不要写进生产线。
           去重准确率需要用带标注的样本单独评估，本模块只提供原料。
        """
        query = title if not author else "{} {}".format(title, author)
        status, data, text, url = self._get("works", {
            "query.bibliographic": query, "rows": rows, "select": select})
        if status != 200 or not data:
            return {"status": status, "items": [], "error": text[:200], "url": url}
        return {"status": 200, "items": data.get("message", {}).get("items", []),
                "error": "", "url": url}

    # ---- 方式 3：通用检索（**不推荐作为主要发现手段**） ---------------------
    def query_works(self, query: str, rows: int = 20, filter_expr: str = "",
                    select: str = "DOI,title,author,issued,container-title,type,"
                                  "is-referenced-by-count"):
        """通用检索 /works?query=

        参数
        ----
        query       : 任意关键词
        rows        : 返回条数
        filter_expr : 过滤条件，逗号分隔。常用：
                        from-pub-date:2020-01-01
                        until-pub-date:2024-12-31
                        type:journal-article
                        has-full-text:true
                        license.url:https://creativecommons.org/licenses/by/4.0/
                        ⚠️ 注意 Crossref 的 filter 用**中划线**（from-pub-date），
                           和 OpenAlex 的下划线风格不同

        返回 {"status", "items", "total", "error"}

        ⚠️ 为什么不推荐用它做论文发现：
          - Crossref 全文检索质量远不如 OpenAlex/S2/arXiv
          - 大量论文没有注册 abstract，相关性排序不可靠
          - 预印本覆盖不全
        它的定位是"按已知信息反查 DOI"，不是"按主题找论文"。
        """
        params = {"query": query, "rows": rows, "select": select}
        if filter_expr:
            params["filter"] = filter_expr
        status, data, text, url = self._get("works", params)
        if status != 200 or not data:
            return {"status": status, "items": [], "total": 0,
                    "error": text[:200], "url": url}
        msg = data.get("message", {})
        return {"status": 200, "items": msg.get("items", []),
                "total": msg.get("total-results", 0), "error": "", "url": url}


def to_internal_paper(work: dict) -> dict:
    """把 Crossref work 转成内部统一 Paper 结构（主要贡献是"规范化出版信息"）。"""
    authors = []
    for a in (work.get("author") or []):
        name = " ".join(filter(None, [a.get("given"), a.get("family")])) or a.get("name")
        if name:
            authors.append(name)
    issued = (work.get("issued") or {}).get("date-parts") or [[None]]
    licenses = [l.get("URL") for l in (work.get("license") or []) if l.get("URL")]
    return {
        "identity": {"doi": work.get("DOI"), "arxiv_id": None,
                     "openalex_id": None, "s2_id": None, "openreview_id": None},
        "metadata": {
            "title": (work.get("title") or [None])[0],
            "authors": authors,
            "year": issued[0][0],
            "venue": (work.get("container-title") or [None])[0],
            "venue_short": (work.get("short-container-title") or [None])[0],
            "publisher": work.get("publisher"),
            "type": work.get("type"),
            "volume": work.get("volume"), "issue": work.get("issue"),
            "page": work.get("page"),
        },
        "license": {"urls": licenses, "is_preprint": work.get("type") == "posted-content"},
        "search_meta": {"found_by": [{"source": "crossref", "kind": "entity_resolution"}]},
    }


# =============================================================================
# 自检与命令行入口
# =============================================================================

def check(cr: Crossref) -> int:
    failed = 0

    section("1) DOI → 元数据")
    r = cr.get_work_by_doi("10.1038/nature12373")
    if r["status"] == 200 and r["work"]:
        w = r["work"]
        kv("标题", (w.get("title") or [""])[0])
        kv("类型", w.get("type"))
        kv("出版年份", ((w.get("issued") or {}).get("date-parts") or [[None]])[0][0])
        kv("期刊", (w.get("container-title") or [""])[0])
        kv("出版社", w.get("publisher"))
        kv("被引", w.get("is-referenced-by-count"))
        kv("license", [l.get("URL") for l in (w.get("license") or [])][:1] or "（无）")
        kv("作者数", len(w.get("author") or []))
    else:
        kv("错误", r.get("error"))
        failed += 1

    section("2) 标题匹配（实体统一用）")
    m = cr.match_title("Deep Counterfactual Regret Minimization")
    kv("状态", "HTTP {}".format(m["status"]))
    kv("候选数", len(m["items"]))
    if m["items"]:
        show_records(m["items"], ["DOI", "score", "title"])
        kv("说明", "score 是相关性打分，不是相似度；只用于候选间相对比较")
    else:
        kv("说明", "0 条属正常 —— 预印本可能未在 Crossref 注册")

    section("3) 过滤检索（不推荐做发现，仅演示）")
    q = cr.query_works("counterfactual regret minimization", rows=3,
                       filter_expr="from-pub-date:2015-01-01")
    kv("状态", "HTTP {}".format(q["status"]))
    kv("总命中", q["total"])
    show_records(q["items"], ["DOI", "issued", "container-title"])

    print("\n" + "-" * 78)
    print("Crossref 自检：{}".format("全部通过" if failed == 0 else "{} 项失败".format(failed)))
    kv("提示", "无需 key，带 mailto={} 进 polite pool".format(cr.email or "（未设置）"))
    return failed


def main(argv) -> int:
    cr = Crossref()
    if not argv or argv[0] == "check":
        return check(cr)

    cmd = argv[0]

    if cmd == "doi":
        if len(argv) < 2:
            print("用法: python3 crossref.py doi 10.1038/nature12373")
            return 2
        r = cr.get_work_by_doi(argv[1])
        section("Crossref DOI: {}".format(argv[1]))
        if r["status"] == 200:
            import json
            w = r["work"]
            kv("标题", (w.get("title") or [""])[0])
            kv("作者", ", ".join(
                "{} {}".format(a.get("given", ""), a.get("family", ""))
                for a in (w.get("author") or [])[:5]))
            kv("类型", w.get("type"))
            kv("期刊", (w.get("container-title") or [""])[0])
            kv("年份", ((w.get("issued") or {}).get("date-parts") or [[None]])[0][0])
            kv("卷/期/页", "{}/{}/{}".format(w.get("volume"), w.get("issue"), w.get("page")))
            kv("被引", w.get("is-referenced-by-count"))
            print("\n  --- 原始 JSON（截断）---")
            print(json.dumps(w, ensure_ascii=False, indent=2)[:1800])
        else:
            kv("错误", r.get("error"))
            return 1
        return 0

    if cmd == "match":
        if len(argv) < 2:
            print("用法: python3 crossref.py match \"论文标题\" [作者姓]")
            return 2
        m = cr.match_title(argv[1], author=argv[2] if len(argv) > 2 else "")
        section("Crossref 标题匹配")
        kv("候选数", len(m["items"]))
        for it in m["items"]:
            print("\n  ▶ score={}  {}".format(it.get("score"), (it.get("title") or [""])[0]))
            print("    DOI   : {}".format(it.get("DOI")))
            print("    年份  : {}".format(
                ((it.get("issued") or {}).get("date-parts") or [[None]])[0][0]))
            print("    venue : {}".format((it.get("container-title") or [""])[0]))
        return 0

    if cmd == "query":
        q = argv[1] if len(argv) > 1 else "counterfactual regret minimization"
        r = cr.query_works(q, rows=5)
        section("Crossref 检索: {}".format(q))
        kv("总命中", r["total"])
        show_records(r["items"], ["DOI", "title", "issued"])
        return 0

    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
