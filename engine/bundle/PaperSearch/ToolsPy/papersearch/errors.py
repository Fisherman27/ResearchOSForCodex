#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
errors.py —— 错误码表 + 修法建议（给 Agent 看的）

定位
----
这是 ToolsPy **错误处理契约**的一半（另一半是 result.py 的信封结构）。
目标只有一个：**Agent 拿到错误后能自己判断下一步做什么**，不用来问人。

所以每个错误码必须携带三样东西：
    1. `retryable` —— 该不该重试（重试是有代价的：耗时、可能触发限流）
    2. `hint`      —— 该怎么办，用**可执行的措辞**（不是"检查网络"这种废话）
    3. `severity`  —— fatal（这一步做不成）/ degradable（可降级继续）/ input（用户输入问题）

为什么用稳定的字符串码而不是异常类
----------------------------------
异常类在跨模块传递时会被包成各种类型，Agent 还要解析 traceback 才知道发生了什么。
字符串码是**契约**：可以用 == 判断、可以写进文档、可以跨语言（将来接别的 runtime）。
traceback 仍然保留（在 `cause` 里），但那是给人 debug 用的，不是给 Agent 分支用的。

函数索引
─────────────────────────────────────────────────────────────────────────────
  [码表]  CODES                     全部错误码 → {severity, retryable, hint}
  [构造]  make_error(code, **kw)    生成标准化的 error 字典
  [建议]  suggest(code)             取某个码的修法建议
  [归一]  normalize_exception(exc)  把任意异常映射成错误码
  [自检]  all_codes()               列出全部码（供文档/自检用）
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

from typing import Any, Dict, Optional

# =============================================================================
# 一、错误码表
# =============================================================================
# 命名规范：<域>_<具体情形>，全大写下划线。
# 域用来分组，Agent 可以先看域再决定策略：
#   INPUT_   调用方参数问题 —— 重试无用，必须改参数
#   AUTH_    凭证问题       —— 重试无用，必须配 key
#   NET_     网络/传输问题   —— 通常可重试
#   RATE_    限流           —— 可重试但**必须等待**
#   NOTFOUND_ 资源不存在     —— 重试无用
#   PARSE_   解析失败       —— 通常不可重试（内容本身有问题）
#   SERVICE_ 外部服务问题    —— 服务恢复后可重试
#   DATA_    数据质量问题    —— 可降级
#   INTERNAL_ 本库的问题     —— 这是个 bug，应上报

