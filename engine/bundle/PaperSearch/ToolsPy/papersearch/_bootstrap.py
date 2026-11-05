#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
_bootstrap.py —— 复用 PaperSearch 里已验证的实现（ToolsPy 的唯一外部依赖入口）

定位
----
ToolsPy **不重新实现**任何数据源调用。arXiv / OpenAlex / Semantic Scholar /
Crossref / Unpaywall / GROBID 的实现在 `PaperSearch/ExamplePy`，
流程编排在 `PaperSearch/ProcessPy` —— 那些都经过了两轮审核 + 32 项边界测试。

ToolsPy 的职责是把它们**包成规范的工具**（统一返回信封、统一错误码、可被
Agent 稳定调用），而不是重写。

查找顺序
--------
ToolsPy 位于 `PaperSearch/ToolsPy/`，与 `ExamplePy/`、`ProcessPy/` **同级**。

1. 环境变量 `TOOLSPY_HOME`（指向 PaperSearch 目录）
2. `<ToolsPy 上级>` —— 即 PaperSearch 本身（默认布局，**无需配置**）
3. `<ToolsPy 上级的上级>/PaperSearch`
4. 环境变量 `PROCESSPY_EXAMPLEPY`（兼容旧配置）

设计说明
--------
- **幂等**：可以重复调用 ensure_importable()，不会重复插 sys.path；
- **早报错**：找不到时给出**具体**的查找路径与修复办法，而不是等到
  后面某个 import 莫名其妙地失败；
- **不静默降级**：缺依赖就是缺依赖，不假装能跑。

函数索引
─────────────────────────────────────────────────────────────────────────────
  [定位]  paper_search_dir()         找到 PaperSearch 根目录
  [接入]  ensure_importable()        把 ExamplePy / ProcessPy 挂上 sys.path
  [状态]  describe()                 返回依赖接入状态（含各源可用性）
  [自检]  check_keys()               检查凭证配置情况（不打印值）
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))

# ExamplePy 里必须存在的模块（少一个就说明目录不对）
_REQUIRED = ("_common.py", "arxiv.py", "openalex.py", "crossref.py",
             "unpaywall.py", "grobid.py")
# ProcessPy 里必须存在的模块
_REQUIRED_PROCESS = ("pipeline.py", "sources.py", "model.py")

# ToolsPy 在 PaperSearch/ToolsPy/ 下，本模块又在 ToolsPy/papersearch/ 里；
# 所以 PaperSearch 根 = 本文件的「上两层」。
_TOOLSPY = os.path.dirname(_HERE)                                  # PaperSearch/ToolsPy
_PAPERSEARCH_PARENT = os.path.dirname(_TOOLSPY)                    # PaperSearch
_CANDIDATES = [
    _PAPERSEARCH_PARENT,                                           # ← 默认，无需配置
    os.path.join(os.path.dirname(_PAPERSEARCH_PARENT), "PaperSearch"),
]


def paper_search_dir(explicit: str = "") -> str:
    """定位 PaperSearch 根目录（含 ExamplePy 与 ProcessPy）。

    ToolsPy 本身就在 PaperSearch 里，所以**默认情况下不需要任何配置** ——
    只有把 ToolsPy 单独拷出去用时才需要设 TOOLSPY_HOME。

    返回绝对路径；找不到时抛 FileNotFoundError 并列出所有尝试过的路径。
    """
    tried = []
    env = (explicit or os.environ.get("TOOLSPY_HOME", "")
           or os.environ.get("PROCESSPY_EXAMPLEPY", ""))
    cands = ([env] if env else []) + _CANDIDATES
    for c in cands:
        if not c:
            continue
        p = os.path.abspath(c)
        # 兼容"直接指向 ExamplePy"的旧配置
        if os.path.basename(p) == "ExamplePy":
            p = os.path.dirname(p)
        tried.append(p)
        if (os.path.isdir(os.path.join(p, "ExamplePy"))
                and all(os.path.isfile(os.path.join(p, "ExamplePy", f))
                        for f in _REQUIRED)):
            return p
    raise FileNotFoundError(
        "找不到 PaperSearch 目录（需要其中的 ExamplePy/）。尝试过：\n  "
        + "\n  ".join(tried)
        + "\n\n默认布局下不需要配置（ToolsPy 就在 PaperSearch 内）。"
          "只有把 ToolsPy 单独拷出去时才需要：\n"
          "  设置环境变量 TOOLSPY_HOME=/path/to/PaperSearch")


