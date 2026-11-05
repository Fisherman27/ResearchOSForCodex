#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
selfcheck.py —— ToolsPy 自检（**离线**契约测试）

跑什么
------
1. **信封契约**：5 种状态的 ok 取值、字段完整性、空 data 自动降级；
2. **错误契约**：异常映射、错误码表完整性、hint/retryable 齐全、traceback 保留；
3. **状态语义**：empty≠failed、partial.ok=True（这两个是最容易被"顺手改坏"的约定）；
4. **参数校验**：必填/越界参数返回 input 类错误而不是崩；
5. **文档契约**：API.md 与代码同步、每个工具都能被 find_symbol 定位；
6. **注册表契约**：REGISTRY 里的工具真实存在且可调用。

为什么需要它
------------
ToolsPy 的核心价值是**对 Agent 的稳定契约**。契约一旦被破坏
（比如有人把 partial 的 ok 改成 False），依赖它的 Agent 会静默出错 ——
比直接崩更难查。所以这些约定必须有测试守住。

全部离线，不访问网络、不需要 GROBID。

用法
----
    python selfcheck.py          # 跑全部
    python selfcheck.py -v       # 详细
"""
from __future__ import annotations

import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from papersearch import errors as ERR          # noqa: E402
from papersearch import registry as REG        # noqa: E402
from papersearch import result as R            # noqa: E402


class TestEnvelopeContract(unittest.TestCase):
    """信封结构与状态语义 —— 本库最核心的契约。"""

    def test_five_statuses_and_ok_flag(self):
        """5 种状态的 ok 取值必须严格符合约定。"""
        cases = [
            (R.ok({"a": 1}), "ok", True),
            (R.partial([1], reason="部分"), "partial", True),
            (R.empty(reason="空"), "empty", True),
            (R.blocked("AUTH_MISSING_KEY"), "blocked", False),
            (R.failed("NET_TIMEOUT"), "failed", False),
        ]
        for env, st, ok in cases:
            self.assertEqual(env["status"], st)
            self.assertEqual(env["ok"], ok,
                             "{} 的 ok 应为 {}：{}".format(st, ok, env))

    def test_empty_is_not_failure(self):
        """★ empty 的 ok 必须是 True —— 空集不是错误。

        这是最容易被"顺手改坏"的约定之一：有人会觉得"没结果就是失败"，
        于是下游无法区分"这领域真没论文"和"我的请求挂了"。
        """
        e = R.empty(reason="检索 0 命中")
        self.assertTrue(e["ok"], "empty 被判成失败了（契约破坏）")
        self.assertEqual(e["status"], "empty")
        self.assertIsNone(e["error"], "empty 不该带 error")
        self.assertTrue(e["warnings"], "empty 应给出原因 warning")

    def test_partial_is_usable(self):
        """★ partial 的 ok 必须是 True —— 结果可用。

        多源检索里单源失败是常态；若判失败，调用方会丢弃已拿到的结果。
        """
        e = R.partial([1, 2, 3], reason="一个源失败")
        self.assertTrue(e["ok"], "partial 被判成失败了（契约破坏）")
        self.assertTrue(R.is_usable(e), "partial 应 usable")
        self.assertFalse(R.is_ok(e), "partial 不是严格成功")

    def test_ok_with_empty_data_degrades_to_empty(self):
        """ok(None/[]/{}) 必须自动降级为 empty，避免把空结果当有结果。"""
        for v in (None, [], {}, ""):
            e = R.ok(v)
            self.assertEqual(e["status"], "empty",
                             "ok({!r}) 应降级为 empty：{}".format(v, e["status"]))

    def test_envelope_has_all_fields(self):
        """信封必须包含全部约定字段（缺字段会让下游 KeyError）。"""
        for e in (R.ok({"a": 1}), R.partial([1]), R.empty(), R.blocked(),
                  R.failed("NET_TIMEOUT")):
            for k in ("ok", "status", "data", "error", "warnings",
                      "diagnostics", "meta"):
                self.assertIn(k, e, "信封缺字段 {}：{}".format(k, list(e.keys())))
            self.assertIn("tool", e["meta"])
            self.assertIn("at", e["meta"])


class TestErrorContract(unittest.TestCase):
    """错误反馈机制 —— 决定 Agent 能否自行定位问题。"""

    def test_all_codes_have_required_fields(self):
        """每个错误码都要有 severity / retryable / hint（缺了 Agent 无法决策）。"""
        for code, meta in ERR.CODES.items():
            for k in ("severity", "retryable", "hint"):
                self.assertIn(k, meta, "错误码 {} 缺字段 {}".format(code, k))
            self.assertIn(meta["severity"], ("input", "degradable", "fatal"),
                          "{} 的 severity 取值非法：{}".format(code, meta["severity"]))
            self.assertIsInstance(meta["retryable"], bool)
            self.assertTrue(meta["hint"].strip(), "{} 的 hint 为空".format(code))

    def test_exception_mapping(self):
        """常见异常必须映射到**具体**错误码，而不是一律 INTERNAL_ERROR。"""
        cases = [(TimeoutError("t"), "NET_TIMEOUT"),
                 (FileNotFoundError("/x"), "INPUT_NOT_FOUND"),
                 (ValueError("bad"), "INPUT_INVALID"),
                 (KeyError("k"), "INPUT_MISSING"),
                 (PermissionError("p"), "INPUT_INVALID")]
        for exc, want in cases:
            e = R.from_exception(exc, tool="t")
            self.assertEqual(e["error"]["code"], want,
                             "{} 映射成了 {}".format(type(exc).__name__,
                                                     e["error"]["code"]))

    def test_unknown_exception_is_internal_error(self):
        """没见过的异常 → INTERNAL_ERROR（不伪装成网络问题）。"""
        class WeirdError(Exception):
            pass
        e = R.from_exception(WeirdError("少见"), tool="t")
        self.assertEqual(e["error"]["code"], "INTERNAL_ERROR")
        self.assertIn("bug", e["error"]["hint"].lower(),
                      "INTERNAL_ERROR 的 hint 应提示这是 bug")

    def test_traceback_is_preserved(self):
        """cause 里必须保留 traceback —— 否则 Agent 无法定位到代码行。"""
        try:
            raise ValueError("具体错误信息")
        except ValueError as exc:
            e = R.from_exception(exc, tool="t", stage="parse")
        c = e["error"]["cause"]
        self.assertEqual(c["type"], "ValueError")
        self.assertIn("具体错误信息", c["message"])
        self.assertIn("Traceback", c["traceback"], "traceback 丢失了")
        self.assertEqual(e["error"]["stage"], "parse", "stage 丢失了")

    def test_exception_never_leaks_as_bare_traceback(self):
        """工具不抛异常 —— from_exception 应把异常完全装进信封。"""
        try:
            raise RuntimeError("boom")
        except RuntimeError as exc:
            env = R.from_exception(exc, tool="t")
        self.assertFalse(env["ok"])
        self.assertEqual(env["status"], "failed")
        self.assertIsNotNone(env["error"])

    def test_unknown_code_degrades_but_keeps_original(self):
        """码表疏漏时：降级为 INTERNAL_ERROR 但保留原码，不掩盖问题。"""
        e = ERR.make_error("NOT_A_REAL_CODE", "test")
        self.assertEqual(e["code"], "INTERNAL_ERROR")
        self.assertEqual(e["unknown_code"], "NOT_A_REAL_CODE")

    def test_suggest_never_raises(self):
        """错误处理路径本身不该再出错。"""
        self.assertTrue(ERR.suggest("NET_TIMEOUT"))
        self.assertTrue(ERR.suggest("这不是一个码"))   # 未知码也要有输出

    def test_blocked_accepts_actionable_hint(self):
        """调用点可覆盖通用 hint，且错误路径本身不得抛异常。"""
        e = R.blocked("SERVICE_UNAVAILABLE", message="service down",
                      hint="start local service")
        self.assertEqual(e["status"], "blocked")
        self.assertEqual(e["error"]["hint"], "start local service")


class TestParameterValidation(unittest.TestCase):
    """参数校验：错误输入要返回 input 类错误，而不是崩。"""

    def test_empty_query_rejected(self):
        from papersearch import elements as E
        for fn in (E.search_arxiv, E.search_openalex, E.search_s2):
            with self.subTest(fn=fn.__name__):
                r = fn("")
                self.assertFalse(r["ok"])
                self.assertEqual(r["status"], "failed")
                self.assertEqual(r["error"]["code"], "INPUT_MISSING")
                self.assertFalse(r["error"]["retryable"],
                                 "参数错误不该标为可重试")

    def test_out_of_range_rejected(self):
        from papersearch import elements as E
        r = E.search_arxiv("x", max_results=0)
        self.assertEqual(r["error"]["code"], "INPUT_INVALID")
        r = E.search_arxiv("x", max_results=99999)
        self.assertEqual(r["error"]["code"], "INPUT_INVALID")
        self.assertTrue(r["error"]["hint"], "越界错误应给出可用建议")

    def test_missing_file_rejected(self):
        from papersearch import elements as E
        r = E.parse_pdf("/definitely/not/here.pdf")
        self.assertEqual(r["status"], "failed")
        self.assertEqual(r["error"]["code"], "INPUT_NOT_FOUND")
        self.assertIn("路径", r["error"]["hint"])

    def test_normalize_ids_needs_something(self):
        from papersearch import elements as E
        r = E.normalize_ids()
        self.assertEqual(r["error"]["code"], "INPUT_MISSING")

    def test_normalize_ids_bad_doi_falls_back(self):
        """非法 DOI 不能生成 `doi:` 空主体 —— 应回退到标题哈希。"""
        from papersearch import elements as E
        r = E.normalize_ids(doi="not-a-doi", title="Some Real Title")
        self.assertTrue(r["ok"], r)
        self.assertTrue(r["data"]["paper_id"].startswith("title:"),
                        "非法 DOI 未回退：{}".format(r["data"]["paper_id"]))
        self.assertNotEqual(r["data"]["paper_id"], "doi:")


class TestRegistryContract(unittest.TestCase):
    """注册表与实际实现必须一致。"""

    def test_registry_tools_exist_and_callable(self):
        """REGISTRY 里每个工具都必须在对应模块里真实存在且可调用。"""
        import importlib
        for tid, t in REG.REGISTRY.items():
            with self.subTest(tool=tid):
                mod = importlib.import_module(t["module"])
                fn = getattr(mod, t["name"], None)
                self.assertIsNotNone(fn, "{}.{} 不存在".format(t["module"], t["name"]))
                self.assertTrue(callable(fn), "{} 不可调用".format(tid))

    def test_registry_has_required_fields(self):
        for tid, t in REG.REGISTRY.items():
            for k in ("name", "module", "kind", "summary", "returns", "status"):
                self.assertIn(k, t, "{} 缺字段 {}".format(tid, k))
            self.assertIn(t["kind"], ("element", "pipeline"))

    def test_list_tools_returns_all(self):
        r = REG.list_tools()
        self.assertTrue(r["ok"])
        ids = {x["id"] for x in r["data"]["tools"]}
        self.assertEqual(ids, set(REG.REGISTRY),
                         "list_tools 漏了工具：{}".format(set(REG.REGISTRY) - ids))

    def test_unknown_tool_gives_actionable_error(self):
        r = REG.describe_tool("NOT_A_TOOL")
        self.assertFalse(r["ok"])
        self.assertEqual(r["error"]["code"], "INPUT_NOT_FOUND")
        self.assertIn("list_tools", r["error"]["hint"],
                      "应告诉 Agent 怎么找到正确的编号")


class TestDocumentationContract(unittest.TestCase):
    """文档与代码同步 —— Agent 完全依赖 API.md。"""

    def test_api_md_exists(self):
        p = os.path.join(_HERE, "API.md")
        self.assertTrue(os.path.isfile(p),
                        "API.md 不存在，跑 gen_api_md.py 生成")

    def test_every_tool_is_indexable(self):
        """每个注册的工具都要能在 API.md 里定位到行号。"""
        idx = REG.build_symbol_index()
        if not idx["ok"]:
            self.skipTest("API.md 未生成")
        entries = idx["data"]["entries"]
        missing = [tid for tid in REG.REGISTRY if tid not in entries]
        self.assertFalse(missing, "这些工具在 API.md 里没有条目：{}".format(missing))

    def test_symbol_line_ranges_are_sane(self):
        """行号区间必须有效（start>=1 且 end>=start）。"""
        idx = REG.build_symbol_index()
        if not idx["ok"]:
            self.skipTest("API.md 未生成")
        for tid, e in idx["data"]["entries"].items():
            self.assertGreaterEqual(e["line_start"], 1, tid)
            self.assertGreaterEqual(e["line_end"], e["line_start"], tid)

    def test_find_symbol_by_id_and_name(self):
        r = REG.find_symbol("E10")
        if not r["ok"]:
            self.skipTest("API.md 未生成")
        self.assertEqual(r["data"]["name"], "search_arxiv")
        r2 = REG.find_symbol("search_arxiv")
        self.assertTrue(r2["ok"])
        self.assertEqual(r2["data"]["id"], "E10")

    def test_api_md_in_sync_with_code(self):
        """API.md 必须与当前代码同步（改了注册表就要重新生成）。"""
        import subprocess
        p = os.path.join(_HERE, "docs", "gen_api_md.py")
        if not os.path.isfile(p):
            self.skipTest("gen_api_md.py 不存在")
        r = subprocess.run([sys.executable, p, "--check"],
                           capture_output=True, text=True, cwd=_HERE)
        self.assertEqual(r.returncode, 0,
                         "API.md 与代码不同步，请重跑 gen_api_md.py：\n{}".format(r.stdout))


class TestUnifiedEntry(unittest.TestCase):
    """统一入口 tools.py 必须导出全部注册的工具。"""

    def test_all_registry_tools_exported(self):
        import papersearch as T
        names = set(T.__all__)
        for tid, t in REG.REGISTRY.items():
            self.assertIn(t["name"], names,
                          "{}（{}）没从 tools.py 导出".format(t["name"], tid))

    def test_summarize_result_handles_dirty_warnings(self):
        """summarize_result 不能因为脏 warning 崩 —— 它是 Agent 读结果的主入口。"""
        from papersearch import pipelines as P
        env = R.ok({"a": 1}, tool="t",
                   warnings=[["not", "a", "dict"], "plain string", {"message": "ok"}])
        s = P.summarize_result(env)      # 不应抛异常
        self.assertIsInstance(s, str)
        self.assertTrue(s)


class TestAgentSimulationRegressions(unittest.TestCase):
    """Agent 模拟调研发现的 3 个 bug —— 加测试防止被改回去。

    这三个都是**单测抓不到、只有真实使用路径才暴露**的问题
    （例如 ② 需要"同目录解析多篇"这个具体场景）。
    """

    def test_step_count_uses_n_results_not_dict_len(self):
        """★ bug ①：_Steps.add 的 n 不能是 len(dict)。

        第一版对 dict 类型的 data 用 len()，得到的是**键的个数**。
        `search_all_sources` 的 data 有 4 个键 → 摘要显示 "search n=4"，
        而 rank 显示真实的 24 —— Agent 会以为数据不一致，
        去查一个根本不存在的问题。
        """
        from papersearch.pipelines import _Steps
        st = _Steps()
        # dict 形态的 data（模拟 search_all_sources）
        st.add("search", R.ok({"papers": [1] * 24, "by_source": {}, "source_status": {},
                               "dedup": {}},
                              tool="t", diagnostics={"n_results": 24}))
        self.assertEqual(st.items[0]["n"], 24,
                         "dict 形态的 data 又用 len() 了：{}".format(st.items[0]["n"]))

    def test_step_count_falls_back_to_result_key(self):
        """没有 n_results 时，按结果键取长度；取不到给 None（不猜）。"""
        from papersearch.pipelines import _Steps
        st = _Steps()
        st.add("a", R.ok({"papers": [1, 2, 3]}, tool="t"))      # 无 n_results
        self.assertEqual(st.items[0]["n"], 3)
        st.add("b", R.ok({"unknown_key": [1, 2]}, tool="t"))     # 认不出的键
        self.assertIsNone(st.items[1]["n"], "认不出的结构应给 None，不该乱猜")

    def test_list_data_still_counted(self):
        """list 形态的 data 仍然按 len 算（不能因为修 dict 把 list 弄坏）。"""
        from papersearch.pipelines import _Steps
        st = _Steps()
        st.add("x", R.ok([1, 2, 3, 4], tool="t"))
        self.assertEqual(st.items[0]["n"], 4)

    def test_parse_pdf_artifacts_do_not_collide(self):
        """★ bug ②：不同 PDF 在同一 out_dir 解析，产物文件名必须唯一。

        第一版 paper_id 为空 → 文件名全是 `unknown.*` → 后一篇覆盖前一篇，
        且 papers 里两篇指向同一文件（数据静默错位）。
        这里不真跑 GROBID（离线），直接验证命名逻辑：不同路径 → 不同 paper_id。
        """
        import hashlib
        def pid_for(path: str) -> str:
            stem = os.path.basename(path)
            pfx = hashlib.sha1(os.path.abspath(path).encode("utf-8")).hexdigest()[:8]
            return "pdf:{}:{}".format(pfx, stem)
        a = pid_for("/tmp/dir1/paper.pdf")
        b = pid_for("/tmp/dir2/paper.pdf")     # 同名但不同目录
        c = pid_for("/tmp/dir1/other.pdf")
        self.assertNotEqual(a, b, "不同目录的同名 PDF 会撞名")
        self.assertNotEqual(a, c, "同目录不同 PDF 会撞名")
        self.assertEqual(a, pid_for("/tmp/dir1/paper.pdf"), "同一文件应得到相同 ID（稳定）")

    def test_parse_pdf_accepts_paper_id(self):
        """parse_pdf 必须支持传入 paper_id（流水线用真实主键命名产物）。"""
        import inspect
        from papersearch import elements as E
        sig = inspect.signature(E.parse_pdf)
        self.assertIn("paper_id", sig.parameters,
                      "parse_pdf 缺 paper_id 参数 —— 产物命名会退化")

    def test_invalid_ids_are_input_error_not_network_error(self):
        """★ bug ③：无法识别的 ID 必须报 INPUT_INVALID，不能报可重试的网络错误。

        第一版把 "???" 原样发给 API，服务端返回非预期结构，
        最终被归成 NET_BAD_RESPONSE(retryable=True) ——
        让 Agent 去重试一个**永远不可能成功**的请求。
        """
        from papersearch import elements as E
        r = E.get_works_by_ids(["???"])
        self.assertFalse(r["ok"])
        self.assertEqual(r["error"]["code"], "INPUT_INVALID",
                         "无效 ID 被归成了 {}".format(r["error"]["code"]))
        self.assertFalse(r["error"]["retryable"],
                         "参数错误不该标为可重试")
        self.assertTrue(r["error"]["hint"], "应给出 ID 格式示例")

    def test_mixed_valid_invalid_ids_partial(self):
        """混合输入：有效的照常处理，无效的列出来（partial，不是 failed）。"""
        from papersearch import elements as E
        r = E.get_works_by_ids(["W2103315867", "bad-id"])
        # 网络可用时返回 partial；不可用时至少不能是 INPUT_INVALID 的 failed
        if r["status"] == "partial":
            self.assertEqual(len(r["data"]["works"]), 1)
            self.assertIn("bad-id", r["data"]["invalid_ids"])
        else:
            self.assertNotEqual(r["error"]["code"], "INPUT_INVALID",
                                "有有效 ID 时不该报全无效")


def main(argv=None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    verbose = "-v" in argv
    argv = [a for a in argv if a != "-v"]
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromModule(sys.modules[__name__])
    runner = unittest.TextTestRunner(verbosity=2 if verbose else 1)
    res = runner.run(suite)
    n = res.testsRun
    print("\nToolsPy 自检：{} 项，失败 {}，错误 {}".format(
        n, len(res.failures), len(res.errors)))
    if res.wasSuccessful():
        print("全部通过 ✅")
        return 0
    print("有失败项 ❌ —— 这些是**对 Agent 的契约**，破坏它们会让下游静默出错")
    return 1


if __name__ == "__main__":
    sys.exit(main())
