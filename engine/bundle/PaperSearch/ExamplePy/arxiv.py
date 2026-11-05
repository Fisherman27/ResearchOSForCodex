#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""arxiv.py —— arXiv 数据源的全部采集方式

arXiv 有 **四条** 官方通道，用途完全不同，别混用：

  方式 1  Export API（Atom）    关键词检索，最常用；单查询有深分页上限
  方式 2  PDF 下载              按 arXiv ID 拿免费全文 PDF
  方式 3  OAI-PMH               批量 metadata 采集（官方推荐），无深分页上限
  方式 4  S3 全量全文            整月 PDF/LaTeX 打包，requester-pays（要 AWS 凭证+付费）

实测状态（2026-09-28）：方式 1/2/3 全部 PASS，方式 4 确认存在但需 AWS 凭证。

─────────────────────────────────────────────────────────────────────────────
方式 1：Export API —— 关键词检索
─────────────────────────────────────────────────────────────────────────────
URL   http://export.arxiv.org/api/query   （https 也行，自动化建议用 https）
返回  Atom XML（不是 JSON！）
限速  官方要求请求间隔 ≥ 3 秒；实测按 3s 间隔连打 10 次 100% 成功
限额  官方手册：max_results ≤ 2000/次，单次调用总计 ≤ 30000（>30000 返回 400）
      ⚠️ 但本机实测深分页在约 10000 处就返回 500（见 OBSERVED_DEEP_PAGE_LIMIT）
       —— 无论按哪个数，采 10 万条都必须按「分类 × 时间窗」切分查询，
          或者直接走 OAI-PMH（方式 3，没有这个限制）

search_query 字段前缀（可组合，用 AND / OR / ANDNOT 连接）：
    all:xxx            全部字段
    ti:xxx             标题
    abs:xxx            摘要
    au:xxx             作者
    cat:cs.AI          分类（cs.AI / cs.LG / cs.MA / cs.GT / stat.ML …）
    co:xxx             备注/评论
    jr:xxx             期刊引用
    rn:xxx             报告号
    submittedDate:[202401010000 TO 202412312359]   提交时间区间（注意格式）
    短语用引号：all:"counterfactual regret minimization"

返回字段（Atom entry）：id(含 arXiv ID) / updated / published / title /
    summary(摘要) / author[] / category[] / arxiv:doi / arxiv:journal_ref /
    link[rel=alternate]=落地页 / link[title=pdf]=PDF 直链

─────────────────────────────────────────────────────────────────────────────
方式 3：OAI-PMH —— 批量 metadata
─────────────────────────────────────────────────────────────────────────────
URL   https://oaipmh.arxiv.org/oai     （export.arxiv.org/oai2 会 301 到这里）
返回  OAI-PMH XML，metadataPrefix=arXiv 时每条 record 含：
      id / created / updated / authors / title / categories / comments /
      journal-ref / doi / abstract
分页  单页约 1300 条（≈3.9MB，约 7~50s，取决于网络）；响应里带 resumptionToken，
      把 token 原样回传就能取下一页；token 会过期（通常到当天 24:00）
窗口  from / until 用 YYYY-MM-DD，**按提交时间过滤**，是切分大批量采集的主力手段
优点  没有深分页上限、免 key、字段比 Export API 更全（含 journal-ref）
缺点  只能按日期扫，不能按关键词检索；单条约 3ms，比 Export API 慢

─────────────────────────────────────────────────────────────────────────────
方式 4：S3 全量全文
─────────────────────────────────────────────────────────────────────────────
桶    s3://arxiv  （美国东部 us-east-1，requester-pays）
内容  pdf/arXiv_pdf_YYMM_NNN.tar   整月 PDF 的 tar 分片
      src/arXiv_src_YYMM_NNN.tar   LaTeX 源文件
增长  官方称约 100GB/月
代价  需要 AWS 账号 + 凭证，且**下载流量由你付费**；匿名访问直接 403
      （"Anonymous users cannot invoke requests against Requester Pays buckets"）
替代  没有 AWS 账号时：改用 Kaggle 的 Cornell-University/arxiv 数据集，
      或用 OpenAlex / Semantic Scholar 里的 arXiv 元数据（它们都存了 arXiv ID）

命令行用法：
    python3 arxiv.py search "counterfactual regret minimization"
    python3 arxiv.py search "cat:cs.AI" --max 50 --sort submittedDate
    python3 arxiv.py pdf 1603.01121            # 下载到 ./downloads/
    python3 arxiv.py oai                        # 拉最近 1 天的 OAI 记录
    python3 arxiv.py oai-window 2026-09-01 2026-09-02 --max 3000
    python3 arxiv.py check                      # 快速自检四个通道

─────────────────────────────────────────────────────────────────────────────
函数索引（快速定位）
  [检索]  search(query, max_results, start, sort_by, sort_order)
          Export API 关键词检索 → {"status","ok","total","entries"}
  [检索]  search_paged(query, limit, page_size)
          自动翻页采集；受官方 30000 / 本机实测 10000 的深分页限制
  [解析]  parse_atom(xml_bytes) / normalize_arxiv_id(raw)
          Atom XML → 结构化记录；统一新老两代 arXiv ID
  [全文]  download_pdf(arxiv_id, out_dir)
          下载免费 PDF；**判据是 ok 字段**（HTTP 200 也可能是 HTML 错误页）
  [批量]  oai_identify() / oai_list_records(...) / oai_harvest(...)
          OAI-PMH 三件套：探活 / 取一页 / 按时间窗连续采集
  [批量]  parse_oai_records(xml) / parse_oai_deleted(xml)
          OAI XML → 记录列表 / 删除（撤稿）记录列表
  [全量]  s3_bulk_check() / s3_download(month, chunk, kind)
          S3 requester-pays 桶探测 / 整月分片下载（会产生流量费）
  [工具]  to_internal_paper(entry, provenance)
          统一 Paper schema；**同时接受 Atom 与 OAI 两套字段名**
  [自检]  check(email)                 四通道自检
  [CLI]   main(argv)                   search / paged / pdf / oai / oai-window / s3
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import os
import re
import sys
import time
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import (http_request, url_with, section, kv, show_records,
                     get_key, mask, curl_get, curl_available)

# =============================================================================
# 常量：改这里就能适配不同环境
# =============================================================================

