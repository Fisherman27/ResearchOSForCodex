#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gen_api_md.py —— 从代码生成 API.md（保证文档与实现同步）

为什么要生成而不是手写
----------------------
手写的 API 文档一定会和代码脱节（参数改了、状态语义改了，文档没跟上）。
而 Agent 完全依赖这份文档来调用工具 —— 文档错了，Agent 就会用错。

本脚本从**三个来源**合成 API.md：
    1. `registry.REGISTRY`  —— 工具清单、参数、返回、状态语义、使用时机（机器可读的事实）
    2. 各函数的 **docstring** —— 详细说明、示例、注意事项（人写的解释）
    3. `errors.CODES`       —— 错误码表（错因 + 可否重试 + 修法）

用法
----
    python gen_api_md.py            # 生成/覆盖 API.md
    python gen_api_md.py --check    # 只检查是否需要更新（CI 用）

产物
----
    API.md      —— 详细 API 文档（Agent 按需读；用 registry.find_symbol 定位行号）
"""
from __future__ import annotations

import argparse
import inspect
import io
import os
import sys
from typing import Any, Dict, List

_HERE = os.path.dirname(os.path.abspath(__file__))          # ToolsPy/docs
_TOOLSPY = os.path.dirname(_HERE)                           # ToolsPy 根
if _TOOLSPY not in sys.path:
    sys.path.insert(0, _TOOLSPY)

from papersearch import errors as ERR          # noqa: E402
from papersearch import registry as REG        # noqa: E402

OUT = os.path.join(_TOOLSPY, "API.md")

_KIND_TITLE = {"element": "元件工具（原子能力）", "pipeline": "自动化工具流（流水线）"}


def _func_of(tool_id: str):
    """按注册表拿到实际的 Python 函数对象（用于抽 docstring）。"""
    t = REG.REGISTRY.get(tool_id)
    if not t:
        return None
    try:
        mod = __import__(t["module"])
        return getattr(mod, t["name"], None)
    except Exception:
        return None


def _sig(fn) -> str:
    """取函数签名字符串（去掉 self，只保留参数名与默认值）。"""
    try:
        return "{}({})".format(fn.__name__, str(inspect.signature(fn))[1:-1])
    except Exception:
        return getattr(fn, "__name__", "?")


def render() -> str:
    L: List[str] = []
    A = L.append

    A("# ToolsPy API 文档")
    A("")
    A("> 本文档由 `gen_api_md.py` 从代码自动生成 —— **不要手工编辑**，")
    A("> 改代码后重跑该脚本。手写文档一定会和实现脱节，而 Agent 完全依赖它。")
    A("")
    A("## 怎么用这份文档（给 Agent）")
    A("")
    A("别一次读完 —— 它很长。正确姿势：")
    A("")
    A("```python")
    A("from papersearch import registry")
    A("")
    A("registry.list_tools()              # 1. 看有哪些工具（含一句话说明与使用时机）")
    A("registry.find_symbol(\"E10\")        # 2. 拿到该条目在本文档的**行号区间**")
    A("#   → {'line_start': 123, 'line_end': 178, 'read_hint': \"read(..., offset=123, ...)\"}")
    A("registry.read_symbol(\"E10\")        #    或直接取该段 markdown（更省事）")
    A("```")
    A("")
    A("这样只读需要的那一段，不必把整份文档塞进上下文。")
    A("")
    A("---")
    A("")

    # ---------------- 目录（含行号占位，最后回填）----------------
    A("## 目录")
    A("")
    toc_at = len(L)             # 记录目录插入位置，稍后回填
    A("")
    A("---")
    A("")

    # ---------------- 核心契约 ----------------
    A("## 一、核心契约（读工具前必看）")
    A("")
    A("### 1.1 所有工具都返回同一个信封")
    A("")
    A("```jsonc")
    A("{")
    A("  \"ok\":       true,          // 调用是否成功（语义见下表）")
    A("  \"status\":   \"partial\",     // ok | partial | empty | blocked | failed")
    A("  \"data\":     {...},          // 主产物（成功时）")
    A("  \"error\":    null,           // 失败时是标准错误字典（含 code/retryable/hint/cause）")
    A("  \"warnings\": [...],          // 非致命问题（数据可疑、源降级……）")
    A("  \"diagnostics\": {...},       // 过程指标：耗时、各步状态、命中数、失败详情")
    A("  \"meta\":     {...}           // 工具名、参数快照、时间")
    A("}")
    A("```")
    A("")
    A("### 1.2 status 的语义边界（**本库最重要的约定**）")
    A("")
    A("| status | ok | 含义 | 调用方该做什么 |")
    A("|---|---|---|---|")
    A("| `ok` | ✅ | 全部成功，data 完整 | 直接用 |")
    A("| `partial` | ✅ | 拿到了**部分**结果且有明确原因 | **结果可用**，看 diagnostics 决定是否补救 |")
    A("| `empty` | ✅ | 成功执行但结果为空 | **空集不是错误**；放宽条件或接受 |")
    A("| `blocked` | ❌ | 缺前提（无 key/服务没起） | 按 error.hint 补前提，**重试无用** |")
    A("| `failed` | ❌ | 执行出错 | 看 error.code 与 retryable 决定重试/上报 |")
    A("")
    A("> ⚠️ **为什么 `empty` 要单独一类**：")
    A("> \"检索到 0 篇\" 和 \"检索失败\" 在传统代码里都表现为空列表/None，")
    A("> 下游无法区分\"这个领域真的没论文\"和\"我的请求挂了\"。")
    A("> 实测踩过：HTTP 200 + HTML 错误页被当成\"成功的空集\"，整个流程报 done ——")
    A("> **失败伪装成功**是最难发现的一类 bug。")
    A("")
    A("> ⚠️ **为什么 `partial` 的 ok 是 True**：")
    A("> 多源检索里单源失败是常态（限流/缺凭证）。若判为失败，")
    A("> 调用方会丢弃已经拿到的结果 —— 那才是真正的损失。")
    A("")
    A("### 1.3 判断要不要继续往下走")
    A("")
    A("```python")
    A("from papersearch import result")
    A("r = search_pipeline(\"deep cfr\")")
    A("result.is_usable(r)   # True  → 结果可用（含 partial/empty），可以继续")
    A("result.is_ok(r)       # True  → 严格成功（仅 ok）")
    A("result.pretty(r)      # 一行摘要，适合打日志")
    A("```")
    A("")
    A("### 1.4 出错时 Agent 该怎么定位")
    A("")
    A("每个错误都带这几样（全部来自 `errors.py`）：")
    A("")
    A("| 字段 | 怎么用 |")
    A("|---|---|")
    A("| `error.code` | 稳定的字符串码，可 `==` 判断、可写进分支逻辑 |")
    A("| `error.retryable` | **该不该重试** —— 重试有代价（耗时/限流），别盲目重试 |")
    A("| `error.hint` | 该怎么办，**可执行**的措辞（不是\"检查网络\"这种废话） |")
    A("| `error.severity` | `input`(改参数) / `degradable`(可降级) / `fatal`(做不成) |")
    A("| `error.stage` | 多步工具的步骤名 —— 告诉你是第几步挂的 |")
    A("| `error.component` | 出错的工具编号（如 `E41`）→ 用 `find_symbol` 跳到本文档对应节 |")
    A("| `error.context` | 相关输入快照 —— \"我传了什么导致这样\" |")
    A("| `error.cause` | 原始异常 + **traceback**（本项目自己的 bug 靠它定位） |")
    A("")
    A("---")
    A("")

    # ---------------- 工具清单 ----------------
    A("## 二、工具清单")
    A("")
    A("| 编号 | 名称 | 类型 | 一句话说明 |")
    A("|---|---|---|---|")
    for tid in sorted(REG.REGISTRY, key=lambda x: (REG.REGISTRY[x]["kind"], x)):
        t = REG.REGISTRY[tid]
        A("| `{}` | `{}` | {} | {} |".format(
            tid, t["name"], "元件" if t["kind"] == "element" else "流水线",
            t["summary"]))
    A("")
    A("### 常用组合（菜谱）")
    A("")
    for r in REG.RECIPES:
        A("- **{}** → `{}`".format(r["goal"], r["call"]))
        A("  - {}".format(r["note"]))
    A("")
    A("---")
    A("")

    # ---------------- 逐个工具的详细说明 ----------------
    A("## 三、工具详解")
    A("")
    for kind in ("element", "pipeline"):
        A("### {}".format(_KIND_TITLE[kind]))
        A("")
        for tid in sorted(REG.REGISTRY):
            t = REG.REGISTRY[tid]
            if t["kind"] != kind:
                continue
            toc_marks.append((tid, t["name"], len(L) + 1))
            A("### [{}] {}".format(tid, t["name"]))
            A("")
            A("**模块**：`{}` ｜ **签名**：`{}`".format(
                t["module"], _sig(_func_of(tid)) if _func_of(tid) else t["name"]))
            A("")
            A("**用途**：{}".format(t["summary"]))
            A("")
            A("**什么时候用**：{}".format(t.get("when", "—")))
            A("")
            if t.get("params"):
                A("**参数**")
                A("")
                A("| 参数 | 说明 |")
                A("|---|---|")
                for k, v in t["params"].items():
                    A("| `{}` | {} |".format(k, v))
                A("")
            else:
                A("**参数**：无")
                A("")
            A("**返回**：`{}`".format(t.get("returns", "")))
            A("")
            A("**状态语义**：{}".format(t.get("status", "")))
            A("")
            fn = _func_of(tid)
            doc = (inspect.getdoc(fn) or "").strip() if fn else ""
            if doc:
                # 去掉 docstring 第一行（已在"用途"里），保留详细说明与示例
                body = "\n".join(doc.splitlines()[1:]).strip()
                if body:
                    A("**详细说明**")
                    A("")
                    A("```text")
                    A(body)
                    A("```")
                    A("")
            A("---")
            A("")

    # ---------------- 错误码表 ----------------
    A("## 四、错误码表")
    A("")
    A("| 错误码 | 严重度 | 可重试 | 修法 |")
    A("|---|---|---|---|")
    for code in sorted(ERR.CODES):
        c = ERR.CODES[code]
        A("| `{}` | {} | {} | {} |".format(
            code, c["severity"], "✅ 是" if c["retryable"] else "❌ 否",
            c["hint"].replace("\n", " ")[:150]))
    A("")
    A("---")
    A("")

    # ---------------- Paper 结构 ----------------
    A("## 五、Paper 结构（所有工具返回的论文对象）")
    A("")
    A("```jsonc")
    A("{")
    A("  \"paper_id\": \"doi:10.1609/aaai.v33i01.33012157\",  // 确定性主键，见下")
    A("  \"identity\": { \"doi\", \"arxiv_id\", \"openalex_id\", \"s2_id\",")
    A("                \"openreview_id\", \"core_id\" },")
    A("  \"metadata\": { \"title\", \"authors\", \"year\", \"preprint_year\",")
    A("                \"venue\", \"categories\", \"keywords\", \"topics\",")
    A("                \"citation_count\", \"is_retracted\", \"arxiv_version\", ... },")
    A("  \"abstract\": \"...\",")
    A("  \"full_text\": { \"pdf_path\", \"pdf_url\", \"pdf_source\", \"oa_status\",")
    A("                 \"version\", \"license\", \"pdf_bytes\" },")
    A("  \"citation_graph\": { \"references\", \"cited_by\", \"reference_count\", \"source\" },")
    A("  \"document_structure\": {")
    A("      \"parsed_by\": \"grobid\",")
    A("      \"sections\":  [{title, level, paragraphs, paragraph_list, text}],")
    A("      \"spans\":     [{span_id, section, char_start, char_end, text,")
    A("                      coords, coords_level, page_first, page_last}],")
    A("      \"figure_objects\": [{id, label, text, coords, coords_boxes_per_page,")
    A("                           coords_raw_boxes, coords_n_raw_boxes}],")
    A("      \"table_objects\": [...], \"formula_objects\": [...],")
    A("      \"citation_links\": {links, unlinked, link_rate},")
    A("      \"coordinates\": {count, by_element, elements},")
    A("      \"references_parsed\": [{title, authors, year, doi}] },")
    A("  \"provenance\": { \"found_by\": [{source, kind, query, at}], \"history\": [...] },")
    A("  \"score\": { \"total\", \"parts\", \"why\", \"rank\" }")
    A("}")
    A("```")
    A("")
    A("### paper_id 生成规则（确定性、可读）")
    A("")
    A("```")
    A("doi:10.1038/nature12373      有 DOI（归一化后小写）")
    A("arxiv:1603.01121             有 arXiv ID（**去版本号**，v1/v2 是同一篇）")
    A("openalex:W2741809807         只有 OpenAlex ID")
    A("title:<sha1前16位>           都没有，用标题哈希兜底")
    A("```")
    A("")
    A("> 非法标识符会被跳过，**逐级回退**（不会生成 `doi:` 这种空主体）。")
    A("> 版本号本身存在 `metadata.arxiv_version`，不丢。")
    A("")
    A("### 坐标（document_structure）")
    A("")
    A("| 项 | 说明 |")
    A("|---|---|")
    A("| 坐标系 | **原点页面左上角，y 向下**，单位 PDF 点（1/72 英寸） |")
    A("| `coords` | 合并后的**包围盒** `{x,y,x2,y2,width,height,page_first,page_last}` |")
    A("| `coords_boxes_per_page` | 跨页时每页一个框（跨页元素**没有**单一矩形） |")
    A("| `coords_raw_boxes` | GROBID 原始**行框**（证据留存） |")
    A("| `coords_n_raw_boxes` | 原始行框数；**==1 时坐标可疑**（GROBID 可能只认到图注） |")
    A("| `coords_level` (spans) | 恒为 `paragraph` —— GROBID 只给段落级框，句子级是**假精度** |")
    A("")
    A("> 画到 PDF 上要转换：`pdf_y = page_height - grobid_y2`。")
    A("")
    A("---")
    A("")
    A("## 六、复现与诊断")
    A("")
    A("- 流水线产物目录含 `run.json`：**参数快照 + 逐步状态** —— 复现一次运行只需要它。")
    A("- `result.summarize_result(env)` 给你一段人类可读的逐步摘要。")
    A("- 出错先跑 `doctor()`：多数\"工具不工作\"是缺 key 或 GROBID 没启动。")
    A("")

    # ---- 回填目录（含行号）----
    toc: List[str] = []
    for tid, name, line in toc_marks:
        toc.append("- [`{}` {}](#{}-{}) — 见第 {} 行".format(
            tid, name, tid.lower(), name.lower(), line))
    L[toc_at:toc_at] = toc
    return "\n".join(L) + "\n"


# 记录每个条目写入时的行号（render 过程中填充）
toc_marks: List = []


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="从代码生成 ToolsPy API.md")
    ap.add_argument("--check", action="store_true",
                    help="只检查 API.md 是否与代码同步（不写入）")
    a = ap.parse_args(argv)
    toc_marks.clear()
    content = render()
    if a.check:
        old = ""
        if os.path.isfile(OUT):
            with open(OUT, "r", encoding="utf-8") as fh:
                old = fh.read()
        if old == content:
            print("API.md 已是最新（{} 字节）".format(len(content)))
            return 0
        print("API.md 与代码不同步，请重跑 gen_api_md.py")
        return 1
    with open(OUT, "w", encoding="utf-8") as fh:
        fh.write(content)
    print("已生成 {}（{} 字节，{} 个工具条目）".format(
        OUT, len(content), len(toc_marks)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
