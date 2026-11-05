#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""_common.py —— 共享基础设施（不是数据源，是给其他 py 复用的工具箱）

本文件提供三样东西：
  1. http_request / get_json  —— 统一的 HTTP 调用层（重试、退避、超时、User-Agent）
  2. load_keys / get_key      —— 统一读取 Keys.txt
  3. section / show / mask    —— 统一的打印输出格式

为什么要有这一层：
  - 所有数据源都要处理 429 / 超时 / 重试，写一遍就够；
  - 所有数据源都要读 Key，集中管理避免每个文件各写一套；
  - 只依赖标准库 urllib，不需要 pip install requests，迁移到任何机器都能跑。

不依赖任何第三方库，Python 3.8+ 可用。

─────────────────────────────────────────────────────────────────────────────
函数索引（快速定位）
  [HTTP]  http_request(url, method, headers, data, timeout, retries, backoff, email)
          统一请求层：重试 / 退避 / Retry-After / 超时 → (status, body, headers)
  [HTTP]  curl_get(url, timeout, user_agent, headers)
          curl 子进程兜底传输；**检查退出码**，部分传输不当作成功
  [HTTP]  get_json(url, ...) / url_with(base, params)
          JSON 解析封装 / 安全拼接 query string
  [凭证]  find_keys_file / load_keys / get_key / mask
          Keys.txt 查找与解析、环境变量优先级、密钥脱敏
  [工具]  _retry_after_seconds(headers)
          解析 Retry-After（秒数或 HTTP 日期两种格式）
  [输出]  section / kv / show_records / jprint / eprint
          统一的打印格式
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# =============================================================================
# 一、Keys.txt 读取
# =============================================================================
# 查找顺序（找到第一个就停）：
#   1. 参数显式传入的路径
#   2. 与 _common.py 同目录的 Keys.txt
#   3. 上一级目录的 Keys.txt（兼容 PaperSearch/Keys.txt 是唯一真源的情况）
#
# 文件格式：KEY=VALUE，一行一条
#   - 以 # 开头的行是注释
#   - 空行忽略
#   - VALUE 首尾的引号会被自动去掉，但建议直接写裸值
# =============================================================================

_HERE = os.path.dirname(os.path.abspath(__file__))
_KEYS_CANDIDATES = [
    os.path.join(_HERE, "Keys.txt"),
    os.path.join(os.path.dirname(_HERE), "Keys.txt"),
]

_KEYS_CACHE: dict = {}
_KEYS_LOADED = False


def find_keys_file(explicit: str = "") -> str:
    """返回第一个存在的 Keys.txt 路径；都没有则返回空字符串。"""
    for path in ([explicit] if explicit else []) + _KEYS_CANDIDATES:
        if path and os.path.isfile(path):
            return path
    return ""


