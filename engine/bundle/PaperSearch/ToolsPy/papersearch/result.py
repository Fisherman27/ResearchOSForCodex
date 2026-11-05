#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
result.py —— 统一返回信封（ToolsPy 的工具都返回它）

定位
----
Agent 用工具时最怕三件事：
    1. **不知道成没成** —— 有的工具抛异常、有的返回 None、有的返回空列表
    2. **不知道缺了什么** —— "返回 3 篇" 但没说是 30 篇里的 3 篇，还是只有 3 篇
    3. **不知道错在哪** —— 只有一句 "failed"，无法定位

所以本库所有工具**统一返回一个信封**（普通 dict，不用自定义类 —— 便于直接
JSON 序列化、便于跨语言）：

    {
      "ok":       bool,        # 调用是否成功（语义见下）
      "status":   str,         # ok | partial | empty | blocked | failed
      "data":     Any,         # 主产物（成功时）
      "error":    dict | None, # 失败时的标准错误（errors.py 生成）
      "warnings": [ ... ],     # 非致命问题（数据可疑、源降级……）
      "diagnostics": { ... },  # 过程指标：耗时、各源状态、分页、命中数……
      "meta":     { ... },     # 工具名、版本、参数快照、时间
    }

status 的语义边界（这是本库最重要的一条契约）
---------------------------------------------
    ok       全部成功，data 完整
    partial  拿到了**部分**结果，且有明确原因（某源失败/分页中断/降级）
             ★ ok=True —— 结果可用，但调用方应看 diagnostics 决定是否补救
    empty    成功执行但**结果为空**（如检索 0 命中、论文没有参考文献）
             ★ ok=True —— 空集不是错误！与 failed 严格区分
    blocked  缺少前提（无 key、服务没起）→ 本步做不了，但**不是代码问题**
             ★ ok=False，retryable 取决于补前提后能否继续
    failed   执行出错（网络/解析/内部 bug）
             ★ ok=False

为什么 empty 要单独一类：
    "检索到 0 篇" 和 "检索失败" 在旧代码里都表现为空列表/None，
    下游无法区分"这个领域真的没论文"和"我的请求挂了"。
    实测踩过：HTTP 200 + HTML 错误页被当成"成功的空集"，
    整个流程报 done —— 失败伪装成功是最难发现的 bug。

函数索引
─────────────────────────────────────────────────────────────────────────────
  [构造]  ok(data, **kw)            成功（data 为空时自动转 empty）
  [构造]  partial(data, **kw)       部分成功（**ok=True**，但有 warnings）
  [构造]  empty(**kw)               成功但空集（**ok=True**）
  [构造]  blocked(code, **kw)       缺前提（无 key / 服务没起）
  [构造]  failed(code=None, **kw)   失败
  [异常]  from_exception(exc, **kw) 异常 → 失败信封（**工具里统一用这个兜底**）
  [检查]  is_ok(e) / is_usable(e)   判定辅助
  [展示]  pretty(e)                 人可读单行摘要（日志/CLI 用）
  [诊断]  tracer(name)              计时/记录阶段的上下文管理器
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import time
import traceback
from typing import Any, Dict, List, Optional

from .errors import make_error, normalize_exception, suggest

__all__ = ["ok", "partial", "empty", "blocked", "failed", "from_exception",
           "is_ok", "is_usable", "pretty", "tracer", "envelope", "add_warning",
           "DIAG_VERSION"]

# 信封结构版本。将来字段变动时调用方可以据此兼容。
DIAG_VERSION = "1.0"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def envelope(status: str, *, data: Any = None, error: Optional[dict] = None,
             warnings: Optional[List[dict]] = None,
             diagnostics: Optional[dict] = None,
             tool: str = "", params: Optional[dict] = None,
             ok_override: Optional[bool] = None) -> Dict[str, Any]:
    """构造信封（其他构造器都调它，保证结构一致）。

    ⚠️ 不要直接调它 —— 用 ok()/partial()/empty()/blocked()/failed()，
       它们会做参数校验（比如 failed 必须有 error）。
    """
    ok_flag = (status in ("ok", "partial", "empty")
               if ok_override is None else bool(ok_override))
    return {
        "ok": ok_flag,
        "status": status,
        "data": data,
        "error": error,
        "warnings": warnings or [],
        "diagnostics": diagnostics or {},
        "meta": {
            "tool": tool,
            "tool_version": DIAG_VERSION,
            "params": params or {},
            "at": _now(),
        },
    }


# =============================================================================
# 构造器
# =============================================================================

def ok(data: Any = None, *, tool: str = "", params: Optional[dict] = None,
       warnings: Optional[List[dict]] = None,
       diagnostics: Optional[dict] = None) -> Dict[str, Any]:
    """成功。

    ★ data 为空（None / 空 list / 空 dict / 空 str）时**自动降级为 empty** ——
      这是有意的：避免调用方把"空结果"误当"有结果"。
      如果你确实要返回空 data 且语义是 ok，用 empty()。
    """
    if data is None or (hasattr(data, "__len__") and len(data) == 0):
        return empty(tool=tool, params=params, warnings=warnings,
                     diagnostics=diagnostics)
    return envelope("ok", data=data, warnings=warnings,
                    diagnostics=diagnostics, tool=tool, params=params)


