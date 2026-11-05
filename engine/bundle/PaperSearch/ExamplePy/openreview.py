#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""openreview.py —— OpenReview 数据源采集方式

定位：**AI 会议论文增强源**：审稿意见、评分、rebuttal、decision。
这些是其它数据库完全没有的信息 —— 别人只看论文正文，你能看到"评审为什么接受/拒稿"。
对 ICLR、部分 NeurIPS workshop、部分 ML 会议特别有价值。

官方文档：https://docs.openreview.net/reference/api-v2

─────────────────────────────────────────────────────────────────────────────
当前状态：**PARTIAL —— 检索通过，但有两处被挡**
─────────────────────────────────────────────────────────────────────────────
实测（2026-09-28）：
  ✅ api2.openreview.net/notes/search?term=...        HTTP 200，可用，免 key
  ✅ api.openreview.net/notes/search?query=...        HTTP 200（老会议仍用 v1）
  ❌ api2.openreview.net/notes?content.venueid=...    HTTP 403 ChallengeRequiredError
        "Challenge verification required" → 需要登录 token 才能按 venue 批量枚举
  ❌ api2.openreview.net/notes?forum=...              HTTP 403 同上
        ⇒ **拉某篇论文的全部 review 也需要 token**，不是只有按 venue 枚举才要
           （search 是唯一真正免 token 的入口）
  ❌ openreview.net/pdf?id=...  /  /pdf/{hash}.pdf     HTTP 403 Cloudflare 挑战页
        openreview.net 主站对脚本化下载做了拦截，需要浏览器会话
  注：note 的 content 里**能拿到 pdf 字段**（相对路径或外部链接如 arXiv/IEEE/IJCAI），
      但直接 GET 那个路径会被 403。所以 OpenReview 只做"元数据增强"，
      全文请走 arXiv / 外部链接。

─────────────────────────────────────────────────────────────────────────────
API v1 vs v2：必须区分
─────────────────────────────────────────────────────────────────────────────
    v2  https://api2.openreview.net   ← 2023 年后的会议（ICLR 2023+ 等）
    v1  https://api.openreview.net    ← 老会议，至今仍在用
两个版本**参数名不同**，写错了会 400：
    v2  search 用 `term` 或 `terms`     ← 用 `query` 会 400 ValidationError
    v1  search 用 `query`