# 检索接口。https 和 http 都可用；BOTH 都写上是为了一边被限流时自动换另一边
ARXIV_API_BASES = ("https://export.arxiv.org/api/query",
                   "http://export.arxiv.org/api/query")

# 批量 metadata 接口（OAI-PMH）
ARXIV_OAI = "https://oaipmh.arxiv.org/oai"

# PDF 下载基址：https://arxiv.org/pdf/{id} 或 {id}v{n}
ARXIV_PDF = "https://arxiv.org/pdf/{}"

# S3 桶（requester-pays，仅用于说明与可选下载）
ARXIV_S3_BUCKET = "arxiv"
ARXIV_S3_REGION = "us-east-1"

# ---- 可调参数：限速与重试 ----------------------------------------------------
# arXiv 官方明确要求「连续请求间隔约 3 秒」。调小会增加被 429 的概率，
# 调大则更保险但更慢。实测 3.0 稳定，2.0 偶尔 429。
REQUEST_INTERVAL = 3.0      # 每次 arXiv 请求之间的强制间隔（秒）
HTTP_TIMEOUT = 45.0         # 单次请求超时；OAI 单页 4MB，给宽松些
HTTP_RETRIES = 2            # 每个基址的重试次数（总共最多 2 个基址 × (1+2) 次）

# ---- 分页上限：区分「官方文档」与「本环境实测」-----------------------------
# 官方 API 手册（https://info.arxiv.org/help/api/user-manual.html）原文：
#     "the maximum number of results returned from a single call (max_results)
#      is limited to 30000 in slices of at most 2000 at a time"
#     "A request with max_results >30,000 will result in an HTTP 400 error code"
# ⇒ 官方口径：单次 max_results ≤ 2000，单次调用总计 ≤ 30000。
MAX_RESULTS_PER_REQUEST = 2000   # 官方：单次请求条数上限
MAX_TOTAL_RESULTS = 30000        # 官方：单次调用可返回的总条数上限

# ⚠️ 但本机 2026-09-28 实测：深分页在 **约 10000 处就开始 500**
#       start=9998  max_results=2   → HTTP 200
#       start=9999  max_results=2   → HTTP 500
#       start=11000 / 15000 / 20000 / 29000 → 全部 HTTP 500
#    官方文档没有提 10000 这个数，它更像是**服务端在高负载下的实际降级**，
#    而不是协议规定。这里记录为「观测阈值」，用于告警，不作为硬拒绝条件。
#    工程结论：无论按哪个数，大规模采集都该按「分类 × 时间窗」切分查询，
#              或直接用 OAI-PMH（没有这个限制）。
OBSERVED_DEEP_PAGE_LIMIT = 10000

MAX_PER_REQUEST = MAX_RESULTS_PER_REQUEST   # 兼容旧名
OAI_PAGE_FALLBACK = 1300    # OAI 单页条数（实测约 1300）

# Atom 命名空间
NS = {"atom": "http://www.w3.org/2005/Atom",
      "arxiv": "http://arxiv.org/schemas/atom",
      "oai": "http://www.openarchives.org/OAI/2.0/",
      "oai_arxiv": "http://arxiv.org/OAI/arXiv/"}


# =============================================================================
# 方式 1：Export API 检索
# =============================================================================

def _arxiv_get(url: str, email: str = "", timeout: float = None, retries: int = None):
    """底层 GET：urllib(https) → urllib(http) → curl 兜底，逐级降级。

    实测 arXiv 在负载高时会返回三种"软性拒绝"，都不是代码错误，稍后重试即可：
        429  "Rate exceeded."         —— 明确的限流
        406  Not Acceptable（空 body）—— WAF 拦截，见下方说明
        0    连接超时 / 502           —— 网关抖动

    ⚠️ 观测到的现象（不是确定的因果解释）：本机同一时段、同一 URL 曾出现
           curl   → HTTP 200
           urllib → HTTP 406（空 body）
       之后 urllib 也恢复正常。能确定的是「406 会出现且空 body」，无法确定
       是 TLS 指纹、HTTP 版本还是瞬时负载造成的（同一台机器 curl --http1.1 也曾 200）。
       因此这里不做因果假设，只做工程处理：多基址重试 + curl 子进程兜底，
       两条路都留记录（attempts），方便下次复现时取证。

    返回 (status, body_bytes, url_used, attempts)
      attempts 是 [(传输方式, 状态码), ...]，用于判断"这次成功是第几次试出来的"
    """
    timeout = timeout or HTTP_TIMEOUT
    retries = HTTP_RETRIES if retries is None else retries
    query = url.split("?", 1)[1] if "?" in url else ""
    attempts = []
    last = (0, b"", url)

    for base in ARXIV_API_BASES:
        target = base + "?" + query
        for _ in range(retries + 1):
            status, body, _ = http_request(target, timeout=timeout, retries=0, email=email)
            attempts.append((base.split("//")[1].split("/")[0], status))
            last = (status, body, target)
            if status == 200 and b"<feed" in body[:400]:
                time.sleep(REQUEST_INTERVAL)
                return status, body, target, attempts
            time.sleep(REQUEST_INTERVAL)

    # 最后一级：curl 兜底（专治 406 TLS 指纹拦截）
    if curl_available():
        status, body, _ = curl_get(ARXIV_API_BASES[0] + "?" + query,
                                   timeout=timeout,
                                   user_agent="ResearchOS-PaperSearch/0.1 (+{})".format(
                                       email or "anonymous"))
        attempts.append(("curl", status))
        if status == 200 and b"<feed" in body[:400]:
            time.sleep(REQUEST_INTERVAL)
            return status, body, ARXIV_API_BASES[0] + "?" + query, attempts
        last = (status, body, ARXIV_API_BASES[0] + "?" + query)
    return last[0], last[1], last[2], attempts


SOFT_REJECT = {
    406: "HTTP 406（未文档化的软性限流，通常几分钟后自动恢复）",
    429: "HTTP 429 Rate exceeded（明确限流，请加大请求间隔）",
    0: "连接超时（网关抖动）",
    500: "HTTP 500（多半是深分页超限；官方 30000 / 本机实测约 10000）",
}