def load_keys(explicit: str = "", reload: bool = False) -> dict:
    """把 Keys.txt 解析成 {KEY: VALUE} 字典；同名 KEY 后者覆盖前者。

    优先级：本目录 Keys.txt > 上级目录 Keys.txt（按 _KEYS_CANDIDATES 顺序）。
    """
    global _KEYS_CACHE, _KEYS_LOADED
    if _KEYS_LOADED and not reload and not explicit:
        return _KEYS_CACHE

    path = find_keys_file(explicit)
    data: dict = {}
    if path:
        with open(path, "r", encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                data[k.strip().upper()] = v.strip().strip('"').strip("'")
    _KEYS_CACHE, _KEYS_LOADED = data, True
    return data


def get_key(name: str, explicit: str = "", default: str = "") -> str:
    """按优先级取一个凭证：环境变量 > Keys.txt > default。

    环境变量用同名大写，例如 get_key("OPENALEX_API_KEY") 会先看
    环境变量 OPENALEX_API_KEY，再看 Keys.txt 里的同名键。
    """
    return (os.environ.get(name)
            or load_keys(explicit).get(name)
            or default)


def mask(secret: str) -> str:
    """脱敏打印：只留前 3 位和后 3 位，避免日志/截图泄漏密钥。"""
    if not secret:
        return "（未提供）"
    if len(secret) <= 8:
        return secret[:2] + "*" * max(1, len(secret) - 2)
    return "{}…{}（len={}）".format(secret[:3], secret[-3:], len(secret))


# =============================================================================
# 二、HTTP 调用层
# =============================================================================
# 设计要点：
#   - 默认 30s 超时；对慢接口（OAI-PMH 一次 4MB）要显式传 timeout=120
#   - 对 429 / 5xx 自动重试，重试间隔递增（backoff × 第几次）
#   - 返回 (status_code, body_bytes, headers_dict)，status_code=0 表示网络层错误
#   - 不抛异常，让调用方自己判断，便于写成"优雅降级"的采集流程
# =============================================================================

USER_AGENT_TEMPLATE = "ResearchOS-PaperSearch/0.1 (+{email})"

# 服务器返回 Retry-After 时，最多愿意等多少秒。
# 设上限是为了避免被一个离谱的值（比如 3600）挂死整个采集任务；
# 超过上限时按上限等待，重试仍失败则由上层决定是否放弃该请求。
MAX_RETRY_AFTER = 60.0


def _retry_after_seconds(headers) -> float:
    """从响应头解析 Retry-After。

    支持两种格式（RFC 7231）：
        秒数        Retry-After: 30
        HTTP 日期   Retry-After: Wed, 21 Oct 2026 07:28:00 GMT
    返回 None 表示没有该头或无法解析。
    """
    if not headers:
        return None
    raw = None
    try:
        # urllib 的 headers 是大小写不敏感的 email.message.Message
        raw = headers.get("Retry-After")
    except Exception:
        for k, v in dict(headers).items():
            if k.lower() == "retry-after":
                raw = v
                break
    if not raw:
        return None
    raw = str(raw).strip()
    try:
        return float(raw)
    except ValueError:
        pass
    try:
        from email.utils import parsedate_to_datetime
        import datetime
        when = parsedate_to_datetime(raw)
        if when is None:
            return None
        delta = (when - datetime.datetime.now(datetime.timezone.utc)).total_seconds()
        return max(0.0, delta)
    except Exception:
        return None


def http_request(url: str,
                 method: str = "GET",
                 headers: dict = None,
                 data: bytes = None,
                 timeout: float = 30.0,
                 retries: int = 2,
                 backoff: float = 3.0,
                 email: str = "",
                 verify_ssl: bool = True):
    """统一的 HTTP 请求。

    参数
    ----
    url      : 完整 URL（含 query string）
    method   : "GET" / "POST"
    headers  : 额外请求头，例如 {"x-api-key": "..."} 或 {"Authorization": "Bearer ..."}
    data     : POST 的 body（bytes）；做 multipart 上传时由调用方自己拼
    timeout  : 单次请求超时秒数。基准 30s；OAI-PMH 建议 120s，本地 GROBID 建议 8s
    retries  : 失败后额外重试次数。0 = 只试一次
    backoff  : 重试基础间隔秒数，实际等待 backoff × 第几次重试
    email    : 放进 User-Agent，让对方能联系到你（polite pool 礼仪）
    verify_ssl: 遇到自签名证书（公司代理/中间人）时可设 False；
                本机访问 huggingface.co 就会报自签名错误，属于环境问题

    返回
    ----
    (status_code, body_bytes, headers_dict)
    status_code = 0 表示连接超时/DNS 失败等网络层错误，body 里是错误描述
    """
    hdrs = {
        "User-Agent": USER_AGENT_TEMPLATE.format(email=email or "anonymous"),
        # 显式禁用压缩，省得自己解 gzip；OAI-PMH 的 4MB 响应会稍慢一点但更好调试
        "Accept-Encoding": "identity",
    }
    if headers:
        hdrs.update(headers)

    ctx = None
    if not verify_ssl:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

    last_err = ""
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                return resp.status, resp.read(), dict(resp.headers)
        except urllib.error.HTTPError as exc:
            # 有响应但状态码不是 2xx —— 这是"接口告诉你不行了"，通常要读 body
            body = b""
            try:
                body = exc.read()
            except Exception:
                pass
            if exc.code in (429, 500, 502, 503) and attempt < retries:
                last_err = "HTTP {} (重试中)".format(exc.code)
                # ★ 服务器可能明确要求等更久（OpenAlex 会返回 Retry-After: 30~35）。
                #   盲目按 3/6 秒重试会被继续拒绝，所以取两者较大值，并设上限防止挂死。
                wait = backoff * (attempt + 1)
                retry_after = _retry_after_seconds(exc.headers)
                if retry_after is not None:
                    wait = max(wait, min(retry_after, MAX_RETRY_AFTER))
                time.sleep(wait)
                continue
            return exc.code, body, dict(exc.headers or {})
        except Exception as exc:
            # 连响应都没有：超时 / DNS / TLS
            last_err = "{}: {}".format(type(exc).__name__, exc)
            if attempt < retries:
                time.sleep(backoff * (attempt + 1))
                continue
            return 0, last_err.encode("utf-8"), {}
    return 0, last_err.encode("utf-8"), {}


def get_json(url: str, headers: dict = None, timeout: float = 30.0,
             retries: int = 2, backoff: float = 3.0, email: str = ""):
    """GET 一个 JSON 接口。

    返回 (status, data, raw_text, headers)
      - 解析成功：data 是 dict/list，raw_text 是原始文本
      - 解析失败：data 为 None，raw_text 是前 300 字符，方便肉眼排查错误页
    """
    status, body, hdrs = http_request(url, headers=headers, timeout=timeout,
                                      retries=retries, backoff=backoff, email=email)
    text = body.decode("utf-8", "replace")
    if status != 200:
        return status, None, text[:300], hdrs
    try:
        return status, json.loads(text), text, hdrs
    except Exception as exc:
        return status, None, "JSON 解析失败: {} | 前 300 字: {}".format(exc, text[:300]), hdrs


def url_with(base: str, params: dict) -> str:
    """安全拼接 query string：自动处理中文/空格/特殊字符，自动丢掉空值。"""
    clean = {k: v for k, v in (params or {}).items() if v is not None}
    if not clean:
        return base
    return base + ("&" if "?" in base else "?") + urllib.parse.urlencode(clean)


# =============================================================================
# 二·补、curl 兜底传输（应对 TLS 指纹拦截）
# =============================================================================
# 背景：本库实测发现 **export.arxiv.org 会按 TLS 指纹拦截 Python 的 ssl 客户端**：
#     同一时刻、同一 URL，
#         curl   → HTTP 200
#         urllib → HTTP 406 Not Acceptable（空 body）
#     OpenAI 的某些网关、部分 Cloudflare/Fastly 站点也有类似行为。
#     这是 WAF 行为，不是代码错误，改 header 无效。
#
# 解决办法：给请求加一个 curl 子进程兜底。curl 几乎在所有 macOS/Linux 上都有，
# 不引入任何 Python 依赖。只在主通道失败时才调用，不污染正常路径。
# =============================================================================

def curl_available() -> bool:
    import shutil
    return shutil.which("curl") is not None


def curl_get(url: str, timeout: float = 45.0, user_agent: str = "",
             headers: dict = None):
    """用 curl 子进程发 GET，作为 urllib 被 WAF 拦截时的兜底。

    参数
    ----
    url        : 完整 URL
    timeout    : curl --max-time
    user_agent : User-Agent；留空用 curl 默认
    headers    : 额外请求头 {"Key": "Value"}

    返回 (status, body_bytes, headers_dict)
      status=0 表示**传输未成功**（curl 不存在 / 非零退出 / 分片传输），
      body 里是错误描述；headers 里带 curl_exit 便于诊断。
      这里不做自动重试 —— 重试逻辑由上层控制，避免嵌套重试放大延迟

    ⚠️ 关键点：**必须检查 curl 的退出码，不能只看 -w 打印的 HTTP 状态码。**
       curl 退出码 18（部分传输）、28（超时）、56（接收数据失败）等情况下，
       -w 仍可能打印 200，但 body 是**残缺的**。
       把它们当成功会导致下游解析半截内容却毫无察觉。
       常见退出码：0 成功 / 6 DNS / 7 连接失败 / 18 部分传输 / 28 超时 / 35 TLS
    """
    import shutil
    import subprocess
    import tempfile
    exe = shutil.which("curl")
    if not exe:
        return 0, "curl 未安装，无法使用兜底传输".encode("utf-8"), {}

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".body")
    tmp.close()
    cmd = [exe, "-sS", "-L", "--max-time", str(int(timeout)), "-o", tmp.name,
           "-w", "%{http_code}"]
    if user_agent:
        cmd += ["-A", user_agent]
    for k, v in (headers or {}).items():
        cmd += ["-H", "{}: {}".format(k, v)]
    cmd.append(url)
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=timeout + 15)
        code_txt = proc.stdout.decode("utf-8", "replace").strip()
        http_status = int(code_txt) if code_txt.isdigit() else 0
        err_txt = proc.stderr.decode("utf-8", "replace").strip()

        if proc.returncode != 0:
            # 传输没完成 → 一律算失败，无论 -w 报了什么状态码
            msg = "curl 退出码 {}（-w 报告 HTTP {}）：{}".format(
                proc.returncode, http_status or "?", err_txt[:200] or "无 stderr")
            return 0, msg.encode("utf-8"), {
                "curl_exit": str(proc.returncode),
                "curl_http_status": str(http_status),
            }

        with open(tmp.name, "rb") as fh:
            body = fh.read()
        if http_status == 0:
            return 0, (err_txt or "curl 未返回 HTTP 状态码").encode("utf-8"), {
                "curl_exit": "0", "curl_http_status": "0"}
        return http_status, body, {"curl_exit": "0",
                                   "curl_http_status": str(http_status)}
    except Exception as exc:
        return 0, "curl 执行失败: {}".format(exc).encode("utf-8"), {}
    finally:
        try:
            os.unlink(tmp.name)
        except Exception:
            pass