实测 v2 传 query 的报错原文：
    {"name":"ValidationError","message":"request requires at least one of
      term, terms as parameter"}

─────────────────────────────────────────────────────────────────────────────
note 与 forum 的关系（抓 review 的关键）
─────────────────────────────────────────────────────────────────────────────
    forum   = 一篇投稿的"帖子 ID"，所有相关 note 都挂在同一个 forum 下
    note    = 一篇具体的帖子：submission / official review / author rebuttal /
              decision 各是一条 note
所以：
    1. 先在 search 里找到 submission note，拿到它的 forum（= id）
    2. 再拉 /notes?forum={forum} 就能拿到这篇论文的**全部** review + rebuttal + decision
    3. review 内容在 note.content 里，字段名随会议模板变化，
       常见：rating / confidence / recommendation / summary / strengths /
             weaknesses / questions / limitations / soundness / presentation

命令行用法：
    python3 openreview.py search "deep counterfactual regret"
    python3 openreview.py forum lFYEklFizT          # 拉一篇论文的所有 note
    python3 openreview.py v1 "deep cfr"
    python3 openreview.py check

─────────────────────────────────────────────────────────────────────────────
函数索引（快速定位）
  [检索]  OpenReview.search_notes_v2(term, limit, offset, source)
          ★ v2 必须用 term；用 query 会 400。**唯一免 token 的入口**
  [评审]  OpenReview.get_forum(forum_id, limit)
          拉某篇投稿的全部 note，按 invitation 分类 reviews/decisions/rebuttals/comments
  [枚举]  OpenReview.list_notes_by_venue(venue_id, limit)
          按会议枚举投稿；实测 **403 ChallengeRequired，需要登录 token**
  [检索]  OpenReview.search_notes_v1(query, limit, offset)
          v1 老会议；参数名是 query（不是 term）
  [探针]  OpenReview.probe_pdf(note_id)
          探测 PDF 是否可下载；实测**被 Cloudflare 403 拦下**
  [工具]  invitation_of(note) / unwrap_content(content) / extract_pdf_link(note)
          兼容 v1/v2 的 invitation 字段 / 拆 value 包装 / 取可用 PDF 链接
  [工具]  to_internal_paper(note)              统一 schema（含 peer_review 段）
  [自检]  check(orv)  [CLI] main(argv)         search / forum / v1 / venue
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import http_request, url_with, section, kv, show_records, get_key

# ---- 两个版本的基址，不要混用 -----------------------------------------------
API_V2 = "https://api2.openreview.net"
API_V1 = "https://api.openreview.net"
WEB = "https://openreview.net"

HTTP_TIMEOUT = 30.0
HTTP_RETRIES = 1

# 浏览器 UA：openreview.net 主站对脚本 UA 会直接给 Cloudflare 403
BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36")

# 常见会议的 venue id 写法（用于 notes?content.venueid=... 枚举，需要 token）
VENUE_IDS = {
    "ICLR 2024": "ICLR.cc/2024/Conference",
    "ICLR 2025": "ICLR.cc/2025/Conference",
    "NeurIPS 2024": "NeurIPS.cc/2024/Conference",
    "ICML 2024": "ICML.cc/2024/Conference",
}

# review 类 note 里可能出现的字段名（不同会议模板不同，用于自动识别）
REVIEW_FIELDS = ("rating", "confidence", "recommendation", "summary", "strengths",
                 "weaknesses", "questions", "limitations", "soundness",
                 "presentation", "contribution", "decision", "comment")


class OpenReview:
    """OpenReview 客户端（同时支持 v1 / v2）。

    用法：
        orv = OpenReview()
        n = orv.search_notes_v2("deep cfr")
        all_notes = orv.get_forum("lFYEklFizT")      # 拿到 review/rebuttal
        orv.token = "..."                            # 有登录 token 后可枚举 venue
    """

    def __init__(self, token: str = "", email: str = "",
                 timeout: float = HTTP_TIMEOUT, retries: int = HTTP_RETRIES):
        # ★ token 必须也支持从 Keys.txt 读：
        #   否则 INDEX 里写的"填配置即可用"对这个模块不成立
        #   （早期实现只读环境变量，用户填了 Keys.txt 却完全不生效）
        self.token = token or get_key("OPENREVIEW_TOKEN")
        self.email = email or get_key("PAPERSEARCH_EMAIL")
        self.timeout = timeout
        self.retries = retries

    def _headers(self):
        h = {"User-Agent": BROWSER_UA}
        if self.token:
            h["Authorization"] = "Bearer " + self.token
        return h

    def _get(self, base: str, path: str, params: dict = None):
        url = url_with(base + path, params or {})
        status, body, _ = http_request(url, headers=self._headers(),
                                       timeout=self.timeout, retries=self.retries,
                                       email=self.email)
        text = body.decode("utf-8", "replace")
        try:
            import json
            return status, json.loads(text), text, url
        except Exception:
            return status, None, text[:300], url

    # ---- v2 检索 ------------------------------------------------------------
    def search_notes_v2(self, term: str, limit: int = 10, offset: int = 0,
                        source: str = "all"):
        """v2 全文检索 /notes/search  ← **免 key 可用，最实用的入口**

        参数
        ----
        term   : 检索词。⚠️ **v2 必须用 term/terms，不能用 query**（会 400）
        limit  : 返回条数
        offset : 偏移量翻页
        source : 'all' / 'forum' / 'reply' 等，限定搜索范围
                 搜综述/审稿内容时用 'reply'；找投稿用 'forum'

        返回 {"status", "notes", "error"}

        note 结构：
          id        note ID
          forum     所属投稿的 ID（= submission 的 id）★ 用它拉全部 review
          replyto   回复的是哪条 note
          invitation  模板名，如 'ICLR.cc/2024/Conference/-/Official_Review'
                      ★ 判断这条 note 是"审稿意见"还是"作者回复"就看它
          content   {字段: {"value": 值}} —— v2 把每个字段包了一层 value
          cdate/tcdate  创建时间
          signatures / readers  权限与作者

        用途：找到目标论文的 submission note → 拿 forum → 拉全部评审。
        """
        params = {"term": term, "limit": limit}
        if offset:
            params["offset"] = offset
        if source and source != "all":
            params["source"] = source
        status, data, text, url = self._get(API_V2, "/notes/search", params)
        if status != 200 or not data:
            return {"status": status, "notes": [], "error": text, "url": url}
        return {"status": 200, "notes": data.get("notes", []), "error": "", "url": url}

    def get_forum(self, forum_id: str, limit: int = 200):
        """拉一篇投稿下的所有 note（**抓 review / rebuttal / decision 的正确姿势**）

        参数
        ----
        forum_id : submission note 的 id（= forum 字段）
        limit    : 最多多少条

        返回 {"status", "notes", "reviews", "decisions", "rebuttals",
              "comments", "decision", "error"}

        ★ 分类逻辑（踩过坑，必须按 invitation 判，不能只靠 content 字段猜）：
            v2 的 note 用 **`invitations`（复数，数组）**，v1 用 `invitation`（单数字符串）。
            早期实现只读单数，导致 v2 的 Decision note 完全识别不出来。
          然后按 invitation 里的关键字归类：
            .../Decision          → decisions（最终决定）
            .../Official_Review 或 /Review → reviews（审稿意见）
            .../Rebuttal 或 /Author_Rebuttal → rebuttals（作者回复）
            .../Comment           → comments（公开评论）
          不要用「content 里出现 rating 就当成 review」那种模糊规则 ——
          它会把 decision、comment 一起卷进 reviews，污染下游统计。

        工程提示：
          - 内容字段名随会议模板变化，常见：rating / confidence / recommendation /
            summary / strengths / weaknesses / questions / limitations / soundness
          - v2 的 content 是 {field: {"value": v}}，本方法用 unwrap_content 拆掉包装
          - `decision` 字段是 decisions 里最后一条的值（没有则为 None），
            方便"只看结论"的调用方直接用
        """
        status, data, text, url = self._get(API_V2, "/notes",
                                            {"forum": forum_id, "limit": limit})
        if status == 403:
            return {"status": 403, "notes": [], "reviews": [], "decisions": [],
                    "rebuttals": [], "comments": [], "decision": None,
                    "error": "403：需要登录 token（按 forum / venueid 查询会被 Challenge 拦截）",
                    "url": url}
        if status != 200 or not data:
            return {"status": status, "notes": [], "reviews": [], "decisions": [],
                    "rebuttals": [], "comments": [], "decision": None,
                    "error": text, "url": url}

        notes = data.get("notes", [])
        reviews, decisions, rebuttals, comments = [], [], [], []
        for n in notes:
            content = unwrap_content(n.get("content") or {})
            inv = invitation_of(n)
            entry = {"invitation": inv, "id": n.get("id"), "content": content,
                     "signatures": n.get("signatures")}
            low = inv.lower()
            if "decision" in low:
                decisions.append(entry)
            elif "rebuttal" in low:
                rebuttals.append(entry)
            elif "review" in low:
                reviews.append(entry)
            elif "comment" in low:
                comments.append(entry)
            else:
                # 归类不了就放 comments，至少不污染 reviews
                comments.append(entry)

        decision = None
        if decisions:
            last = decisions[-1]["content"]
            decision = last.get("decision") or last.get("recommendation")
        return {"status": 200, "notes": notes, "reviews": reviews,
                "decisions": decisions, "rebuttals": rebuttals,
                "comments": comments, "decision": decision,
                "error": "", "url": url}

    def list_notes_by_venue(self, venue_id: str, limit: int = 10):
        """按会议枚举投稿 /notes?content.venueid=...  ⚠️ 实测需要登录 token

        参数 venue_id : 见 VENUE_IDS，如 'ICLR.cc/2024/Conference'

        返回 {"status", "notes", "error", "blocked": bool}

        实测结果（无 token）：
            HTTP 403 ChallengeRequiredError
            {"name":"ChallengeRequiredError","message":"Challenge verification required"}
        ⇒ 想批量拉某会议全部论文，必须先在浏览器登录 openreview.net，
          从 cookie / API token 里取出 Bearer token，赋给 OpenReview(token=...)。
        """
        status, data, text, url = self._get(
            API_V2, "/notes", {"content.venueid": venue_id, "limit": limit})
        if status == 403:
            return {"status": 403, "notes": [], "blocked": True,
                    "error": "403 ChallengeRequiredError：需要登录 token", "url": url}
        if status != 200 or not data:
            return {"status": status, "notes": [], "blocked": False,
                    "error": text, "url": url}
        return {"status": 200, "notes": data.get("notes", []), "blocked": False,
                "error": "", "url": url}

    # ---- v1（老会议）-------------------------------------------------------
    def search_notes_v1(self, query: str, limit: int = 10, offset: int = 0):
        """v1 全文检索 /notes/search  ⚠️ 参数名是 query（不是 term）

        用途：2013~2022 年的老会议（如 ICLR 2017-2022）只能用 v1。
        返回 v1 的 note 结构：content 是**扁平的**（没有 v2 的 value 包装）。

        返回 {"status", "notes", "error"}
        """
        status, data, text, url = self._get(API_V1, "/notes/search",
                                            {"query": query, "limit": limit,
                                             "offset": offset})
        if status != 200 or not data:
            return {"status": status, "notes": [], "error": text, "url": url}
        return {"status": 200, "notes": data.get("notes", []), "error": "", "url": url}

    # ---- PDF（已知会被拦）---------------------------------------------------
    def probe_pdf(self, note_id: str):
        """探测 PDF 是否可下载。**实测会被 Cloudflare 403 拦下。**

        尝试三种 URL 形式：
            https://openreview.net/pdf?id={note_id}
            https://openreview.net/attachment?id={note_id}&name=pdf
            note.content.pdf.value 里给的相对路径（如 /pdf/{hash}.pdf）

        返回 {"status", "ok": bool, "tried": [...], "note": str}

        结论：**不要指望用脚本直接下 OpenReview PDF。**
        变通方案（按推荐顺序）：
          1. 从 note.content.pdf.value 里取外部链接（很多是 arXiv / IEEE / IJCAI，
             这些站点通常允许脚本下载）
          2. 用 Playwright / Selenium 起真实浏览器会话
          3. 先拿元数据，全文去 arXiv 找同名论文
        """
        candidates = [
            "{}/pdf?id={}".format(WEB, note_id),
            "{}/attachment?id={}&name=pdf".format(WEB, note_id),
        ]
        tried = []
        for url in candidates:
            status, body, hdrs = http_request(
                url, headers={"User-Agent": BROWSER_UA, "Range": "bytes=0-1024"},
                timeout=self.timeout, retries=0, email=self.email)
            is_pdf = body[:5] == b"%PDF-"
            tried.append({"url": url, "status": status,
                          "content_type": hdrs.get("Content-Type", "?"),
                          "is_pdf": is_pdf})
            if is_pdf:
                return {"status": status, "ok": True, "tried": tried, "note": ""}
        return {"status": tried[-1]["status"] if tried else 0, "ok": False,
                "tried": tried,
                "note": "全部 403：openreview.net 对脚本化下载做了 Cloudflare 拦截，"
                        "需浏览器会话；建议改用 note.content.pdf 里的外部链接"}


def invitation_of(note: dict) -> str:
    """取出 note 的 invitation，**同时兼容 v1 和 v2 的字段名**。

    v2 用 `invitations`（复数，列表，可能多条）  → 取第一条
    v1 用 `invitation`（单数，字符串）

    ⚠️ 这是 v1/v2 之间最容易踩的差异之一：只读单数字段时，
       v2 的所有 note 都会被判成"无类型"，review / decision 全部丢失。
    """
    inv = note.get("invitations")
    if isinstance(inv, list) and inv:
        return str(inv[0])
    if isinstance(inv, str) and inv:
        return inv
    single = note.get("invitation")
    return str(single) if single else ""


def unwrap_content(content: dict) -> dict:
    """拆掉 v2 的 {"field": {"value": v}} 包装，返回 {field: v}。

    v1 的 content 本来就是扁平的，这个函数对 v1 也安全（原样返回）。
    """
    out = {}
    for k, v in (content or {}).items():
        if isinstance(v, dict) and "value" in v:
            out[k] = v["value"]
        else:
            out[k] = v
    return out


def extract_pdf_link(note: dict):
    """从 note 里抽出可用的 PDF 链接。

    优先返回**外部链接**（arXiv/IEEE/IJCAI 等，脚本可下载），
    只有内部相对路径时才回退到 openreview.net（该路径会被 403）。
    """
    content = unwrap_content(note.get("content") or {})
    pdf = content.get("pdf")
    if isinstance(pdf, dict):
        pdf = pdf.get("value")
    if not pdf:
        return None
    if str(pdf).startswith("http"):
        return str(pdf)                       # 外部链接，可直接下载
    return WEB + str(pdf)                     # 内部路径，大概率 403


def to_internal_paper(note: dict) -> dict:
    """把 OpenReview note 并入内部 Paper 结构。"""
    content = unwrap_content(note.get("content") or {})
    return {
        "identity": {"openreview_id": note.get("id"),
                     "openreview_forum": note.get("forum"),
                     "doi": None, "arxiv_id": content.get("arxiv_id"),
                     "openalex_id": None, "s2_id": None},
        "metadata": {
            "title": content.get("title"),
            "authors": content.get("authors") or [],
            "venue": content.get("venue") or content.get("venueid"),
            "keywords": content.get("keywords") or [],
        },
        "abstract": content.get("abstract"),
        "peer_review": {
            "pdf_link": extract_pdf_link(note),
            "reviews": [], "decision": None,
        },
        "search_meta": {"found_by": [{"source": "openreview", "kind": "search"}]},
    }


# =============================================================================
# 自检与命令行入口
# =============================================================================

def check(orv: OpenReview) -> int:
    failed = 0

    section("1) v2 检索 api2/notes/search（免 key）")
    r = orv.search_notes_v2("deep counterfactual regret", limit=3)
    kv("状态", "HTTP {}".format(r["status"]))
    kv("返回条数", len(r["notes"]))
    if r["notes"]:
        n = r["notes"][0]
        content = unwrap_content(n.get("content") or {})
        kv("note id / forum", "{} / {}".format(n.get("id"), n.get("forum")))
        kv("invitation", n.get("invitation"))
        kv("content 字段", sorted(content.keys()))
        kv("pdf 链接", extract_pdf_link(n))
        kv("用途", "找到 submission → 拿 forum → 拉全部 review/rebuttal/decision")
    else:
        kv("错误", r.get("error"))
        failed += 1

    section("2) 拉 forum 全部 note（review / decision 在这里）")
    forum_id = (r["notes"][0].get("forum") if r["notes"] else "lFYEklFizT")
    f = orv.get_forum(forum_id)
    kv("forum", forum_id)
    kv("状态", "HTTP {}".format(f["status"]))
    kv("note 总数", len(f["notes"]))
    kv("识别为 review 的", len(f["reviews"]))
    kv("decision", f.get("decision"))
    if f["reviews"]:
        rev = f["reviews"][0]
        kv("review 模板", rev["invitation"])
        kv("review 字段", sorted(rev["content"].keys())[:10])
    elif f["status"] == 403:
        kv("结论", "forum 查询同样被 Challenge 拦截 —— 拉 review 也需要 token")
    else:
        kv("说明", "该 forum 下没有 review note（可能是非评审类记录）")

    section("3) v1 检索（老会议）")
    v1 = orv.search_notes_v1("deep cfr", limit=3)
    kv("状态", "HTTP {}".format(v1["status"]))
    kv("返回条数", len(v1["notes"]))
    if not v1["notes"]:
        kv("错误", v1.get("error"))
        failed += 1

    section("4) 按 venue 枚举（需要登录 token）")
    v = orv.list_notes_by_venue(VENUE_IDS["ICLR 2024"], limit=1)
    kv("状态", "HTTP {}".format(v["status"]))
    kv("是否被拦", v["blocked"])
    kv("结论", "403 ChallengeRequired → 需要浏览器登录后取 Bearer token" if v["blocked"]
       else "未登录也可枚举（与上次实测不符，请记录）")
    kv("token", "已配置" if orv.token else "未配置（OPENREVIEW_TOKEN 环境变量）")

    section("5) PDF 下载探测（预期被拦）")
    p = orv.probe_pdf(forum_id)
    kv("可否下载", p["ok"])
    for t in p["tried"]:
        print("      {} → HTTP {} {} {}".format(
            t["url"][:62], t["status"], t["content_type"], "PDF✅" if t["is_pdf"] else ""))
    kv("结论", p["note"])

    print("\n" + "-" * 78)
    print("OpenReview 自检：检索{}通过；PDF/venue 枚举需 token".format(
        "" if failed == 0 else "未完全"))
    return failed


def main(argv) -> int:
    orv = OpenReview()
    if not argv or argv[0] == "check":
        return check(orv)

    cmd = argv[0]

    if cmd == "search":
        q = argv[1] if len(argv) > 1 else "deep counterfactual regret"
        r = orv.search_notes_v2(q, limit=5)
        section("OpenReview v2 检索: {}".format(q))
        kv("状态", "HTTP {}".format(r["status"]))
        kv("条数", len(r["notes"]))
        for n in r["notes"]:
            c = unwrap_content(n.get("content") or {})
            print("\n  ▶ {}".format(c.get("title") or "（无标题字段）"))
            print("    id/forum : {} / {}".format(n.get("id"), n.get("forum")))
            print("    venue    : {}".format(c.get("venue")))
            print("    pdf      : {}".format(extract_pdf_link(n)))
        return 0

    if cmd == "forum":
        if len(argv) < 2:
            print("用法: python3 openreview.py forum {forum_id}")
            return 2
        f = orv.get_forum(argv[1])
        section("OpenReview forum: {}".format(argv[1]))
        kv("状态", "HTTP {}".format(f["status"]))
        kv("note 总数", len(f["notes"]))
        kv("review 数", len(f["reviews"]))
        kv("decision", f.get("decision"))
        for rev in f["reviews"][:3]:
            print("\n  ── review ({}) ──".format(rev["invitation"]))
            for k, v in rev["content"].items():
                s = str(v)
                print("    {:<14}: {}".format(k, s[:100] + ("…" if len(s) > 100 else "")))
        return 0

    if cmd == "v1":
        q = argv[1] if len(argv) > 1 else "deep cfr"
        r = orv.search_notes_v1(q, limit=5)
        section("OpenReview v1 检索（老会议）: {}".format(q))
        kv("状态", "HTTP {}".format(r["status"]))
        kv("条数", len(r["notes"]))
        show_records(r["notes"], ["id", "invitation", "cdate"])
        return 0

    if cmd == "venue":
        v = argv[1] if len(argv) > 1 else VENUE_IDS["ICLR 2024"]
        r = orv.list_notes_by_venue(v, limit=3)
        section("OpenReview 按 venue 枚举: {}".format(v))
        kv("状态", "HTTP {}".format(r["status"]))
        kv("被拦", r["blocked"])
        kv("错误", (r.get("error") or "")[:120])
        return 0

    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
