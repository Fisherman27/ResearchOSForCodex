"""统一返回信封（PaperRead 工具层）。

按 `ToolsPyRules.md`的语义实现。PaperRead 侧选择**普通 dict**形态
（与 PaperSearch 一致），而非 PaperOrganization 的 ToolResult 数据类。

信封字段：
    ok / status / data / error / warnings / diagnostics / meta

status 语义（ToolsPyRules）：
    ok       全部成功
    partial  拿到部分结果且有明确原因（结果可用）
    empty    成功执行但结果为空（**空集不是错误**）
    blocked  被前置条件挡住，未执行
    failed   执行失败
"""

STATUS_OK = "ok"
STATUS_PARTIAL = "partial"
STATUS_EMPTY = "empty"
STATUS_BLOCKED = "blocked"
STATUS_FAILED = "failed"

TOOL_VERSION = "paperread-tools-0.1.0"


def ok(data=None, warnings=None, diagnostics=None, meta=None, status=STATUS_OK) -> dict:
    """成功信封。status 可为 ok / partial / empty。"""
    return {
        "ok": True,
        "status": status,
        "data": data,
        "error": None,
        "warnings": list(warnings or []),
        "diagnostics": dict(diagnostics or {}),
        "meta": dict(meta or {}),
    }


def empty(data=None, warnings=None, diagnostics=None, meta=None) -> dict:
    """成功但结果为空（ok=True，status=empty）。"""
    return ok(data=data, warnings=warnings, diagnostics=diagnostics,
              meta=meta, status=STATUS_EMPTY)


def fail(error: dict, data=None, warnings=None, diagnostics=None, meta=None,
         status=STATUS_FAILED) -> dict:
    """失败信封（ok=False）。status 可为 failed / blocked。"""
    return {
        "ok": False,
        "status": status,
        "data": data,
        "error": error,
        "warnings": list(warnings or []),
        "diagnostics": dict(diagnostics or {}),
        "meta": dict(meta or {}),
    }


def blocked(error: dict, data=None, warnings=None, diagnostics=None, meta=None) -> dict:
    """被前置条件挡住：未执行、而非执行失败。"""
    return fail(error, data=data, warnings=warnings, diagnostics=diagnostics,
                meta=meta, status=STATUS_BLOCKED)