CODES: Dict[str, Dict[str, Any]] = {
    # ---------------- 输入 ----------------
    "INPUT_MISSING": {
        "severity": "input", "retryable": False,
        "hint": "必填参数为空。检查调用参数（见 error.context.missing）。",
    },
    "INPUT_INVALID": {
        "severity": "input", "retryable": False,
        "hint": "参数取值非法。对照 API.md 里该工具的「参数」小节。",
    },
    "INPUT_NOT_FOUND": {
        "severity": "input", "retryable": False,
        "hint": "传入的本地路径不存在。确认路径是否拼错、文件是否已下载。",
    },
    "INPUT_UNSUPPORTED": {
        "severity": "input", "retryable": False,
        "hint": "该组合/取值本版本不支持。看 error.hint 里的替代做法。",
    },

    # ---------------- 凭证 ----------------
    "AUTH_MISSING_KEY": {
        "severity": "fatal", "retryable": False,
        "hint": ("缺少 API key。在 Keys.txt 里补上对应字段（见 error.context.key_name），"
                 "或改用不依赖该源的路径。"),
    },
    "AUTH_INVALID_KEY": {
        "severity": "fatal", "retryable": False,
        "hint": "key 无效（服务端返回 401）。核对 Keys.txt 里的值是否过期/写错。",
    },
    "AUTH_FORBIDDEN": {
        "severity": "fatal", "retryable": False,
        "hint": "服务端拒绝（403）。多为该接口需要更高权限或 token；换接口或补权限。",
    },

    # ---------------- 网络 ----------------
    "NET_TIMEOUT": {
        "severity": "degradable", "retryable": True,
        "hint": "请求超时，可重试。若反复超时，调大 timeout 或换源。",
    },
    "NET_UNREACHABLE": {
        "severity": "degradable", "retryable": True,
        "hint": "网络不可达，可重试；持续失败则检查网络/代理。",
    },
    "NET_HTTP_ERROR": {
        "severity": "degradable", "retryable": True,
        "hint": "服务端返回错误状态码。5xx 可重试；4xx 通常重试无用（看 error.context.status）。",
    },
    "NET_BAD_RESPONSE": {
        "severity": "degradable", "retryable": True,
        "hint": ("HTTP 通了但内容不是预期格式（常见：网关以 200 返回 HTML 错误页）。"
                 "可重试；持续出现说明该源或代理有问题。"),
    },
    "NET_TRUNCATED": {
        "severity": "degradable", "retryable": True,
        "hint": "响应不完整（传输中断）。可重试；大文件建议带断点续传。",
    },

    # ---------------- 限流 ----------------
    "RATE_LIMITED": {
        "severity": "degradable", "retryable": True,
        "hint": ("被限流。**必须等待**再重试 —— 优先按 error.context.retry_after 秒等待；"
                 "匿名调用建议配置 key（OpenAlex/S2 都有免费额度）。"),
    },
    "RATE_QUOTA_EXHAUSTED": {
        "severity": "fatal", "retryable": False,
        "hint": "配额用尽（如 OpenAlex 每日 $1）。等次日重置，或改用其他源。",
    },

    # ---------------- 查找 ----------------
    "NOTFOUND_DOI": {
        "severity": "degradable", "retryable": False,
        "hint": "该 DOI 在源里不存在。核对 DOI；或改用标题检索。",
    },
    "NOTFOUND_PDF": {
        "severity": "degradable", "retryable": False,
        "hint": ("没有可用的开放获取全文。**这不是 bug** —— 闭源论文拿不到。"
                 "下游应把该论文标记为「无全文」并继续处理其他论文。"),
    },
    "NOTFOUND_EMPTY_RESULT": {
        "severity": "degradable", "retryable": False,
        "hint": ("检索命中 0 条（成功的空集，不是错误）。放宽查询词/年份，或换源。"
                 "注意与 NET_BAD_RESPONSE 区分：后者是请求失败，这是请求成功但没结果。"),
    },

    # ---------------- 解析 ----------------
    "PARSE_PDF_INVALID": {
        "severity": "degradable", "retryable": True,
        "hint": ("PDF 不完整或结构损坏（缺 %%EOF / 打不开 / 0 页）。"
                 "可重试下载；若换了源仍失败，该 PDF 本身有问题。"),
    },
    "PARSE_TEI_INVALID": {
        "severity": "degradable", "retryable": False,
        "hint": "GROBID 返回的 TEI 不是合法 XML（常见于响应被截断）。重跑该篇解析。",
    },
    "PARSE_NO_COORDS": {
        "severity": "degradable", "retryable": False,
        "hint": ("TEI 里没有坐标 —— 解析时**没请求** tei_coordinates。"
                 "坐标不是默认输出，要在 params 里给 coordinate_elements。"),
    },
    "PARSE_EMPTY": {
        "severity": "degradable", "retryable": False,
        "hint": ("解析成功但内容为空（0 字符/0 章节）。可能是扫描版 PDF（无文字层）"
                 "或该文档本身没有正文。换一篇，或标记为需 OCR。"),
    },

    # ---------------- 外部服务 ----------------
    "SERVICE_UNAVAILABLE": {
        "severity": "fatal", "retryable": True,
        "hint": "依赖的服务没起来。GROBID 启动：在 ExamplePy 下跑 ./grobid-serve.sh start（约 20 秒）。",
    },
    "SERVICE_DEGRADED": {
        "severity": "degradable", "retryable": False,
        "hint": "服务可用但功能降级（如 GROBID 不可用时退回纯文本）。产物里会标明降级原因。",
    },

    # ---------------- 数据质量 ----------------
    "DATA_PARTIAL": {
        "severity": "degradable", "retryable": False,
        "hint": "只拿到了部分数据（如分页中断、某源失败）。**结果可用**，但检查 diagnostics 看缺了什么。",
    },
    "DATA_CONFLICT": {
        "severity": "degradable", "retryable": False,
        "hint": "多源字段冲突（如两个不同的 arXiv ID）。已按保守策略处理，详见 error.context。",
    },
    "DATA_LOW_QUALITY": {
        "severity": "degradable", "retryable": False,
        "hint": "数据可能不可靠（如坐标只识别到图注、摘要缺失）。按需人工复核。",
    },

    # ---------------- 本库自身 ----------------
    "INTERNAL_ERROR": {
        "severity": "fatal", "retryable": False,
        "hint": ("ToolsPy 内部异常 —— **这是 bug**。把 error.cause 里的 traceback "
                 "和 error.context 一起反馈，可直接定位到代码行。"),
    },
    "INTERNAL_NOT_IMPLEMENTED": {
        "severity": "fatal", "retryable": False,
        "hint": "该能力尚未实现。看 API.md 里该工具的「状态」标注。",
    },
}


def suggest(code: str) -> str:
    """取某个错误码的修法建议（未知码给通用建议，不抛异常）。

    为什么未知码也返回而不是报错：错误处理路径本身不该再产生错误。
    """
    c = CODES.get(code)
    if c:
        return c["hint"]
    return ("未知错误码 {}。请把 code + message + cause 一起反馈；"
            "先用 result.pretty() 打印完整信封。".format(code))