def search(query: str,
           max_results: int = 20,
           start: int = 0,
           sort_by: str = "relevance",
           sort_order: str = "descending",
           email: str = ""):
    """方式 1：按关键词检索 arXiv，返回结构化论文列表。

    参数
    ----
    query       : 检索式，见文件头 "search_query 字段前缀" 说明。
                  例：'all:"deep cfr"'、'cat:cs.AI AND abs:regret'
    max_results : 本次要几条，默认 20。上限 2000；
                  且深分页会失败（官方口径 30000，本机实测约 10000 起 500）
    start       : 从第几条开始，用于翻页。默认 0。
                  翻页公式：第 n 页 → start = (n-1) * max_results
    sort_by     : relevance（相关性，默认）/ submittedDate（提交时间）/
                  lastUpdatedDate（最后更新）
    sort_order  : descending（默认，新→旧）/ ascending

    返回
    ----
    {"status": int, "total": int, "count": int, "entries": [dict], "error": str}
    每个 entry 的字段：
      arxiv_id     '2609.01234v1' 形式的短 ID（去掉了 abs/ 前缀）
      id_url       http://arxiv.org/abs/... 落地页
      title        标题（已去掉换行与多余空格）
      summary      摘要
      authors      ["姓名", ...]
      published    首次提交时间 ISO8601
      updated      最后更新时间 ISO8601
      categories   ['cs.AI', 'cs.LG']  ← 可用于领域筛选
      primary_category  主分类
      doi          正式 DOI（没有则为 None）← 可用于和 Crossref 打通
      journal_ref  期刊引用（没有则为 None）
      comment      作者备注（常含 "Accepted at NeurIPS 2024" 这类会议信息）
      pdf_url      PDF 直链

    用途：初期候选论文发现、拿摘要做相关性初筛、拿 DOI 去 Crossref 做实体统一。
    """
    if start + max_results > MAX_TOTAL_RESULTS:
        return {"status": 400, "total": 0, "count": 0, "entries": [],
                "error": "start+max_results 超过官方上限 {}：当前 {}+{}。"
                         "请改为按分类/时间窗切分查询，或改用 OAI-PMH。".format(
                             MAX_TOTAL_RESULTS, start, max_results)}
    # 本机实测深分页在约 10000 处就会 500 —— 给出告警但**不阻止**
    # （不同时间/不同查询的表现可能不同，硬拦截反而会挡住本来能成的请求）
    warning = ""
    if start >= OBSERVED_DEEP_PAGE_LIMIT:
        warning = ("start={} 已超过本机观测阈值 {}，很可能返回 HTTP 500；"
                   "建议改用分类/时间窗切分查询或 OAI-PMH".format(
                       start, OBSERVED_DEEP_PAGE_LIMIT))
    url = url_with(ARXIV_API_BASES[0], {
        "search_query": query,
        "start": start,
        "max_results": min(max_results, MAX_PER_REQUEST),
        "sortBy": sort_by,
        "sortOrder": sort_order,
    })
    status, body, used, attempts = _arxiv_get(url, email=email)
    if status != 200:
        return {"status": status, "ok": False, "total": 0, "count": 0, "entries": [],
                "warning": warning,
                "error": "{}；尝试记录 {}；原文 {}".format(
                    SOFT_REJECT.get(status, "HTTP {}".format(status)),
                    attempts, body.decode("utf-8", "replace")[:200])}
    entries, total = parse_atom(body)
    return {"status": 200, "ok": True, "total": total, "count": len(entries),
            "entries": entries, "error": "", "warning": warning,
            "attempts": attempts, "url": used}


def parse_atom(xml_bytes: bytes):
    """把 Export API 的 Atom XML 解析成 (entries, total)。

    total 从 <opensearch:totalResults> 取，表示"这个检索式总共命中多少篇"，
    常用于判断是否需要继续翻页。
    """
    root = ET.fromstring(xml_bytes)
    total = 0
    for e in root:
        if e.tag.endswith("totalResults") and e.text:
            total = int(e.text)

    out = []
    for e in root.findall("atom:entry", NS):
        def txt(tag):
            v = e.findtext(tag, default="", namespaces=NS)
            return " ".join(v.split()) if v else ""

        id_url = txt("atom:id")
        # arXiv 有两代 ID 形式，必须都支持：
        #   新式  2609.01234v1            → http://arxiv.org/abs/2609.01234v1
        #   老式  hep-th/9901001v1        → http://arxiv.org/abs/hep-th/9901001v1
        # 老式 ID 里**含斜杠**，所以不能用 [^/]+ —— 那样会匹配失败并把整个 URL 当 ID。
        m = re.search(r"/abs/(.+)$", id_url)
        arxiv_id = m.group(1) if m else id_url
        pdf_url = ""
        for link in e.findall("atom:link", NS):
            if link.get("title") == "pdf" or link.get("type") == "application/pdf":
                pdf_url = link.get("href", "")
        authors = [a.findtext("atom:name", default="", namespaces=NS)
                   for a in e.findall("atom:author", NS)]
        cats = [c.get("term", "") for c in e.findall("atom:category", NS)]
        primary = e.find("arxiv:primary_category", NS)
        out.append({
            "arxiv_id": arxiv_id,
            "id_url": id_url,
            "title": txt("atom:title"),
            "summary": txt("atom:summary"),
            "authors": [a for a in authors if a],
            "published": txt("atom:published"),
            "updated": txt("atom:updated"),
            "categories": cats,
            "primary_category": primary.get("term", "") if primary is not None else "",
            "doi": txt("arxiv:doi") or None,
            "journal_ref": txt("arxiv:journal_ref") or None,
            "comment": txt("arxiv:comment") or None,
            "pdf_url": pdf_url,
        })
    return out, total


def search_paged(query: str, limit: int = 100, page_size: int = 200, email: str = ""):
    """方式 1 的翻页封装：自动按 start 递增取够 limit 条。

    为什么需要它：单次最多 2000 条，且深分页有上限
    （官方 30000；本机实测约 10000 起返回 500）。
    要取更多必须换检索式（分类/时间窗切分），或者改用 OAI-PMH。

    参数
    ----
    limit     : 总共想要多少条（会被深分页上限截断，见上面说明）
    page_size : 每次请求多少条，建议 200~2000。太小会触发更多次限速等待

    返回 {"collected": N, "truncated": bool, "entries": [...]}
      truncated=True 表示"因为深分页上限没取满"，调用方应改用时间窗切分
    """
    collected, start, truncated = [], 0, False
    while len(collected) < limit and start < MAX_TOTAL_RESULTS:
        want = min(page_size, limit - len(collected), MAX_TOTAL_RESULTS - start)
        res = search(query, max_results=want, start=start, email=email)
        if res["status"] != 200 or not res["entries"]:
            break
        collected.extend(res["entries"])
        start += len(res["entries"])
        if start + 1 > MAX_TOTAL_RESULTS:
            truncated = True
            break
    if len(collected) < limit and start >= MAX_TOTAL_RESULTS:
        truncated = True
    return {"collected": len(collected), "truncated": truncated, "entries": collected}


