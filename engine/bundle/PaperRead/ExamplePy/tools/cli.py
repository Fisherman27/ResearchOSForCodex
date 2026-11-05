#!/usr/bin/env python3
"""PaperRead 工具库命令行入口（给人用）。

用法：
    python cli.py strategies                      列出所有策略及其状态
    python cli.py show S4                         打印某策略的正文（送入模型的部分）
    python cli.py messages S4                     打印组装好的纯消息数组
    python cli.py validate out.json --mode experiment
    python cli.py validate out.json --mode production
    python cli.py validate-qa qa.json
    python cli.py adapt exp.json --out prod.json  中间格式 → 生产格式
    python cli.py selfcheck                       跑离线契约自检
"""

import argparse
import json
import os
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from paperread import (  # noqa: E402
    list_strategies, load_strategy, build_read_messages,
    validate_reading_result, validate_qa_answer, experiment_to_production,
)


def _emit(env: dict, as_json: bool = True) -> int:
    """打印信封并按 ok 决定退出码。"""
    if as_json:
        print(json.dumps(env, ensure_ascii=False, indent=2))
    else:
        if env["ok"]:
            print("[{}] {}".format(env["status"], env["diagnostics"]))
        else:
            print("[{}] {}".format(env["status"], env["error"]["message"]))
    for w in env.get("warnings") or []:
        print("  warning: {}".format(w), file=sys.stderr)
    return 0 if env["ok"] else 1


def cmd_strategies(args):
    env = list_strategies(args.prompts_dir)
    if not env["ok"] or args.json:
        return _emit(env)
    for e in env["data"]:
        flag = "runnable" if e["runnable"] else "NOT-RUNNABLE"
        print("{:<4} {:<26} {:<10} {}".format(
            e["strategy_id"], e["name"] or "-", e["status"], flag))
        print("     {}".format(e["file"]))
    return 0


def cmd_show(args):
    env = load_strategy(args.strategy_id, args.prompts_dir)
    if not env["ok"]:
        return _emit(env)
    if args.json:
        return _emit(env)
    d = env["data"]
    print("# strategy_id : {}".format(d["strategy_id"]))
    print("# status      : {}".format(d["status"]))
    print("# sha256      : {}".format(d["body_sha256"]))
    print("# path        : {}".format(d["path"]))
    print("-" * 70)
    print(d["body"])
    return 0


def cmd_messages(args):
    env = build_read_messages(args.strategy_id, args.prompts_dir)
    if not env["ok"]:
        return _emit(env)
    if args.json:
        return _emit(env)
    for m in env["data"]["messages"]:
        print("=" * 70)
        print("[{}]".format(m["role"].upper()))
        print("=" * 70)
        print(m["content"])
    print("=" * 70)
    print("common_prompt_sha256   : {}".format(env["data"]["common_prompt_sha256"]))
    print("strategy_prompt_sha256 : {}".format(env["data"]["strategy_prompt_sha256"]))
    return 0


def _read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f), None


def cmd_validate(args):
    try:
        data, _ = _read_json(args.path)
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 1
    env = validate_reading_result(data, mode=args.mode, strict_anchor=args.strict_anchor)
    return _emit(env)


def cmd_validate_qa(args):
    try:
        data, _ = _read_json(args.path)
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 1
    return _emit(validate_qa_answer(data))


def cmd_adapt(args):
    try:
        data, _ = _read_json(args.path)
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 1
    env = experiment_to_production(
        data, generated_by=args.generated_by, reading_depth=args.reading_depth,
        generated_at=args.generated_at or "")
    if args.out and env["ok"]:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(env["data"], f, ensure_ascii=False, indent=2)
        print("wrote {}".format(args.out))
    return _emit(env) if not args.out else (0 if env["ok"] else 1)


def cmd_selfcheck(args):
    script = os.path.join(_HERE, "selfcheck.py")
    return subprocess.call([sys.executable, script])


def main(argv=None):
    p = argparse.ArgumentParser(prog="paperread", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--prompts-dir", default=None,
                   help="策略目录覆盖（默认 <repo>/prompts）")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("strategies", help="列出所有策略")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_strategies)

    s = sub.add_parser("show", help="打印某策略送入模型的正文")
    s.add_argument("strategy_id")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_show)

    s = sub.add_parser("messages", help="打印组装好的纯消息数组")
    s.add_argument("strategy_id", nargs="?", default=None)
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_messages)

    s = sub.add_parser("validate", help="校验 ReadingResult")
    s.add_argument("path")
    s.add_argument("--mode", default="experiment",
                   choices=["experiment", "production"])
    s.add_argument("--strict-anchor", dest="strict_anchor", action="store_true",
                   default=None, help="要求 evidence 必须是 span_id/media_id 形状")
    s.set_defaults(func=cmd_validate)

    s = sub.add_parser("validate-qa", help="校验 QA 回答")
    s.add_argument("path")
    s.set_defaults(func=cmd_validate_qa)

    s = sub.add_parser("adapt", help="中间格式 → 生产格式")
    s.add_argument("path")
    s.add_argument("--out", default=None)
    s.add_argument("--generated-by", dest="generated_by", default=None)
    s.add_argument("--generated-at", dest="generated_at", default=None)
    s.add_argument("--reading-depth", dest="reading_depth", default=None)
    s.set_defaults(func=cmd_adapt)

    s = sub.add_parser("selfcheck", help="跑离线契约自检")
    s.set_defaults(func=cmd_selfcheck)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