# =============================================================================
# 三、打印辅助（让每个数据源脚本输出格式一致）
# =============================================================================

def section(title: str, width: int = 78) -> None:
    """打印一个醒目的分节标题，例如 section("1) 关键词检索")。"""
    print("\n" + "=" * width)
    print(title)
    print("=" * width)


def kv(label: str, value, indent: int = 2) -> None:
    """打印一行 "标签 : 值"，值太长自动截断到 100 字符。"""
    text = str(value)
    if len(text) > 160:
        text = text[:159] + "…"
    print("{}{:<{w}}: {}".format(" " * indent, label, text, w=18))


def show_records(records: list, fields: list, limit: int = 5) -> None:
    """以紧凑表格形式打印记录列表，每条只显示 fields 里指定的字段。

    用于在示例脚本里快速确认"返回的字段到底长什么样"。
    """
    for i, rec in enumerate(records[:limit], 1):
        parts = []
        for f in fields:
            v = rec.get(f)
            if isinstance(v, (list, tuple)):
                v = "{}项".format(len(v))
            elif isinstance(v, dict):
                v = "{}键".format(len(v))
            if v in (None, ""):
                v = "-"
            s = str(v)
            parts.append("{}={}".format(f, s if len(s) <= 42 else s[:41] + "…"))
        print("  [{}] {}".format(i, " | ".join(parts)))
    if len(records) > limit:
        print("  … 还有 {} 条未显示".format(len(records) - limit))


def jprint(obj, max_chars: int = 900) -> None:
    """缩进打印 JSON，用于展示"原始返回长什么样"；超长自动截断。"""
    text = json.dumps(obj, ensure_ascii=False, indent=2)
    print(text if len(text) <= max_chars else text[:max_chars] + "\n…（已截断）")


def is_main() -> bool:
    return __name__ == "__main__"


def eprint(*args) -> None:
    print(*args, file=sys.stderr)