def make_error(code: str, message: str = "", *, stage: str = "",
               component: str = "", context: Optional[dict] = None,
               cause: Optional[BaseException] = None,
               hint: str = "", override_retryable: Optional[bool] = None,
               **extra) -> Dict[str, Any]:
    """构造标准化的 error 字典。

    参数
    ----
    code      : 必须是 CODES 里的键（拼错会退化成 INTERNAL_ERROR 并保留原码，
                这样既不掩盖问题、也不会因为码表疏漏而崩）
    message   : 人类可读描述
    stage     : 出错的阶段名（多步工具里用来定位是第几步）
    component : 出错的模块名
    context   : 相关输入/参数的快照 —— **Agent 靠它判断"我传了什么导致这样"**
    cause     : 原始异常对象（traceback 会被保留）
    hint      : 覆盖码表里的默认建议（当这次的具体做法不同时用）
    override_retryable : 覆盖码表判断（例如明确知道这次不该重试）

    返回
    ----
    {
      "code", "message", "severity", "retryable", "hint",
      "stage", "component", "context",
      "cause": {"type", "message", "traceback"} | None,
      "at": ISO 时间
    }
    """
    import traceback as _tb
    import time as _t

    known = code in CODES
    meta = CODES.get(code) or CODES["INTERNAL_ERROR"]
    err: Dict[str, Any] = {
        "code": code if known else "INTERNAL_ERROR",
        "message": message or meta.get("message", ""),
        "severity": meta["severity"],
        "retryable": (meta["retryable"] if override_retryable is None
                      else bool(override_retryable)),
        "hint": hint or meta["hint"],
        "stage": stage,
        "component": component,
        "context": context or {},
        "cause": None,
        "at": _t.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    if not known:
        # 保留原码，方便发现"码表漏了一个"
        err["unknown_code"] = code
    if cause is not None:
        err["cause"] = {
            "type": type(cause).__name__,
            "message": str(cause)[:500],
            "traceback": "".join(_tb.format_exception(
                type(cause), cause, cause.__traceback__))[-2000:],
        }
    err.update(extra)
    return err


# 异常类型 → 错误码 的兜底映射。
# 只在"底层没给出明确码"时使用；顺序有意义（先具体后宽泛）。
_EXC_MAP = (
    ("TimeoutError", "NET_TIMEOUT"),
    ("socket.timeout", "NET_TIMEOUT"),
    ("urllib.error.URLError", "NET_UNREACHABLE"),
    ("ConnectionError", "NET_UNREACHABLE"),
    ("HTTPError", "NET_HTTP_ERROR"),
    ("json.JSONDecodeError", "NET_BAD_RESPONSE"),
    ("ParseError", "PARSE_TEI_INVALID"),
    ("FileNotFoundError", "INPUT_NOT_FOUND"),
    ("PermissionError", "INPUT_INVALID"),
    ("KeyError", "INPUT_MISSING"),
    ("ValueError", "INPUT_INVALID"),
    ("TypeError", "INTERNAL_ERROR"),
    ("AttributeError", "INTERNAL_ERROR"),
)


def normalize_exception(exc: BaseException, *, stage: str = "",
                        component: str = "",
                        context: Optional[dict] = None) -> Dict[str, Any]:
    """把任意异常映射成标准错误字典。

    ★ 用途：任何工具的 except 分支都可以只写
          return fail(exc=exc, component=__name__, stage="fetch")
      而不必手写错误码 —— 保证**异常永远不会以裸 traceback 形式泄漏给调用方**。
      （裸 traceback 对 Agent 没用：它无法据此分支，只能把原文转给人看。）

    映射策略：按异常类名逐级匹配（见 _EXC_MAP）。匹配不到就是 INTERNAL_ERROR
    —— 因为"没见过的异常"最可能就是本库的 bug，不该伪装成网络问题。
    """
    names = [type(exc).__name__]
    mro = getattr(type(exc), "__mro__", ())
    names += [c.__name__ for c in mro]
    mod = type(exc).__module__ or ""
    full = "{}.{}".format(mod, type(exc).__name__)

    code = "INTERNAL_ERROR"
    for pat, c in _EXC_MAP:
        if pat in names or pat == full or full.endswith("." + pat):
            code = c
            break
    return make_error(code, "{}: {}".format(type(exc).__name__, str(exc)[:300]),
                      stage=stage, component=component, context=context,
                      cause=exc)


def all_codes() -> Dict[str, Dict[str, Any]]:
    """返回全部错误码（供文档生成与自检使用）。"""
    return dict(CODES)
