"""PaperScheduler 工具注册表与 API.md 符号定位器。"""
from __future__ import annotations

import os
import re
REGISTRY = {
    "E10": {"name": "plan_reading", "kind": "element", "summary": "规划阅读顺序"},
    "E20": {"name": "select_context", "kind": "element", "summary": "选择前置知识"},
    "E30": {"name": "build_reading_task", "kind": "element", "summary": "构建 ReadingTask"},
    "E31": {"name": "filter_context_for_question", "kind": "element", "summary": "按问题补充上下文"},
    "P10": {"name": "next_reading_tasks", "kind": "pipeline", "summary": "从知识库生成下一批任务"},
}


def list_tools(kind: str = "") -> dict:
    from .result import ok
    rows = [{"id": key, **value} for key, value in REGISTRY.items()
            if not kind or value["kind"] == kind]
    return ok(rows, tool="list_tools")


def _api_path(path: str = "") -> str:
    return path or os.path.join(os.path.dirname(os.path.dirname(__file__)), "API.md")


def build_symbol_index(api_md: str = "") -> dict:
    from .errors import make_error
    from .result import failed, ok
    path = _api_path(api_md)
    try:
        with open(path, encoding="utf-8") as handle:
            lines = handle.readlines()
    except OSError as exc:
        return failed(make_error("INPUT_INVALID", str(exc),
                                 location="registry.build_symbol_index"),
                      tool="build_symbol_index")
    starts = []
    pattern = re.compile(r"^### \[([EP]\d+)\] ([A-Za-z_][A-Za-z0-9_]*)")
    for index, line in enumerate(lines, 1):
        match = pattern.match(line)
        if match:
            starts.append((match.group(1), match.group(2), index))
    result = {}
    for i, (symbol, name, start) in enumerate(starts):
        end = starts[i + 1][2] - 1 if i + 1 < len(starts) else len(lines)
        result[symbol] = {"symbol": symbol, "name": name,
                          "line_start": start, "line_end": end, "path": path}
        result[name] = result[symbol]
    return ok(result, tool="build_symbol_index")


def find_symbol(symbol: str, api_md: str = "") -> dict:
    from .errors import make_error
    from .result import failed, ok
    index = build_symbol_index(api_md)
    if not index["ok"]:
        return index
    item = index["data"].get(symbol)
    if item is None:
        return failed(make_error("INPUT_INVALID", "API 符号不存在：{}".format(symbol),
                                 location="registry.find_symbol"),
                      tool="find_symbol")
    return ok(item, tool="find_symbol")


def read_symbol(symbol: str, api_md: str = "") -> dict:
    from .result import ok
    location = find_symbol(symbol, api_md)
    if not location["ok"]:
        return location
    item = location["data"]
    with open(item["path"], encoding="utf-8") as handle:
        lines = handle.readlines()
    return ok({**item, "markdown": "".join(
        lines[item["line_start"] - 1:item["line_end"]])}, tool="read_symbol")
