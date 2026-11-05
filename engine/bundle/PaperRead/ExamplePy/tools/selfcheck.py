#!/usr/bin/env python3
"""PaperRead Skill 自检（离线契约测试，必须能一键跑通）。

跑法：
    python selfcheck.py            # 完整自检（含 unittest）
    python selfcheck.py --quick    # 只做结构性检查，不跑完整测试套

设计依据：`ToolsPyRules.md`（selfcheck 必须离线可跑、一键通过）。
本自检**不接触网络、不接触 PDF、不调用模型**。
"""

import argparse
import json
import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)                      # .../PaperRead/ExamplePy
sys.path.insert(0, _HERE)

from paperread import (  # noqa: E402
    load_strategy, build_read_messages, load_common_contract,
    validate_reading_result, validate_qa_answer, experiment_to_production,
    CORE_STRATEGY_IDS, schema_check, strategy_loader,
)

PASS, FAIL, WARN = "PASS", "FAIL", "WARN"
_results = []


def check(name, condition, detail=""):
    status = PASS if condition else FAIL
    _results.append((status, name, detail))
    return condition


def warn(name, detail=""):
    _results.append((WARN, name, detail))


def run_structural_checks():
    print("=" * 72)
    print("PaperRead Skill 自检 —— 结构性检查")
    print("=" * 72)

    # 1. 目录与关键文件存在
    expected_files = [
        "SKILL.md",
        "prompts/common_read.md", "prompts/common_qa.md",
        "schemas/reading_result.schema.json",
        "schemas/reading_result_experiment.schema.json",
        "schemas/qa_answer.schema.json",
        "tools/cli.py", "tools/selfcheck.py", "tools/paperread/__init__.py",
    ]
    for rel in expected_files:
        path = os.path.join(_ROOT, rel)
        check("存在 {}".format(rel), os.path.isfile(path))

    # 2. 策略文件齐全
    for sid in CORE_STRATEGY_IDS:
        path = strategy_loader.resolve_strategy_path(sid)
        check("策略 {} 文件存在".format(sid), path is not None)

    # 3. S9/S10 存在但不可运行（状态必须显式标注）
    for sid, expect_status in (("S9", "excluded"), ("S10", "future")):
        env = load_strategy(sid, allow_non_runnable=True)
        if check("{} 可读取正文（仅检查用）".format(sid), env["ok"]):
            check("{} status == {}".format(sid, expect_status),
                  env["data"]["status"] == expect_status,
                  "实际 {}".format(env["data"]["status"]))
        blocked = load_strategy(sid)
        check("{} 默认拒绝加载".format(sid), not blocked["ok"])

    # 4. 每个可运行策略都能加载且正文非空
    for sid in CORE_STRATEGY_IDS:
        env = load_strategy(sid)
        check("策略 {} 可加载".format(sid), env["ok"])
        if env["ok"]:
            check("策略 {} 正文非空".format(sid), bool(env["data"]["body"].strip()))
            check("策略 {} 正文不含 frontmatter".format(sid),
                  not env["data"]["body"].startswith("---"))

    # 5. 公共契约一致性与哈希
    common = load_common_contract("common_read")
    check("common_read 可加载", common["ok"])
    hashes = set()
    for sid in CORE_STRATEGY_IDS:
        env = build_read_messages(sid)
        if env["ok"]:
            hashes.add(env["data"]["common_prompt_sha256"])
    check("所有策略共享同一公共契约", len(hashes) == 1,
          "得到 {} 个不同哈希".format(len(hashes)))

    # 6. 消息组装只含 system + user 两条，且无 pdf
    env = build_read_messages("S4")
    if check("消息组装成功", env["ok"]):
        msgs = env["data"]["messages"]
        check("消息条数为 2", len(msgs) == 2, "实际 {}".format(len(msgs)))
        blob = json.dumps(msgs, ensure_ascii=False).lower()
        check("消息中不含 pdf", ".pdf" not in blob)

    # 7. 未知策略必须报错（不得静默降级）
    bad = load_strategy("S_TYPO")
    check("未知策略报错而非降级", not bad["ok"])
    if not bad["ok"]:
        check("未知策略错误码正确",
              bad["error"]["code"] == "E_STRATEGY_UNKNOWN",
              bad["error"]["code"])

    # 8. Schema 全部可解析且无未支持关键字
    sdir = os.path.join(_ROOT, "schemas")
    for fname in sorted(os.listdir(sdir)):
        if not fname.endswith(".json"):
            continue
        schema, err = schema_check.load_schema(os.path.join(sdir, fname))
        check("Schema {} 可解析".format(fname), err is None, err or "")

    # 9. fixtures 通过各自模式的校验
    fx = os.path.join(_ROOT, "tests", "fixtures")
    exp_path = os.path.join(fx, "valid_experiment_result.json")
    prod_path = os.path.join(fx, "valid_production_result.json")
    with open(exp_path, encoding="utf-8") as f:
        exp = json.load(f)
    with open(prod_path, encoding="utf-8") as f:
        prod = json.load(f)

    env = validate_reading_result(exp, mode="experiment")
    check("合法实验 fixture 通过校验", env["ok"], _err_tail(env))
    env = validate_reading_result(prod, mode="production")
    check("合法生产 fixture 通过校验", env["ok"], _err_tail(env))

    # 10. 适配器：中间 → 生产，再通过生产校验
    env = experiment_to_production(exp, generated_by="selfcheck",
                                   generated_at="2026-01-01T00:00:00Z")
    if check("适配器执行成功", env["ok"], _err_tail(env)):
        recheck = validate_reading_result(env["data"], mode="production")
        check("适配产物通过生产校验", recheck["ok"], _err_tail(recheck))
        check("适配产物不伪造 supported",
              all(c["verification_status"] != "supported"
                  for c in env["data"]["core_claims"]))
        check("适配产物把软定位留在 soft_locator",
              all(c["evidence"] == [None] for c in env["data"]["core_claims"]))

    # 11. 负例必须被拒（契约真的在起作用）
    neg = json.loads(json.dumps(prod))
    neg["core_claims"][0]["evidence"] = ["page 4"]
    check("伪造 evidence 锚点被拒", not validate_reading_result(neg, mode="production")["ok"])

    neg2 = json.loads(json.dumps(exp))
    neg2["core_claims"][0]["paper_evidence_locator"] = "span_1"
    check("隔离模式伪造 span_id 被拒",
          not validate_reading_result(neg2, mode="experiment")["ok"])

    neg3 = json.loads(json.dumps(prod))
    neg3["reading_depth"] = "second_pass"
    check("second_pass 携带 assumptions 被拒",
          not validate_reading_result(neg3, mode="production")["ok"])

    # 12. QA Schema
    env = validate_qa_answer({"answer": "a", "answerable_from_paper": True,
                              "support_locator": "p.3"})
    check("合法 QA 结果通过校验", env["ok"])
    env = validate_qa_answer({"answer": "a"})
    check("缺字段的 QA 结果被拒", not env["ok"])

    # 13. schema_version 与契约版本对齐
    check("生产 Schema 版本为 2.0-draft",
          json.load(open(prod_path, encoding="utf-8"))["schema_version"] == "2.0-draft")


