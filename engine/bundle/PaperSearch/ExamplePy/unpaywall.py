#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""unpaywall.py —— Unpaywall 数据源采集方式

定位：**DOI → 合法免费全文位置**。
它不是论文发现工具，是"拿到 DOI 之后，去哪儿下合法免费版"的查询服务。

官方文档：https://unpaywall.org/products/api
**完全免费，但必须提供真实 email**（不是 key，不涉及计费）。

─────────────────────────────────────────────────────────────────────────────
⚠️ 两个必须知道的事实
─────────────────────────────────────────────────────────────────────────────
1. **search 接口已于 2026-09-18 退役**
   实测：GET /v2/search?query=... → HTTP 410 Gone
   所以 Unpaywall 现在**只能做 DOI → OA 定位**，不能做 keyword → paper。
   官方让论文搜索改用 OpenAlex。

2. **email 会被校验**
   实测：用 test@example.com 之类的占位域名 → HTTP 422
        "Please use your own email address in API calls."
   必须填真实邮箱（本库从 Keys.txt 的 PAPERSEARCH_EMAIL 读）。

官方建议每日调用不超过 100,000 次 —— 对个人使用等于无限制。
响应里的 `evidence` 字段有时显示 "deprecated"，这只是数据标记，不影响使用。

命令行用法：
    python3 unpaywall.py doi 10.1038/nature12373
    python3 unpaywall.py batch 10.1038/nature12373 10.1103/PhysRevLett.116.061102
    python3 unpaywall.py check

─────────────────────────────────────────────────────────────────────────────
函数索引（快速定位）
  [全文]  Unpaywall.get_by_doi(doi)            DOI → OA 全文位置（含 version/license）
  [全文]  Unpaywall.get_many(dois, sleep_between, on_result)
          批量查询（官方无批量端点，串行 + 可回调落盘）
  [探针]  Unpaywall.probe_search_retired()
          确认 search 接口已退役（实测 HTTP 410）
  [工具]  to_internal_paper(result)            并入统一 schema 的 full_text 段
  [自检]  check(up)  [CLI] main(argv)          doi / batch
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import os
import sys
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import http_request, url_with, section, kv, get_key

BASE = "https://api.unpaywall.org/v2"

HTTP_TIMEOUT = 30.0
HTTP_RETRIES = 2

# oa_status 取值含义（用于决定"要不要继续找全文"）：
#   gold     出版商自己的 OA 期刊，最正规
#   hybrid   订阅期刊里的单篇 OA（作者付费开放）
#   bronze   出版商网站免费可读，但**没有明确 license** ← 常见但法律地位模糊
#   green    作者自存档（机构库/预印本），最稳妥
#   closed   完全闭源，应放弃或改用其它途径
OA_STATUS_MEANING = {
    "gold": "出版商 OA 期刊，最正规",
    "hybrid": "订阅期刊中的单篇开放",
    "bronze": "出版商网站免费读，但无明确 license",
    "green": "作者自存档 / 预印本，最稳妥",
    "closed": "闭源，建议放弃或走机构订阅",
}


