#!/usr/bin/env python3
"""从注册表与函数签名生成 API.md。"""
from __future__ import annotations

import inspect
import os
import sys


DOCS = os.path.abspath(os.path.dirname(__file__))
ROOT = os.path.dirname(DOCS)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import paperscheduler
from paperscheduler.registry import REGISTRY


def render() -> str:
    lines = [
        "# PaperScheduler API\n\n",
        "> 自动生成，请勿手工编辑。运行 `python docs/gen_api_md.py` 更新。\n\n",
        "所有工具返回 `ToolsPyRules.md` 规定的统一信封。\n\n",
    ]
    for symbol, item in REGISTRY.items():
        fn = getattr(paperscheduler, item["name"])
        lines.extend([
            "### [{}] {}\n\n".format(symbol, item["name"]),
            "`{}{}`\n\n".format(item["name"], inspect.signature(fn)),
            (inspect.getdoc(fn) or item["summary"]) + "\n\n",
            "类型：`{}`。\n\n".format(item["kind"]),
        ])
    return "".join(lines)


def main(argv=None) -> int:
    expected = render()
    target = os.path.join(ROOT, "API.md")
    if argv and "--check" in argv:
        try:
            with open(target, encoding="utf-8") as handle:
                actual = handle.read()
        except OSError:
            return 1
        return 0 if actual == expected else 1
    with open(target, "w", encoding="utf-8") as handle:
        handle.write(expected)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
