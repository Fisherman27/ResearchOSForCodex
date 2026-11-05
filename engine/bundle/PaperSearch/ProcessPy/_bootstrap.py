#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
_bootstrap.py —— 把 ExamplePy 接进来（本包唯一的"对外依赖入口"）

定位
----
ProcessPy 是**流程编排层**，不重复实现任何数据源调用。所有底层能力都来自
ExamplePy（arXiv / OpenAlex / Semantic Scholar / Crossref / Unpaywall / GROBID）。
本模块负责把那批模块找出来并挂到 sys.path 上。

为什么单独做一个 bootstrap，而不是在每个文件里 sys.path.insert
------------------------------------------------------------------
1. **路径只有一处**：换目录、换机器只改这里（或设环境变量）；
2. **导入失败时报错清晰**：直接告诉用户 ExamplePy 在哪、怎么改；
3. **避免相对导入的坑**：ProcessPy 可能被当作脚本直接跑，也可能被 skill 导入，
   相对导入在两种场景下行为不同；显式挂路径最稳。

查找顺序
--------
1. 环境变量 `PROCESSPY_EXAMPLEPY`（指向 ExamplePy 目录）
2. `<ProcessPy 同级>/ExamplePy`        ← 默认布局
3. `<ProcessPy 上级>/PaperSearch/ExamplePy`
4. `<ProcessPy>/../ExamplePy`

函数索引
─────────────────────────────────────────────────────────────────────────────
  [定位]  examplepy_dir()            找到 ExamplePy 目录（找不到就抛异常）
  [接入]  ensure_importable()        把 ExamplePy 挂上 sys.path，返回目录
  [自检]  describe()                 返回接入状态（给 CLI / INDEX 用）
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_CANDIDATES = [
    os.path.join(os.path.dirname(_HERE), "ExamplePy"),          # 同级
    os.path.join(os.path.dirname(os.path.dirname(_HERE)), "ExamplePy"),
    os.path.join(os.path.dirname(_HERE), "PaperSearch", "ExamplePy"),
]

# ExamplePy 里必须存在的模块（少一个就说明目录不对）
REQUIRED = ("_common.py", "arxiv.py", "openalex.py", "crossref.py", "unpaywall.py")


def examplepy_dir(explicit: str = "") -> str:
    """定位 ExamplePy 目录。

    参数
    ----
    explicit : 显式指定；留空则按上表的顺序查找

    返回绝对路径；**找不到直接抛 FileNotFoundError**（而不是后面才莫名其妙地
    ImportError —— 早报错、报得清楚，比擦屁股便宜）。
    """
    tried = []
    env = explicit or os.environ.get("PROCESSPY_EXAMPLEPY", "")
    cands = ([env] if env else []) + _CANDIDATES
    for c in cands:
        if not c:
            continue
        p = os.path.abspath(c)
        tried.append(p)
        if os.path.isdir(p) and all(os.path.isfile(os.path.join(p, f)) for f in REQUIRED):
            return p
    raise FileNotFoundError(
        "找不到 ExamplePy 目录。尝试过：\n  " + "\n  ".join(tried) +
        "\n\n解决办法（任选其一）：\n"
        "  1. 把 ProcessPy 放在 ExamplePy 同级目录下（默认布局）\n"
        "  2. 设置环境变量 PROCESSPY_EXAMPLEPY=/path/to/ExamplePy\n"
        "  3. 用 --examplepy /path/to/ExamplePy 显式指定")


def ensure_importable(explicit: str = "") -> str:
    """确保 `import arxiv` / `import openalex` 等可用，返回 ExamplePy 目录。

    可以重复调用（幂等）。挂载后 ExamplePy 里的模块就能直接 import，
    例如 `from examplepy import arxiv` 之前必须先调用本函数。

    用法：
        from _bootstrap import ensure_importable
        ensure_importable()          # 之后就能 import arxiv / openalex ...
        import arxiv
    """
    p = examplepy_dir(explicit)
    if p not in sys.path:
        sys.path.insert(0, p)
    return p


def describe(explicit: str = "") -> dict:
    """返回接入状态，供 CLI 打印或写进报告。

    返回 {"ok", "dir", "python", "error"}
    """
    try:
        p = examplepy_dir(explicit)
    except FileNotFoundError as exc:
        return {"ok": False, "dir": "", "python": sys.version.split()[0],
                "error": str(exc)[:300]}
    return {"ok": True, "dir": p, "python": sys.version.split()[0], "error": ""}


# 方便 `from _bootstrap import arxiv` 这种写法：导入本模块即自动挂载
ensure_importable()