# =============================================================================
# 方式 2：PDF 下载
# =============================================================================

# 同时匹配两代 arXiv ID：
#   新式  2609.01234 / 2609.01234v2
#   老式  hep-th/9901001 / math.GT/0309136v1 / cs/0701001v1
ARXIV_ID_RE = re.compile(
    r"("
    r"\d{4}\.\d{4,5}(v\d+)?"                       # 新式
    r"|"
    r"[a-z][a-z\-]*(?:\.[A-Z]{2})?/\d{7}(v\d+)?"   # 老式（含 hep-th/9901001 形式）
    r")"
)


def normalize_arxiv_id(raw: str) -> str:
    """从任意输入里提取 arXiv ID，保留版本号。

    支持输入：'1603.01121'、'1603.01121v2'、'hep-th/9901001v1'、
              'http://arxiv.org/abs/1603.01121v1'、'arXiv:1603.01121'
    返回 '' 表示无法识别。

    ⚠️ 老式 ID 里含斜杠，所以不能只按「斜杠后的部分」取，必须用正则整体匹配。
    """
    m = ARXIV_ID_RE.search(raw or "")
    return m.group(1) if m else ""


def download_pdf(arxiv_id: str, out_dir: str = "downloads", email: str = "") -> dict:
    """方式 2：按 arXiv ID 下载免费全文 PDF。

    参数
    ----
    arxiv_id : '1603.01121'、'1603.01121v2'、'hep-th/9901001v1'（老式也支持）、
               甚至完整 URL（内部用 normalize_arxiv_id 抠出 ID）
    out_dir  : 保存目录，不存在会自动创建

    返回 {"status", "ok", "path", "bytes", "content_type", "error"}
      ★ `ok` 才是「是否真的拿到 PDF」的判据。
        `status` 只是 HTTP 状态码 —— 存在 **HTTP 200 但返回的是 HTML 错误页** 的情况
        （例如被 WAF 拦截、或 ID 不存在）。只看 status 会「假通过」，所以：
            ok=True  ⇔ status==200 且 body 以 %PDF- 开头 且 文件已落盘
      调用方请统一判 `ok`，不要判 `status`。

    用途：论文分析阶段的第一步 —— 有了 PDF 才能做结构解析（见 grobid.py）。
    注意：arXiv 论文几乎都有 PDF，所以它是"全文获取"的第一优先级来源。
    """
    clean = normalize_arxiv_id(arxiv_id)
    if not clean:
        return {"status": 400, "ok": False, "path": "", "bytes": 0,
                "content_type": "", "error": "无法从 '{}' 解析出 arXiv ID"
                                          "（新式 2609.01234 / 老式 hep-th/9901001 均可）".format(arxiv_id)}
    url = ARXIV_PDF.format(clean)
    out_dir = os.path.abspath(out_dir)
    os.makedirs(out_dir, exist_ok=True)
    # 老式 ID 含斜杠，作为文件名要替换掉
    path = os.path.join(out_dir, clean.replace("/", "_") + ".pdf")

    status, body, hdrs = http_request(url, timeout=HTTP_TIMEOUT, retries=1, email=email)
    ctype = hdrs.get("Content-Type", "")
    if status == 200 and body[:5] == b"%PDF-":
        with open(path, "wb") as fh:
            fh.write(body)
        return {"status": 200, "ok": True, "path": path, "bytes": len(body),
                "content_type": ctype, "error": ""}
    return {"status": status, "ok": False, "path": "", "bytes": len(body),
            "content_type": ctype,
            "error": "HTTP {} 但内容不是 PDF（content-type={}）：{}".format(
                status, ctype or "?", body[:120].decode("utf-8", "replace"))}


# =============================================================================
# 方式 3：OAI-PMH 批量采集
# =============================================================================

def oai_identify(timeout: float = 20.0) -> dict:
    """OAI-PMH Identify：拿仓库自述信息，用来确认服务可用。

    返回 {"status", "repository", "earliest", "deleted_policy", "error"}
      earliest 是库里最早一条记录的时间（实测 2005-09-16），
      可用来判断"我要的年份区间有没有覆盖"。
    """
    status, body, _ = http_request(url_with(ARXIV_OAI, {"verb": "Identify"}),
                                   timeout=timeout, retries=1)
    text = body.decode("utf-8", "replace")
    if status != 200:
        return {"status": status, "error": text[:200]}
    def grab(tag):
        m = re.search(r"<{}>(.*?)</{}>".format(tag, tag), text, re.S)
        return m.group(1).strip() if m else ""
    return {"status": 200, "repository": grab("repositoryName"),
            "earliest": grab("earliestDatestamp"),
            "deleted_policy": grab("deletedRecord"), "error": ""}


