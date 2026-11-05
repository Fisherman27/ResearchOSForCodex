"""统一的结果与错误机制（PaperOrganization 工具库的基础层）。

设计目标（面向 Agent 使用）：
    1. **不抛裸异常**：所有工具函数返回 ToolResult，ok=False 时携带结构化错误。
    2. **错误可定位**：错误必须说清「哪里错、为什么错、怎么修」。
    3. **错误可编程**：错误带机器可读的 code，Agent 可按 code 分支处理。
    4. **默认不静默**：跳过/降级必须记进 warnings，不假装成功。

核心类型：
    ToolError   —— 单个错误的结构（code/message/location/hint/detail/context）
    ToolResult  —— 所有工具函数的统一返回类型

典型用法（工具实现方）：
    from paperorganization.errors import ok, fail, ErrorCode

    def do_something(x):
        if not x:
            return fail(ErrorCode.MISSING_FIELD, "x 不能为空",
                        location="do_something(x)", hint="调用方需传入非空 x")
        return ok({"result": x}, warnings=["这是演示"])

典型用法（Agent 调用方）：
    r = do_something("")
    if not r:
        print(r.error.code, r.error.hint)      # E_MISSING_FIELD ...
    print(r.data)
"""
from __future__ import annotations

import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# 错误码（机器可读，Agent 可按 code 分支处理）
# ---------------------------------------------------------------------------

class ErrorCode:
    """错误码常量。

    命名规则：E_<类别>_<细节>，全部大写。
    类别前缀：
        E_INPUT_*   调用方输入问题（Agent 可自行修正）
        E_STATE_*   环境/数据状态问题（Agent 需检查前置条件）
        E_FORMAT_*  数据格式/结构不符
        E_IO_*      文件/数据库读写问题
        E_INTERNAL_ 工具内部 bug（Agent 应上报，不应自责）
        E_PARTIAL_  部分成功
    """
    # —— 输入问题（Agent 可自行修正）——
    MISSING_FIELD = "E_INPUT_MISSING_FIELD"
    INVALID_ENUM = "E_INPUT_INVALID_ENUM"
    INVALID_TYPE = "E_INPUT_INVALID_TYPE"
    INVALID_VALUE = "E_INPUT_INVALID_VALUE"
    MISSING_ARG = "E_INPUT_MISSING_ARG"
    INCONSISTENT = "E_INPUT_INCONSISTENT"

    # —— 状态问题（前置条件未满足）——
    NOT_FOUND = "E_STATE_NOT_FOUND"
    ALREADY_EXISTS = "E_STATE_ALREADY_EXISTS"
    EMPTY = "E_STATE_EMPTY"
    NOT_INITIALIZED = "E_STATE_NOT_INITIALIZED"
    DEPENDENCY_MISSING = "E_STATE_DEPENDENCY_MISSING"

    # —— 格式问题 ——
    FORMAT_MISMATCH = "E_FORMAT_MISMATCH"
    PARSE_ERROR = "E_FORMAT_PARSE_ERROR"
    SCHEMA_MISMATCH = "E_FORMAT_SCHEMA_MISMATCH"

    # —— IO 问题 ——
    FILE_NOT_FOUND = "E_IO_FILE_NOT_FOUND"
    READ_ERROR = "E_IO_READ_ERROR"
    WRITE_ERROR = "E_IO_WRITE_ERROR"
    DB_ERROR = "E_IO_DB_ERROR"

    # —— 内部问题（工具 bug）——
    INTERNAL = "E_INTERNAL"
    UNEXPECTED = "E_INTERNAL_UNEXPECTED"

    # —— 部分成功 ——
    PARTIAL = "E_PARTIAL"


# ---------------------------------------------------------------------------
# 错误对象
# ---------------------------------------------------------------------------

@dataclass
class ToolError:
    """结构化的错误信息。

    字段（Agent 定位问题需要的全部信息）：
        code     机器可读错误码（ErrorCode 里的常量）
        message  人类可读的一句话说明
        location 出错位置："模块.函数" 或 "表.列" 或 "文件:行" 或 "records[3]"
        hint     修复建议（Agent 据此自我调整）
        detail   原始异常/堆栈（工具内部 bug 时给出，便于上报）
        context  相关数据快照（如出错的记录片段）
    """
    code: str
    message: str
    location: str = ""
    hint: str = ""
    detail: str = ""
    context: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "location": self.location,
            "hint": self.hint,
            "detail": self.detail,
            "context": self.context,
        }

    def __str__(self) -> str:
        s = "[{}] {}".format(self.code, self.message)
        if self.location:
            s += "  @{}".format(self.location)
        if self.hint:
            s += "  → {}".format(self.hint)
        return s


# ---------------------------------------------------------------------------
# 结果对象
# ---------------------------------------------------------------------------

