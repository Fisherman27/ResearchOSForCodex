#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
arxiv_harvest.py —— arXiv OAI-PMH 增量采集器（批量 metadata 落盘 + 断点续采）

定位 / 用途
─────────────────────────────────────────────────────────────────────────────
`arxiv.py` 提供的是**单次请求**能力（oai_identify / oai_list_records /
parse_oai_records），它不负责「把几年数据完整拉下来」。本文件补上这一层，是
整个论文检索 skill 的**核心采集工具**：

    规划日期窗口 → 逐窗口逐页拉取 → 边采边落盘 JSONL → 每页写 checkpoint
    → 中断可续采 → 采完出统计 → 可校验产出完整性

它做对的几件"容易踩坑"的事：
  1. 窗口按 datestamp 切，相邻窗口不重叠（前一个 until = 后一个 from 的前一天）
  2. resumptionToken 断点精确到「窗口 + 页」，中断后从**那一页**继续，不是重头
  3. token 过期（badResumptionToken）→ 丢弃 token 从当前窗口开头重采 + 去重兜底
  4. 空窗口（noRecordsMatch）标记 done 而不是 failed
  5. 窗口过大（首页就返回满页 + 还有 token）→ 自动二分切分成更小的窗口
  6. 每页落盘后立刻写 checkpoint，崩溃最多只丢一页

⚠️ 关键语义（必读）
─────────────────────────────────────────────────────────────────────────────
【语义 1】OAI 的 from/until 过滤的是 **datestamp（记录入库/更新时间）**，
          **不是** 论文的 created（首次提交时间）。

    一条 2020 年提交、2024 年做过修订的论文，它的 datestamp 是 2024 年，
    因此会出现在 2024 年的采集窗口里。直接后果有两條，都必须记住：

      (a) 按窗口采全会**必然产生重复** —— 同一篇论文只要被更新过，就会在
          多个窗口里各出现一次。所以本模块默认开启去重（dedupe=True），
          以 arXiv id 为键，重复记录计入 `duplicates` 而不再写盘。
      (b) **不能用窗口年份反推「某年的新论文」**。想要"2024 年新提交的论文"，
          要读记录里的 `created` 字段（本模块 stats() 的 by_year 就是按
          created 分组的），而不是按采集窗口的年份。

    换句话说：datestamp 窗口只适合做 **增量同步**（"上次同步之后有哪些变化"），
    不适合做"按年切片统计发表量"。本模块的 `--since-last` 就是干这个的。

【语义 2】resumptionToken 有有效期（通常到当天 24:00）。
    token 过期时 OAI **不是**返回 4xx/5xx，而是 HTTP 200 + `<error
    code="badResumptionToken">`。本模块的处理是：丢弃这个 token，**从当前窗口
    的开头重新采**（state.windows[i].token 清空）。因为已经写的记录会被去重跳过，
    重复写入是安全的，只是会多花一点流量。单窗口内重启次数上限 max_restarts，
    超过则把窗口标记 failed，交给下一次 resume 重试。

【语义 3】noRecordsMatch 是**正常空窗口**，不是失败。
    OAI 语义里它就是"这个集合是空的"。本模块把它标记为 done（records=0），
    绝不记为 error，否则 --since-last 每次都会卡在一个空窗口上。

【语义 4】窗口可能被服务端截断：单页约 1300 条 + resumptionToken 翻页。
    如果第一页就返回满页且带 token，说明这个窗口太大、页数太多，
    中途一旦 token 过期就要从头重来（代价高）。此时自动把窗口**二分**成
    两个更小的窗口分别采集（split_threshold，默认 1200；可 --split-threshold 调）。

【语义 5】metadataPrefix=arXivRaw 本模块**明确拒绝**。
    arxiv.parse_oai_records 只实现了 `arXiv` 和 `oai_dc` 两种 schema 的解析，
    传 arXivRaw 会解析出空列表 —— 那会被误判成"空窗口"，静默产出 0 条数据。
    所以 validate_prefix() 在发请求之前就报错，绝不静默。

命令行用法
─────────────────────────────────────────────────────────────────────────────
    # 1) 只规划窗口，不发任何请求（--dry-run 亦可）
    python3 arxiv_harvest.py plan --start 2026-08-01 --end 2026-09-01 --days 30

    # 2) 采集（默认写到脚本同目录 harvest.jsonl，断点在 harvest.jsonl.state.json）
    python3 arxiv_harvest.py run --start 2026-08-01 --end 2026-09-01

    # 3) 小规模试采 + 只看某一 set（183 个 set，格式是 cs:cs:AI 这种冒号分隔）
    python3 arxiv_harvest.py run --start 2026-09-01 --end 2026-09-02 \
        --max-records 10 --out test_harvest.jsonl
    python3 arxiv_harvest.py run --start 2026-09-01 --end 2026-09-02 --set cs:cs:AI

    # 4) 增量：读 checkpoint 的 last_success_until，采「那之后到现在」
    python3 arxiv_harvest.py run --since-last

    # 5) 中断后续采（从 checkpoint 的窗口 + token 继续）
    python3 arxiv_harvest.py resume

    # 6) 看断点 / 看统计 / 校验产出
    python3 arxiv_harvest.py status
    python3 arxiv_harvest.py stats  --out harvest.jsonl
    python3 arxiv_harvest.py verify --out harvest.jsonl

    # 7) 其它
    python3 arxiv_harvest.py sets            # 列 OAI set（验证 --set 的取值格式）
    python3 arxiv_harvest.py check           # 离线自检（不联网）
    python3 arxiv_harvest.py check --online  # 额外探一次网络

常用参数：
    --start/--end YYYY-MM-DD   采集区间（含首尾）
    --days N                   窗口长度，默认 30 天
    --out PATH                 落盘文件，.gz 或 --gzip 走 gzip
    --state PATH               断点文件，默认 <out>.state.json
    --set SPEC                 OAI set，如 cs:cs:AI
    --metadata-prefix P        arXiv | oai_dc（arXivRaw 会被拒绝）
    --max-records N            全局硬上限，严格遵守、绝不超出
    --max-pages N              全局页数上限
    --sleep S                  页间礼貌限速，默认 1.0 秒
    --since-last               从 last_success_until 的下一天采到现在
    --dry-run                  只规划并打印，不发请求
    --gzip / --no-dedupe / --stop-on-error

─────────────────────────────────────────────────────────────────────────────
函数索引
─────────────────────────────────────────────────────────────────────────────
  [规划]  plan_windows(start_date, end_date, days=30)
          把日期区间切成不重叠的采集窗口 → [(from, until), ...]
  [规划]  validate_prefix(metadata_prefix)
          发请求前校验 metadataPrefix，arXivRaw 明确拒绝而不是静默出空
  [工具]  parse_date(s) / fmt_date(d) / today_str() / clamp_until(until, today)
          宽松解析/格式化日期；把未来的 until 夹到今天（否则 badArgument）
  [断点]  HarvestState.load() / .save() / .next_pending_window()
          读写 checkpoint（原子写：.tmp + os.replace），断点粒度到窗口+token
  [断点]  HarvestState.mark() / .split_window() / .progress() / .note_last_success()
          改窗口状态 / 大窗口二分 / 看完成率 / 推进 last_success_until
  [分页]  oai_page(from, until, prefix, token, set_spec)
          取一页 OAI 记录（支持 set；失败自动 curl 兜底；返回结构与 arxiv.py 对齐）
  [分页]  fetch_page(...)
          在 oai_page 之上加重试与指数退避；协议错误不重试
  [分页]  list_sets(max_pages)
          列 OAI ListSets，用于确认 --set 的 setSpec 写法
  [落盘]  RecordWriter.write(records, window, page, ...) / .close()
          JSONL 追加写 + 去重 + gzip；每行带 _harvest 元数据（窗口/时刻/set）
  [采集]  harvest(...)
          主循环：逐窗口逐页拉取、边写盘边存 checkpoint、支持 max_records/max_pages
  [增量]  resolve_since_last(state_path)
          读 checkpoint 的 last_success_until，算出「增量起点」
  [统计]  stats(path, state_path) / HarvestState.progress()
          总数/去重后/primary category 分布/按年分布/窗口完成率
  [校验]  verify_output(path)
          流式校验 JSONL（坏行/重复 id/缺 id/缺 _harvest），不整文件读进内存
  [自检]  check(online=False)
          离线自检：切窗正确性、断点推进、去重落盘、坏行检出
  [命令行] main(argv)
          plan / run / resume / status / stats / verify / sets / check
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import datetime as _dt
import gzip
import json
import os
import re
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import (http_request, url_with, section, kv, get_key,
                     curl_get, curl_available)
import arxiv

# =============================================================================
# 常量：改这里就能适配不同环境
# =============================================================================

_HERE = os.path.dirname(os.path.abspath(__file__))

# 默认产物都放在脚本同目录，避免污染调用方的 cwd
DEFAULT_OUT = os.path.join(_HERE, "harvest.jsonl")

DEFAULT_WINDOW_DAYS = 30        # 单个采集窗口的默认长度（天）
DEFAULT_PREFIX = "arXiv"        # 字段最全的 schema
DEFAULT_SLEEP = 1.0             # 页与页之间的礼貌限速（秒）
DEFAULT_TIMEOUT = 180.0         # OAI 单页约 4MB，给足时间
DEFAULT_RETRIES = 3             # 网络失败重试次数（指数退避）
DEFAULT_BACKOFF = 3.0           # 退避基数：第 n 次等 backoff * 2**(n-1)
DEFAULT_SPLIT_THRESHOLD = 1200  # 首页条数 ≥ 此值且还有 token ⇒ 窗口太大，二分
DEFAULT_MAX_SPLITS = 6          # 单个窗口最多二分几次（30d→1d 只需 5 次）
DEFAULT_MAX_RESTARTS = 3        # 单窗口内因 token 过期重启的次数上限
DEFAULT_SPLIT_MIN_DAYS = 2      # 窗口至少这么多天（含首尾）才允许二分；单日窗口切不动
OAI_PAGE_FALLBACK = 1300        # arXiv 单页实测条数，仅用于估算

# 本模块能真正采到的 schema。parse_oai_records 只实现了这两支。
SUPPORTED_PREFIXES = ("arXiv", "oai_dc")
# 仓库支持但本模块解析不了的：必须显式拒绝，不能静默产出空数据
KNOWN_UNSUPPORTED_PREFIXES = ("arXivRaw",)

# OAI 协议错误的识别关键字
ERR_NO_RECORDS = "noRecordsMatch"
ERR_BAD_TOKEN = "badResumptionToken"
ERR_BAD_ARGUMENT = "badArgument"

# 采集中断的原因（写进返回值，供调用方判断"为什么没采完"）
STOP_MAX_RECORDS = "max_records"
STOP_MAX_PAGES = "max_pages"
STOP_ERROR = "error"


# =============================================================================
# 一、日期工具
# =============================================================================

def parse_date(text: str) -> _dt.date:
    """把各种常见的日期写法解析成 datetime.date。

    参数
    ----
    text : 支持 'YYYY-MM-DD' / 'YYYY/MM/DD' / 'YYYYMMDD' / 'YYYY-MM'（当月 1 号）
           / 'YYYY'（当年 1 月 1 号） / 'today' / 'now' / 'yesterday'。
           首尾空白会被忽略。

    返回
    ----
    datetime.date

    异常
    ----
    ValueError : 无法识别、或日期本身非法（例如 2026-02-30）

    用途：CLI 参数、checkpoint 里的日期、--since-last 的推算都走这里，
    保证整个模块只有一种日期解析口径。
    """
    if text is None:
        raise ValueError("日期为空")
    s = str(text).strip()
    if not s:
        raise ValueError("日期为空")
    low = s.lower()
    today = _dt.date.today()
    if low in ("today", "now"):
        return today
    if low == "yesterday":
        return today - _dt.timedelta(days=1)
    s = s.replace("/", "-").replace(".", "-")
    m = re.fullmatch(r"(\d{4})(\d{2})(\d{2})", s)
    if m:
        return _dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return _dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = re.fullmatch(r"(\d{4})-(\d{1,2})", s)
    if m:
        return _dt.date(int(m.group(1)), int(m.group(2)), 1)
    m = re.fullmatch(r"(\d{4})", s)
    if m:
        return _dt.date(int(m.group(1)), 1, 1)
    raise ValueError("无法解析日期 '{}'（支持 YYYY-MM-DD / YYYY-MM / YYYY / today）".format(text))


def fmt_date(d: _dt.date) -> str:
    """把 datetime.date 格式化成 OAI 要的 'YYYY-MM-DD'。"""
    return d.strftime("%Y-%m-%d")


def today_str() -> str:
    """返回今天（本地时区）的 'YYYY-MM-DD'。"""
    return fmt_date(_dt.date.today())


def clamp_until(until: str, today: str = "", verbose: bool = True) -> str:
    """把晚于今天的 until 夹到今天。

    为什么需要：arXiv OAI 对「until 超过今天」返回的是
        <error code="badArgument">... Reason: until date too late</error>
    也就是整个窗口直接失败。与其让最后一个窗口必然报错，不如夹到今天并提示。

    参数
    ----
    until   : 'YYYY-MM-DD'
    today   : 参考「今天」，留空取系统日期（测试时可注入固定值）
    verbose : 发生夹取时是否打印提示

    返回：夹取后的 'YYYY-MM-DD'；不合法或无需夹取时原样返回。
    """
    try:
        u = parse_date(until)
    except ValueError:
        return until
    t = parse_date(today) if today else _dt.date.today()
    if u > t:
        if verbose:
            print("  ⚠️ until={} 晚于今天 {}，已自动夹到今天（否则 OAI 返回 badArgument）"
                  .format(until, fmt_date(t)))
        return fmt_date(t)
    return until


# =============================================================================
# 二、窗口规划
# =============================================================================

def plan_windows(start_date: str, end_date: str = "", days: int = DEFAULT_WINDOW_DAYS):
    """把日期区间切成**不重叠**的采集窗口。

    参数
    ----
    start_date : 区间起点，'YYYY-MM-DD'（也接受 YYYYMMDD / YYYY-MM / YYYY）
    end_date   : 区间终点（含）。留空视为与 start_date 同一天
    days       : 每个窗口的长度（天），默认 30。必须 ≥ 1

    返回
    ----
    [(from_str, until_str), ...]  ← 每个元素是元组，两端都是 'YYYY-MM-DD'
      * 第一个窗口的 from = start_date，最后一个窗口的 until = end_date
      * 窗口含首尾，长度 = days 天（最后一个窗口可能不足 days 天）
      * **相邻窗口不重叠**：后一个 from = 前一个 until + 1 天
      * start_date > end_date 时返回 []（不抛异常，交给调用方判断）

    异常
    ----
    ValueError : 日期无法解析，或 days < 1

    用途：
      - CLI `plan` 直接展示将要采哪些窗口；
      - `harvest()` 用它生成 checkpoint 里的 windows 列表；
      - 想知道"这堆日期要发多少次首页请求"时，len(plan_windows(...)) 即答案。

    例：plan_windows("2026-08-01", "2026-09-01", days=30)
        → [("2026-08-01","2026-08-30"), ("2026-08-31","2026-09-01")]
    """
    if days is None or int(days) < 1:
        raise ValueError("days 必须 ≥ 1，当前 {}".format(days))
    days = int(days)
    start = parse_date(start_date)
    end = parse_date(end_date) if end_date else start
    if start > end:
        return []

    out = []
    cur = start
    while cur <= end:
        # 含首尾的 days 天窗口：cur .. cur + (days-1)
        stop = cur + _dt.timedelta(days=days - 1)
        if stop > end:
            stop = end
        out.append((fmt_date(cur), fmt_date(stop)))
        cur = stop + _dt.timedelta(days=1)   # 下一个窗口从"下一天"开始 ⇒ 不重叠
    return out