def oai_list_records(from_date: str = "", until_date: str = "",
                     metadata_prefix: str = "arXiv",
                     resumption_token: str = "",
                     timeout: float = 180.0) -> dict:
    """方式 3：取 OAI-PMH 的一页记录。

    参数
    ----
    from_date / until_date : 'YYYY-MM-DD'，按 **datestamp（记录入库/更新时间）** 过滤。
               ⚠️ 这**不是**论文的首次提交时间（created）：
                  一篇 2020 年提交、2024 年更新的论文，会出现在 2024 的窗口里。
                  所以它不能用来界定「某年发表的新论文」，只能用来做**增量同步**。
               这是切分大批量采集的主力：一个月装不下就按周、按天切。
               都不传 = 从头开始扫全库（巨慢，不要这么干）
    metadata_prefix        : 'arXiv'（arXiv 自己的 schema，最全，推荐）
                             'oai_dc'（都柏林核心，通用但字段少）
                             'arXivRaw'（含 LaTeX 摘要原文）——
                             ⚠️ **本模块尚未实现 arXivRaw 的解析**，传它会被显式拒绝
                             （避免静默返回空列表）
    resumption_token       : 上一页返回的分页 token。传了它就忽略 from/until/prefix
    timeout                : OAI 单页 4MB 左右，给足时间；慢的时候要 50s+

    返回 {"status", "ok", "records": [...], "token": str, "count": int,
          "complete": bool, "no_records": bool, "deleted": [...], "error": str}
      - `ok`          : 本次协议交互是否成功（**不要用 status==200 代替它**）
      - token 非空    : 还有下一页，把它原样传给 resumption_token 继续
      - token 为空    : 这个时间窗已取完（complete=True）
      - token 有时效（官方通常到当天 24:00）；过期时返回 `<error>badResumptionToken</error>`
      - `no_records`  : 该窗口没有记录 —— **这是正常结果，不是失败**
                        （OAI 会返回 <error>noRecordsMatch</error>，协议语义是「空集」）
      - `deleted`     : 带 status="deleted" 的 header 列表（撤回/删除的论文），
                        默认不混入 records，避免下游把已撤稿当成有效论文

    每条 record 的字段：id / title / abstract / authors / categories /
      created / updated / comments / journal_ref / doi
    注意：OAI 返回的是**元数据**，不含正文；正文要另走 PDF 下载。
    """
    # arXivRaw 的 schema 是 <arXivRaw>，本模块没有对应解析分支，
    # 若放行会得到 records=[] 而被误判为「空窗口」—— 所以直接拒绝。
    if metadata_prefix == "arXivRaw" and not resumption_token:
        return {"status": 400, "ok": False, "records": [], "token": "", "count": 0,
                "complete": False, "no_records": False, "deleted": [],
                "error": "metadata_prefix='arXivRaw' 尚未实现解析。"
                         "请改用 'arXiv' 或先实现 parse_oai_records 的 arXivRaw 分支"}

    params = {"verb": "ListRecords"}
    if resumption_token:
        params["resumptionToken"] = resumption_token
    else:
        params["metadataPrefix"] = metadata_prefix
        if from_date:
            params["from"] = from_date
        if until_date:
            params["until"] = until_date
    status, body, _ = http_request(url_with(ARXIV_OAI, params), timeout=timeout,
                                   retries=1, backoff=5.0)
    text = body.decode("utf-8", "replace")
    if status != 200:
        return {"status": status, "ok": False, "records": [], "token": "", "count": 0,
                "complete": False, "no_records": False, "deleted": [],
                "error": "HTTP {}: {}".format(status, text[:200])}
    if "<error" in text:
        # OAI 用 200 + <error> 表达错误。**HTTP 200 不代表协议成功**，
        # 必须按 error code 区分「正常空集」与「真失败」：
        #   noRecordsMatch      → 该窗口确实没有记录，是正常结果（ok=True, complete=True）
        #   badResumptionToken  → token 过期，必须重采该窗口（ok=False）
        #   badArgument / 其它  → 请求参数有问题（ok=False）
        # ⚠️ 早期实现把所有分支都写死 ok=True / complete=True，只改 error 文本 ——
        #    结果 token 过期会被上层当成「窗口已采完」，静默丢掉整个窗口的数据。
        m = re.search(r'<error[^>]*>(.*?)</error>', text, re.S)
        code_m = re.search(r'<error[^>]*code="([^"]+)"', text)
        err_code = code_m.group(1) if code_m else ""
        err_msg = (m.group(1).strip() if m else text[:150])
        is_empty = err_code == "noRecordsMatch" or "noRecordsMatch" in err_msg
        if is_empty:
            return {"status": 200, "ok": True, "records": [], "token": "", "count": 0,
                    "complete": True, "no_records": True, "deleted": [],
                    "error": "", "oai_error_code": err_code}
        return {"status": 200, "ok": False, "records": [], "token": "", "count": 0,
                "complete": False, "no_records": False, "deleted": [],
                "error": "OAI error[{}]: {}".format(err_code or "?", err_msg),
                "oai_error_code": err_code}
    records = parse_oai_records(body)
    deleted = parse_oai_deleted(body)
    if not records and not deleted and "<record" in text:
        # 有 record 元素却解析出 0 条 —— 多半是 metadataPrefix 的 schema 没覆盖到
        return {"status": 200, "ok": False, "records": [], "token": "", "count": 0,
                "complete": False, "no_records": False, "deleted": [],
                "error": "XML 含 <record> 但解析结果为 0 条，"
                         "可能是 metadataPrefix='{}' 的 schema 未实现".format(metadata_prefix)}
    m = re.search(r"<resumptionToken[^>]*>(.*?)</resumptionToken>", text, re.S)
    token = m.group(1).strip() if m else ""
    return {"status": 200, "ok": True, "records": records, "token": token,
            "count": len(records), "complete": not token, "no_records": False,
            "deleted": deleted,
            "cursor": (re.search(r'skip=(\d+)', token).group(1) if "skip=" in token else ""),
            "error": ""}


def parse_oai_deleted(xml_bytes: bytes) -> list:
    """抽出 status="deleted" 的记录头（撤回/删除的论文）。

    返回 [{"identifier": "oai:arXiv.org:2609.00001", "datestamp": "...", "sets": [...]}]

    为什么单独抽出来：OAI 的删除记录**没有 metadata**，只有 header。
    如果把它们和正常记录混在一起，下游会把「已撤稿」当成有效论文。
    删除记录有 `deletedRecord=persistent`（arXiv 的 Identify 返回值），
    意味着删除后仍会持续报告，所以增量同步时必须处理。
    """
    try:
        root = ET.fromstring(xml_bytes)
    except Exception:
        return []
    out = []
    for rec in root.iter("{%s}record" % NS["oai"]):
        header = rec.find("{%s}header" % NS["oai"])
        if header is None or header.get("status") != "deleted":
            continue
        out.append({
            "identifier": header.findtext("{%s}identifier" % NS["oai"], default=""),
            "datestamp": header.findtext("{%s}datestamp" % NS["oai"], default=""),
            "sets": [s.text for s in header.findall("{%s}setSpec" % NS["oai"]) if s.text],
        })
    return out