def empty(data: Any = None, *, tool: str = "", params: Optional[dict] = None,
          reason: str = "", code: str = "NOTFOUND_EMPTY_RESULT",
          warnings: Optional[List[dict]] = None,
          diagnostics: Optional[dict] = None) -> Dict[str, Any]:
    """成功执行，但结果为空。

    ★ ok=True —— 空集**不是**错误。区分"没有命中"与"请求失败"是本库的核心契约。

    参数
    ----
    data   : 可选。**即使主结果为空，也可能有值得返回的结构**
             （例如解析失败时仍想给出 stats / 已尝试的地址列表）。
             不传则 data=None。
    reason : 为什么空（写清楚，下游要靠它判断该不该放宽条件）
    code   : 默认 NOTFOUND_EMPTY_RESULT；也可用别的（如 NOTFOUND_PDF）

    ⚠️ 修复记录：第一版写成 `data=[] if isinstance(diagnostics, dict) else None`，
       这个条件**恒为真**（diagnostics 默认就是 dict），于是 data 永远是 []，
       调用方拿不到本该返回的统计信息。而且它没有 data 参数，
       连"带数据返回空状态"都做不到。
    """
    w = list(warnings or [])
    if reason:
        w.append({"code": code, "message": reason, "hint": suggest(code)})
    return envelope("empty", data=data, warnings=w, diagnostics=diagnostics,
                    tool=tool, params=params)


def partial(data: Any = None, *, tool: str = "", params: Optional[dict] = None,
            reason: str = "", code: str = "DATA_PARTIAL",
            warnings: Optional[List[dict]] = None,
            diagnostics: Optional[dict] = None) -> Dict[str, Any]:
    """部分成功：拿到了结果，但有明确缺口。

    ★ ok=True —— **结果可用**。这与 failed 的关键区别：
      调用方可以拿着 partial 的结果继续往下走，只是要知道缺了什么
      （例如"3 个源里 OpenAlex 挂了，但有 arXiv 的 50 篇"）。

    为什么不用 ok=False：
      多源检索里单个源失败是常态（限流/凭证）。若判为失败，调用方会丢弃
      已经拿到的 50 篇 —— 那才是真正的损失。
    """
    w = list(warnings or [])
    if reason:
        w.append({"code": code, "message": reason, "hint": suggest(code)})
    return envelope("partial", data=data, warnings=w, diagnostics=diagnostics,
                    tool=tool, params=params)


def blocked(code: str = "AUTH_MISSING_KEY", *, message: str = "",
            tool: str = "", params: Optional[dict] = None,
            context: Optional[dict] = None,
            hint: str = "",
            warnings: Optional[List[dict]] = None,
            diagnostics: Optional[dict] = None,
            component: str = "") -> Dict[str, Any]:
    """缺少前提，本步做不了（但不是代码问题）。

    典型：没配 S2_API_KEY、GROBID 服务没启动、本地文件还没下载。

    ★ 与 failed 的区别：blocked 的含义是"补上前提就能做"，
      所以 error.hint 里一定给出**可执行的补救动作**。
      Agent 看到 blocked 应该去补前提或改走别的路径，而不是重试。
    """
    return envelope("blocked",
                    error=make_error(code, message, component=component,
                                     context=context, hint=hint),
                    warnings=warnings, diagnostics=diagnostics,
                    tool=tool, params=params)


def failed(code: Optional[str] = None, *, message: str = "", tool: str = "",
           params: Optional[dict] = None, context: Optional[dict] = None,
           cause: Optional[BaseException] = None, stage: str = "",
           component: str = "", hint: str = "",
           warnings: Optional[List[dict]] = None,
           diagnostics: Optional[dict] = None) -> Dict[str, Any]:
    """失败。

    参数
    ----
    code  : 省略时按 `cause` 自动映射（见 errors.normalize_exception）
    cause : 原始异常 —— **一定要传**，否则丢失 traceback，Agent 无法定位
    stage : 多步工具的步骤名（告诉 Agent 是第几步挂的）
    """
    if code is None:
        if cause is not None:
            err = normalize_exception(cause, stage=stage, component=component,
                                      context=context)
        else:
            err = make_error("INTERNAL_ERROR",
                             message or "未提供错误码与异常",
                             stage=stage, component=component, context=context)
    else:
        err = make_error(code, message, stage=stage, component=component,
                         context=context, cause=cause, hint=hint)
    return envelope("failed", error=err, warnings=warnings,
                    diagnostics=diagnostics, tool=tool, params=params)