class Unpaywall:
    """Unpaywall 客户端。

    用法：
        up = Unpaywall()                       # 自动读 PAPERSEARCH_EMAIL
        r = up.get_by_doi("10.1038/nature12373")
        if r["is_oa"]: download(r["best_pdf"])
    """

    def __init__(self, email: str = "", timeout: float = HTTP_TIMEOUT,
                 retries: int = HTTP_RETRIES):
        self.email = email or get_key("PAPERSEARCH_EMAIL")
        self.timeout = timeout
        self.retries = retries

    # ---- 主接口：DOI → OA 位置 ---------------------------------------------
    def get_by_doi(self, doi: str):
        """查询一个 DOI 的开放获取情况 GET /v2/{doi}?email=

        参数
        ----
        doi : '10.1038/nature12373' 或完整 URL（会自动清洗）或 'doi:xxx'

        返回一个扁平化后的字典：
        {
          "status": 200 | 404 | 422 | ...,
          "is_oa": bool,                    是否开放获取
          "oa_status": "gold|hybrid|bronze|green|closed",
          "oa_status_meaning": str,         上面取值的中文解释
          "best_pdf": str|None,             ★ 最佳 PDF 直链，直接可下载
          "best_landing": str|None,         落地页
          "host_type": "publisher|repository",
          "version": "publishedVersion|acceptedVersion|submittedVersion",
                                            ★ 版本！预印本 vs 正式版，分析时要区分
          "license": str|None,              许可协议
          "title": str, "year": int, "journal": str, "publisher": str,
          "all_locations": [...],           所有 OA 位置（可能有多个仓库副本）
          "genuine_oa": bool,               是否"真 OA"（有 license 且非 bronze）
          "error": str
        }

        用途与工程建议：
          1. 全文获取链路的第三优先级：arXiv → OpenAlex best_oa_location → Unpaywall
          2. `version` 很关键：submittedVersion/acceptedVersion 与正式版内容可能不同，
             做引用页码定位或精确复现时要小心
          3. bronze 状态虽然没有明确 license，但官方 URL 公开可读，
             作为"人工查看"入口可用；自动爬取前建议先看 license
          4. 404 = 这个 DOI 不在 Unpaywall 库里，不代表没有 OA 版本
             （尤其是很新的论文和 arXiv 预印本）
        """
        clean = doi.strip()
        if clean.lower().startswith("http"):
            clean = clean.split("doi.org/")[-1]
        clean = clean.replace("doi:", "").strip()

        if not self.email:
            return {"status": 400, "is_oa": None, "error":
                    "未配置 PAPERSEARCH_EMAIL。Unpaywall 强制要求真实 email（占位域名会被 422 拒绝）"}

        url = url_with("{}/{}".format(BASE, urllib.parse.quote(clean)),
                       {"email": self.email})
        status, body, _ = http_request(url, timeout=self.timeout,
                                       retries=self.retries, email=self.email)
        text = body.decode("utf-8", "replace")

        if status == 422 and "own email" in text:
            return {"status": 422, "is_oa": None, "error":
                    "422：官方拒绝该 email（example.com 等占位域名会被拒），请换成真实邮箱"}
        if status == 404:
            return {"status": 404, "is_oa": None, "error":
                    "404：该 DOI 不在 Unpaywall 库中（新论文/预印本常见，不代表没有 OA 版）"}
        if status != 200:
            return {"status": status, "is_oa": None,
                    "error": "HTTP {}: {}".format(status, text[:200])}

        try:
            import json
            d = json.loads(text)
        except Exception as exc:
            return {"status": status, "is_oa": None, "error": "JSON 解析失败: {}".format(exc)}

        best = d.get("best_oa_location") or {}
        locs = d.get("oa_locations") or []
        status_key = d.get("oa_status") or ("closed" if not d.get("is_oa") else "unknown")
        return {
            "status": 200,
            "is_oa": d.get("is_oa"),
            "oa_status": status_key,
            "oa_status_meaning": OA_STATUS_MEANING.get(status_key, "未知"),
            "best_pdf": best.get("url_for_pdf"),
            "best_landing": best.get("url_for_landing_page") or best.get("url"),
            "host_type": best.get("host_type"),
            "version": best.get("version"),
            "license": best.get("license"),
            "title": d.get("title"),
            "year": d.get("year"),
            "journal": d.get("journal_name"),
            "publisher": d.get("publisher"),
            "genuine_oa": d.get("is_oa") and status_key != "bronze",
            "all_locations": [
                {"url": l.get("url"), "pdf": l.get("url_for_pdf"),
                 "host": l.get("host_type"), "version": l.get("version"),
                 "repository": l.get("repository_institution")}
                for l in locs
            ],
            "error": "",
        }

    # ---- 批量（串行，官方没有 batch 端点） ----------------------------------
    def get_many(self, dois: list, sleep_between: float = 0.1, on_result=None):
        """批量查询（**官方没有批量端点**，只能循环；这里加了温和的间隔）。

        参数
        ----
        dois          : DOI 列表
        sleep_between : 每个请求之间的间隔秒数。官方限 10 万次/天，
                        正常使用 0.1~0.5s 足够礼貌，不会触发限流
        on_result     : 回调 on_result(doi, result)，用于边查边落盘

        返回 {"total", "oa_count", "results": {doi: result}}

        工程建议：DOI 列表可能有几百上千个，一定要用 on_result 落盘，
        否则中途失败会丢掉所有已完成的结果。
        """
        import time
        results, oa_count = {}, 0
        for doi in dois:
            r = self.get_by_doi(doi)
            results[doi] = r
            if r.get("is_oa"):
                oa_count += 1
            if on_result:
                on_result(doi, r)
            if sleep_between:
                time.sleep(sleep_between)
        return {"total": len(dois), "oa_count": oa_count, "results": results}

    # ---- 退役接口的验证（保留它是为了监控"是否真的退役了"） ------------------
    def probe_search_retired(self):
        """确认 search 接口确实已退役。

        实测（2026-09-28）：HTTP 410 Gone
        保留这个探针的意义：如果哪天官方又开放了，这里的返回值会变，
        此时计划里"Unpaywall 只做 DOI→OA"的假设就要重新评估。
        """
        if not self.email:
            return {"status": None, "retired": None, "error": "需要 email"}
        url = url_with("{}/search".format(BASE),
                       {"query": "counterfactual regret minimization",
                        "email": self.email})
        status, body, _ = http_request(url, timeout=self.timeout, retries=0,
                                       email=self.email)
        text = body.decode("utf-8", "replace")
        return {"status": status,
                "retired": status in (404, 410, 403),
                "body": text[:200],
                "error": "" if status != 200 else "search 仍返回 200，需复核退役结论"}