def parse_oai_records(xml_bytes: bytes) -> list:
    """把 OAI-PMH 的 XML 解析成结构化列表。

    兼容两种 schema：
      - metadataPrefix=arXiv   → <arXiv> 元素（字段最全）
      - metadataPrefix=oai_dc  → <dc> 元素（通用，字段少）

    ★ 每条记录都会带上 **OAI header 的 datestamp 与 identifier**：
        datestamp 是"这条元数据记录最后变更的时间"，是增量更新判定的唯一可靠依据
        （arXiv schema 的 updated 只在 arXiv 前缀下存在，oai_dc 根本没有）。
        不带它，下游就无法区分"同一条重取"和"同一条被更新"，只能按 id 一律丢弃，
        论文的修订版元数据就永远同步不进来。
    """
    root = ET.fromstring(xml_bytes)
    out = []
    for rec in root.iter("{%s}record" % NS["oai"]):
        # ---- OAI header（所有 schema 共有）----
        header = rec.find("{%s}header" % NS["oai"])
        identifier = datestamp = ""
        if header is not None:
            identifier = (header.findtext("{%s}identifier" % NS["oai"], default="") or "").strip()
            datestamp = (header.findtext("{%s}datestamp" % NS["oai"], default="") or "").strip()
        common = {"identifier": identifier, "datestamp": datestamp}

        arxiv_el = rec.find(".//{%s}arXiv" % NS["oai_arxiv"])
        if arxiv_el is not None:
            def g(tag):
                v = arxiv_el.findtext("{%s}%s" % (NS["oai_arxiv"], tag), default="")
                return " ".join(v.split()) if v else ""
            authors = []
            for a in arxiv_el.findall(".//{%s}author" % NS["oai_arxiv"]):
                name = " ".join(
                    filter(None, [(a.findtext("{%s}forenames" % NS["oai_arxiv"]) or "").strip(),
                                  (a.findtext("{%s}keyname" % NS["oai_arxiv"]) or "").strip()]))
                if name:
                    authors.append(name)
            # ⚠️ categories 在 OAI 里是**一个元素内含空格分隔的多个分类**：
            #      <categories>math.DG math.GR</categories>
            #    不是多个 <category> 子元素 —— 这点和 Export API 的 Atom 不同，
            #    解析错了会静默得到空列表。
            cats_text = arxiv_el.findtext("{%s}categories" % NS["oai_arxiv"], default="")
            cats = [c for c in re.split(r"[,\s]+", (cats_text or "").strip()) if c]
            out.append(dict({
                "id": g("id"), "title": g("title"), "abstract": g("abstract"),
                "authors": authors, "categories": cats,
                "created": g("created"), "updated": g("updated"),
                "comments": g("comments") or None,
                "journal_ref": g("journal-ref") or None,
                "doi": g("doi") or None,
            }, **common))
            continue
        dc = rec.find(".//{http://www.openarchives.org/OAI/2.0/oai_dc/}dc")
        if dc is not None:
            def d(tag):
                return [(" ".join((e.text or "").split())) for e in
                        dc.findall("{http://purl.org/dc/elements/1.1/}" + tag)]
            # oai_dc 没有 created/updated 概念，但 header 有 datestamp ——
            # 把它同时填进 created/updated，让版本比较在两种前缀下行为一致。
            out.append(dict({
                "id": (d("identifier") or [""])[0], "title": (d("title") or [""])[0],
                "abstract": (d("description") or [""])[0], "authors": d("creator"),
                "categories": [], "created": datestamp, "updated": datestamp,
                "comments": None, "journal_ref": None,
                "doi": next((x for x in d("identifier") if "doi" in x.lower()), None),
            }, **common))
    return out


def oai_harvest(from_date: str, until_date: str = "", max_records: int = 5000,
                metadata_prefix: str = "arXiv", sleep_between: float = 0.0,
                on_page=None, verbose: bool = False) -> dict:
    """方式 3 的完整封装：按时间窗把 OAI-PMH 一路翻到底。

    参数
    ----
    from_date / until_date : 采集窗口。**建议窗口不超过 1 个月**，
               否则单窗口页数太多，中途 token 过期就要重来
    max_records : 安全阀，最多采多少条就停（防止误传空窗口把全库拉下来）
    sleep_between : 两页之间额外 sleep 秒数。OAI 官方没有 Export API 那种
               3 秒硬要求，保持 0 也可以；被限流时再加
    on_page     : 回调 on_page(page_index, records, token)，
               用于**边采边落盘**（推荐做法，避免一次性占满内存）
    verbose     : 打印每页进度

    返回 {"status", "ok", "collected", "pages", "complete", "records",
          "deleted", "error"}

      ★ 三个状态字段的语义必须分清（这是最容易误判的地方）：
        ok        : 本次采集是否**没有出错**（协议失败、网络失败都算 False）
        complete  : 该窗口是否**已经翻到底**。只有 ok=True 且 token 耗尽才是 True
        判断"采完了吗"必须用 complete；判断"顺利吗"必须用 ok。
        ⚠️ 第一页就失败时 token 仍为空，若用 `complete = not token` 会**误报完成**。
        status    : 保留真实的 HTTP 状态码；失败时不会被伪造成 200 或 500

     工程建议：把 (from_date, until_date, resumptionToken, 已落盘条数) 存成断点，
     这样中途 token 过期时可以从窗口重新开始而不用重采已完成的部分。
     （完整实现见 arxiv_harvest.py）
    """
    collected, token, page = [], "", 0
    error, ok, status_code, deleted = "", True, 200, []
    exhausted = False          # 是否真的翻到了最后一页

    while True:
        res = oai_list_records(from_date=from_date, until_date=until_date,
                               metadata_prefix=metadata_prefix,
                               resumption_token=token)
        if not res.get("ok", res.get("status") == 200):
            ok, error = False, res.get("error", "") or "协议/网络失败"
            status_code = res.get("status", 0)
            break
        deleted.extend(res.get("deleted") or [])
        if res.get("no_records"):
            exhausted = True   # 空窗口 = 正常结束，不是失败
            break

        remaining = max_records - len(collected)
        if remaining <= 0:
            exhausted = False
            break
        page += 1
        # ★ 严格遵守 max_records：整页 extend 会超上限，
        #   多出来的部分宁可丢弃（调用方可以用 token 从这里续采）
        chunk = res["records"][:remaining]
        collected.extend(chunk)
        if verbose:
            print("    第 {} 页：{} 条（累计 {}）".format(page, res["count"], len(collected)))
        if len(chunk) < len(res["records"]):
            # 本页被 max_records 截断：不能跳到下一 token，否则会漏数据
            exhausted = False
            break
        if on_page:
            on_page(page, res["records"], res["token"])

        token = res["token"]
        if not token:
            exhausted = True
            break
        if sleep_between:
            time.sleep(sleep_between)

    return {"status": status_code, "ok": ok, "collected": len(collected),
            "pages": page, "complete": ok and exhausted,
            "records": collected, "deleted": deleted, "error": error}