def validate_prefix(metadata_prefix: str):
    """在发请求之前校验 metadataPrefix。

    参数
    ----
    metadata_prefix : 'arXiv' / 'oai_dc' / 'arXivRaw' / 任意字符串

    返回
    ----
    (ok: bool, message: str)
      ok=False 时 message 是给用户看的中文原因。ok=True 时 message 为 ""。

    为什么必须要有这一步：
      arxiv.parse_oai_records 只实现了 `arXiv` 与 `oai_dc` 两种 schema。
      传 `arXivRaw` 会解析出 `records=[]`，若上游把它当成"空窗口"就会**静默
      产出 0 条数据**（最难排查的一类 bug）。所以这里在采集前直接拒绝。
    """
    p = (metadata_prefix or "").strip()
    if p in SUPPORTED_PREFIXES:
        return True, ""
    if p in KNOWN_UNSUPPORTED_PREFIXES:
        return False, ("metadataPrefix='{}' 无法采集：本模块（以及 arxiv.parse_oai_records）"
                       "尚未实现该 schema 的解析分支，继续下去只会解析出空列表。"
                       "请改用 --metadata-prefix arXiv。".format(p))
    return False, ("未知的 metadataPrefix='{}'；目前可用：{}。"
                   "（arXivRaw 已被显式排除）".format(p, " / ".join(SUPPORTED_PREFIXES)))


# =============================================================================
# 三、断点 / checkpoint
# =============================================================================

def _now_iso() -> str:
    """本地时间的 ISO 字符串（秒级），用于 created_at / updated_at / _harvest。"""
    return _dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _new_window(frm: str, until: str, depth: int = 0) -> dict:
    """造一个状态为 pending 的窗口字典（HarvestState.windows 的元素结构）。"""
    return {
        "from": frm,
        "until": until,
        "status": "pending",     # pending / in_progress / done / failed
        "token": "",             # 断点核心：下一页的 resumptionToken；空 = 还没开始或已到窗口尾
        "records": 0,            # 本窗口**实际写盘**的条数（已去重）
        "raw_records": 0,        # 本窗口从服务端收到的原始条数（含被去重的）
        "duplicates": 0,         # 本窗口被去重跳过的条数
        "pages": 0,              # 本窗口已完成的页数
        "attempts": 0,           # 本窗口发起的取页请求次数（每页 +1，含触发上限那次）
        "restarts": 0,           # 因 badResumptionToken 从窗口头重采的次数
        "depth": depth,          # 二分深度：0 = 原始窗口
        "error": "",
        "started_at": "",
        "finished_at": "",
        "last_page_at": "",
    }