def _err_tail(env):
    if env.get("ok"):
        return ""
    err = env.get("error") or {}
    details = err.get("details") or []
    if details:
        return "{}: {}".format(details[0].get("pointer"), details[0].get("message"))
    return err.get("message", "")


def run_unittest_suite():
    print()
    print("=" * 72)
    print("PaperRead Skill 自检 —— 契约测试套（unittest）")
    print("=" * 72)
    tests_dir = os.path.join(_ROOT, "tests")
    suite = unittest.defaultTestLoader.discover(tests_dir, pattern="test_*.py",
                                                top_level_dir=tests_dir)
    runner = unittest.TextTestRunner(verbosity=1)
    result = runner.run(suite)
    total = result.testsRun
    failed = len(result.failures) + len(result.errors)
    check("unittest 契约测试全部通过（{} 项）".format(total), failed == 0,
          "{} 项失败".format(failed) if failed else "")
    return total, failed


def print_report():
    print()
    print("=" * 72)
    print("自检报告")
    print("=" * 72)
    for status, name, detail in _results:
        line = "[{}] {}".format(status, name)
        if detail:
            line += "  → {}".format(detail)
        print(line)

    n_pass = sum(1 for s, _, _ in _results if s == PASS)
    n_fail = sum(1 for s, _, _ in _results if s == FAIL)
    n_warn = sum(1 for s, _, _ in _results if s == WARN)
    print("-" * 72)
    print("{} PASS / {} FAIL / {} WARN".format(n_pass, n_fail, n_warn))
    print()
    print("注意：本自检只验证**结构、契约与隔离性**。")
    print("      Schema 合法、字段齐全、evidence 形状正确，都不构成'理解正确'的证明。")
    print("      科学正确性必须由独立评估回答（见 ../）。")
    return 0 if n_fail == 0 else 1


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--quick", action="store_true", help="跳过 unittest 套件")
    args = p.parse_args(argv)

    run_structural_checks()
    if not args.quick:
        run_unittest_suite()
    return print_report()


if __name__ == "__main__":
    sys.exit(main())