def ensure_importable(explicit: str = "") -> str:
    """把 ExamplePy 与 ProcessPy 挂到 sys.path，返回 PaperSearch 目录。

    幂等。挂载后可以直接 `import arxiv` / `import pipeline` / `import grobid`。
    """
    root = paper_search_dir(explicit)
    for sub in ("ExamplePy", "ProcessPy"):
        p = os.path.join(root, sub)
        if os.path.isdir(p) and p not in sys.path:
            sys.path.insert(0, p)
    return root


def describe(explicit: str = "") -> dict:
    """返回依赖接入状态 —— 供 tools.doctor() 与调用方自检。

    返回 {"ok", "paper_search_dir", "examplepy", "processpy", "python",
          "missing", "error"}
    """
    out = {"ok": False, "paper_search_dir": "", "examplepy": "", "processpy": "",
           "python": sys.version.split()[0], "missing": [], "error": ""}
    try:
        root = paper_search_dir(explicit)
    except FileNotFoundError as exc:
        out["error"] = str(exc)[:500]
        return out
    out["paper_search_dir"] = root
    out["examplepy"] = os.path.join(root, "ExamplePy")
    out["processpy"] = os.path.join(root, "ProcessPy")
    miss = [f for f in _REQUIRED
            if not os.path.isfile(os.path.join(out["examplepy"], f))]
    miss += [f for f in _REQUIRED_PROCESS
             if not os.path.isfile(os.path.join(out["processpy"], f))]
    out["missing"] = miss
    out["ok"] = not miss
    if miss:
        out["error"] = "缺少模块：{}".format(", ".join(miss))
    return out


def check_keys() -> dict:
    """检查凭证配置情况。

    ★ **只返回"有没有"，绝不返回凭证明文** —— 工具输出会被写进日志、
      交给 Agent、甚至进报告，泄漏 key 是不可接受的。

    返回 {"email": bool, "openalex": bool, "s2": bool, "core": bool,
          "openreview": bool, "keys_file": path, "hint": str}
    """
    try:
        ensure_importable()
        import _common
    except Exception as exc:
        return {"error": "无法读取凭证：{}".format(str(exc)[:150])}

    def has(name: str) -> bool:
        try:
            return bool(_common.get_key(name))
        except Exception:
            return False

    keys_file = ""
    try:
        keys_file = getattr(_common, "KEYS_PATH", "") or ""
    except Exception:
        pass
    info = {
        "email": has("PAPERSEARCH_EMAIL"),
        "openalex": has("OPENALEX_API_KEY"),
        "s2": has("S2_API_KEY"),
        "core": has("CORE_API_KEY"),
        "openreview": has("OPENREVIEW_TOKEN"),
        "keys_file": keys_file,
    }
    miss = [k for k in ("email", "openalex", "s2", "core", "openreview")
            if not info[k]]
    info["hint"] = ("未配置：{}。缺 key 只影响对应数据源，其它工具照常可用。"
                    .format(", ".join(miss)) if miss else "凭证齐备")
    return info


# 导入本模块即完成接入（方便 `import papersearch` 直接可用）
try:
    ensure_importable()
except FileNotFoundError:
    # 不在导入期抛异常 —— 让 describe()/doctor() 能把问题清楚地报给调用方，
    # 而不是让一次 import 直接崩掉整个进程。
    pass
