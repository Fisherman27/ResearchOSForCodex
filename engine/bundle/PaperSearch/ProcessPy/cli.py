#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cli.py —— ProcessPy 命令行入口

用法
----
    python3 cli.py run "Deep CFR 在多人扑克中的收敛性"          # 跑完整流程
    python3 cli.py run "..." --top 40 --no-expand --no-pdf      # 只做检索+排序
    python3 cli.py run "..." --resume                           # 中断后接着跑
    python3 cli.py check                                        # 环境自检（不联网）
    python3 cli.py show out/corpus.jsonl                        # 看语料库摘要
    python3 cli.py query "..."                                  # 只看会生成哪些检索式

设计说明
--------
- 参数名与 PipelineConfig 字段一一对应（`--fetch-top-n` ↔ `fetch_top_n`），
  不另造一套词汇，避免"命令行叫 A、配置里叫 B"的记忆负担；
- 所有开关都有默认值，**最常用的一次调用就是 `cli.py run "研究问题"`**。

函数索引
─────────────────────────────────────────────────────────────────────────────
  [入口]  main(argv)               解析参数并分发
  [命令]  cmd_run / cmd_check / cmd_show / cmd_query
  [工具]  build_config(args)       命令行 → PipelineConfig
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import PipelineConfig                              # noqa: E402
import _bootstrap                                              # noqa: E402


def build_config(a) -> PipelineConfig:
    """把 argparse 的命名空间转成 PipelineConfig。

    用 `getattr(a, name, 默认)` 而不是直接取属性：
    因为 `run` 子命令才有那些检索参数，而 `query` / `check` / `show` 共用
    同一个 build_config —— 直接取属性会让 `cli.py query` 直接崩
    （实测：`AttributeError: 'Namespace' object has no attribute 'resume'`）。
    默认值取自 PipelineConfig 本身，所以**只有一处**定义默认行为。

    仍然逐字段赋值而不是 `**vars(a)`：这样新增参数时不会因为名字拼错被静默忽略。
    """
    d = PipelineConfig()          # 用它的字段默认值作为 getattr 的兜底
    cfg = PipelineConfig()
    cfg.out_dir = os.path.abspath(getattr(a, "out", d.out_dir))
    cfg.examplepy = getattr(a, "examplepy", "") or ""
    cfg.resume = bool(getattr(a, "resume", False))
    cfg.research_question = getattr(a, "question", "") or ""
    cfg.queries = getattr(a, "query", None) or []
    cfg.use_arxiv = not getattr(a, "no_arxiv", False)
    cfg.use_openalex = not getattr(a, "no_openalex", False)
    cfg.use_semantic_scholar = not getattr(a, "no_s2", False)
    cfg.per_source_limit = getattr(a, "per_source", d.per_source_limit)
    cfg.year_from = getattr(a, "year_from", 0) or 0
    cfg.year_to = getattr(a, "year_to", 0) or 0
    cfg.keep_top = getattr(a, "top", d.keep_top)
    cfg.expand_citations = not getattr(a, "no_expand", False)
    cfg.expand_top_n = getattr(a, "expand_top", d.expand_top_n)
    cfg.expansion_keep = getattr(a, "expand_keep", d.expansion_keep)
    cfg.fetch_pdf = not getattr(a, "no_pdf", False)
    cfg.fetch_top_n = getattr(a, "fetch_top", d.fetch_top_n)
    cfg.parse_structure = not getattr(a, "no_parse", False)
    cfg.parse_top_n = getattr(a, "parse_top", d.parse_top_n)
    cfg.grobid_url = getattr(a, "grobid_url", d.grobid_url)
    cfg.verbose = not getattr(a, "quiet", False)
    return cfg


def cmd_query(a) -> int:
    """只做 STEP 1-2：看看会生成哪些检索式和概念（不联网，秒回）。

    用途：检索前先人工过一眼 —— 概念抽歪了（比如把"深度"当概念）在这里
    就能发现，比跑完一轮再回头查便宜得多。
    """
    import query as q
    plan = q.build_queries(a.question or "", build_config(a))
    print("研究问题 :", plan["question"])
    print("子问题   :", plan["sub_questions"])
    print()
    print("核心概念（用于检索，{} 个）:".format(len(plan["core_concepts"])))
    for c in plan["core_concepts"]:
        print("   -", c)
    print()
    print("全部概念（含同义词，{} 个，用于打分）:".format(len(plan["concepts"])))
    print("   ", ", ".join(plan["concepts"]))
    print()
    for src in ("arxiv", "openalex", "s2"):
        print("{} 检索式（{} 条）:".format(src, len(plan["queries"][src])))
        for x in plan["queries"][src]:
            print("   ", x)
    print()
    print("人工补查（Google Scholar 等）建议用:")
    for x in plan["human"]:
        print("   ", x)
    return 0


