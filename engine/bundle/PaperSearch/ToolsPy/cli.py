#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cli.py —— ToolsPy 命令行（给人用；Agent 建议直接 import）

    python cli.py doctor                      环境自检
    python cli.py list                        列出全部工具
    python cli.py doc E41                     打印某工具的文档（含 API.md 行号）
    python cli.py doc --symbol search_arxiv   按函数名查
    python cli.py search "deep cfr" --top 10  跑检索流水线
    python cli.py run "deep cfr" --out /tmp/kb --fetch-top 3   跑完整流水线
    python cli.py parse paper.pdf             解析单个 PDF
    python cli.py selfcheck                   离线自检
"""
from __future__ import annotations

import argparse
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import papersearch as T                     # noqa: E402  （统一入口）
from papersearch import result as R         # noqa: E402


def _emit(r: dict, as_json: bool) -> int:
    """统一输出：默认人类可读，--json 给机器。"""
    if as_json:
        print(json.dumps(r, ensure_ascii=False, indent=2))
    else:
        print(R.pretty(r))
        if r.get("status") in ("failed", "blocked") and r.get("error"):
            e = r["error"]
            print("  code      :", e.get("code"))
            print("  retryable :", e.get("retryable"))
            print("  hint      :", e.get("hint"))
            if e.get("stage"):
                print("  stage     :", e.get("stage"))
    return 0 if R.is_usable(r) else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="ToolsPy 命令行")
    ap.add_argument("--json", action="store_true", help="输出原始 JSON 信封")
    sub = ap.add_subparsers(dest="cmd")

    sub.add_parser("doctor", help="环境自检").add_argument("--json", action="store_true")

    p = sub.add_parser("list", help="列出全部工具")
    p.add_argument("--kind", choices=["element", "pipeline"], default="")

    p = sub.add_parser("doc", help="查某工具的文档")
    p.add_argument("symbol", nargs="?", default="", help="工具编号（E41）或函数名")
    p.add_argument("--symbol", dest="symbol_opt", default="")
    p.add_argument("--full", action="store_true", help="打印 API.md 里的完整正文")

    p = sub.add_parser("search", help="检索流水线（P10）")
    p.add_argument("query")
    p.add_argument("--top", type=int, default=20)
    p.add_argument("--per-source", type=int, default=10)
    p.add_argument("--year-from", type=int, default=0)
    p.add_argument("--year-to", type=int, default=0)

    p = sub.add_parser("run", help="完整流水线（P30）")
    p.add_argument("query")
    p.add_argument("--out", default="", help="产物目录（建议显式指定）")
    p.add_argument("--top", type=int, default=20)
    p.add_argument("--per-source", type=int, default=10)
    p.add_argument("--expand-top", type=int, default=2)
    p.add_argument("--fetch-top", type=int, default=3)
    p.add_argument("--parse-top", type=int, default=3)

    p = sub.add_parser("parse", help="解析单个 PDF（E41）")
    p.add_argument("pdf")
    p.add_argument("--out", default="")

    p = sub.add_parser("download", help="下载 PDF（E31）")
    p.add_argument("--doi", default="")
    p.add_argument("--arxiv", default="")
    p.add_argument("--out", default="")

    sub.add_parser("selfcheck", help="离线自检")

    a = ap.parse_args(argv)
    js = getattr(a, "json", False) or ("--json" in (argv or sys.argv))

    if a.cmd == "doctor":
        r = T.doctor()
        if js:
            return _emit(r, True)
        print(T.pretty(r) if hasattr(T, "pretty") else R.pretty(r))
        d = r.get("data") or {}
        for k in ("dependencies", "credentials", "grobid"):
            if k in d:
                print("  {}: {}".format(k, json.dumps(d[k], ensure_ascii=False)[:150]))
        print("  各源:", {k: v.get("usable") for k, v in (d.get("sources") or {}).items()})
        for act in (d.get("next_actions") or []):
            print("  → {}: {} | {}".format(act.get("action"), act.get("detail", ""),
                                           act.get("command", "")))
        return 0 if R.is_usable(r) else 1

    if a.cmd == "list":
        r = T.list_tools(kind=a.kind)
        if js:
            return _emit(r, True)
        for t in r["data"]["tools"]:
            print("  {} {:<22} [{}] {}".format(t["id"], t["name"], t["kind"][:4], t["summary"]))
        print("\n菜谱：")
        for rec in r["data"]["recipes"]:
            print("  {} → {}".format(rec["goal"], rec["call"]))
        return 0

    if a.cmd == "doc":
        sym = a.symbol or a.symbol_opt
        if not sym:
            print("用法: cli.py doc E41  或  cli.py doc --symbol search_arxiv")
            return 2
        if a.full:
            r = T.read_symbol(sym)
            if r["ok"]:
                print("### API.md 第 {}-{} 行 ({})".format(
                    r["data"]["line_start"], r["data"]["line_end"], r["data"]["path"]))
                print(r["data"]["markdown"])
                return 0
            return _emit(r, js)
        r = T.describe_tool(sym)
        if js:
            return _emit(r, True)
        if not r["ok"]:
            return _emit(r, js)
        d = r["data"]
        print("### [{}] {}".format(d["id"], d["name"]))
        print("模块  :", d["module"], "| 类型:", d["kind"])
        print("用途  :", d["summary"])
        print("何时用:", d.get("when", ""))
        print("返回  :", d.get("returns", ""))
        print("状态  :", d.get("status", ""))
        if d.get("params"):
            print("参数  :")
            for k, v in d["params"].items():
                print("   - {}: {}".format(k, v))
        if d.get("api_md"):
            m = d["api_md"]
            print("文档  : API.md 第 {}-{} 行".format(m["line_start"], m["line_end"]))
            print("        ", m["read_hint"])
        return 0

    if a.cmd == "search":
        return _emit(T.search_pipeline(a.query, max_per_source=a.per_source,
                                       keep_top=a.top, year_from=a.year_from,
                                       year_to=a.year_to), js)
    if a.cmd == "run":
        r = T.full_pipeline(a.query, out_dir=a.out, max_per_source=a.per_source,
                            keep_top=a.top, expand_top=a.expand_top,
                            fetch_top=a.fetch_top, parse_top=a.parse_top)
        if js:
            return _emit(r, True)
        print(T.summarize_result(r))
        d = r.get("data") or {}
        if d.get("stats"):
            print("\n  统计:", json.dumps(d["stats"], ensure_ascii=False))
        for k in ("corpus_path", "graph_path", "out_dir"):
            if d.get(k):
                print("  {}: {}".format(k, d[k]))
        return 0 if R.is_usable(r) else 1
    if a.cmd == "parse":
        r = T.parse_pdf(a.pdf, out_dir=a.out)
        if js:
            return _emit(r, True)
        print(R.pretty(r))
        st = (r.get("data") or {}).get("stats") if isinstance(r.get("data"), dict) else None
        if st:
            print("  统计:", json.dumps(st, ensure_ascii=False))
        return 0 if R.is_usable(r) else 1
    if a.cmd == "download":
        if not (a.doi or a.arxiv):
            print("需要 --doi 或 --arxiv")
            return 2
        return _emit(T.download_pdf(doi=a.doi, arxiv_id=a.arxiv, out_dir=a.out), js)
    if a.cmd == "selfcheck":
        import selfcheck
        return selfcheck.main([])

    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