# =============================================================================
# 方式 4：S3 全量全文（说明 + 可选实现）
# =============================================================================

def s3_bulk_check(timeout: float = 20.0) -> dict:
    """方式 4：确认 S3 桶状态。**不需要 AWS 凭证**，用匿名访问看返回码即可。

    返回 {"status", "requester_pays": bool, "message": str}
      匿名返回 403 + "Requester Pays" ⇒ 桶存在，但下载要 AWS 凭证且流量付费

    真要下载时的做法（本函数不实现，避免误触发计费）：
      aws s3 cp s3://arxiv/pdf/arXiv_pdf_2401_001.tar . \\
          --request-payer requester --region us-east-1
    """
    url = "https://{}.s3.amazonaws.com/?list-type=2&max-keys=1".format(ARXIV_S3_BUCKET)
    status, body, _ = http_request(url, timeout=timeout, retries=0)
    text = body.decode("utf-8", "replace")
    if status == 403 and "Requester Pays" in text:
        return {"status": status, "requester_pays": True,
                "message": "桶存在且为 requester-pays：需 AWS 凭证 + 自付流量费"}
    if status == 200:
        return {"status": status, "requester_pays": False,
                "message": "意外地可匿名访问，需复核官方文档"}
    return {"status": status, "requester_pays": None, "message": text[:200]}


def s3_download(month: str, chunk: int = 1, kind: str = "pdf",
                out_dir: str = "downloads", profile: str = "") -> dict:
    """方式 4：用 AWS CLI 下载一个整月分片（会**产生流量费用**，默认不调用）。

    参数
    ----
    month  : 'YYMM'，例如 '2401' 表示 2024-01
    chunk  : 分片号，从 1 开始。用 aws s3 ls 列表看有哪些分片
    kind   : 'pdf'（PDF 打包）或 'src'（LaTeX 源文件）
    profile: AWS CLI profile 名；留空用默认凭证链

    返回 {"status", "ok", "path", "key", "cmd", "error"}
      key 形如 `pdf/arXiv_pdf_2401_002.tar` —— 由 kind + month + chunk 三段拼成。
      ⚠️ 拼接时必须三段都用到：早期版本占位符数量写错，chunk 被静默丢弃，
         结果所有分片都指向 _001.tar。

    底层调用 aws 命令，需先安装并配置 AWS CLI。
    """
    import subprocess
    # 三段：目录=kind、文件名前缀=kind、月份=month、分片=chunk（3 位补零）
    key = "{0}/arXiv_{0}_{1}_{2:03d}.tar".format(kind, month, int(chunk))
    out_dir = os.path.abspath(out_dir)
    os.makedirs(out_dir, exist_ok=True)
    cmd = ["aws", "s3", "cp", "s3://{}/{}".format(ARXIV_S3_BUCKET, key), out_dir,
           "--request-payer", "requester", "--region", ARXIV_S3_REGION]
    if profile:
        cmd += ["--profile", profile]
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              timeout=3600)
        return {"status": proc.returncode, "ok": proc.returncode == 0,
                "path": os.path.join(out_dir, os.path.basename(key)), "key": key,
                "cmd": " ".join(cmd),
                "error": "" if proc.returncode == 0 else proc.stdout.decode("utf-8", "replace")[:300]}
    except FileNotFoundError:
        return {"status": 127, "ok": False, "path": "", "key": key, "cmd": " ".join(cmd),
                "error": "未安装 aws CLI（brew install awscli）"}
    except Exception as exc:
        return {"status": 1, "ok": False, "path": "", "key": key, "cmd": " ".join(cmd),
                "error": str(exc)}


# =============================================================================
# 附录：把 arXiv 结果喂给下游的辅助函数
# =============================================================================

def to_internal_paper(entry: dict, provenance: dict = None) -> dict:
    """把 arXiv 记录转成检索模块内部的 Paper 结构（对应）。

    ⚠️ 必须同时接受**两种输入**，因为 arXiv 的检索和批量走的是不同通道、
       字段名完全不同。只认其中一种会导致另一种静默丢字段：

        Export API（Atom）   arxiv_id / summary / published / comment / journal_ref / pdf_url
        OAI-PMH              id / abstract / created / updated / comments / journal-ref

    统一后的字段映射：
        arxiv_id  ← arxiv_id | id            （OAI 的 id 就是 arXiv ID）
        abstract  ← summary  | abstract      （**两边的键名不一样，这是最常踩的坑**）
        published ← published | created
        updated   ← updated  | updated
        venue     ← journal_ref | journal_ref | journal-ref
        comment   ← comment  | comments

    参数
    ----
    entry      : 来自 search()/parse_atom 或 oai_list_records 的字典
    provenance : 溯源信息，如 {"query": "...", "window": "2026-09-01/02", "source": "oai"}
                 ★ 建议一定传 ——「Search Metadata」要求记录
                   「这篇论文是怎么找到的」，否则事后无法解释它为何进入语料库

    返回统一的 Paper 字典。
    """
    def pick(*keys):
        for k in keys:
            v = entry.get(k)
            if v not in (None, "", []):
                return v
        return None

    arxiv_id = pick("arxiv_id", "id") or ""
    # Atom 的 pdf_url 是直链；OAI 没有，用 ID 现推一个
    pdf_url = pick("pdf_url")
    if not pdf_url and arxiv_id:
        pdf_url = ARXIV_PDF.format(arxiv_id)

    found_by = [{"source": "arxiv", "kind": "search"}]
    if provenance:
        found_by = [dict({"source": "arxiv"}, **provenance)]

    return {
        "identity": {
            "arxiv_id": arxiv_id,
            "doi": pick("doi"),
            "openalex_id": None,
            "s2_id": None,
            "openreview_id": None,
        },
        "metadata": {
            "title": pick("title"),
            "authors": pick("authors") or [],
            "published": pick("published", "created"),
            "updated": pick("updated"),
            "categories": pick("categories") or [],
            "primary_category": pick("primary_category"),
            "venue": pick("journal_ref", "journal-ref"),
            "comment": pick("comment", "comments"),
        },
        "abstract": pick("summary", "abstract"),
        "full_text": {"pdf_url": pdf_url, "locations": []},
        "search_meta": {"found_by": found_by},
    }


