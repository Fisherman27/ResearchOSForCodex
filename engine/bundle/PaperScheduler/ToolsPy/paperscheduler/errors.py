"""PaperScheduler 稳定错误码。"""
from __future__ import annotations

import traceback


ERRORS = {
    "INPUT_MISSING": (False, "补齐必填参数后重试。"),
    "INPUT_INVALID": (False, "按 API 文档修正参数后重试。"),
    "STATE_EMPTY_GRAPH": (False, "先由 PaperOrganization 导入论文并建立索引。"),
    "STATE_NO_CANDIDATE": (False, "检查完成状态、候选过滤条件或阅读预算。"),
    "STATE_ORGANIZATION_UNAVAILABLE": (False, "确认 PaperOrganization/ToolsPy 可导入且知识库已初始化。"),
    "FORMAT_SNAPSHOT": (False, "重新从 Organization 的 get_graph_snapshot() 获取快照。"),
    "INTERNAL_ERROR": (False, "保留 detail 堆栈并上报工具实现问题。"),
}


def make_error(code: str, message: str, *, location: str = "",
               cause: str = "", context=None) -> dict:
    retryable, hint = ERRORS.get(code, (False, "查看错误上下文后修正。"))
    return {
        "code": code,
        "message": message,
        "retryable": retryable,
        "hint": hint,
        "location": location,
        "cause": cause,
        "context": context or {},
    }


def from_exception(exc: BaseException, *, location: str) -> dict:
    return make_error(
        "INTERNAL_ERROR", "{}: {}".format(type(exc).__name__, exc),
        location=location, cause=traceback.format_exc()[-3000:])