def cmd_check(a) -> int:
    """环境自检：接入、依赖、各源可用性、GROBID 服务状态（只探活，不检索）。"""
    print("=" * 70)
    print("ProcessPy 环境自检")
    print("=" * 70)
    bs = _bootstrap.describe()
    print("ExamplePy 接入 : {}".format("✓ " + bs["dir"] if bs["ok"] else "✗ " + bs["error"][:100]))
    if not bs["ok"]:
        return 1
    print("Python         : {}".format(bs["python"]))
    try:
        import _common
        email = _common.get_key("PAPERSEARCH_EMAIL")
        oakey = _common.get_key("OPENALEX_API_KEY")
        s2key = _common.get_key("S2_API_KEY")
        corekey = _common.get_key("CORE_API_KEY")
        print("邮箱           : {}".format(email or "（未配置）"))
        print("OpenAlex key   : {}".format("已配置" if oakey else "未配置（search 可能被匿名限流）"))
        print("S2 key         : {}".format("已配置" if s2key else "未配置（该源会被跳过）"))
        print("CORE key       : {}".format("已配置" if corekey else "未配置（V1 不需要）"))
    except Exception as exc:
        print("凭证读取失败   : {}".format(exc))

    import fulltext
    cfg = PipelineConfig()
    try:
        import grobid
        print("pypdf 兜底     : {}".format(grobid.PDF_BACKEND_NOTE))
    except Exception as exc:
        print("pypdf 兜底     : 不可用（{}）".format(str(exc)[:60]))
    alive, msg = fulltext.grobid_available(cfg)
    print("GROBID 服务    : {} | {}".format("✓ 在线" if alive else "○ 未启动", msg[:70]))
    if not alive:
        print("                 启动：./grobid-serve.sh start （在 ExamplePy 下）")
        print("                 不启动也能跑，STEP8 会降级为纯文本（无章节结构）")
    print()
    print("下一步：python3 cli.py run \"你的研究问题\"")
    return 0


def cmd_show(a) -> int:
    """看一个已生成语料库的摘要（不重新检索）。"""
    import store
    p = a.path
    if os.path.isdir(p):
        p = os.path.join(p, "corpus.jsonl")
    if not os.path.isfile(p):
        print("找不到语料库文件：{}".format(p))
        return 1
    st = store.CorpusStore(os.path.dirname(os.path.abspath(p)))
    papers = st.load_papers()
    s = store.summarize(papers)
    print("语料库：{}".format(p))
    print("论文 {} 篇 | 有摘要 {} | 有 PDF {} | 已结构解析 {} | 有引用图 {}".format(
        s["papers"], s["with_abstract"], s["with_pdf"],
        s["with_structure"], s["with_citation_graph"]))
    print("来源分布：{}".format(s["by_source"]))
    print()
    print("Top 10：")
    for i, pp in enumerate(papers[:10], 1):
        print("  {:2}. [{:.3f}] {} ({})".format(
            i, pp.score.get("total", 0),
            (pp.metadata.get("title") or "")[:66], pp.metadata.get("year") or "?"))
    return 0


def cmd_run(a) -> int:
    """跑完整流程。"""
    import pipeline
    cfg = build_config(a)
    # 显式指定 --examplepy 时同步给 bootstrap，保证后续 import 一致
    if cfg.examplepy:
        _bootstrap.ensure_importable(cfg.examplepy)
    r = pipeline.run(cfg)
    return 0 if r.get("ok") else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="论文检索流程（V1）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__)
    sub = ap.add_subparsers(dest="cmd")

    def add_common(p):
        p.add_argument("-o", "--out", default=os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "out"),
            help="产物目录（默认 ProcessPy/out）")
        p.add_argument("--examplepy", default="", help="ExamplePy 目录（默认自动定位）")
        p.add_argument("--quiet", action="store_true")
        p.add_argument("--grobid-url", default="http://localhost:8070")

    p_run = sub.add_parser("run", help="跑完整流程")
    p_run.add_argument("question", nargs="?", default="", help="研究问题")
    p_run.add_argument("--query", action="append", help="手工指定检索式（可多次）")
    p_run.add_argument("--per-source", type=int, default=60, help="每个源每条检索式取多少")
    p_run.add_argument("--top", type=int, default=60, help="STEP5 保留多少篇")
    p_run.add_argument("--year-from", type=int, default=0)
    p_run.add_argument("--year-to", type=int, default=0)
    p_run.add_argument("--expand-top", type=int, default=8, help="对前 N 篇做引用扩展")
    p_run.add_argument("--expand-keep", type=int, default=60, help="扩展后再保留多少篇")
    p_run.add_argument("--fetch-top", type=int, default=12, help="抓前 N 篇 PDF")
    p_run.add_argument("--parse-top", type=int, default=12, help="解析前 N 篇结构")
    p_run.add_argument("--no-arxiv", action="store_true")
    p_run.add_argument("--no-openalex", action="store_true")
    p_run.add_argument("--no-s2", action="store_true")
    p_run.add_argument("--no-expand", action="store_true", help="不做引用扩展")
    p_run.add_argument("--no-pdf", action="store_true", help="不抓 PDF")
    p_run.add_argument("--no-parse", action="store_true", help="不做结构解析")
    p_run.add_argument("--resume", action="store_true", help="接着上次跑")
    add_common(p_run)

    p_q = sub.add_parser("query", help="只看检索式（不联网）")
    p_q.add_argument("question")
    add_common(p_q)

    p_c = sub.add_parser("check", help="环境自检")
    add_common(p_c)

    p_s = sub.add_parser("show", help="看已生成语料库的摘要")
    p_s.add_argument("path")
    add_common(p_s)

    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 0
    if a.cmd == "run":
        return cmd_run(a)
    if a.cmd == "query":
        return cmd_query(a)
    if a.cmd == "check":
        return cmd_check(a)
    if a.cmd == "show":
        return cmd_show(a)
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