def from_exception(exc: BaseException, *, tool: str = "",
                   params: Optional[dict] = None, stage: str = "",
                   component: str = "", context: Optional[dict] = None,
                   diagnostics: Optional[dict] = None) -> Dict[str, Any]:
    """异常 → 失败信封。

    ★ **工具里的 except 分支统一用这个**。保证：
        1. 异常永远不以裸 traceback 形式泄漏给调用方；
        2. 错误码自动映射（超时→NET_TIMEOUT、404→NOTFOUND_…）；
        3. traceback 保留在 error.cause 里供 debug。
    """
    return failed(None, tool=tool, params=params, stage=stage,
                  component=component, context=context, cause=exc,
                  diagnostics=diagnostics)


# =============================================================================
# 判定辅助
# =============================================================================

def is_ok(e: dict) -> bool:
    """严格成功：status == "ok"（不含 partial/empty）。"""
    return bool(e) and e.get("status") == "ok"


def is_usable(e: dict) -> bool:
    """**结果可用**：ok=True（含 ok / partial / empty）。

    多步流程里判断"能不能继续往下走"应该用这个，而不是 is_ok ——
    拿 partial 的结果继续跑通常比直接放弃更合理。
    """
    return bool(e) and bool(e.get("ok"))


def add_warning(e: dict, code: str, message: str, **context) -> dict:
    """给已有信封追加一条 warning（就地修改并返回）。

    用途：多步工具里每一步发现的小问题先攒着，最后统一交出去 ——
    比中途丢弃更有用（Agent 能看到全貌）。
    """
    e.setdefault("warnings", []).append(
        {"code": code, "message": message, "hint": suggest(code), "context": context})
    return e


# =============================================================================
# 展示
# =============================================================================

def pretty(e: dict, max_data: int = 120) -> str:
    """一行摘要，供日志/CLI/Agent 快速阅读。

    例：
      [ok]      检索命中 50 篇（1.2s）
      [partial] 3 源中 1 失败；拿到 50 篇（2.1s）｜ w: openalex 限流
      [empty]   检索命中 0 篇 ｜ 放宽查询词
      [blocked] AUTH_MISSING_KEY ｜ 在 Keys.txt 里补 S2_API_KEY
      [failed]  NET_TIMEOUT ｜ 请求超时，可重试
    """
    if not e:
        return "[?] 空信封"
    st = e.get("status", "?")
    d = e.get("diagnostics") or {}
    took = d.get("elapsed_sec")
    n = d.get("n_results", d.get("count"))
    bits = []
    if n is not None:
        bits.append("{} 项".format(n))
    if took is not None:
        bits.append("{:.1f}s".format(took))
    head = " ".join(bits)

    if st in ("ok", "partial", "empty"):
        line = "[{}] {}{}".format(st, head or "完成", "")
    else:
        err = e.get("error") or {}
        line = "[{}] {} ｜ {}".format(st, err.get("code", "?"), err.get("hint", ""))
    ws = e.get("warnings") or []
    if ws:
        line += " ｜ w: " + "; ".join(
            "{}: {}".format(w.get("code", ""), (w.get("message") or "")[:60])
            for w in ws[:2])
    return line


# =============================================================================
# 阶段计时
# =============================================================================

class tracer:
    """阶段计时/记录上下文管理器。

    用法：
        with tracer("fetch") as t:
            ... 干活 ...
            t.note(pages=3)
        # 结束后 t.elapsed 有耗时，t.notes 有备注

    为什么需要它：多步工具的 diagnostics 里要能回答"哪一步慢/哪一步空"，
    否则 Agent 无法判断该优化哪里、或该不该跳过某步。
    """

    def __init__(self, name: str):
        self.name = name
        self.t0 = time.time()
        self.elapsed = 0.0
        self.notes: Dict[str, Any] = {}
        self.error: Optional[BaseException] = None

    def __enter__(self) -> "tracer":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.elapsed = round(time.time() - self.t0, 3)
        if exc is not None:
            self.error = exc
        return False        # 不吞异常

    def note(self, **kw) -> None:
        self.notes.update(kw)

    def as_dict(self) -> dict:
        d = {"elapsed_sec": self.elapsed}
        d.update(self.notes)
        if self.error is not None:
            d["error"] = "{}: {}".format(type(self.error).__name__,
                                         str(self.error)[:150])
        return d


def __getattr__(name: str):
    """惰性转发：让 `result.summarize_result` 也能用。

    ★ 用模块级 __getattr__（PEP 562）而不是在 pipelines 里赋值：
      后者依赖"pipelines 先于调用被导入"，而导入顺序是不确定的
      （实测：先 `from papersearch import result` 再调用就 AttributeError）。
      __getattr__ 在任何访问时机都能兜住，与导入顺序无关。

    为什么放在 result 里：调用方判断/展示结果时会习惯性去 result 找展示函数
      （pretty 就在那儿），少记一个模块名就少一次查文档。
    """
    if name == "summarize_result":
        from pipelines import summarize_result as _sr
        return _sr
    raise AttributeError("module 'result' has no attribute {!r}".format(name))