def to_internal_paper(result: dict) -> dict:
    """把 Unpaywall 结果并入内部 Paper 结构的 full_text 部分。"""
    return {
        "full_text": {
            "oa_status": result.get("oa_status"),
            "pdf_url": result.get("best_pdf"),
            "landing_url": result.get("best_landing"),
            "version": result.get("version"),
            "license": result.get("license"),
            "alt_locations": result.get("all_locations") or [],
        },
        "search_meta": {"found_by": [{"source": "unpaywall",
                                      "kind": "fulltext_resolution"}]},
    }


# =============================================================================
# 自检与命令行入口
# =============================================================================

def check(up: Unpaywall) -> int:
    failed = 0

    section("1) DOI → OA 全文定位")
    kv("email", up.email or "（未配置！Unpaywall 必须要有真实邮箱）")
    if not up.email:
        kv("结论", "BLOCKED：缺少 PAPERSEARCH_EMAIL")
        return 1
    r = up.get_by_doi("10.1038/nature12373")
    kv("状态", "HTTP {}".format(r["status"]))
    if r["status"] == 200:
        kv("is_oa / oa_status", "{} / {}（{}）".format(
            r["is_oa"], r["oa_status"], r["oa_status_meaning"]))
        kv("best_pdf", r["best_pdf"])
        kv("version", "{} ← 预印本/正式版要区分".format(r["version"]))
        kv("host_type", r["host_type"])
        kv("license", r["license"])
        kv("OA 位置数", len(r["all_locations"]))
    else:
        kv("错误", r["error"])
        failed += 1

    section("2) search 接口退役确认")
    p = up.probe_search_retired()
    kv("状态", "HTTP {}".format(p["status"]))
    kv("是否已退役", p["retired"])
    kv("结论", "退役 ✅ → Unpaywall 只做 DOI→OA，不做关键词检索" if p["retired"]
       else "⚠️ 仍在返回 200，计划的假设需要复核")
    if not p["retired"]:
        failed += 1

    section("3) 404 的行为（新论文常见）")
    r404 = up.get_by_doi("10.9999/this.doi.does.not.exist")
    kv("状态", "HTTP {}".format(r404["status"]))
    kv("说明", (r404.get("error") or "")[:110])

    print("\n" + "-" * 78)
    print("Unpaywall 自检：{}".format("全部通过" if failed == 0 else "{} 项失败".format(failed)))
    return failed


def main(argv) -> int:
    up = Unpaywall()
    if not argv or argv[0] == "check":
        return check(up)

    cmd = argv[0]

    if cmd == "doi":
        doi = argv[1] if len(argv) > 1 else "10.1038/nature12373"
        r = up.get_by_doi(doi)
        section("Unpaywall DOI: {}".format(doi))
        if r["status"] == 200:
            for k in ("is_oa", "oa_status", "oa_status_meaning", "best_pdf",
                      "best_landing", "host_type", "version", "license",
                      "genuine_oa", "title", "year", "journal", "publisher"):
                kv(k, r.get(k))
            kv("全部 OA 位置", len(r["all_locations"]))
            for loc in r["all_locations"]:
                print("      - {} [{} / {}]".format(loc["url"], loc["host"], loc["version"]))
        else:
            kv("错误", r["error"])
            return 1
        return 0

    if cmd == "batch":
        dois = argv[1:]
        if not dois:
            print("用法: python3 unpaywall.py batch DOI1 DOI2 ...")
            return 2
        r = up.get_many(dois, sleep_between=0.2)
        section("Unpaywall 批量")
        kv("总计", r["total"])
        kv("有 OA 的", r["oa_count"])
        for doi, res in r["results"].items():
            print("  {} → {} {}".format(
                doi, res.get("oa_status"), res.get("best_pdf") or "（无 PDF 直链）"))
        return 0

    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