@dataclass
class ToolResult:
    """所有工具函数的统一返回类型。

    字段：
        ok       结果是否可用（partial/empty 也为 True）
        status   ok / partial / empty / blocked / failed
        data     成功时的产物（dict/list/值）
        error    ok=False 时的 ToolError
        warnings 非致命问题（降级/跳过/可疑数据），必须显式记录
        diagnostics 过程指标
        meta     元信息（耗时、统计等）

    用法：结果对象支持 `if not r:` 判断失败，也支持 `r["key"]` 取 data 字段。
    """
    ok: bool
    status: str = "ok"
    data: Any = None
    error: Optional[ToolError] = None
    warnings: List[str] = field(default_factory=list)
    diagnostics: Dict[str, Any] = field(default_factory=dict)
    meta: Dict[str, Any] = field(default_factory=dict)

    # —— 便捷访问 ——

    def __bool__(self) -> bool:
        """允许 `if result:` 判断成功。"""
        return self.ok

    def __getitem__(self, key):
        """允许 result["key"] 直接取 data 的字段（data 是 dict 时）。"""
        if isinstance(self.data, dict):
            return self.data[key]
        raise KeyError("data 不是 dict，无法用 key 访问")

    def get(self, key, default=None):
        if isinstance(self.data, dict):
            return self.data.get(key, default)
        return default

    # —— 转换 ——

    def to_dict(self) -> Dict[str, Any]:
        """转成纯 dict（便于 JSON 序列化 / 给 Agent 看）。"""
        return {
            "ok": self.ok,
            "status": self.status,
            "data": self.data,
            "error": self.error.to_dict() if self.error else None,
            "warnings": self.warnings,
            "diagnostics": self.diagnostics,
            "meta": self.meta,
        }

    def summary(self) -> str:
        """一行摘要（用于日志）。"""
        if self.ok:
            s = "OK"
            if self.data is not None:
                s += " data={}".format(_brief(self.data))
            if self.warnings:
                s += " warnings={}".format(len(self.warnings))
            return s
        return "FAIL {}".format(self.error)

    def raise_if_failed(self):
        """在确实需要异常语义的场景使用（一般不推荐 Agent 用）。"""
        if not self.ok and self.error:
            raise ToolException(self.error)


class ToolException(Exception):
    """工具异常（仅在 raise_if_failed 等场景使用）。

    常规流程一律用 ToolResult 返回，不抛异常。
    """
    def __init__(self, error: ToolError):
        super().__init__(str(error))
        self.error = error


# ---------------------------------------------------------------------------
# 构造辅助
# ---------------------------------------------------------------------------

def ok(data: Any = None, warnings: Optional[List[str]] = None,
       meta: Optional[Dict[str, Any]] = None,
       diagnostics: Optional[Dict[str, Any]] = None) -> ToolResult:
    """构造成功结果。"""
    return ToolResult(ok=True, status="ok", data=data, warnings=warnings or [],
                      diagnostics=diagnostics or {}, meta=meta or {})


def empty(data: Any = None, warnings: Optional[List[str]] = None,
          meta: Optional[Dict[str, Any]] = None,
          diagnostics: Optional[Dict[str, Any]] = None) -> ToolResult:
    """成功执行但结果为空；严格区别于 failed。"""
    return ToolResult(ok=True, status="empty", data=data, warnings=warnings or [],
                      diagnostics=diagnostics or {}, meta=meta or {})


def fail(code: str, message: str, location: str = "", hint: str = "",
         detail: str = "", context: Optional[Dict[str, Any]] = None,
         warnings: Optional[List[str]] = None) -> ToolResult:
    """构造失败结果。"""
    return ToolResult(
        ok=False, status="failed", data=None,
        error=ToolError(code=code, message=message, location=location,
                        hint=hint, detail=detail, context=context or {}),
        warnings=warnings or [], meta={})


def fail_from_exception(exc: BaseException, location: str = "",
                        hint: str = "", code: str = ErrorCode.UNEXPECTED,
                        message: str = "", context: Optional[Dict[str, Any]] = None,
                        warnings: Optional[List[str]] = None) -> ToolResult:
    """从捕获的异常构造失败结果（自动带上堆栈，便于 Agent 定位工具 bug）。"""
    return fail(
        code=code,
        message=message or "{}: {}".format(type(exc).__name__, str(exc)[:200]),
        location=location,
        hint=hint or "这是工具内部异常，请把 detail 中的堆栈上报；若为输入导致，请先校验输入",
        detail=traceback.format_exc()[-2000:],
        context=context,
        warnings=warnings)


def partial(data: Any = None, warnings: Optional[List[str]] = None,
            message: str = "部分成功", location: str = "",
            hint: str = "") -> ToolResult:
    """构造「部分成功」结果：ok=True，data 可用，边界写入 warnings。

    用于批量场景：一部分记录成功、一部分失败，调用方需要知道边界。
    """
    boundary = message + (("；" + hint) if hint else "")
    return ToolResult(
        ok=True, status="partial", data=data, error=None,
        warnings=(warnings or []) + ([boundary] if boundary else []),
        diagnostics={"partial_location": location} if location else {}, meta={})


def blocked(code: str, message: str, location: str = "", hint: str = "",
            context: Optional[Dict[str, Any]] = None) -> ToolResult:
    """缺少前提，补齐前提前重试无用。"""
    return ToolResult(
        ok=False, status="blocked", data=None,
        error=ToolError(code=code, message=message, location=location,
                        hint=hint, context=context or {}),
        warnings=[], diagnostics={}, meta={})


# ---------------------------------------------------------------------------
# 计时器（工具函数记录耗时，便于性能诊断）
# ---------------------------------------------------------------------------

class Timer:
    """计时器：with Timer() as t: ... ; 之后 t.ms 得到毫秒。

    也支持在 with 块**内部**调用 t.elapsed_ms() 取当前耗时（t.ms 只在退出时才可用）。
    """
    def __enter__(self):
        self._start = time.time()
        self.ms = None
        return self

    def elapsed_ms(self) -> float:
        """取当前已经过的毫秒数（可在 with 块内部调用）。"""
        return round((time.time() - self._start) * 1000, 1)

    def __exit__(self, *a):
        self.ms = self.elapsed_ms()
        return False


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------

def _brief(obj: Any, limit: int = 80) -> str:
    """把对象压成一行短摘要（用于日志）。"""
    try:
        s = repr(obj)
    except Exception:
        s = "<不可表示>"
    if len(s) > limit:
        s = s[:limit] + "..."
    return s
