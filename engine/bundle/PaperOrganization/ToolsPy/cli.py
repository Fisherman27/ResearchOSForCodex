#!/usr/bin/env python3
"""PaperOrganization 工具库的命令行入口。

用法：
    python cli.py init      --kb <kb>                            初始化知识库
    python cli.py import    --search-out <out> --kb <kb> [--no-pdf]  导入 PaperSearch 产物
    python cli.py read      --file <results.json> --kb <kb>      批量落库阅读结果
    python cli.py export    --kb <kb>                            重新导出 TSV
    python cli.py stats     --kb <kb>                            知识库统计
    python cli.py check     --kb <kb>                            符合性检查（对照）
    python cli.py locate    --kb <kb> [--media-id <id>]          坐标定位验证
    python cli.py papers    --kb <kb>                            列出论文
    python cli.py elements                                       列出所有元件/流水线工具

退出码：0=成功，1=失败（错误详情打印到 stderr，含 code/location/hint）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

from paperorganization import conformance, locate, pipelines, query  # noqa: E402
from paperorganization.errors import ToolResult                     # noqa: E402
from paperorganization.paths import resolve_kb                       # noqa: E402

# 包名（用于打印模块路径，避免硬编码漂移）
_PKG = "paperorganization"


def _cfg(args):
    """解析 --kb 得到配置。"""
    r = resolve_kb(getattr(args, "kb", None))
    if not r.ok:
        _print_result(r)
        sys.exit(1)
    return r.data


def _print_result(r: ToolResult, show_data: bool = True):
    """统一打印结果：成功给摘要，失败给结构化错误（方便 Agent 定位）。"""
    if r.warnings:
        for w in r.warnings:
            print("  ⚠ {}".format(w), file=sys.stderr)
    if r.ok:
        print(r.summary())
        if show_data and r.data is not None:
            print(json.dumps(r.data, ensure_ascii=False, indent=2, default=str)[:4000])
        return
    e = r.error
    print("FAIL [{}] {}".format(e.code, e.message), file=sys.stderr)
    if e.location:
        print("  location: {}".format(e.location), file=sys.stderr)
    if e.hint:
        print("  hint    : {}".format(e.hint), file=sys.stderr)
    if e.context:
        print("  context : {}".format(json.dumps(e.context, ensure_ascii=False,
                                                 default=str)[:1500]), file=sys.stderr)
    if e.detail:
        print("  detail  :", file=sys.stderr)
        for line in e.detail.strip().splitlines()[-12:]:
            print("    " + line, file=sys.stderr)
    if r.data is not None and show_data:
        print("  partial data: {}".format(json.dumps(r.data, ensure_ascii=False,
                                                     default=str)[:1000]), file=sys.stderr)


# ---------------------------------------------------------------------------
# 子命令
# ---------------------------------------------------------------------------

def cmd_init(args):
    cfg = _cfg(args)
    _print_result(pipelines.init_kb(cfg))
    return 0


def cmd_import(args):
    cfg = _cfg(args)
    r = pipelines.import_search_output(args.search_out, cfg, copy_pdf=not args.no_pdf)
    _print_result(r)
    return 0 if r.ok else 1


def cmd_read(args):
    cfg = _cfg(args)
    r = pipelines.store_reading_results_file(args.file, cfg)
    _print_result(r)
    # partial 也算「有失败」，返回 1，但 data 已经打印
    return 0 if r.ok else 1


def cmd_export(args):
    cfg = _cfg(args)
    _print_result(pipelines.rebuild_exports(cfg))
    return 0


def cmd_stats(args):
    cfg = _cfg(args)
    r = query.stats(cfg)
    _print_result(r)
    return 0 if r.ok else 1


def cmd_check(args):
    cfg = _cfg(args)
    r = conformance.check_conformance(cfg)
    # 检查结果即使有 FAIL，也是「成功执行了检查」，打印报告并返回 1 表示不合格
    if r.ok:
        s = r.data["summary"]
        print("符合性检查：{} PASS / {} FAIL / {} WARN".format(
            s["pass"], s["fail"], s["warn"]))
        if r.data["failures"]:
            print("\nFAIL 项：")
            for f in r.data["failures"]:
                print("  - {}  {}".format(f["item"], f["detail"]))
        if r.data["warnings"]:
            print("\nWARN 项：")
            for w in r.data["warnings"]:
                print("  - {}  {}".format(w["item"], w["detail"]))
        return 0 if s["fail"] == 0 else 1
    _print_result(r)
    return 1


def cmd_locate(args):
    cfg = _cfg(args)
    if args.media_id:
        r = locate.verify_locate(cfg, args.media_id)
    else:
        r = locate.verify_all(cfg)
    _print_result(r, show_data=bool(args.media_id))
    if r.ok and not args.media_id:
        print("\n按类型统计：")
        for k, s in r.data["by_kind"].items():
            print("  {:<9s} 通过 {}/{}  ({}%)".format(
                k, s["passed"], s["passed"] + s["failed"],
                r.data["pass_rate"].get(k)))
    return 0


def cmd_papers(args):
    cfg = _cfg(args)
    r = query.list_papers(cfg, limit=args.limit)
    if r.ok:
        print("{:<34s} {:<6s} {:<12s} {}".format("paper_id", "year", "depth", "title"))
        for p in r.data:
            # ★ year 可能是 int，统一转 str 再格式化（原来直接格式化 int 会 ValueError）
            print("{:<34s} {:<6s} {:<12s} {}".format(
                str(p["paper_id"]), str(p.get("year") or ""),
                str(p.get("reading_depth") or ""), (p.get("title") or "")[:50]))
    else:
        _print_result(r)
        return 1
    return 0


def cmd_elements(args):
    """列出工具库的全部元件和流水线工具（供 Agent 发现可用能力）。"""
    print("PaperOrganization 工具库 —— 元件层与流水线层\n")
    print("元件层（element，可精细调整）：")
    elements = {
        "errors": "统一结果/错误机制（ToolResult / ToolError / ErrorCode）",
        "paths": "路径与配置（resolve_kb / assert_ready / KBConfig）",
        "schema": "建表与连接（ensure_schema / connect / clear_tables / safe_id）",
        "ids": "ID 规则（claim_id / assumption_id / relation_id / media_id / span_id）",
        "importers": "数据转换纯函数（build_spans / build_media / paper_to_raw）",
        "writers": "写库（write_paper / write_spans / write_media / write_citations / write_raw_json）",
        "validators": "校验纯函数（validate_reading_result / check_evidence_resolvable）",
        "store": "阅读结果落库（store_reading_result / store_claim_relations）",
        "query": "查询（list_papers / get_paper / get_claims / get_media / get_media_detail / stats...）",
        "exporters": "导出 TSV（export_tsv）",
        "locate": "全文定位（get_locate_info / verify_locate / verify_all）",
        "conformance": "符合性检查（check_conformance）",
    }
    for name, desc in elements.items():
        # ★ 前缀必须与实际包名一致（曾硬编码为旧包名 tools.，Agent 照抄会 import 失败）
        print("  {}.{:<13s} {}".format(_PKG, name, desc))
    print("\n流水线层（pipelines，自动化工具流）：")
    flows = {
        "init_kb": "初始化知识库（建目录+建表）",
        "import_search_output": "PaperSearch 产物 → 知识库（完整导入）",
        "store_reading_results": "批量落库阅读结果（列表）",
        "store_reading_results_file": "从 JSON 文件批量落库",
        "rebuild_exports": "重新导出 TSV",
        "run_import_pipeline": "一键：初始化 + 导入 + 导出",
    }
    for name, desc in flows.items():
        print("  pipelines.{:<24s} {}".format(name, desc))
    print("\n详细用法见 API.md（docs/apidoc.py list 可列出所有条目及行号）")
    return 0


def main():
    ap = argparse.ArgumentParser(description="PaperOrganization 工具库 CLI")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("init", help="初始化知识库")
    p.add_argument("--kb")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("import", help="导入 PaperSearch 产物")
    p.add_argument("--search-out", required=True)
    p.add_argument("--kb")
    p.add_argument("--no-pdf", action="store_true", help="不复制 PDF")
    p.set_defaults(func=cmd_import)

    p = sub.add_parser("read", help="批量落库阅读结果")
    p.add_argument("--file", required=True, help="PaperRead 结果 JSON 文件")
    p.add_argument("--kb")
    p.set_defaults(func=cmd_read)

    p = sub.add_parser("export", help="重新导出 TSV")
    p.add_argument("--kb")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("stats", help="知识库统计")
    p.add_argument("--kb")
    p.set_defaults(func=cmd_stats)

    p = sub.add_parser("check", help="符合性检查")
    p.add_argument("--kb")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("locate", help="坐标定位验证")
    p.add_argument("--kb")
    p.add_argument("--media-id", help="只验证指定 media")
    p.set_defaults(func=cmd_locate)

    p = sub.add_parser("papers", help="列出论文")
    p.add_argument("--kb")
    p.add_argument("--limit", type=int)
    p.set_defaults(func=cmd_papers)

    p = sub.add_parser("elements", help="列出所有工具")
    p.set_defaults(func=cmd_elements)

    args = ap.parse_args()
    if not getattr(args, "func", None):
        ap.print_help()
        return 1
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