# =============================================================================
# 自检与命令行入口
# =============================================================================

def check(email: str = "") -> int:
    """依次验证四个通道，返回失败数（0 = 全通）。"""
    failed = 0

    section("1) Export API 检索")
    r = search('all:"counterfactual regret minimization"', max_results=3, email=email)
    kv("状态", "HTTP {}".format(r["status"]))
    kv("命中总数", r.get("total"))
    kv("本次返回", r["count"])
    kv("尝试记录", r.get("attempts"))
    if r["status"] != 200 or not r["entries"]:
        kv("错误", r.get("error"))
        failed += 1
    else:
        show_records(r["entries"], ["arxiv_id", "published", "categories", "doi"])
        kv("用途", "候选论文发现 + 摘要初筛 + DOI 交给 Crossref 做实体统一")
        kv("注意", "start+max_results 不得超过 {}".format(MAX_TOTAL_RESULTS))

    section("2) PDF 下载")
    p = download_pdf("1603.01121", out_dir=os.path.join(os.path.dirname(__file__), "downloads"),
                     email=email)
    kv("状态", "HTTP {}".format(p["status"]))
    kv("大小", "{} bytes".format(p["bytes"]))
    kv("路径", p["path"] or p["error"])
    if p["status"] != 200:
        failed += 1

    section("3) OAI-PMH 批量通道")
    ident = oai_identify()
    kv("Identify", "HTTP {} | 最早记录 {} | 删除策略 {}".format(
        ident["status"], ident.get("earliest"), ident.get("deleted_policy")))
    if ident["status"] != 200:
        failed += 1
    res = oai_list_records(from_date="2026-09-01", until_date="2026-09-02")
    kv("ListRecords", "HTTP {} | 本页 {} 条 | 还有下一页: {}".format(
        res["status"], res["count"], bool(res["token"])))
    if res["records"]:
        show_records(res["records"], ["id", "created", "categories"])
    else:
        kv("错误", res.get("error"))
        failed += 1

    section("4) S3 全量全文（requester-pays）")
    s3 = s3_bulk_check()
    kv("状态", "HTTP {} | requester_pays={}".format(s3["status"], s3["requester_pays"]))
    kv("说明", s3["message"])

    print("\n" + "-" * 78)
    print("arXiv 四通道自检：{}".format("全部通过" if failed == 0 else "{} 项失败".format(failed)))
    return failed


def main(argv) -> int:
    email = get_key("PAPERSEARCH_EMAIL")
    if not argv or argv[0] == "check":
        return check(email)

    cmd = argv[0]
    if cmd == "search":
        if len(argv) < 2:
            print("用法: python3 arxiv.py search \"检索式\" [--max N] [--start N] [--sort relevance|submittedDate]")
            return 2
        query = argv[1]
        max_n = int(argv[argv.index("--max") + 1]) if "--max" in argv else 20
        start = int(argv[argv.index("--start") + 1]) if "--start" in argv else 0
        sort = argv[argv.index("--sort") + 1] if "--sort" in argv else "relevance"
        r = search(query, max_results=max_n, start=start, sort_by=sort, email=email)
        section("arXiv 检索: {}".format(query))
        kv("状态", "HTTP {}".format(r["status"]))
        kv("命中总数", r.get("total"))
        if r["status"] == 200:
            for e in r["entries"][:max_n]:
                print("\n  ▶ {}".format(e["title"]))
                print("    arXiv ID : {}   分类: {}".format(
                    e["arxiv_id"], ",".join(e["categories"][:4])))
                print("    提交时间 : {}   作者数: {}".format(
                    e["published"][:10], len(e["authors"])))
                print("    DOI      : {}".format(e["doi"] or "（无）"))
                print("    摘要     : {}…".format(e["summary"][:100]))
        else:
            kv("错误", r["error"])
            return 1
        return 0

    if cmd == "paged":
        query = argv[1]
        limit = int(argv[argv.index("--limit") + 1]) if "--limit" in argv else 500
        r = search_paged(query, limit=limit, email=email)
        section("arXiv 翻页采集: {}".format(query))
        kv("实采", r["collected"])
        kv("被深分页上限截断", r["truncated"])
        return 0

    if cmd == "pdf":
        if len(argv) < 2:
            print("用法: python3 arxiv.py pdf 1603.01121 [--out downloads]")
            return 2
        out = argv[argv.index("--out") + 1] if "--out" in argv else \
            os.path.join(os.path.dirname(__file__), "downloads")
        p = download_pdf(argv[1], out_dir=out, email=email)
        section("arXiv PDF")
        kv("状态", "HTTP {}".format(p["status"]))
        kv("路径", p["path"] or p["error"])
        kv("大小", "{} bytes".format(p["bytes"]))
        return 0 if p["status"] == 200 else 1

    if cmd == "oai":
        r = oai_list_records(from_date="2026-09-01", until_date="2026-09-02")
        section("OAI-PMH 单页")
        kv("条数", r["count"])
        kv("下一页 token", "有" if r["token"] else "无（已取完）")
        show_records(r["records"], ["id", "created", "categories"])
        return 0

    if cmd == "oai-window":
        if len(argv) < 2:
            print("用法: python3 arxiv.py oai-window 2026-09-01 [2026-09-08] [--max N]")
            return 2
        frm = argv[1]
        unt = argv[2] if len(argv) > 2 and not argv[2].startswith("--") else ""
        mx = int(argv[argv.index("--max") + 1]) if "--max" in argv else 5000
        r = oai_harvest(frm, unt, max_records=mx, verbose=True)
        section("OAI-PMH 窗口采集 {} → {}".format(frm, unt or "现在"))
        kv("总计", r["collected"])
        kv("页数", r["pages"])
        kv("窗口已取完", r["complete"])
        return 0

    if cmd == "s3":
        section("S3 全量全文")
        kv("检查", s3_bulk_check())
        print("  下载示例（会产生流量费）:")
        print("    aws s3 cp s3://arxiv/pdf/arXiv_pdf_2401_001.tar . \\")
        print("        --request-payer requester --region us-east-1")
        return 0

    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