class HarvestState:
    """OAI 增量采集的断点（checkpoint）。

    定位：把「采到哪儿了」落成一个 JSON 文件，让整个采集可以随时 Ctrl-C，
    下一次 `resume` 从**中断的那一页**继续，而不是重头再来。

    字段（对应 save() 写出的 JSON）
    ----
    created_at / updated_at : 创建/最后更新时间（本地 ISO 字符串）
    start / end             : 本次采集覆盖的日期区间
    window_days             : 规划时用的窗口长度
    metadata_prefix         : schema（arXiv / oai_dc）
    set_spec                : OAI set（空 = 全库）
    windows                 : 窗口列表，每项见 _new_window() 的注释
    last_success_until      : **已完全采完**的最大窗口 until；--since-last 的起点
    stats                   : 累计计数 {pages, records, duplicates, errors, restarts, requests}

    用法
    ----
        st = HarvestState.load("harvest.jsonl.state.json")
        idx, win = st.next_pending_window()      # 找到下一个没采完的窗口
        st.mark(idx, status="in_progress")
        st.save()                                # 原子写

    设计取舍：
      - 断点粒度是「窗口 + resumptionToken」，所以恢复时能精确定位到某一页；
      - save() 用 .tmp + os.replace 原子替换，避免写一半断电得到半截 JSON；
      - 一个窗口失败只标记该窗口 failed，其它窗口照常推进（resume 时重试）。
    """

    STATUSES = ("pending", "in_progress", "done", "failed")

    def __init__(self, data: dict = None):
        """从字典构造；data 为空表示造一个空 state（通常随后用 from_plan 填）。"""
        data = data or {}
        self.created_at = data.get("created_at") or _now_iso()
        self.updated_at = data.get("updated_at") or self.created_at
        self.start = data.get("start", "")
        self.end = data.get("end", "")
        self.window_days = int(data.get("window_days") or DEFAULT_WINDOW_DAYS)
        self.metadata_prefix = data.get("metadata_prefix") or DEFAULT_PREFIX
        self.set_spec = data.get("set_spec") or ""
        self.windows = [_new_window(w.get("from", ""), w.get("until", ""))
                        for w in (data.get("windows") or [])]
        # 保留窗口里除基础字段外的全部内容（token/records/status/... 都在这里恢复）
        for w, raw in zip(self.windows, (data.get("windows") or [])):
            for k, v in raw.items():
                w[k] = v
        self.last_success_until = data.get("last_success_until", "")
        self.stats = data.get("stats") or {
            "pages": 0, "records": 0, "duplicates": 0,
            "errors": 0, "restarts": 0, "requests": 0,
        }
        self.path = ""       # 运行时记住文件位置；不参与序列化

    # ---- 构造 -----------------------------------------------------------------

    @classmethod
    def from_plan(cls, start: str, end: str, plan, window_days: int = DEFAULT_WINDOW_DAYS,
                  metadata_prefix: str = DEFAULT_PREFIX, set_spec: str = "") -> "HarvestState":
        """用 plan_windows() 的结果造一个新 state（所有窗口 pending）。

        参数
        ----
        start / end     : 区间（写进 state 便于审计）
        plan            : plan_windows() 返回的 [(from, until), ...]
        window_days     : 记下当时用的窗口长度
        metadata_prefix : schema
        set_spec        : OAI set

        返回 HarvestState（未落盘，需要自己 save()）。
        """
        st = cls({
            "start": start, "end": end, "window_days": window_days,
            "metadata_prefix": metadata_prefix, "set_spec": set_spec,
        })
        st.windows = [_new_window(f, u) for f, u in plan]
        st.updated_at = _now_iso()
        return st

    # ---- 读写 -----------------------------------------------------------------

    @classmethod
    def load(cls, path: str) -> "HarvestState":
        """从 JSON 文件读回 checkpoint。

        参数
        ----
        path : state 文件路径（不存在会抛 FileNotFoundError）

        返回 HarvestState（其 .path 已设成该路径，之后 save() 不传参即可）

        异常
        ----
        FileNotFoundError : 文件不存在（用 try_load() 可拿到 None 而不抛）
        ValueError        : 文件存在但不是合法 JSON / 不是对象
        """
        if not os.path.isfile(path):
            raise FileNotFoundError("checkpoint 不存在: {}".format(path))
        with open(path, "r", encoding="utf-8") as fh:
            try:
                data = json.load(fh)
            except Exception as exc:
                raise ValueError("checkpoint 不是合法 JSON（{}）: {}".format(exc, path))
        if not isinstance(data, dict):
            raise ValueError("checkpoint 顶层必须是对象: {}".format(path))
        st = cls(data)
        st.path = os.path.abspath(path)
        return st

    @classmethod
    def try_load(cls, path: str):
        """load() 的"不抛异常"版本：读不了就返回 None（CLI 里用来做存在性判断）。"""
        try:
            return cls.load(path)
        except Exception:
            return None

    def save(self, path: str = "") -> str:
        """把 checkpoint **原子地**写盘（先写 <path>.tmp 再 os.replace）。

        参数
        ----
        path : 目标路径；留空用 self.path（load 时记住的）

        返回：实际写入的路径

        为什么必须原子：采集过程随时可能被 Ctrl-C / 断网 / 关机打断，
        直接覆盖写可能留下半截 JSON，导致整个 checkpoint 报废。
        os.replace 在同一文件系统上是原子的，要么旧内容要么新内容。
        """
        target = os.path.abspath(path or self.path)
        if not target:
            raise ValueError("save() 需要 path（state 没有记住路径）")
        self.updated_at = _now_iso()
        self.path = target
        parent = os.path.dirname(target) or "."
        os.makedirs(parent, exist_ok=True)
        tmp = target + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.to_dict(), fh, ensure_ascii=False, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
        return target

    def to_dict(self) -> dict:
        """导出成可直接 json.dump 的字典（字段与类注释一一对应）。"""
        return {
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "start": self.start,
            "end": self.end,
            "window_days": self.window_days,
            "metadata_prefix": self.metadata_prefix,
            "set_spec": self.set_spec,
            "windows": self.windows,
            "last_success_until": self.last_success_until,
            "stats": self.stats,
        }

    # ---- 断点推进 -------------------------------------------------------------

    def next_pending_index(self, start: int = 0):
        """返回第一个下标 ≥ start 且状态不是 done 的窗口下标；全采完返回 None。

        ⚠️ 注意 first-child 语义：下标会随 split_window() 变化，
        所以主循环每次都要重新用"当前下标 + 1"来查，不能缓存住旧下标当数组用。
        """
        n = len(self.windows)
        i = max(0, int(start))
        while i < n:
            if self.windows[i].get("status") != "done":
                return i
            i += 1
        return None

    def next_pending_window(self):
        """返回下一个未完成的窗口 → (index, window_dict)；全部完成时 (None, None)。

        `in_progress` 和 `failed` 都算"未完成"：
          - in_progress 带着 token，续采时从**那一页**继续；
          - failed 会在下一次 resume 时整体重试。

        用途：resume 的入口判断，也是 status 命令展示"下一个该采谁"的依据。
        """
        idx = self.next_pending_index(0)
        if idx is None:
            return None, None
        return idx, self.windows[idx]

    def mark(self, index: int, **fields) -> dict:
        """更新某个窗口的字段并刷新 updated_at（不落盘，需要时自己 save()）。

        参数
        ----
        index  : 窗口下标
        fields : 任意要覆盖的键，最常用 status / token / records / attempts / error。
                 传了 status 会校验取值必须在 STATUSES 里，防止写错状态导致续采混乱。

        返回：更新后的窗口字典

        例：state.mark(0, status="done", records=1300)
        """
        win = self.windows[index]
        if "status" in fields and fields["status"] not in self.STATUSES:
            raise ValueError("非法状态 '{}'；允许 {}".format(fields["status"], self.STATUSES))
        win.update(fields)
        self.updated_at = _now_iso()
        return win

    def split_window(self, index: int) -> bool:
        """把一个窗口二分成两个更小的 pending 窗口（自适应切分）。

        参数
        ----
        index : 要切的窗口下标

        返回
        ----
        True  : 切成功（self.windows 里原来的 1 个窗口变成连续的 2 个）
        False : 切不动（窗口只剩 1 天），调用方应正常采集它

        语义：
          - token 一律清空，因为新窗口的第一页必须重新取；
          - depth 继承 +1，用于限制递归切分次数；
          - 两个子窗口仍然不重叠（左 until = 右 from 的前一天）。

        用途：首页就返回满页 + 还有 token ⇒ 这个窗口页数太多，
        与其冒"中途 token 过期从头再来"的风险，不如先切成两半。
        """
        win = self.windows[index]
        try:
            frm = parse_date(win["from"])
            unt = parse_date(win["until"])
        except ValueError:
            return False
        # 窗口至少要 2 天（含首尾）才有得切；单日窗口没法再分
        if ((unt - frm).days + 1) < DEFAULT_SPLIT_MIN_DAYS:
            return False
        mid = frm + _dt.timedelta(days=(unt - frm).days // 2)
        depth = int(win.get("depth") or 0) + 1
        left = _new_window(fmt_date(frm), fmt_date(mid), depth)
        right = _new_window(fmt_date(mid + _dt.timedelta(days=1)), fmt_date(unt), depth)
        self.windows[index:index + 1] = [left, right]
        self.updated_at = _now_iso()
        return True

    def note_last_success(self, until: str, today: str = "") -> None:
        """把 last_success_until 推进到 until（只增不减）。

        参数
        ----
        until : 刚被完整采完的窗口的 until，'YYYY-MM-DD'
        today : 今天（'YYYY-MM-DD'）。给了就启用"当日边界"保护。

        ★ 当日边界：OAI 按 datestamp（记录入库/更新时间）过滤，**今天还没过完**，
          今天稍晚提交的论文还没进库。如果把 today 记成"已完整覆盖"，
          下次 `--since-last` 会从明天开始，**今天后半天的论文就永久漏掉**。
          所以当 until == today 时，只记到昨天，让下一次增量重新覆盖今天
          （重叠回采靠 id+版本去重，重复的会被跳过，更新会被写入）。

        用途：`--since-last` 的起点就是 last_success_until + 1 天。
        只增不减是刻意的：即使之后的窗口又失败，已经确认覆盖的区间不会倒退。
        """
        if not until:
            return
        if today and until >= today:
            # 只提交"已经封闭的日期"
            try:
                from datetime import date, timedelta
                d = date.fromisoformat(until)
                t = date.fromisoformat(today)
                if d >= t:
                    until = (t - timedelta(days=1)).isoformat()
            except Exception:
                pass
        if not until:
            return
        if not self.last_success_until or until > self.last_success_until:
            self.last_success_until = until
            self.updated_at = _now_iso()

    def progress(self) -> dict:
        """统计各状态窗口数 + 完成率。

        返回
        ----
        {"total", "done", "pending", "in_progress", "failed",
         "completion_rate", "records", "pages", "duplicates"}

        用途：status 命令一眼看清"还剩多少"；harvest() 也用它算最终 status。
        """
        counts = {"pending": 0, "in_progress": 0, "done": 0, "failed": 0}
        records = pages = dups = 0
        for w in self.windows:
            st = w.get("status", "pending")
            counts[st] = counts.get(st, 0) + 1
            records += int(w.get("records") or 0)
            pages += int(w.get("pages") or 0)
            dups += int(w.get("duplicates") or 0)
        total = len(self.windows)
        done = counts.get("done", 0)
        return {
            "total": total, "done": done,
            "pending": counts.get("pending", 0),
            "in_progress": counts.get("in_progress", 0),
            "failed": counts.get("failed", 0),
            "completion_rate": (done / total) if total else 0.0,
            "records": records, "pages": pages, "duplicates": dups,
        }

    def __repr__(self):
        p = self.progress()
        return "<HarvestState {}→{} windows={} done={} pending={} failed={}>".format(
            self.start or "?", self.end or "?", p["total"], p["done"],
            p["pending"] + p["in_progress"], p["failed"])


# =============================================================================
# 四、OAI-PMH 分页（在 arxiv.py 之上补 set 支持与 curl 兜底）
# =============================================================================

def _oai_params(from_date: str = "", until_date: str = "",
                metadata_prefix: str = DEFAULT_PREFIX,
                resumption_token: str = "", set_spec: str = "") -> dict:
    """按 OAI-PMH 协议拼参数。⚠️ 带 token 时协议规定不能再带别的参数。"""
    params = {"verb": "ListRecords"}
    if resumption_token:
        params["resumptionToken"] = resumption_token
        return params
    params["metadataPrefix"] = metadata_prefix
    if set_spec:
        params["set"] = set_spec
    if from_date:
        params["from"] = from_date
    if until_date:
        params["until"] = until_date
    return params


def _normalize_oai_result(res: dict) -> dict:
    """把 arxiv.py 的返回补齐成本模块统一使用的形状（对旧版/新版都兼容）。

    统一后的键：status / ok / records / token / count / complete / no_records /
    deleted / cursor / error。
    """
    out = dict(res or {})
    out.setdefault("records", [])
    out.setdefault("token", "")
    out.setdefault("count", len(out.get("records") or []))
    out.setdefault("complete", not out.get("token"))
    out.setdefault("no_records", False)
    out.setdefault("deleted", [])
    out.setdefault("cursor", "")
    out.setdefault("error", "")
    # 老版 arxiv.py 没有 ok 字段，用 status+error 推出来
    if "ok" not in out:
        out["ok"] = (out.get("status") == 200 and not out.get("error"))
    return out


def _parse_oai_body(status: int, body: bytes) -> dict:
    """把一页 OAI-PMH 响应解析成本模块统一结构。

    参数
    ----
    status : HTTP 状态码
    body   : 响应体（bytes）

    返回
    ----
    {"status", "ok", "records", "token", "count", "complete",
     "no_records", "deleted", "cursor", "error"}

    关键语义：
      - `<error code="noRecordsMatch">` ⇒ ok=True, no_records=True（正常空集）
      - `<error code="badResumptionToken">` ⇒ ok=False，error 里保留关键字，
        让上层能识别并"丢弃 token 从窗口头重采"
      - 解析出 0 条但 XML 里确实有 <record> ⇒ 判为 schema 未覆盖（ok=False），
        绝不伪装成空窗口

    用途：当 arxiv.oai_list_records 不适用（需要 set 参数）或走 curl 兜底时使用。
    """
    text = body.decode("utf-8", "replace")
    out = {"status": status, "ok": status == 200, "records": [], "token": "",
           "count": 0, "complete": False, "no_records": False, "deleted": [],
           "cursor": "", "error": ""}
    if status != 200:
        out["ok"] = False
        out["error"] = "HTTP {}: {}".format(status, text[:200])
        return out
    if "<error" in text:
        code_m = re.search(r"<error[^>]*code\s*=\s*[\"']([^\"']+)[\"']", text)
        code = code_m.group(1) if code_m else ""
        inner = re.search(r"<error[^>]*>(.*?)</error>", text, re.S)
        msg = inner.group(1).strip() if inner else ""
        if code == ERR_NO_RECORDS or ERR_NO_RECORDS in text:
            out.update({"ok": True, "complete": True, "no_records": True})
            return out
        out["ok"] = False
        out["error"] = "OAI error[{}]: {}".format(code, msg or re.sub(r"\s+", " ", text)[:200])
        return out
    try:
        records = arxiv.parse_oai_records(body)
    except Exception as exc:                       # XML 破损
        out["ok"] = False
        out["error"] = "XML 解析失败: {}".format(exc)
        return out
    try:
        deleted = arxiv.parse_oai_deleted(body)
    except Exception:
        deleted = []
    m = re.search(r"<resumptionToken[^>]*>(.*?)</resumptionToken>", text, re.S)
    token = m.group(1).strip() if m else ""
    if not records and not deleted and "<record" in text:
        # 有 record 却解析不出内容 —— 极可能 metadataPrefix 的 schema 没覆盖
        out["ok"] = False
        out["error"] = "XML 含 <record> 但解析结果为 0 条（schema 未实现？）"
        return out
    out.update({"ok": True, "records": records, "token": token, "count": len(records),
                "complete": not token, "deleted": deleted, "error": ""})
    if token:
        cm = re.search(r"skip=(\d+)", token)
        out["cursor"] = cm.group(1) if cm else ""
    return out


def _request_page(url: str, timeout: float, email: str = ""):
    """取一页的原始响应：urllib 优先，非 200 时用 curl 兜底。

    背景：本库实测 oaipmh.arxiv.org 会**间歇性**返回 HTTP 406（空 body），
    属于按 TLS 指纹/WAF 的软性拒绝 —— 同一 URL 稍后重试或换 curl 就能 200。
    所以这里保留 curl 子进程兜底（_common.curl_get），避免整轮采集被 406 打断。

    返回 (status, body_bytes)
    """
    status, body, _ = http_request(url, timeout=timeout, retries=1, backoff=5.0, email=email)
    if status != 200 and curl_available():
        c_status, c_body, _ = curl_get(
            url, timeout=timeout,
            user_agent="ResearchOS-PaperSearch/0.1 (+{})".format(email or "anonymous"))
        if c_status == 200:
            return c_status, c_body
    return status, body


def oai_page(from_date: str = "", until_date: str = "",
             metadata_prefix: str = DEFAULT_PREFIX, resumption_token: str = "",
             set_spec: str = "", timeout: float = DEFAULT_TIMEOUT,
             email: str = "") -> dict:
    """取 OAI-PMH 的**一页**记录（支持 set；失败自动 curl 兜底）。

    参数
    ----
    from_date / until_date : 'YYYY-MM-DD'，过滤的是 **datestamp**（见文件头语义 1）
    metadata_prefix        : 'arXiv'（推荐）或 'oai_dc'；arXivRaw 会被 validate_prefix 拒
    resumption_token       : 上一页返回的分页 token，原样回传。⚠️ 传了就忽略其它参数
    set_spec               : OAI set，如 'cs:cs:AI'；留空=全库
    timeout                : 单页约 4MB，慢时要 50s+，默认 180s
    email                  : 放进 User-Agent（礼貌池）

    返回
    ----
    {"status", "ok", "records", "token", "count", "complete",
     "no_records", "deleted", "cursor", "error"}
      - `ok`         : 协议交互是否成功。**不要用 status==200 代替它** ——
                       OAI 用 200 + <error> 表达失败。
      - `no_records` : 空窗口（正常），不是失败
      - token 非空   : 还有下一页

    实现说明：无 set 时直接调 `arxiv.oai_list_records`（保持与现有接口一致）；
    有 set 时 arxiv.py 的函数没有该参数，于是本函数自己拼 URL、自己解析
    （解析仍复用 `arxiv.parse_oai_records`），返回结构完全对齐。

    用途：harvest() 的最小取数单元；也可单独用来"先看一眼某天某 set 有多少条"。
    """
    params = _oai_params(from_date=from_date, until_date=until_date,
                         metadata_prefix=metadata_prefix,
                         resumption_token=resumption_token, set_spec=set_spec)

    if not set_spec:
        # ---- 主路径：复用 arxiv.py 的实现 ----
        try:
            res = arxiv.oai_list_records(
                from_date=from_date, until_date=until_date,
                metadata_prefix=metadata_prefix,
                resumption_token=resumption_token, timeout=timeout)
            res = _normalize_oai_result(res)
            if res.get("status") == 200:
                return res
        except Exception as exc:                   # arxiv.py 被改坏时也不至于全挂
            res = {"status": 0, "error": "arxiv.oai_list_records 异常: {}".format(exc)}
        # ---- 降级路径：curl 兜底 / 自己重发 ----
        url = url_with(arxiv.ARXIV_OAI, params)
        status, body = _request_page(url, timeout, email=email)
        return _parse_oai_body(status, body)

    # ---- 带 set：arxiv.py 的签名没有 set 参数，只能自己发 ----
    url = url_with(arxiv.ARXIV_OAI, params)
    status, body = _request_page(url, timeout, email=email)
    return _parse_oai_body(status, body)


def fetch_page(from_date: str = "", until_date: str = "",
               metadata_prefix: str = DEFAULT_PREFIX, resumption_token: str = "",
               set_spec: str = "", timeout: float = DEFAULT_TIMEOUT,
               retries: int = DEFAULT_RETRIES, backoff: float = DEFAULT_BACKOFF,
               sleep: float = 0.0, email: str = "", verbose: bool = False) -> dict:
    """oai_page 之上加"网络重试 + 指数退避"，是采集循环真正调用的取页函数。

    参数
    ----
    （前 6 个同 oai_page）
    retries : 网络失败后的额外重试次数（默认 3）
    backoff : 退避基数；第 n 次失败等待 backoff * 2**(n-1) 秒（默认 3 → 3/6/12s）
    sleep   : 成功后额外 sleep 秒（页间礼貌限速）
    verbose : 打印每次重试

    返回
    ----
    同 oai_page 的字典（最后一次的结果）

    重试策略（重要）：
      - HTTP 层失败（status != 200，含 406/5xx/超时）→ 重试，值得等；
      - OAI 协议错误（200 + <error>）→ **不重试**：badResumptionToken 需要换
        "从窗口头重采"的策略，badArgument 是参数问题，重试只是浪费配额；
      - noRecordsMatch 是成功（ok=True），立即返回。

    用途：harvest() 的取数入口；单独调用可用于快速验证某个窗口是否可用。
    """
    last = {}
    total = max(1, int(retries) + 1)
    for attempt in range(total):
        res = oai_page(from_date=from_date, until_date=until_date,
                       metadata_prefix=metadata_prefix,
                       resumption_token=resumption_token, set_spec=set_spec,
                       timeout=timeout, email=email)
        res = _normalize_oai_result(res)
        last = res
        if res.get("status") == 200 and res.get("ok", True) and not res.get("error"):
            if sleep:
                time.sleep(sleep)
            return res
        if res.get("status") == 200:
            # 协议错误：重试无意义，交给上层按 error 关键字处理
            return res
        if attempt < total - 1:
            wait = backoff * (2 ** attempt)
            if verbose:
                print("    ↻ 第 {}/{} 次重试：HTTP {}，{:.0f}s 后重来"
                      .format(attempt + 1, total - 1, res.get("status"), wait))
            time.sleep(wait)
    return last


def list_sets(max_pages: int = 5, timeout: float = 60.0, email: str = "") -> dict:
    """列出 OAI 仓库的 set（arXiv 实测 183 个）。

    参数
    ----
    max_pages : 最多翻几页（ListSets 一般一页返回完，留点余量）
    timeout   : 单次请求超时

    返回
    ----
    {"status", "ok", "sets": [{"set_spec", "set_name"}], "count", "pages",
     "complete", "error"}

    用途：
      - 确认 `--set` 的取值格式（arXiv 是 `cs:cs:AI` 这种"归档:分类"的冒号写法，
        不是 Export API 里的 `cs.AI`）；
      - 想知道某个方向有哪些 set 可采时先跑一次 `python3 arxiv_harvest.py sets`。
    """
    sets, token, pages, err = [], "", 0, ""
    while pages < max(1, int(max_pages)):
        params = {"verb": "ListSets"}
        if token:
            params["resumptionToken"] = token
        url = url_with(arxiv.ARXIV_OAI, params)
        status, body = _request_page(url, timeout, email=email)
        pages += 1
        if status != 200:
            err = "HTTP {}".format(status)
            break
        text = body.decode("utf-8", "replace")
        if "<error" in text:
            code_m = re.search(r"<error[^>]*code\s*=\s*[\"']([^\"']+)[\"']", text)
            inner = re.search(r"<error[^>]*>(.*?)</error>", text, re.S)
            err = "OAI error[{}]: {}".format(code_m.group(1) if code_m else "",
                                             inner.group(1).strip() if inner else "")
            break
        for m in re.finditer(r"<set>(.*?)</set>", text, re.S):
            block = m.group(1)
            spec = re.search(r"<setSpec>(.*?)</setSpec>", block, re.S)
            name = re.search(r"<setName>(.*?)</setName>", block, re.S)
            sets.append({"set_spec": (spec.group(1).strip() if spec else ""),
                         "set_name": (name.group(1).strip() if name else "")})
        t = re.search(r"<resumptionToken[^>]*>(.*?)</resumptionToken>", text, re.S)
        token = t.group(1).strip() if t else ""
        if not token:
            break
    return {"status": 200 if not err else 0, "ok": not err, "sets": sets,
            "count": len(sets), "pages": pages, "complete": not token, "error": err}


# =============================================================================
# 五、落盘（JSONL + 去重 + gzip + provenance）
# =============================================================================

def _looks_gzip(path: str) -> bool:
    """按**文件头魔数**判断是不是 gzip（比看扩展名可靠）。"""
    try:
        with open(path, "rb") as fh:
            return fh.read(2) == b"\x1f\x8b"
    except Exception:
        return False


def _open_read(path: str):
    """打开一个可能被 gzip 压缩的 JSONL 文本文件（流式，不会整文件读进内存）。"""
    if _looks_gzip(path):
        return gzip.open(path, "rt", encoding="utf-8", errors="replace")
    return open(path, "r", encoding="utf-8", errors="replace")


def _iter_jsonl(path: str):
    """逐行 yield 原始文本（含空行），供 verify/stats/去重预载流式使用。"""
    with _open_read(path) as fh:
        for line in fh:
            yield line


def _version_of(rec: dict) -> str:
    """取一条记录的"版本标记"，用于区分「同一条重取」与「同一条被更新」。

    优先级：OAI header 的 datestamp（最权威，OAI 正是用它表达记录变更时间）
            > updated（arXiv schema 的最后更新时间）
            > created（首次提交时间）
    返回 "" 表示这条记录没有版本信息 —— 此时退化为纯 id 去重（保守）。

    ⚠️ 为什么必须有它：只看 id 去重会把"论文被修订后的新元数据"当成重复丢掉，
       于是增量同步永远拿不到新版标题/作者/分类。这与模块的目标直接冲突。
    """
    if not isinstance(rec, dict):
        return ""
    for k in ("datestamp", "_datestamp", "updated", "created"):
        v = rec.get(k)
        if v:
            return str(v)
    return ""


class RecordWriter:
    """JSONL 追加写器：去重 + gzip + 每行附 `_harvest` provenance。

    设计要点
    ----
    - **追加写**：同一个文件可以被多轮采集复用（resume 不会覆盖已有数据）；
    - **每行一条记录**：记录字段平铺在顶层（stats/verify 直接读 id/categories），
      外加一个 `_harvest` 子对象记录来源窗口、采集时刻、metadataPrefix、set；
    - **去重**：以 record["id"] 为键维护已见集合。可预载已有文件里的 id，
      这样"昨天采过的今天再采"也不会重复写盘；重复只计数不写。
    - **gzip**：gzip_out=True 或路径以 .gz 结尾即压缩写；读取时按魔数自动识别。

    用法
    ----
        w = RecordWriter("harvest.jsonl", dedupe=True)
        w.write(records, window={"from": "2026-09-01", "until": "2026-09-02"}, page=1)
        w.close()
    """

    SOURCE = "oai-pmh:oaipmh.arxiv.org"

    def __init__(self, path: str, gzip_out: bool = False, dedupe: bool = True,
                 preload: bool = False, verbose: bool = False):
        """参数
        path     : 目标路径（相对路径按当前工作目录解析）
        gzip_out : 强制 gzip 写
        dedupe   : 是否按 id 去重
        preload  : 是否先扫一遍已有文件、把 id 装进去重集合（续采必备）
        verbose  : 打印"预载了多少 id"
        """
        self.path = os.path.abspath(path)
        self.gz = bool(gzip_out) or self.path.endswith(".gz")
        self.dedupe = bool(dedupe)
        self.verbose = verbose
        self.ids = set()
        # ★ 版本表：id -> 版本标记（datestamp / updated / created）。
        #   只按 id 去重会**把论文的更新当成重复丢掉** —— 一篇论文被修订后
        #   在 OAI 里是同一个 id、不同的 datestamp，那是新数据，不该丢。
        #   所以去重判据是 (id, version)：id 相同但版本更新 ⇒ 视为更新，照写。
        self.versions = {}
        self.preloaded = 0
        self.written = 0        # 本次运行新写入的条数（含更新版本）
        self.seen = 0           # 本次运行收到的原始条数（含重复）
        self.duplicates = 0     # 本次运行因 (id, version) 完全相同被跳过的条数
        self.updated = 0        # 本次运行中"同 id 但版本更新"而写盘的条数
        self.deleted = 0        # 本次运行持久化的删除事件条数
        self.deletions_path = self.path + ".deletions.jsonl"
        self._fh = None
        self._dfh = None
        if preload and self.dedupe and os.path.exists(self.path):
            for raw in _iter_jsonl(self.path):
                line = raw.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                rid = obj.get("id") if isinstance(obj, dict) else None
                if rid:
                    self.ids.add(rid)
                    v = _version_of(obj)
                    # 保留"见到过的最新版本"，这样下一轮的更新才能被识别出来
                    if v and v > self.versions.get(rid, ""):
                        self.versions[rid] = v
            self.preloaded = len(self.ids)
            if self.verbose and self.preloaded:
                print("  · 去重集合已预载 {} 个已有 id".format(self.preloaded))

    def _ensure_open(self):
        """惰性打开文件：没有任何记录写盘时不会创建空文件。"""
        if self._fh is None:
            parent = os.path.dirname(self.path) or "."
            os.makedirs(parent, exist_ok=True)
            if self.gz:
                self._fh = gzip.open(self.path, "at", encoding="utf-8")
            else:
                self._fh = open(self.path, "a", encoding="utf-8")
        return self._fh

    def write(self, records, window=None, page: int = 0,
              metadata_prefix: str = DEFAULT_PREFIX, set_spec: str = "",
              source: str = "", max_new: int = 0) -> tuple:
        """把一页记录写盘。

        参数
        ----
        records         : arxiv.parse_oai_records 出来的记录列表
        window          : {"from": ..., "until": ...}，会原样写进 `_harvest`
        page            : 页码（写进 `_harvest`，便于回溯"这条来自哪一页"）
        metadata_prefix : schema
        set_spec        : OAI set
        source          : 来源标识，默认 SOURCE

        max_new : >0 时，写满这么多条**新记录**就停止扫描本页（预算控制）。
                  ★ 预算必须按"实际写入数"消耗，不能先切片再过滤：
                    若先 recs[:remaining] 再去重，而切片头部恰好全是已写过的
                    记录，就会一条都写不进去，resume 永远停在同一页、
                    永远没有进展。
                  返回的 consumed 告诉调用方本页消费到第几条，用来判断
                  "整页吃完了没" —— 没吃完就必须保留进入本页的 token。

        返回
        ----
        (written, duplicates, consumed)
          written    本页真正写盘的条数（新增 + 更新版本）
          duplicates 本页因 (id, version) 完全相同被跳过的条数
          consumed   本页实际检查过的记录条数（≤ len(records)）

        副作用：写盘后立即 flush（保证进程被 kill 时数据已落 OS 缓冲）。
        用途：harvest() 每取一页就调用一次，实现"边采边落盘"。
        """
        window = window or {}
        now = _now_iso()
        src = source or self.SOURCE
        lines, dups, consumed = [], 0, 0
        for rec in (records or []):
            consumed += 1
            self.seen += 1
            rid = rec.get("id") if isinstance(rec, dict) else None
            is_update = False
            if self.dedupe and rid and isinstance(rec, dict):
                version = _version_of(rec)
                known = self.versions.get(rid)
                if rid in self.ids:
                    # 同 id：版本更新才算新数据，否则是重复
                    if version and known and version > known:
                        is_update = True
                    else:
                        dups += 1
                        self.duplicates += 1
                        continue
                if version:
                    self.versions[rid] = version
                self.ids.add(rid)
            if not isinstance(rec, dict):
                continue
            line = dict(rec)
            line["_harvest"] = {
                "from": window.get("from", ""),
                "until": window.get("until", ""),
                "page": page,
                "harvested_at": now,
                "metadata_prefix": metadata_prefix,
                "set_spec": set_spec or "",
                "source": src,
            }
            lines.append(json.dumps(line, ensure_ascii=False))
            if is_update:
                self.updated += 1
            if max_new and len(lines) >= max_new:
                break       # 预算用尽：停止扫描，consumed 反映本页消费位置
        if lines:
            fh = self._ensure_open()
            fh.write("\n".join(lines) + "\n")
            fh.flush()
            self.written += len(lines)
        return len(lines), dups, consumed

    def write_deletions(self, deleted, window=None, page: int = 0,
                        metadata_prefix: str = DEFAULT_PREFIX,
                        set_spec: str = "", source: str = "") -> int:
        """把 OAI 的删除事件（tombstone）持久化到旁路账本。

        OAI 的 `<header status="deleted">` 表示**该元数据记录被删除**，
        不是学术意义上的"撤稿" —— 可能是记录合并、迁移或元数据下线。
        所以它不能混进论文集合里，但**必须留下来**，否则：
          - 下游以为这篇论文还在（旧记录仍在主 JSONL 里）；
          - 而且窗口会被标记 done，以后再也不会重新发现这个删除。

        落盘位置：`<out>.deletions.jsonl`（旁路账本，主文件保持"只有论文"）。
        每行：{"identifier":..., "datestamp":..., "sets":[...], "_harvest":{...}}

        返回写入条数。**必须先写成功，调用方才推进断点。**
        """
        items = list(deleted or [])
        if not items:
            return 0
        now = _now_iso()
        win = window or {}
        lines = []
        for d in items:
            if not isinstance(d, dict):
                continue
            line = dict(d)
            line["_harvest"] = {
                "from": win.get("from", ""), "until": win.get("until", ""),
                "page": page, "harvested_at": now,
                "metadata_prefix": metadata_prefix, "set_spec": set_spec or "",
                "source": source or self.SOURCE, "event": "deleted",
            }
            lines.append(json.dumps(line, ensure_ascii=False))
        if not lines:
            return 0
        if self._dfh is None:
            parent = os.path.dirname(self.deletions_path) or "."
            os.makedirs(parent, exist_ok=True)
            self._dfh = open(self.deletions_path, "a", encoding="utf-8")
        self._dfh.write("\n".join(lines) + "\n")
        self._dfh.flush()
        self.deleted += len(lines)
        return len(lines)

    def close(self):
        """关闭文件句柄（可重复调用）。"""
        if self._fh is not None:
            try:
                self._fh.close()
            finally:
                self._fh = None
        if self._dfh is not None:
            try:
                self._dfh.close()
            finally:
                self._dfh = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


# =============================================================================
# 六、主采集循环
# =============================================================================

def resolve_since_last(state_path: str, end_date: str = "",
                       today: str = "") -> dict:
    """算出"增量采集"的起点：checkpoint.last_success_until 的下一天。

    参数
    ----
    state_path : checkpoint 路径
    end_date   : 增量区间的终点；留空 = 今天
    today      : 注入"今天"（测试用）

    返回
    ----
    {"ok", "start", "end", "last_success_until", "error"}
      ok=False 时 error 说明原因（文件不存在 / 没有 last_success_until）

    用途：`--since-last` 的取值逻辑集中在这里，CLI 与程序调用共用一份。
    注意：这里只负责"算区间"，不负责合并已有 checkpoint（那步在 harvest()）。
    """
    try:
        st = HarvestState.load(state_path)
    except FileNotFoundError:
        return {"ok": False, "start": "", "end": "", "last_success_until": "",
                "error": "checkpoint 不存在：{}（--since-last 需要先跑过一次 run）".format(state_path)}
    except ValueError as exc:
        return {"ok": False, "start": "", "end": "", "last_success_until": "",
                "error": str(exc)}
    if not st.last_success_until:
        return {"ok": False, "start": "", "end": "", "last_success_until": "",
                "error": "checkpoint 里没有 last_success_until（上一轮可能一条窗口都没采完）"}
    try:
        start = parse_date(st.last_success_until) + _dt.timedelta(days=1)
    except ValueError as exc:
        return {"ok": False, "start": "", "end": "", "last_success_until": st.last_success_until,
                "error": "last_success_until 非法：{}".format(exc)}
    end = end_date or today or today_str()
    if start > parse_date(end):
        return {"ok": True, "start": fmt_date(start), "end": end,
                "last_success_until": st.last_success_until,
                "error": "", "note": "已经是最新，没有需要增量采集的区间"}
    return {"ok": True, "start": fmt_date(start), "end": end,
            "last_success_until": st.last_success_until, "error": ""}


def _empty_result(**kw) -> dict:
    """构造 harvest() 的统一返回骨架，避免每个返回点漏字段。"""
    out = {
        "ok": False, "complete": False, "status": "failed", "error": "",
        "errors": [], "stop_reason": "", "collected": 0, "written": 0,
        "duplicates": 0, "seen": 0, "pages": 0, "requests": 0,
        "windows_total": 0, "windows_done": 0, "windows_failed": 0,
        "state_path": "", "out": "", "state": None, "elapsed": 0.0,
    }
    out.update(kw)
    return out


def _harvest_one_window(state: HarvestState, idx: int, writer: RecordWriter,
                        cfg: dict) -> dict:
    """采一个窗口（内部函数，被 harvest() 调用）。

    参数
    ----
    state  : HarvestState（会被就地修改并**每页 save 一次**）
    idx    : 窗口下标
    writer : RecordWriter
    cfg    : 采集配置字典（metadata_prefix / set_spec / timeout / retries /
             backoff / sleep / max_records / max_pages_left / split_threshold /
             max_splits / max_restarts / state_path / verbose）

    返回
    ----
    {"pages", "status", "stop_reason", "error", "split"}
      status = "done" / "failed" / "in_progress" / "split"
      stop_reason 非空 ⇒ 触发全局停止（max_records / max_pages）

    行为要点：
      1. 从 win["token"] 开始（空 token = 从窗口头开始）⇒ 断点精确到页；
      2. 每写完一页立刻 state.save()，崩溃最多丢一页；
      3. badResumptionToken ⇒ 清 token 从头重采，重复由 RecordWriter 去重；
      4. 首页满页 + 有 token ⇒ 触发二分（窗口太大）；
      5. max_records 严格：若一页装不下，截断后**保留进入本页时的 token**，
         下次 resume 重取本页，已写部分被去重跳过 —— 既不超上限也不丢数据。
    """
    win = state.windows[idx]
    state.mark(idx, status="in_progress")
    if not win.get("started_at"):
        win["started_at"] = _now_iso()
    pages_used = 0
    cfg_verbose = cfg.get("verbose", True)
    max_records = cfg.get("max_records") or 0
    max_pages_left = cfg.get("max_pages_left")
    split_threshold = cfg.get("split_threshold") or DEFAULT_SPLIT_THRESHOLD
    max_splits = cfg.get("max_splits", DEFAULT_MAX_SPLITS)
    max_restarts = cfg.get("max_restarts", DEFAULT_MAX_RESTARTS)
    state_path = cfg["state_path"]

    while True:
        if max_pages_left is not None and pages_used >= max_pages_left:
            win["error"] = "达到 max_pages 上限，本窗口未采完（下次 resume 继续）"
            state.save(state_path)
            return {"pages": pages_used, "status": "in_progress",
                    "stop_reason": STOP_MAX_PAGES, "error": ""}

        # ⚠️ used_token 是"进入本页时"的 token：截断时要用它回退重采
        used_token = win.get("token") or ""
        first_page = not used_token
        res = fetch_page(from_date=win["from"], until_date=win["until"],
                         metadata_prefix=cfg["metadata_prefix"],
                         resumption_token=used_token,
                         set_spec=cfg.get("set_spec", ""),
                         timeout=cfg.get("timeout", DEFAULT_TIMEOUT),
                         retries=cfg.get("retries", DEFAULT_RETRIES),
                         backoff=cfg.get("backoff", DEFAULT_BACKOFF),
                         sleep=cfg.get("sleep", DEFAULT_SLEEP),
                         email=cfg.get("email", ""),
                         verbose=cfg_verbose)
        pages_used += 1
        win["attempts"] = int(win.get("attempts") or 0) + 1
        state.stats["requests"] = int(state.stats.get("requests") or 0) + 1
        err = res.get("error") or ""

        # ---- HTTP 层失败 ----
        if res.get("status") != 200:
            win["status"] = "failed"
            win["error"] = "HTTP {}: {}".format(res.get("status"), err or "网络层失败")
            state.stats["errors"] = int(state.stats.get("errors") or 0) + 1
            state.save(state_path)
            return {"pages": pages_used, "status": "failed", "error": win["error"]}

        # ---- OAI 协议错误 ----
        if not res.get("ok", True) or err:
            if ERR_BAD_TOKEN in err or ERR_BAD_TOKEN in str(res.get("oai_error_code", "")):
                restarts = int(win.get("restarts") or 0) + 1
                win["restarts"] = restarts
                win["error"] = err
                state.stats["restarts"] = int(state.stats.get("restarts") or 0) + 1
                state.stats["errors"] = int(state.stats.get("errors") or 0) + 1
                if used_token and restarts <= max_restarts:
                    # token 过期（通常到当天 24:00）⇒ 丢弃它，从窗口开头重采。
                    # 已写盘的记录会被去重跳过，重复写入是安全的。
                    if cfg_verbose:
                        print("    ⚠️ resumptionToken 已失效，第 {} 次从窗口 {} 开头重采"
                              .format(restarts, win["from"]))
                    state.mark(idx, token="", status="in_progress", error=err)
                    state.save(state_path)
                    continue
                win["status"] = "failed"
                state.save(state_path)
                return {"pages": pages_used, "status": "failed",
                        "error": "resumptionToken 失效且重启次数超限: {}".format(err)}
            if ERR_NO_RECORDS in err or res.get("no_records"):
                # 正常空窗口 —— 不是失败
                win["status"] = "done"
                win["token"] = ""
                win["error"] = ""
                win["finished_at"] = _now_iso()
                state.note_last_success(win["until"], today=cfg.get("today", ""))
                state.save(state_path)
                return {"pages": pages_used, "status": "done", "error": ""}
            win["status"] = "failed"
            win["error"] = err
            state.stats["errors"] = int(state.stats.get("errors") or 0) + 1
            state.save(state_path)
            return {"pages": pages_used, "status": "failed", "error": win["error"]}

        # ---- 删除事件（tombstone）：必须持久化，且要在"空窗口"判定之前 ----
        # 只有 deleted、没有 records 的一页 **不是空窗口**：它是"记录被删除"这一
        # 事实的唯一载体。早期实现只看 records/token，于是这种页被判为空窗口，
        # 删除事件被静默丢弃，窗口却被标记 done（以后不会再发现这个删除）。
        deleted_now = list(res.get("deleted") or [])
        if deleted_now:
            try:
                n_del = writer.write_deletions(
                    deleted_now,
                    window={"from": win["from"], "until": win["until"]},
                    page=int(win.get("pages") or 0) + 1,
                    metadata_prefix=cfg["metadata_prefix"],
                    set_spec=cfg.get("set_spec", ""))
            except Exception as exc:
                # 删除事件写不进去 ⇒ 不能推进断点，否则这次删除就永久丢了
                win["status"] = "failed"
                win["error"] = "删除事件写盘失败：{}".format(exc)
                state.save(state_path)
                return {"pages": pages_used, "status": "failed", "error": win["error"]}
            win["deleted"] = int(win.get("deleted") or 0) + n_del
            if cfg_verbose:
                print("    · 持久化 {} 条删除事件（tombstone）".format(n_del))

        # ---- 空窗口（没有 error 也没有记录、没有 token）----
        if res.get("no_records") or (not res.get("records") and not res.get("token")):
            win["status"] = "done"
            win["token"] = ""
            win["error"] = ""
            win["finished_at"] = _now_iso()
            if cfg_verbose:
                print("  · 窗口 {} → {} 无记录（正常空窗口），标记 done"
                      .format(win["from"], win["until"]))
            state.note_last_success(win["until"], today=cfg.get("today", ""))
            state.save(state_path)
            return {"pages": pages_used, "status": "done", "error": ""}

        # ---- 自适应切分：首页就满页且还有 token ⇒ 这个窗口太大 ----
        can_split = (first_page and res.get("token")
                     and int(win.get("depth") or 0) < max_splits
                     and len(res.get("records") or []) >= split_threshold
                     # 全局预算比一页还小的时候，切分毫无意义（马上就会撞上限）
                     and not (max_records and max_records <= split_threshold))
        if can_split and state.split_window(idx):
            if cfg_verbose:
                print("  ↗ 窗口 {} → {} 过大（首页 {} 条仍有 token），二分切分"
                      .format(win["from"], win["until"], len(res["records"])))
            state.save(state_path)
            return {"pages": pages_used, "status": "split", "error": ""}

        # ---- 写盘（max_records 严格截断，且预算按"实际写入数"消耗）----
        # ★ 这里**不能**先 recs[:remaining] 再交给去重：
        #   若切片头部恰好都是已写过的记录，去重会把它们全跳过，结果是
        #   "一条没写 + 保留原 token" ⇒ 每次 resume 都重复同一页、永远无进展。
        #   正确做法：整页交给 writer，由它跳过重复后按 max_new 停止，
        #   并用返回的 consumed 判断本页是否吃完。
        recs = list(res.get("records") or [])
        remaining = 0
        if max_records:
            remaining = max_records - writer.written
            if remaining <= 0:
                win["token"] = used_token
                state.save(state_path)
                return {"pages": pages_used, "status": "in_progress",
                        "stop_reason": STOP_MAX_RECORDS, "error": ""}
        written, dups, consumed = writer.write(
            recs,
            window={"from": win["from"], "until": win["until"]},
            page=int(win.get("pages") or 0) + 1,
            metadata_prefix=cfg["metadata_prefix"],
            set_spec=cfg.get("set_spec", ""),
            max_new=remaining)
        # 本页没吃完（预算用尽或还有未检查的记录）⇒ 保留页首 token，下次继续
        truncated = consumed < len(recs)
        win["records"] = int(win.get("records") or 0) + written
        win["raw_records"] = int(win.get("raw_records") or 0) + len(res.get("records") or [])
        win["duplicates"] = int(win.get("duplicates") or 0) + dups
        win["pages"] = int(win.get("pages") or 0) + 1
        win["last_page_at"] = _now_iso()
        state.stats["pages"] = int(state.stats.get("pages") or 0) + 1
        state.stats["records"] = int(state.stats.get("records") or 0) + written
        state.stats["duplicates"] = int(state.stats.get("duplicates") or 0) + dups
        if cfg_verbose:
            print("  · [{}/{}] 第 {} 页：收 {} 条 / 写 {} 条 / 重复 {} 条（窗口累计写 {}）"
                  .format(idx + 1, len(state.windows), win["pages"],
                          len(res.get("records") or []), written, dups, win["records"]))

        if truncated:
            # 保留进入本页时的 token：下次 resume 重取本页，已写的会被去重跳过
            win["token"] = used_token
            win["error"] = ("达到 max_records 上限，窗口停在 {} → {} 中途"
                            .format(win["from"], win["until"]))
            state.save(state_path)
            return {"pages": pages_used, "status": "in_progress",
                    "stop_reason": STOP_MAX_RECORDS, "error": ""}

        new_token = res.get("token") or ""
        win["token"] = new_token
        win["error"] = ""
        if not new_token:
            win["status"] = "done"
            win["finished_at"] = _now_iso()
            state.note_last_success(win["until"], today=cfg.get("today", ""))
            state.save(state_path)
            if cfg_verbose:
                print("  ✓ 窗口 {} → {} 采完：{} 条（去重后），{} 页"
                      .format(win["from"], win["until"], win["records"], win["pages"]))
            return {"pages": pages_used, "status": "done", "error": ""}
        state.save(state_path)      # 每页写盘后立刻存 checkpoint


def harvest(start_date: str = "", end_date: str = "",
            out: str = DEFAULT_OUT, state_path: str = "",
            metadata_prefix: str = None, set_spec: str = None,
            window_days: int = DEFAULT_WINDOW_DAYS,
            state: HarvestState = None, resume: bool = False,
            since_last: bool = False,
            max_records: int = 0, max_pages: int = 0,
            retries: int = DEFAULT_RETRIES, backoff: float = DEFAULT_BACKOFF,
            sleep: float = DEFAULT_SLEEP, timeout: float = DEFAULT_TIMEOUT,
            gzip_out: bool = False, dedupe: bool = True, preload: bool = None,
            split_threshold: int = DEFAULT_SPLIT_THRESHOLD,
            max_splits: int = DEFAULT_MAX_SPLITS,
            max_restarts: int = DEFAULT_MAX_RESTARTS,
            stop_on_error: bool = False, dry_run: bool = False,
            verbose: bool = True, email: str = "", today: str = "") -> dict:
    """主采集循环：按窗口把 OAI-PMH 一路采完并落盘，全程可中断可续采。

    参数
    ----
    start_date / end_date : 采集区间（含首尾）。since_last=True 时可省略 start
    out            : 落盘 JSONL 路径（.gz 结尾自动 gzip）
    state_path     : checkpoint 路径；留空 = out + ".state.json"
    metadata_prefix: 'arXiv'（推荐）/ 'oai_dc'；'arXivRaw' 会被显式拒绝。
                     **默认 None = 未显式指定**：resume 时继承 checkpoint 里的值；
                     显式传入且与 checkpoint 不同 ⇒ 作废旧分页 token 并从头重采
                     （旧 token 属于另一批数据，混用会串集）
    set_spec       : OAI set，如 'cs:cs:AI'；None = 未指定（resume 时继承 checkpoint），
                     "" = 显式指定"全库"
    window_days    : 窗口长度（天），默认 30
    state          : 直接传入一个 HarvestState（优先级最高，用于程序化续采）
    resume         : True = 从 state_path 读 checkpoint 继续（CLI resume 用）
    since_last     : True = 以 checkpoint.last_success_until+1 为起点增量采集
    max_records    : 全局硬上限。**严格遵守，绝不超出**；达到即停（status=partial）
    max_pages      : 全局页数上限，同上
    retries/backoff: 网络失败重试策略（协议错误不重试）
    sleep          : 页间礼貌限速（秒），默认 1.0
    timeout        : 单页超时，默认 180s
    gzip_out       : 强制 gzip 写
    dedupe         : 按 arXiv id 去重（默认开；见文件头语义 1）
    preload        : 开始前扫一遍已有 out 文件，把 id 装进去重集合（跨轮次去重）。
                     **默认 None = 自动**：resume/since_last 时自动开启，
                     全新采集时关闭。CLI 与 Python API 语义一致 ——
                     早期实现只有 CLI 补了默认 True，直接调用 harvest(resume=True)
                     仍会重复写盘。
    split_threshold: 首页条数达到该值且还有 token ⇒ 窗口二分
    max_splits     : 单窗口最多切分几次
    max_restarts   : 单窗口内 token 过期重启上限
    stop_on_error  : True = 任一窗口失败就立刻停止；False = 跳过继续采其它窗口
    dry_run        : 只规划并打印，不发任何请求
    verbose        : 打印逐页进度
    email          : 放进 User-Agent
    today          : 注入"今天"（测试用；避免未来 until 触发 badArgument）

    返回
    ----
    {
      "ok"            : bool  —— 本轮**没有发生错误**（空窗口、max_* 主动停止都算 ok）
      "complete"      : bool  —— 请求的区间是否全部采完（被 max_* 打断 ⇒ False）
      "status"        : "done" | "partial" | "failed" | "dry_run"
                        （刻意不用 200/成功码冒充：partial 就是 partial）
      "error"         : str   一句人类可读的总结（空 = 没问题）
      "errors"        : [str] 逐窗口的错误明细
      "stop_reason"   : "" | "max_records" | "max_pages"
      "collected"     : 本轮收到的原始条数（含重复）
      "written"       : 本轮真正写盘的条数
      "duplicates"    : 本轮被去重跳过的条数
      "seen"          : 同 collected（语义化别名）
      "pages"         : 本轮请求的页数
      "requests"      : 本轮 HTTP 请求次数（含重试）
      "windows_total" / "windows_done" / "windows_failed"
      "state_path" / "out"
      "state"         : HarvestState 对象（程序调用方可以直接继续用）
      "elapsed"       : 耗时秒数
    }

    怎么用：
      - CLI `run` / `resume` 就是它的薄封装；
      - 程序里要"每天增量同步"：harvest(since_last=True, preload=True)；
      - 想先干跑看窗口：dry_run=True，只返回 windows 规划不碰网络。
    """
    t0 = time.time()
    out = os.path.abspath(out)
    state_path = os.path.abspath(state_path or (out + ".state.json"))
    today = today or today_str()

    # preload=None ⇒ 自动：续采/增量必须预载已有 id，否则页内截断后重取会重复写。
    # ★ 还要覆盖 `state=<HarvestState>` 这条程序化续采路径 —— 它不传 resume=True，
    #   但语义上就是在续采（早期只判 resume/since_last，这条路径会重复写盘）。
    if preload is None:
        preload = bool(resume or since_last or state is not None)

    # ---- 0. 配置解析 ----
    # None 表示"调用方没有显式指定"，resume/since_last 时应当继承 checkpoint。
    # 不能在这一步就把 None 当成默认值定死，否则续采会悄悄从"某分类"变成"全库"、
    # 从 oai_dc 变成 arXiv —— 采回来的数据口径完全不同，而且很难发现。
    explicit_prefix = metadata_prefix is not None
    explicit_set = set_spec is not None
    if metadata_prefix is None:
        metadata_prefix = DEFAULT_PREFIX
    if set_spec is None:
        set_spec = ""

    # 前缀校验：宁可现在报错，也不要静默产出空数据
    ok_prefix, prefix_msg = validate_prefix(metadata_prefix)
    if not ok_prefix:
        return _empty_result(ok=False, complete=False, status="failed",
                             error=prefix_msg, errors=[prefix_msg],
                             out=out, state_path=state_path, elapsed=time.time() - t0)

    cfg_note = set_spec or "（全库）"

    # ---- 1. 准备 state ----
    warnings = []
    if state is not None:
        st = state
    elif resume:
        try:
            st = HarvestState.load(state_path)
        except Exception as exc:
            return _empty_result(error="无法 resume：{}".format(exc), errors=[str(exc)],
                                 out=out, state_path=state_path, elapsed=time.time() - t0)
        # ★ 未显式指定 ⇒ 继承 checkpoint 的采集口径（set / schema）
        if not explicit_prefix:
            metadata_prefix = st.metadata_prefix or DEFAULT_PREFIX
        if not explicit_set:
            set_spec = st.set_spec or ""
        # 显式改了口径且与 checkpoint 不一致 ⇒ 旧的 resumptionToken 属于另一批
        # 数据，继续用会把两次不同范围的采集串在一起，必须作废重采。
        if (metadata_prefix != (st.metadata_prefix or DEFAULT_PREFIX)
                or set_spec != (st.set_spec or "")):
            warnings.append(
                "显式指定的 set/schema 与断点不同（断点 {!r}/{!r} → 本次 {!r}/{!r}），"
                "已作废全部分页 token 并从头重采".format(
                    st.metadata_prefix, st.set_spec, metadata_prefix, set_spec))
            for w in st.windows:
                w["token"] = ""
                w["status"] = "pending"
        # 继承来的值也要过一遍校验（checkpoint 可能来自旧版本或被手工改过）
        ok2, msg2 = validate_prefix(metadata_prefix)
        if not ok2:
            return _empty_result(ok=False, complete=False, status="failed",
                                 error=msg2, errors=[msg2],
                                 out=out, state_path=state_path,
                                 elapsed=time.time() - t0)
        st.metadata_prefix = metadata_prefix
        st.set_spec = set_spec
        if verbose:
            print("  · 从 checkpoint 续采：{} 个窗口，已完成 {}/{}"
                  .format(len(st.windows), st.progress()["done"], len(st.windows)))
    else:
        # 起点 / 终点
        if since_last:
            if not start_date:
                info = resolve_since_last(state_path, end_date=end_date, today=today)
                if not info["ok"]:
                    return _empty_result(error=info["error"], errors=[info["error"]],
                                         out=out, state_path=state_path,
                                         elapsed=time.time() - t0)
                if info.get("note"):
                    warnings.append(info["note"])
                start_date, end_date = info["start"], info["end"]
            # 增量模式下把新窗口**追加**到已有 checkpoint，而不是丢弃未采完的窗口
            old = HarvestState.try_load(state_path)
            if old is not None:
                st = old
                # 同样尊重"未显式指定"：不要用默认值把已有口径冲掉
                if not explicit_prefix:
                    metadata_prefix = st.metadata_prefix or DEFAULT_PREFIX
                if not explicit_set:
                    set_spec = st.set_spec or ""
                st.metadata_prefix = metadata_prefix
                st.set_spec = set_spec
            else:
                st = HarvestState({
                    "start": start_date, "end": end_date, "window_days": window_days,
                    "metadata_prefix": metadata_prefix, "set_spec": set_spec})
        else:
            if not start_date:
                return _empty_result(
                    error="必须给 start_date（或改用 since_last=True / resume=True）",
                    errors=["缺少 start_date"], out=out, state_path=state_path,
                    elapsed=time.time() - t0)
            st = HarvestState({
                "start": start_date, "end": end_date or start_date,
                "window_days": window_days, "metadata_prefix": metadata_prefix,
                "set_spec": set_spec})
        end_date = end_date or start_date
        try:
            end_date = clamp_until(end_date, today=today, verbose=verbose)
            start_date = clamp_until(start_date, today=today, verbose=verbose)
        except ValueError as exc:
            return _empty_result(error=str(exc), errors=[str(exc)],
                                 out=out, state_path=state_path, elapsed=time.time() - t0)
        try:
            plan = plan_windows(start_date, end_date, window_days)
        except ValueError as exc:
            return _empty_result(error=str(exc), errors=[str(exc)],
                                 out=out, state_path=state_path, elapsed=time.time() - t0)
        if not plan:
            return _empty_result(error="日期区间为空（start > end）：{} → {}"
                                       .format(start_date, end_date),
                                 errors=["空区间"], out=out, state_path=state_path,
                                 elapsed=time.time() - t0)
        # 已有窗口（增量追加场景）不重复添加
        existing = {(w["from"], w["until"]) for w in st.windows}
        added = 0
        for f, u in plan:
            if (f, u) not in existing:
                st.windows.append(_new_window(f, u))
                added += 1
        st.start = min([st.start, start_date]) if st.start else start_date
        st.end = max([st.end, end_date]) if st.end else end_date
        st.window_days = window_days
        st.metadata_prefix = metadata_prefix
        st.set_spec = set_spec
        if verbose:
            print("  · 规划 {} 个窗口（新追加 {} 个）：{} → {}，窗口长度 {} 天，set={}"
                  .format(len(st.windows), added, start_date, end_date, window_days, cfg_note))
        for w in warnings:
            print("  ⚠️ {}".format(w))

    # ---- 2. dry-run：只打印，不碰网络 ----
    if dry_run:
        if verbose:
            section("dry-run：仅规划窗口，不发请求")
            for i, w in enumerate(st.windows, 1):
                print("  [{:>3}] {} → {}   status={}  token={}  records={}"
                      .format(i, w["from"], w["until"], w["status"],
                              "有" if w["token"] else "无", w["records"]))
            print("  落盘文件 : {}".format(out))
            print("  断点文件 : {}".format(state_path))
            print("  共 {} 个窗口（max_records={} max_pages={}）"
                  .format(len(st.windows), max_records or "不限", max_pages or "不限"))
        return _empty_result(ok=True, complete=False, status="dry_run", error="",
                             windows_total=len(st.windows),
                             windows_done=st.progress()["done"],
                             out=out, state_path=state_path, state=st,
                             elapsed=time.time() - t0)

    # ---- 3. 打开落盘器 ----
    try:
        writer = RecordWriter(out, gzip_out=gzip_out, dedupe=dedupe,
                              preload=preload, verbose=verbose)
    except Exception as exc:
        return _empty_result(error="无法打开输出文件：{}".format(exc), errors=[str(exc)],
                             out=out, state_path=state_path, elapsed=time.time() - t0)

    cfg = {"metadata_prefix": metadata_prefix, "set_spec": set_spec,
           "timeout": timeout, "retries": retries, "backoff": backoff,
           "sleep": sleep, "max_records": max_records, "max_pages_left": None,
           "split_threshold": split_threshold, "max_splits": max_splits,
           "max_restarts": max_restarts, "state_path": state_path,
           "verbose": verbose, "email": email, "today": today}

    stop_reason = ""
    errors = []
    # ★ 本次运行的页数预算必须用**局部计数器**。
    #   早期实现拿 state.stats['pages']（跨轮次累计）去和 max_pages 比：
    #   第二次以 max_pages=1 续采时，历史累计已经 ≥1，循环第一次判断就直接
    #   停住，一页都不请求 —— "固定预算的周期续采"永远没有进展。
    run_pages = 0
    try:
        idx = st.next_pending_index(0)
        guard = 0
        guard_max = max(2000, len(st.windows) * 8 + 200)   # 防御性死循环上限
        while idx is not None:
            guard += 1
            if guard > guard_max:
                errors.append("内部保护：循环次数超过 {} 次，已强制停止".format(guard_max))
                stop_reason = STOP_ERROR
                break
            if max_records and writer.written >= max_records:
                stop_reason = STOP_MAX_RECORDS
                break
            if max_pages and run_pages >= max_pages:
                stop_reason = STOP_MAX_PAGES
                break
            cfg["max_pages_left"] = None
            if max_pages:
                cfg["max_pages_left"] = max(0, max_pages - run_pages)
            outcome = _harvest_one_window(st, idx, writer, cfg)
            run_pages += int(outcome.get("pages") or 0)
            if outcome.get("status") == "split":
                continue        # 同一个 idx（已被替换成两个更小的窗口）
            if outcome.get("error"):
                errors.append("[{} → {}] {}".format(st.windows[idx]["from"],
                                                    st.windows[idx]["until"],
                                                    outcome["error"]))
                if stop_on_error:
                    stop_reason = STOP_ERROR
                    break
            if outcome.get("stop_reason"):
                stop_reason = outcome["stop_reason"]
                break
            if max_records and writer.written >= max_records:
                stop_reason = STOP_MAX_RECORDS
                break
            idx = st.next_pending_index(idx + 1)
    finally:
        writer.close()
        try:
            st.save(state_path)
        except Exception as exc:
            errors.append("checkpoint 写盘失败：{}".format(exc))

    prog = st.progress()
    complete = (prog["done"] == prog["total"] and prog["total"] > 0
                and not stop_reason)
    ok = not errors
    if stop_reason == STOP_ERROR or (prog["failed"] and prog["done"] == 0 and errors):
        status = "failed"
    elif complete:
        status = "done"
    else:
        status = "partial"
    err_text = ""
    if errors:
        err_text = ("{} 个窗口失败，示例：{}".format(prog["failed"] or len(errors), errors[0])
                    if prog["failed"] else errors[0])
    elif stop_reason == STOP_MAX_RECORDS:
        err_text = "达到 max_records={} 上限，已停在窗口中途（保留 token，可 resume 续采）".format(max_records)
    elif stop_reason == STOP_MAX_PAGES:
        err_text = "达到 max_pages={} 上限，可 resume 续采".format(max_pages)

    if verbose:
        section("采集结果")
        kv("状态", status)
        kv("ok / complete", "{} / {}".format(ok, complete))
        kv("窗口", "完成 {} / 共 {}（失败 {}）".format(prog["done"], prog["total"], prog["failed"]))
        kv("写盘", "{} 条（收 {}，去重 {}，更新版本 {}，删除事件 {}）".format(
            writer.written, writer.seen, writer.duplicates,
            writer.updated, writer.deleted))
        kv("页数 / 请求", "{} / {}".format(prog["pages"], st.stats.get("requests")))
        kv("停止原因", stop_reason or "（无，已采完）")
        kv("输出", out)
        kv("断点", state_path)
        if err_text:
            kv("提示", err_text)

    return _empty_result(ok=ok, complete=complete, status=status, error=err_text,
                         errors=errors, stop_reason=stop_reason,
                         collected=writer.seen, written=writer.written,
                         duplicates=writer.duplicates, seen=writer.seen,
                         updated=writer.updated, deleted=writer.deleted,
                         pages=prog["pages"], requests=st.stats.get("requests", 0),
                         windows_total=prog["total"], windows_done=prog["done"],
                         windows_failed=prog["failed"],
                         out=out, state_path=state_path, state=st,
                         elapsed=time.time() - t0)


def pages_seen_global(cfg: dict, state: HarvestState) -> int:
    """**历史累计**已请求页数（跨轮次，永不重置）。

    ⚠️ 这个值**不能**用来做 max_pages 预算 —— 参见 harvest() 里的 run_pages。
       曾经误用它做预算，导致"第二次以固定 max_pages 续采"时一页都不请求。
       它的用途只剩报表（"这个 checkpoint 一共请求过多少页"）。
    """
    return int(state.stats.get("pages") or 0)


# =============================================================================
# 七、统计与校验
# =============================================================================

def stats(path: str = DEFAULT_OUT, state_path: str = "", top: int = 20) -> dict:
    """对已落盘的 JSONL 做统计（流式读取，不把文件读进内存）。

    参数
    ----
    path       : JSONL（或 .gz / gzip 内容）路径
    state_path : 可选 checkpoint 路径；给了就额外算窗口完成率
    top        : 分类分布 / 年份分布各返回前多少个（按数量降序）

    返回
    ----
    {
      "ok","path","lines","records","unique_ids","duplicates","bad_lines",
      "blank_lines","bytes",
      "by_category" : {primary category: n}   ← 按记录的第一分类（primary）统计
      "by_year"     : {"2020": n, "unknown": n} ← 按 **created** 分组
      "created_min","created_max",
      "with_doi","with_journal_ref","with_abstract","with_harvest",
      "state" : {窗口完成率等} 或 None
    }

    ⚠️ by_year 用的是记录里的 `created`（首次提交年），**不是**采集窗口的年份。
       原因见文件头语义 1：窗口过滤的是 datestamp，两者不是一回事。

    用途：采完先看分布是否符合预期（比如某分类为 0 说明 set 写错了）；
          unique_ids vs records 能直接看出重复率。
    """
    result = {"ok": False, "path": os.path.abspath(path), "lines": 0, "records": 0,
              "unique_ids": 0, "duplicates": 0, "bad_lines": 0, "blank_lines": 0,
              "bytes": 0, "by_category": {}, "by_year": {}, "created_min": "",
              "created_max": "", "with_doi": 0, "with_journal_ref": 0,
              "with_abstract": 0, "with_harvest": 0, "state": None, "error": ""}
    p = os.path.abspath(path)
    if not os.path.isfile(p):
        result["error"] = "文件不存在：{}".format(p)
        return result
    result["bytes"] = os.path.getsize(p)
    # id -> 版本：与 verify_output 保持同一口径，否则同一份文件
    # stats 报 duplicates=1 而 verify 报 0，谁都不敢信。
    ids = {}
    result.setdefault("superseded", 0)
    result["deletions"] = read_deletions(path)
    cats = {}
    years = {}
    try:
        for raw in _iter_jsonl(p):
            result["lines"] += 1
            line = raw.strip()
            if not line:
                result["blank_lines"] += 1
                continue
            try:
                rec = json.loads(line)
            except Exception:
                result["bad_lines"] += 1
                continue
            if not isinstance(rec, dict):
                result["bad_lines"] += 1
                continue
            result["records"] += 1
            rid = rec.get("id")
            if rid:
                version = _version_of(rec)
                if rid in ids:
                    known = ids[rid]
                    # 同 id 不同版本 = 增量更新（正常）；同 id 同版本 = 真重复
                    if version and known and version != known:
                        result["superseded"] += 1
                    else:
                        result["duplicates"] += 1
                    if version and version > known:
                        ids[rid] = version
                else:
                    ids[rid] = version
            c = rec.get("categories") or []
            if isinstance(c, list) and c:
                key = c[0] or "unknown"
            else:
                key = "unknown"
            cats[key] = cats.get(key, 0) + 1
            created = str(rec.get("created") or "")
            y = created[:4] if re.match(r"^\d{4}", created) else "unknown"
            years[y] = years.get(y, 0) + 1
            if created:
                if not result["created_min"] or created < result["created_min"]:
                    result["created_min"] = created
                if not result["created_max"] or created > result["created_max"]:
                    result["created_max"] = created
            if rec.get("doi"):
                result["with_doi"] += 1
            if rec.get("journal_ref"):
                result["with_journal_ref"] += 1
            if rec.get("abstract"):
                result["with_abstract"] += 1
            if isinstance(rec.get("_harvest"), dict):
                result["with_harvest"] += 1
    except Exception as exc:
        result["error"] = "读取失败：{}".format(exc)
        return result

    result["unique_ids"] = len(ids)
    result["by_category"] = dict(sorted(cats.items(), key=lambda kv_: -kv_[1])[:max(1, top)])
    result["by_year"] = dict(sorted(years.items(), key=lambda kv_: kv_[0]))
    if state_path:
        st = HarvestState.try_load(state_path)
        if st is not None:
            prog = st.progress()
            prog["last_success_until"] = st.last_success_until
            prog["start"] = st.start
            prog["end"] = st.end
            prog["metadata_prefix"] = st.metadata_prefix
            prog["set_spec"] = st.set_spec
            prog["checkpoint_stats"] = st.stats
            result["state"] = prog
    result["ok"] = result["bad_lines"] == 0
    return result


def read_deletions(path: str = DEFAULT_OUT) -> dict:
    """读取旁路删除账本 `<out>.deletions.jsonl`。

    参数
    ----
    path : 主语料路径（账本路径 = path + ".deletions.jsonl"）

    返回 {"path", "exists", "events", "unique_ids", "bad_lines", "identifiers", "error"}

    ★ 为什么必须有这个函数：
      采集器把删除事件写进账本之后，**必须有人去读**，否则就是"只写不读"——
      被删除的记录仍然留在主语料里，而没有任何地方会提示这件事。
      语义上也别过度解读：OAI 的 deleted header 表示"该元数据记录被删除"，
      可能是记录合并/迁移/下线，**不一定是学术撤稿**，所以这里只做事实呈现，
      由下游决定怎么处理（通常是从最新视图里剔除，或人工确认）。
    """
    dp = os.path.abspath(path) + ".deletions.jsonl"
    out = {"path": dp, "exists": os.path.isfile(dp), "events": 0,
           "unique_ids": 0, "bad_lines": 0, "identifiers": [], "error": ""}
    if not out["exists"]:
        return out
    seen = set()
    try:
        with open(dp, "r", encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    out["bad_lines"] += 1
                    continue
                out["events"] += 1
                ident = obj.get("identifier") or ""
                if ident and ident not in seen:
                    seen.add(ident)
                    if len(out["identifiers"]) < 20:
                        out["identifiers"].append(ident)
    except Exception as exc:
        out["error"] = "读取失败：{}".format(exc)
        return out
    out["unique_ids"] = len(seen)
    return out


def verify_output(path: str = DEFAULT_OUT, max_bad: int = 20) -> dict:
    """流式校验 JSONL 产出：能不能逐行 parse、有没有坏行/重复 id/缺字段。

    参数
    ----
    path    : JSONL（或 .gz）路径
    max_bad : 最多记录多少条坏行样本（防止一个坏文件刷屏）

    返回
    ----
    {
      "ok"      : bool  —— 没有坏行、没有缺 id、没有重复 id 才为 True
      "path","bytes","lines","records","blank_lines",
      "bad_lines": int,
      "bad_samples": [{"line": n, "reason": "...", "raw": "前 120 字"}],
      "missing_id": int, "missing_harvest": int,
      "duplicates": int, "duplicate_samples": [id, ...],
      "superseded": int, "superseded_samples": [id, ...]
    }

    两份"同 id"必须区分开（否则会把正常的增量更新误判成损坏）：
      duplicates  : 同 id **且版本相同** —— 真正的重复写入，是缺陷
      superseded  : 同 id **但版本更新** —— 增量同步的正常产物（旧版+新版都在）

    ⚠️ 本函数**只校验文件格式与唯一性**，不校验采集覆盖率。
       "格式没问题" ≠ "语料采全了" —— 覆盖率要看 checkpoint 的窗口完成率
       （`status` / `stats` 里的窗口统计），两者是不同维度的验收。

    实现：**流式逐行**处理，一次只在内存里保留 (id → version) 表
    （要检重复就必须记住所有 id，这是本函数唯一的 O(n) 内存，已在文档注明）。
    不会把整个文件读进内存，所以几个 GB 的 JSONL 也能跑。

    用途：采集完 / 传输后跑一次，确认没有半截行（进程被杀常见产物）。
    """
    p = os.path.abspath(path)
    out = {"ok": False, "path": p, "bytes": 0, "lines": 0, "records": 0,
           "blank_lines": 0, "bad_lines": 0, "bad_samples": [], "missing_id": 0,
           "missing_harvest": 0, "duplicates": 0, "duplicate_samples": [],
           "superseded": 0, "superseded_samples": [], "error": ""}
    if not os.path.isfile(p):
        out["error"] = "文件不存在：{}".format(p)
        return out
    out["bytes"] = os.path.getsize(p)
    # id -> 版本标记：既用于查重，也用于区分"重复"与"更新版本"
    ids = {}
    try:
        for raw in _iter_jsonl(p):
            out["lines"] += 1
            line = raw.strip()
            if not line:
                out["blank_lines"] += 1
                continue
            try:
                rec = json.loads(line)
            except Exception as exc:
                out["bad_lines"] += 1
                if len(out["bad_samples"]) < max_bad:
                    out["bad_samples"].append({
                        "line": out["lines"], "reason": "JSON 解析失败: {}".format(exc),
                        "raw": raw.strip()[:120]})
                continue
            if not isinstance(rec, dict):
                out["bad_lines"] += 1
                if len(out["bad_samples"]) < max_bad:
                    out["bad_samples"].append({
                        "line": out["lines"], "reason": "顶层不是对象（可能是数组/字符串）",
                        "raw": raw.strip()[:120]})
                continue
            out["records"] += 1
            rid = rec.get("id")
            if not rid:
                out["missing_id"] += 1
            else:
                version = _version_of(rec)
                if rid in ids:
                    known = ids[rid]
                    # 版本不同 ⇒ 是同一篇论文的更新版本，属于正常增量产物
                    if version and known and version != known:
                        out["superseded"] += 1
                        if len(out["superseded_samples"]) < max_bad:
                            out["superseded_samples"].append(rid)
                    else:
                        out["duplicates"] += 1
                        if len(out["duplicate_samples"]) < max_bad:
                            out["duplicate_samples"].append(rid)
                    if version and version > known:
                        ids[rid] = version
                else:
                    ids[rid] = version
            if not isinstance(rec.get("_harvest"), dict):
                out["missing_harvest"] += 1
    except Exception as exc:
        out["error"] = "读取失败：{}".format(exc)
        return out
    # ★ missing_harvest 也纳入判定：没有 _harvest 的记录无法溯源，
    #   违背「记录论文是怎么找到的」的要求，不能算通过。
    out["ok"] = (out["bad_lines"] == 0 and out["missing_id"] == 0
                 and out["duplicates"] == 0 and out["missing_harvest"] == 0)
    out["unique_ids"] = len(ids)
    # 合并删除账本：只做事实呈现，不影响 ok（删除是正常事件，不是文件损坏）
    out["deletions"] = read_deletions(path)
    return out


# =============================================================================
# 八、自检（默认完全离线，不依赖网络）
# =============================================================================

def check(online: bool = False) -> int:
    """离线自检：切窗正确性 / 断点推进 / 去重落盘 / 坏行检出 / gzip。

    参数
    ----
    online : True 时额外探一次网络（oai_identify + 拉一页），默认 False

    返回
    ----
    int：失败项数量（0 = 全通过），可直接当进程退出码

    为什么坚持离线：CI / 无网环境也要能验证核心逻辑（切窗、断点、落盘、校验），
    网络只影响"能不能拉到数据"，不该影响"逻辑对不对"。所有断言都在临时目录里做，
    跑完自动清理，不留产物。
    """
    failed = 0
    tmp = tempfile.mkdtemp(prefix="arxiv_harvest_check_")

    def _fail(title, detail):
        nonlocal failed
        failed += 1
        print("  ✗ {}：{}".format(title, detail))

    def _pass(title, detail=""):
        print("  ✓ {}{}".format(title, ("  —— " + detail) if detail else ""))

    try:
        # ---------------- 1) plan_windows ----------------
        section("1) plan_windows 切窗")
        w = plan_windows("2026-08-01", "2026-09-01", days=30)
        expect = [("2026-08-01", "2026-08-30"), ("2026-08-31", "2026-09-01")]
        if w == expect:
            _pass("30 天窗口", "{}".format(w))
        else:
            _fail("30 天窗口", "得到 {}，期望 {}".format(w, expect))

        w2 = plan_windows("2026-01-01", "2026-01-31", days=7)
        expect2 = [("2026-01-01", "2026-01-07"), ("2026-01-08", "2026-01-14"),
                   ("2026-01-15", "2026-01-21"), ("2026-01-22", "2026-01-28"),
                   ("2026-01-29", "2026-01-31")]
        if w2 == expect2:
            _pass("7 天窗口", "{} 个窗口".format(len(w2)))
        else:
            _fail("7 天窗口", "得到 {}".format(w2))

        # 不重叠 + 无缝隙 + 覆盖端点（对随机区间做一次性质检查）
        ok_cover = True
        for days in (1, 3, 30, 400):
            seq = plan_windows("2025-02-25", "2026-03-03", days=days)
            if seq[0][0] != "2025-02-25" or seq[-1][1] != "2026-03-03":
                ok_cover = False
                break
            for i in range(len(seq)):
                if seq[i][1] < seq[i][0]:
                    ok_cover = False
            for i in range(1, len(seq)):
                prev = parse_date(seq[i - 1][1])
                cur = parse_date(seq[i][0])
                if (cur - prev).days != 1:
                    ok_cover = False
        if ok_cover:
            _pass("覆盖性/不重叠/无缝隙", "days=1,3,30,400 均成立")
        else:
            _fail("覆盖性/不重叠/无缝隙", "存在断点或重叠")

        if plan_windows("2026-09-01", "2026-09-01") == [("2026-09-01", "2026-09-01")]:
            _pass("单日区间")
        else:
            _fail("单日区间", "{}".format(plan_windows("2026-09-01", "2026-09-01")))
        if plan_windows("2026-09-02", "2026-09-01") == []:
            _pass("start > end 返回空")
        else:
            _fail("start > end", "应返回 []")
        try:
            plan_windows("2026-09-01", "2026-09-02", days=0)
            _fail("days=0", "应抛 ValueError")
        except ValueError:
            _pass("days=0 抛 ValueError")
        try:
            parse_date("2026-02-30")
            _fail("非法日期", "2026-02-30 应抛 ValueError")
        except ValueError:
            _pass("非法日期抛 ValueError")
        if clamp_until("2099-01-01", today="2026-09-28", verbose=False) == "2026-09-28":
            _pass("clamp_until 把未来日期夹到今天")
        else:
            _fail("clamp_until", "未生效")

        # ---------------- 2) 前缀校验 ----------------
        section("2) metadataPrefix 校验")
        if validate_prefix("arXiv")[0] and validate_prefix("oai_dc")[0]:
            _pass("arXiv / oai_dc 放行")
        else:
            _fail("arXiv / oai_dc", "应放行")
        ok_raw, msg_raw = validate_prefix("arXivRaw")
        if (not ok_raw) and "arXivRaw" in msg_raw:
            _pass("arXivRaw 被拒绝（不静默出空）", msg_raw[:40] + "…")
        else:
            _fail("arXivRaw", "应被拒绝并给出原因")

        # ---------------- 3) HarvestState 存取与断点推进 ----------------
        section("3) HarvestState 断点存取与推进")
        sp = os.path.join(tmp, "st.json")
        st = HarvestState.from_plan("2026-08-01", "2026-09-01",
                                    plan_windows("2026-08-01", "2026-09-01", 30),
                                    30, "arXiv", "cs:cs:AI")
        st.save(sp)
        if os.path.exists(sp) and not os.path.exists(sp + ".tmp"):
            _pass("save 原子写（无残留 .tmp）")
        else:
            _fail("save 原子写", "残留 {}.tmp 或未写出".format(sp))
        st2 = HarvestState.load(sp)
        if (len(st2.windows) == 2 and st2.set_spec == "cs:cs:AI"
                and st2.metadata_prefix == "arXiv"):
            _pass("load 还原字段", "{} 个窗口".format(len(st2.windows)))
        else:
            _fail("load 还原字段", "windows={} set={}".format(len(st2.windows), st2.set_spec))

        idx, win = st2.next_pending_window()
        if idx == 0 and win["from"] == "2026-08-01":
            _pass("next_pending_window 指向第一个窗口")
        else:
            _fail("next_pending_window", "得到 {} {}".format(idx, win))
        # 模拟"采到第 2 页就被中断"：token 必须能存回来
        st2.mark(0, status="in_progress", token="tok-abc", records=1300, attempts=2)
        st2.save(sp)
        st3 = HarvestState.load(sp)
        if (st3.windows[0]["token"] == "tok-abc"
                and st3.windows[0]["status"] == "in_progress"
                and st3.windows[0]["records"] == 1300):
            _pass("断点粒度到 token", "token/status/records 均已持久化")
        else:
            _fail("断点粒度到 token", "{}".format(st3.windows[0]))
        st3.mark(0, status="done", records=2600, token="")
        st3.note_last_success(st3.windows[0]["until"])
        i2, w2_ = st3.next_pending_window()
        if i2 == 1 and st3.last_success_until == "2026-08-30":
            _pass("mark done 后推进到下一个窗口 + last_success_until")
        else:
            _fail("推进", "idx={} last_ok={}".format(i2, st3.last_success_until))
        st3.mark(1, status="done")
        st3.save(sp)        # 落盘后再读回，验证 last_success_until / 完成率确实持久化了
        st_reloaded = HarvestState.load(sp)
        if (st_reloaded.next_pending_window() == (None, None)
                and st_reloaded.progress()["completion_rate"] == 1.0
                and st_reloaded.last_success_until == "2026-08-30"):
            _pass("全部完成后 next 返回 (None, None)，完成率 100%",
                  "last_success_until={}".format(st_reloaded.last_success_until))
        else:
            _fail("完成率", "{}".format(st_reloaded.progress()))
        try:
            st3.mark(0, status="bogus")
            _fail("非法状态", "应抛 ValueError")
        except ValueError:
            _pass("非法状态被拒绝")
        # 二分切分
        st4 = HarvestState.from_plan("2026-08-01", "2026-08-31",
                                     [("2026-08-01", "2026-08-31")], 31)
        if st4.split_window(0) and len(st4.windows) == 2:
            a, b = st4.windows
            if (parse_date(b["from"]) - parse_date(a["until"])).days == 1:
                _pass("split_window 二分且不重叠", "{} | {}".format(a["until"], b["from"]))
            else:
                _fail("split_window", "子窗口重叠")
        else:
            _fail("split_window", "未切分")
        st5 = HarvestState.from_plan("2026-09-01", "2026-09-01",
                                     [("2026-09-01", "2026-09-01")], 1)
        if st5.split_window(0) is False:
            _pass("单日窗口拒绝再切")
        else:
            _fail("单日窗口", "不应能切")
        st6 = HarvestState.from_plan("2026-09-01", "2026-09-02",
                                     [("2026-09-01", "2026-09-02")], 2)
        if st6.split_window(0) and [w["from"] for w in st6.windows] == ["2026-09-01", "2026-09-02"]:
            _pass("两天窗口可切成两天", "{}".format([(w["from"], w["until"]) for w in st6.windows]))
        else:
            _fail("两天窗口切分", "{}".format([(w["from"], w["until"]) for w in st6.windows]))

        # ---------------- 4) 落盘 / 去重 / gzip ----------------
        section("4) RecordWriter 落盘 / 去重 / gzip")
        jp = os.path.join(tmp, "a.jsonl")
        recs = [{"id": "2609.00001", "title": "A", "categories": ["cs.AI"],
                 "created": "2026-09-01", "abstract": "x"},
                {"id": "2609.00002", "title": "B", "categories": ["cs.LG"],
                 "created": "2026-09-01", "abstract": "y"},
                {"id": "2609.00001", "title": "A dup", "categories": ["cs.AI"],
                 "created": "2026-09-01", "abstract": "x"}]
        with RecordWriter(jp, dedupe=True) as w_:
            written, dup, _ = w_.write(recs, window={"from": "2026-09-01", "until": "2026-09-02"},
                                    page=1, metadata_prefix="arXiv", set_spec="cs:cs:AI")
        if (written, dup) == (2, 1):
            _pass("去重", "写 {} 条 / 跳过 {} 条".format(written, dup))
        else:
            _fail("去重", "written={} dup={}".format(written, dup))
        with open(jp, "r", encoding="utf-8") as fh:
            lines = [json.loads(x) for x in fh if x.strip()]
        if (len(lines) == 2 and lines[0]["_harvest"]["from"] == "2026-09-01"
                and lines[0]["_harvest"]["set_spec"] == "cs:cs:AI"
                and lines[0]["_harvest"]["source"].startswith("oai-pmh")):
            _pass("JSONL + _harvest provenance", "{}".format(lines[0]["_harvest"]))
        else:
            _fail("_harvest provenance", "{}".format(lines[0].get("_harvest")))

        gp = os.path.join(tmp, "b.jsonl.gz")
        with RecordWriter(gp, gzip_out=True, dedupe=True) as w_:
            w_.write(recs[:2], window={"from": "2026-09-01", "until": "2026-09-01"}, page=1)
        vg = verify_output(gp)
        if vg["ok"] and vg["records"] == 2:
            _pass("gzip 写 + 流式读回", "records={}".format(vg["records"]))
        else:
            _fail("gzip", "{}".format(vg))
        # 预载已有 id ⇒ 再采不会重复写
        with RecordWriter(jp, dedupe=True, preload=True) as w_:
            written2, dup2, _ = w_.write(recs, window={"from": "2026-09-01", "until": "2026-09-01"},
                                      page=1)
        if (written2, dup2) == (0, 3):
            _pass("preload 跨轮次去重", "写 0 / 跳过 3")
        else:
            _fail("preload", "written={} dup={}".format(written2, dup2))

        # ---------------- 5) verify_output 坏行检出 ----------------
        section("5) verify_output 坏行 / 重复 id 检出")
        bp = os.path.join(tmp, "bad.jsonl")
        with open(bp, "w", encoding="utf-8") as fh:
            fh.write('{"id": "2609.1", "title": "ok"}\n')
            fh.write('{"id": "2609.1", "title": "dup"}\n')   # 重复 id
            fh.write('this is not json\n')                    # 坏行
            fh.write('{"title": "no id"}\n')                  # 缺 id
            fh.write("\n")                                    # 空行
        vb = verify_output(bp)
        if (vb["bad_lines"] == 1 and vb["duplicates"] == 1 and vb["missing_id"] == 1
                and vb["blank_lines"] == 1 and vb["ok"] is False):
            _pass("坏行/重复/缺 id 全部检出",
                  "bad={} dup={} noid={} blank={}".format(vb["bad_lines"], vb["duplicates"],
                                                          vb["missing_id"], vb["blank_lines"]))
        else:
            _fail("坏行检出", "{}".format({k: vb[k] for k in
                                          ("bad_lines", "duplicates", "missing_id", "blank_lines")}))
        vmiss = verify_output(os.path.join(tmp, "nope.jsonl"))
        if vmiss["ok"] is False and vmiss["error"]:
            _pass("不存在的文件被明确报错")
        else:
            _fail("不存在的文件", "{}".format(vmiss))

        # ---------------- 6) stats / resolve_since_last ----------------
        section("6) stats 与 --since-last 推算")
        s = stats(jp)
        if (s["ok"] and s["records"] == 2 and s["unique_ids"] == 2
                and s["by_category"].get("cs.AI") == 1 and s["by_year"].get("2026") == 2):
            _pass("stats 分布", "cat={} year={}".format(s["by_category"], s["by_year"]))
        else:
            _fail("stats", "{}".format({k: s[k] for k in
                                        ("records", "unique_ids", "by_category", "by_year")}))
        s2 = stats(jp, state_path=sp)
        if (s2["state"] and s2["state"]["completion_rate"] == 1.0
                and s2["state"]["last_success_until"] == "2026-08-30"):
            _pass("stats 附带窗口完成率",
                  "{:.0%}，last_success_until={}".format(s2["state"]["completion_rate"],
                                                         s2["state"]["last_success_until"]))
        else:
            _fail("stats + state", "{}".format(s2["state"]))
        info = resolve_since_last(sp, end_date="2026-09-10", today="2026-09-28")
        if info["ok"] and info["start"] and info["start"] > "2026-08-01":
            _pass("resolve_since_last", "{} → {}".format(info["start"], info["end"]))
        else:
            _fail("resolve_since_last", "{}".format(info))

        # ---------------- 7) dry-run 不发请求 ----------------
        section("7) dry_run 只规划不发请求")
        dr = harvest(start_date="2026-08-01", end_date="2026-09-01",
                     out=os.path.join(tmp, "dry.jsonl"), window_days=30,
                     dry_run=True, verbose=False)
        if (dr["status"] == "dry_run" and dr["windows_total"] == 2
                and not os.path.exists(os.path.join(tmp, "dry.jsonl"))):
            _pass("dry_run", "规划 {} 个窗口且未创建输出文件".format(dr["windows_total"]))
        else:
            _fail("dry_run", "status={} total={}".format(dr["status"], dr["windows_total"]))
        bad_prefix = harvest(start_date="2026-08-01", end_date="2026-09-01",
                             metadata_prefix="arXivRaw", dry_run=True, verbose=False,
                             out=os.path.join(tmp, "dry2.jsonl"))
        if bad_prefix["status"] == "failed" and "arXivRaw" in bad_prefix["error"]:
            _pass("harvest 层拒绝 arXivRaw（不静默出空）")
        else:
            _fail("harvest arXivRaw", "{}".format(bad_prefix["status"]))

        # ---------------- 8) 可选联网 ----------------
        if online:
            section("8) 联网探测（--online）")
            ident = arxiv.oai_identify()
            if ident.get("status") == 200:
                _pass("OAI Identify", "{} 最早 {}".format(ident.get("repository"),
                                                          ident.get("earliest")))
            else:
                _fail("OAI Identify", "{}".format(ident))
            page = oai_page(from_date="2026-09-01", until_date="2026-09-01")
            if page["status"] == 200 and page["ok"] and page["count"]:
                _pass("oai_page 一页", "{} 条，还有下一页: {}".format(page["count"],
                                                                     bool(page["token"])))
            else:
                _fail("oai_page", "{}".format({k: page.get(k) for k in
                                               ("status", "ok", "count", "error")}))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "-" * 78)
    print("arxiv_harvest 离线自检：{}".format("全部通过" if failed == 0
                                              else "{} 项失败".format(failed)))
    return failed


# =============================================================================
# 九、命令行入口
# =============================================================================

def _opt(argv, name, default=None):
    """从 argv 里取 '--name value' 的 value；没有该开关、或后面跟的是另一个开关时返回 default。"""
    if name in argv:
        i = argv.index(name)
        if i + 1 < len(argv) and not argv[i + 1].startswith("--"):
            return argv[i + 1]
    return default


def _flag(argv, name) -> bool:
    """argv 里是否有开关 name。"""
    return name in argv


def _int_opt(argv, name, default):
    """取整数开关；解析失败时返回 default（不让一个手滑的参数崩掉整轮采集）。"""
    v = _opt(argv, name)
    try:
        return int(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def _float_opt(argv, name, default):
    """取浮点开关；解析失败时返回 default。"""
    v = _opt(argv, name)
    try:
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


_USAGE = """arxiv_harvest.py —— arXiv OAI-PMH 增量采集器

用法:
  python3 arxiv_harvest.py plan   --start 2026-08-01 --end 2026-09-01 [--days 30] [--state S]
  python3 arxiv_harvest.py run    --start YYYY-MM-DD --end YYYY-MM-DD [选项]
  python3 arxiv_harvest.py run    --since-last [--end YYYY-MM-DD] [选项]
  python3 arxiv_harvest.py resume [--state S] [--out F] [选项]
  python3 arxiv_harvest.py status [--state S]
  python3 arxiv_harvest.py stats  [--out F] [--state S] [--top N]
  python3 arxiv_harvest.py verify [--out F]
  python3 arxiv_harvest.py sets   [--max-pages N]
  python3 arxiv_harvest.py check  [--online]

run/resume 常用选项:
  --out F             落盘 JSONL（默认脚本同目录 harvest.jsonl；.gz 或 --gzip 压缩）
  --state S           断点文件（默认 <out>.state.json）
  --window-days N     窗口长度，默认 30 天
  --set SPEC          OAI set，如 cs:cs:AI（用 sets 子命令查取值）
  --metadata-prefix P arXiv | oai_dc（arXivRaw 会被拒绝）
  --max-records N     全局硬上限（严格遵守）
  --max-pages N       全局页数上限
  --retries N         网络重试次数（默认 3，指数退避）
  --backoff S         退避基数秒（默认 3.0）
  --sleep S           页间限速秒（默认 1.0）
  --timeout S         单页超时秒（默认 180）
  --split-threshold N 首页条数阈值，超过则二分窗口（默认 1200）
  --since-last        从 checkpoint 的 last_success_until+1 采到今天
  --preload           开始前载入已有 out 的 id，做跨轮次去重
                      （resume 时**默认开启**；用 --no-preload 关闭）
  --no-preload        续采时不载入已有 id —— 会重复写入，仅在需要重写时用
  --gzip              强制 gzip 输出
  --no-dedupe         关闭去重（可能写入重复论文）
  --stop-on-error     任一窗口失败就停
  --dry-run           只规划窗口并打印，不发请求
  --quiet             少打印
"""


def _print_plan(plan, title="窗口规划"):
    """把 plan_windows() 的结果打成对齐的表。"""
    section("{}（共 {} 个窗口）".format(title, len(plan)))
    total_days = 0
    for i, (f, u) in enumerate(plan, 1):
        d = (parse_date(u) - parse_date(f)).days + 1
        total_days += d
        print("  [{:>3}] {} → {}   {} 天".format(i, f, u, d))
    if plan:
        print("  合计覆盖 {} 天（{} → {}）".format(total_days, plan[0][0], plan[-1][1]))


def main(argv) -> int:
    """CLI 入口。返回进程退出码：0 = 成功 / 无错，1 = 有错，2 = 用法错误。

    子命令：plan / run / resume / status / stats / verify / sets / check
    每个子命令的选项见 _USAGE。退出码约定：
      - check   → 失败项数量（0 = 全通过）
      - run     → 0 表示"没有发生错误"（被 max-records 主动停住也算 0），
                  有窗口失败或致命错误 → 1
      - verify  → 0 表示校验通过
    """
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(_USAGE)
        return 0
    cmd, rest = argv[0], argv[1:]
    email = get_key("PAPERSEARCH_EMAIL")
    default_state = os.path.join(_HERE, "harvest.jsonl.state.json")

    # ---------------- check ----------------
    if cmd == "check":
        return check(online=_flag(rest, "--online"))

    # ---------------- plan ----------------
    if cmd == "plan":
        start = _opt(rest, "--start")
        end = _opt(rest, "--end", start)
        days = _int_opt(rest, "--days", DEFAULT_WINDOW_DAYS)
        if not start:
            print("用法: python3 arxiv_harvest.py plan --start 2026-08-01 --end 2026-09-01 [--days 30]")
            return 2
        end = clamp_until(end, verbose=True)
        try:
            plan = plan_windows(start, end, days)
        except ValueError as exc:
            print("参数错误：{}".format(exc))
            return 2
        if not plan:
            print("日期区间为空（start > end）：{} → {}".format(start, end))
            return 2
        _print_plan(plan, "窗口规划 {} → {}，每窗 {} 天".format(start, end, days))
        state_path = _opt(rest, "--state")
        if state_path:
            st = HarvestState.from_plan(
                start, end, plan, days,
                _opt(rest, "--metadata-prefix", DEFAULT_PREFIX), _opt(rest, "--set", ""))
            st.save(state_path)
            kv("已写断点", os.path.abspath(state_path))
        print("\n  提示：OAI 的 from/until 过滤的是 datestamp（入库/更新时间），")
        print("        不是论文首次提交时间 created；同一篇论文可能落在多个窗口，")
        print("        采集时由 --no-dedupe 关闭前的默认去重兜底。")
        return 0

    # ---------------- run / resume ----------------
    if cmd in ("run", "resume"):
        out = os.path.abspath(_opt(rest, "--out", DEFAULT_OUT))
        state_path = os.path.abspath(_opt(rest, "--state", out + ".state.json"))
        quiet = _flag(rest, "--quiet")
        kwargs = dict(
            out=out, state_path=state_path,
            metadata_prefix=_opt(rest, "--metadata-prefix", DEFAULT_PREFIX),
            set_spec=_opt(rest, "--set", "") or "",
            window_days=_int_opt(rest, "--window-days", _int_opt(rest, "--days",
                                                                 DEFAULT_WINDOW_DAYS)),
            max_records=_int_opt(rest, "--max-records", 0),
            max_pages=_int_opt(rest, "--max-pages", 0),
            retries=_int_opt(rest, "--retries", DEFAULT_RETRIES),
            backoff=_float_opt(rest, "--backoff", DEFAULT_BACKOFF),
            sleep=_float_opt(rest, "--sleep", DEFAULT_SLEEP),
            timeout=_float_opt(rest, "--timeout", DEFAULT_TIMEOUT),
            gzip_out=_flag(rest, "--gzip"),
            dedupe=not _flag(rest, "--no-dedupe"),
            # 三态：--preload=True / --no-preload=False / 都不给=None（自动）
            preload=(True if _flag(rest, "--preload")
                     else (False if _flag(rest, "--no-preload") else None)),
            split_threshold=_int_opt(rest, "--split-threshold", DEFAULT_SPLIT_THRESHOLD),
            stop_on_error=_flag(rest, "--stop-on-error"),
            dry_run=_flag(rest, "--dry-run"),
            verbose=not quiet,
            email=email,
        )
        if cmd == "resume":
            kwargs["resume"] = True
            if not os.path.isfile(state_path):
                print("找不到断点文件：{}（先跑一次 run）".format(state_path))
                return 2
            # ★ 续采时必须默认预载已有 id。
            #   原因：页内被 max_records 截断时会**保留进入本页的 token**，
            #   下次 resume 会重取同一页；若不预载去重集合，
            #   已写过的记录会被再写一遍 —— 静默产生重复数据。
            #   （实测：不加 --preload 的 resume 写出 20 条，其中 10 条是重复的。）
            #   预载的成本是扫一遍已有 out 文件（流式，可接受），
            #   收益是"重复写入"这个静默错误不会发生；
            #   确实需要重写全部记录时用 --no-preload 显式关闭。
            # preload 交给 harvest() 自动判定（None ⇒ resume 默认开启），
            # 这里只在用户显式 --preload 时才覆盖
            if _flag(rest, "--preload"):
                kwargs["preload"] = True
        else:
            start = _opt(rest, "--start")
            end = _opt(rest, "--end", "")
            kwargs["start_date"] = start or ""
            kwargs["end_date"] = end or ""
            if _flag(rest, "--since-last"):
                kwargs["since_last"] = True
                kwargs["preload"] = True
            elif not start:
                print("用法: python3 arxiv_harvest.py run --start 2026-08-01 --end 2026-09-01")
                print("     或 python3 arxiv_harvest.py run --since-last")
                return 2
        r = harvest(**kwargs)
        return 0 if r["ok"] else 1

    # ---------------- status ----------------
    if cmd == "status":
        state_path = os.path.abspath(_opt(rest, "--state", default_state))
        st = HarvestState.try_load(state_path)
        if st is None:
            print("没有可读的断点文件：{}".format(state_path))
            return 1
        prog = st.progress()
        section("采集断点状态")
        kv("断点文件", state_path)
        kv("区间", "{} → {}（每窗 {} 天）".format(st.start, st.end, st.window_days))
        kv("schema / set", "{} / {}".format(st.metadata_prefix, st.set_spec or "（全库）"))
        kv("最后成功 until", st.last_success_until or "（无）")
        kv("窗口", "完成 {} / 共 {}（进行 {} / 待采 {} / 失败 {}）".format(
            prog["done"], prog["total"], prog["in_progress"], prog["pending"], prog["failed"]))
        kv("完成率", "{:.1%}".format(prog["completion_rate"]))
        kv("累计写盘", "{} 条（页 {} / 去重 {}）".format(prog["records"], prog["pages"],
                                                        prog["duplicates"]))
        kv("created / updated", "{} / {}".format(st.created_at, st.updated_at))
        idx, win = st.next_pending_window()
        if win is None:
            kv("下一步", "已全部采完")
        else:
            kv("下一个窗口", "[{}] {} → {}  状态={}  token={}".format(
                idx, win["from"], win["until"], win["status"],
                (win["token"][:32] + "…") if win["token"] else "无"))
        print("\n  窗口明细：")
        for i, w in enumerate(st.windows, 1):
            print("  [{:>3}] {} → {}  {:<11} 记录 {:<7} 页 {:<4} 重试 {} {}"
                  .format(i, w["from"], w["until"], w["status"], w["records"],
                          w["pages"], w["attempts"], ("err=" + w["error"][:40]) if w["error"] else ""))
        return 0

    # ---------------- stats ----------------
    if cmd == "stats":
        out = os.path.abspath(_opt(rest, "--out", DEFAULT_OUT))
        state_path = _opt(rest, "--state", out + ".state.json")
        if not os.path.isfile(state_path):
            state_path = ""
        s = stats(out, state_path=state_path, top=_int_opt(rest, "--top", 20))
        section("JSONL 统计")
        if not os.path.isfile(out):
            kv("错误", s["error"])
            return 1
        kv("文件", out)
        kv("大小", "{:,} bytes".format(s["bytes"]))
        kv("行数 / 记录", "{} / {}".format(s["lines"], s["records"]))
        kv("唯一 id", "{}（重复 {}，坏行 {}，空行 {}）".format(
            s["unique_ids"], s["duplicates"], s["bad_lines"], s["blank_lines"]))
        kv("created 范围", "{} → {}".format(s["created_min"] or "-", s["created_max"] or "-"))
        kv("字段覆盖", "abstract {} / doi {} / journal_ref {} / _harvest {}".format(
            s["with_abstract"], s["with_doi"], s["with_journal_ref"], s["with_harvest"]))
        print("\n  按 primary category（前 {}）：".format(_int_opt(rest, "--top", 20)))
        for c, n in sorted(s["by_category"].items(), key=lambda kv_: -kv_[1]):
            print("    {:<24} {}".format(c, n))
        print("\n  按 created 年份：")
        for y, n in sorted(s["by_year"].items()):
            print("    {:<24} {}".format(y, n))
        if s["state"]:
            prog = s["state"]
            print("\n  窗口完成率：{:.1%}（完成 {} / 共 {}，失败 {}，最后成功 until {}）".format(
                prog["completion_rate"], prog["done"], prog["total"],
                prog["failed"], prog["last_success_until"] or "-"))
        return 0 if s["ok"] else 1

    # ---------------- verify ----------------
    if cmd == "verify":
        out = os.path.abspath(_opt(rest, "--out", DEFAULT_OUT))
        v = verify_output(out)
        section("JSONL 格式与唯一性校验")
        kv("文件", out)
        if v["error"]:
            kv("错误", v["error"])
            return 1
        kv("大小", "{:,} bytes".format(v["bytes"]))
        kv("行数 / 记录", "{} / {}".format(v["lines"], v["records"]))
        kv("唯一 id", v.get("unique_ids", 0))
        kv("坏行 / 空行", "{} / {}".format(v["bad_lines"], v["blank_lines"]))
        kv("缺 id / 缺 _harvest", "{} / {}".format(v["missing_id"], v["missing_harvest"]))
        kv("重复 id（同版本）", v["duplicates"])
        kv("更新版本（同 id 新版本）", v["superseded"])
        dl = v.get("deletions") or {}
        kv("删除事件账本", "{}（{} 事件 / {} 个标识）".format(
            "有" if dl.get("exists") else "无",
            dl.get("events", 0), dl.get("unique_ids", 0)))
        if dl.get("identifiers"):
            print("    已删除示例：{}".format(", ".join(dl["identifiers"][:5])))
            print("    ⚠️ 这些标识仍可能出现在上面的语料里；本命令只报告，不做剔除")
        for s_ in v["bad_samples"][:5]:
            print("    ✗ 第 {} 行：{}  |  {}".format(s_["line"], s_["reason"], s_["raw"]))
        if v["duplicate_samples"]:
            print("    重复示例：{}".format(", ".join(v["duplicate_samples"][:10])))
        if v["superseded"]:
            print("    更新示例：{}".format(", ".join(v["superseded_samples"][:10])))
        # ★ 措辞收窄：本命令只证明"文件格式与唯一性没问题"，
        #   **不证明语料采全了**。覆盖率是另一个维度，看 status/stats 的窗口完成率。
        #   早期输出"完整（可安全下游使用）"会让人把"格式 OK 的部分语料"
        #   当成"完整语料"。
        print("\n  结论：{}".format(
            "文件格式与唯一性检查通过（注意：这不代表采集覆盖率达标，"
            "覆盖率请看 status / stats 的窗口完成率）" if v["ok"]
            else "存在问题，见上"))
        return 0 if v["ok"] else 1

    # ---------------- sets ----------------
    if cmd == "sets":
        r = list_sets(max_pages=_int_opt(rest, "--max-pages", 5), email=email)
        section("OAI ListSets")
        kv("状态", "ok" if r["ok"] else r["error"])
        kv("数量", "{} 个（{} 页）".format(r["count"], r["pages"]))
        for s_ in r["sets"][:40]:
            print("    {:<28} {}".format(s_["set_spec"], s_["set_name"]))
        if r["count"] > 40:
            print("    … 还有 {} 个".format(r["count"] - 40))
        print("\n  提示：--set 直接传 setSpec，例如 --set cs:cs:AI")
        return 0 if r["ok"] else 1

    print("未知子命令 '{}'\n".format(cmd))
    print(_USAGE)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
