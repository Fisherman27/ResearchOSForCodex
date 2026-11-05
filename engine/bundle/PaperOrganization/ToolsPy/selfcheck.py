#!/usr/bin/env python3
"""工具库自检（selfcheck）：验证所有元件与流水线可用。

用法：
    cd PaperOrganization/ToolsPy
    python selfcheck.py

覆盖：
    1. 模块导入
    2. 从零初始化 + 导入（真实 PaperSearch 产物）
    3. 查询
    4. 阅读结果落库（含校验失败路径）
    5. 关系边落库 + 幂等
    6. 导出
    7. 定位
    8. 符合性检查
    9. 错误机制（结构化错误码/location/hint）

退出码：0=全部通过，1=有失败。
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

_ENGINE = os.path.dirname(os.path.dirname(os.path.dirname(_HERE)))
_EXAMPLE = os.path.dirname(_ENGINE)
_SEARCH_OUT = os.path.join(_EXAMPLE, "tests", "fixtures", "search_output")

# ★ 自检使用**独立的临时目录**，绝不碰工作用的 ToolsPy/kb/
#   （曾经这里 rmtree 工作库，导致跑一次自检就清空真实工作成果）
_KB = tempfile.mkdtemp(prefix="po-selfcheck-")

passed = 0
failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print("  ✅ {}".format(name) + ("  " + str(detail) if detail else ""))
    else:
        failed += 1
        print("  ❌ {}".format(name) + ("  " + str(detail) if detail else ""))


print("== 1. 模块导入 ==")
try:
    from paperorganization import (conformance, errors, exporters, ids, importers,
                                   locate, paths, pipelines, query, schema, store,
                                   validators, writers)
    check("13 个模块导入", True)
except Exception as exc:
    check("13 个模块导入", False, str(exc))
    sys.exit(1)

from paperorganization.paths import resolve_kb  # noqa: E402

print("== 2. 初始化 + 导入（真实产物，使用临时库）==")
print("   临时库：{}".format(_KB))
cfg = resolve_kb(_KB).data
if os.path.isdir(_KB):
    shutil.rmtree(_KB)          # 只清临时库，验证可复现
check("初始化", pipelines.init_kb(cfg).ok)
if os.path.isfile(os.path.join(_SEARCH_OUT, "corpus.jsonl")):
    r = pipelines.import_search_output(_SEARCH_OUT, cfg, copy_pdf=True)
    check("导入成功", r.ok, str(r.data))
    check("导入论文数 > 0", r.data.get("papers", 0) > 0, "{} 篇".format(r.data.get("papers")))
    check("导入 spans > 0", r.data.get("spans", 0) > 0, "{} 个".format(r.data.get("spans")))
    check("导入 media > 0", r.data.get("media", 0) > 0, "{} 个".format(r.data.get("media")))
    check("保留内部引用边", "internal_citations" in r.data,
          "{} 条".format(r.data.get("internal_citations")))
else:
    check("找到 PaperSearch 产物", False, _SEARCH_OUT)

print("== 3. 查询 ==")
papers = query.list_papers(cfg)
check("list_papers", papers.ok and len(papers.data) > 0, "{} 篇".format(len(papers.data)))
st = query.stats(cfg)
check("stats", st.ok and "quality" in st.data)
# 空结果不是错误
empty = query.get_claims(cfg, "nonexistent:paper")
check("空结果 ok=True + warning", empty.ok and empty.warnings, empty.warnings[:1])
# 不存在 → NOT_FOUND
nf = query.get_paper(cfg, "nonexistent:paper")
check("不存在返回 NOT_FOUND", (not nf.ok) and nf.error.code == errors.ErrorCode.NOT_FOUND)

print("== 4. 阅读结果落库 ==")
# 找一个有 media 的论文，用真实 media_id 造结果
media = query.get_media(cfg, papers.data[0]["paper_id"])
target_pid = papers.data[0]["paper_id"]
for p in papers.data:
    m = query.get_media(cfg, p["paper_id"])
    if m.ok and m.data:
        media = m
        target_pid = p["paper_id"]
        break
fig = next((x for x in media.data if x["kind"] == "figure"), None) if media.ok else None

good = {
    "paper_id": target_pid, "reading_depth": "third_pass", "generated_by": "selfcheck",
    "gist": "g", "research_problem": "rp", "contribution": "c",
    "method": {"what": "w", "design_rationale": "d"},
    "core_claims": [{"text": "claim", "evidence": [fig["media_id"]] if fig else [],
                     "evidence_type": "empirical", "confidence": 0.9}],
    "key_results": [], "assumptions": [
        {"text": "a", "explicit": False, "evidence": None, "confidence": 0.5}],
    "limitations": {"acknowledged": ["x"], "unacknowledged": ["y"]},
    "method_family": "mf", "tags": ["t"], "claim_relations": [],
}
r = store.store_reading_result(good, cfg)
check("落库成功", r.ok, str(r.data))

# 校验失败路径
bad = dict(good, reading_depth="second_pass")   # second_pass 不该有 assumptions
rb = store.store_reading_result(bad, cfg)
check("校验失败被拒收", (not rb.ok) and rb.error.code == errors.ErrorCode.INVALID_VALUE)
check("错误有 location", bool(rb.error.location), rb.error.location)
check("错误有 hint", bool(rb.error.hint))
check("错误列表非空", bool(rb.error.context.get("errors")))

print("== 4b. 落库防护（实测发现问题的回归测试）==")
# (1) 不存在的论文 → 拒收（防孤儿数据）
ghost = dict(good, paper_id="arxiv:0000.00000")
rg = store.store_reading_result(ghost, cfg)
check("幽灵论文被拒收", (not rg.ok) and rg.error.code == errors.ErrorCode.NOT_FOUND,
      rg.error.code if not rg.ok else "竟然通过了")
check("拒收后有 hint", bool(rg.error and rg.error.hint))

# (2) 悬空 evidence → 拒收（防幻觉证据入库）
dang = dict(good, core_claims=[{"text": "x", "evidence": ["fig_not_exist_999"],
                                "evidence_type": "empirical", "confidence": 0.9}])
rd = store.store_reading_result(dang, cfg)
check("悬空 evidence 被拒收", (not rd.ok) and rd.error.code == errors.ErrorCode.NOT_FOUND,
      rd.error.code if not rd.ok else "竟然通过了")

# (3) 替换语义 → 旧记录不残留
many = dict(good, core_claims=[
    {"text": "c1", "evidence": [], "evidence_type": "cited", "confidence": 0.5},
    {"text": "c2", "evidence": [], "evidence_type": "cited", "confidence": 0.5}])
store.store_reading_result(many, cfg)
n_many = len(query.get_claims(cfg, target_pid).data)
one = dict(good, core_claims=[
    {"text": "only", "evidence": [], "evidence_type": "cited", "confidence": 0.5}])
rr = store.store_reading_result(one, cfg)
n_one = len(query.get_claims(cfg, target_pid).data)
check("替换语义（2→1 不残留）", n_many == 2 and n_one == 1,
      "{} -> {}；removed={}".format(n_many, n_one, rr.data.get("removed")))

print("== 4c. 跨论文关系边（方案A：<paper_id>#<序号>）==")
# 找另一篇论文做对端
other_pid = next(p["paper_id"] for p in query.list_papers(cfg).data
                 if p["paper_id"] != target_pid)
store.store_reading_result(dict(good, paper_id=other_pid, core_claims=[
    {"text": "other", "evidence": [], "evidence_type": "cited", "confidence": 0.5}]), cfg)

# (4) 在 reading_result 里声明关系边 → 必须被落库（曾经只校验不写入）
rl = dict(good, core_claims=[
    {"text": "self claim", "evidence": [], "evidence_type": "cited", "confidence": 0.5}],
    claim_relations=[{
        "src_claim": "#1",                       # 本论文第 1 个主张
        "dst_claim": "{}#1".format(other_pid),   # 对端论文第 1 个主张
        "kind": "extend", "polarity": "positive",
        "evidence": None, "confidence": 0.8}])
r_ok = store.store_reading_result(rl, cfg)
check("reading_result 内的关系边被落库", r_ok.ok and r_ok.data.get("relations") == 1,
      "relations={}".format(r_ok.data.get("relations") if r_ok.ok else r_ok.error.code))

# (5) 端点无法解析 → 严格模式拒收（不静默丢弃边）
r_bad = store.store_reading_result(dict(good, claim_relations=[{
    "src_claim": "#1", "dst_claim": "arxiv:9999.9#1",
    "kind": "use", "polarity": "neutral", "evidence": None, "confidence": 0.5}]), cfg)
check("无法解析的端点被拒收", (not r_bad.ok) and r_bad.error.code == errors.ErrorCode.NOT_FOUND,
      r_bad.error.code if not r_bad.ok else "竟然通过了")

# (6) 端点写法非法 → 结构校验拦下
r_syn = store.store_reading_result(dict(good, claim_relations=[{
    "src_claim": "#1", "dst_claim": "乱写",
    "kind": "use", "polarity": "neutral", "evidence": None, "confidence": 0.5}]), cfg)
check("非法端点写法被校验拦下", (not r_syn.ok) and
      r_syn.error.code == errors.ErrorCode.INVALID_VALUE,
      r_syn.error.code if not r_syn.ok else "竟然通过了")

# (7) 逐篇阅读：读后面的不毁掉前面的
n_before = len(query.get_claims(cfg, target_pid).data)
store.store_reading_result(dict(good, paper_id=other_pid, core_claims=[
    {"text": "again", "evidence": [], "evidence_type": "cited", "confidence": 0.5}]), cfg)
check("逐篇阅读互不干扰", len(query.get_claims(cfg, target_pid).data) == n_before,
      "{} -> {}".format(n_before, len(query.get_claims(cfg, target_pid).data)))

# (8) 自引用 "#n" 必须先写 claims 再解析（鸡生蛋顺序陷阱）
#     曾经的 bug：在写 claims 之前预检端点 → "#1" 必然解析失败 → 正常输入被拒
self_ref = dict(good, paper_id=other_pid, core_claims=[
    {"text": "new self", "evidence": [], "evidence_type": "cited", "confidence": 0.5}],
    claim_relations=[{
        "src_claim": "#1",                          # 指向本次刚写入的主张
        "dst_claim": "{}#1".format(target_pid),     # 跨论文
        "kind": "extend", "polarity": "positive",
        "evidence": None, "confidence": 0.8}])
r_self = store.store_reading_result(self_ref, cfg)
check("自引用 #1 + 跨论文端点可用", r_self.ok and r_self.data.get("relations") == 1,
      "relations={}".format(r_self.data.get("relations")) if r_self.ok
      else "{}: {}".format(r_self.error.code,
                           (r_self.error.context or {}).get("reasons", [""])[0][:50]))

print("== 5. 关系边 + 幂等 ==")
claims = query.get_claims(cfg, target_pid)
if claims.ok and len(claims.data) >= 1:
    rels = [{"src_claim": claims.data[0]["claim_id"],
             "dst_claim": claims.data[0]["claim_id"],
             "kind": "use", "polarity": "neutral",
             "evidence": fig["media_id"] if fig else None, "confidence": 0.5}]
    r1 = store.store_claim_relations(target_pid, rels, cfg, "selfcheck")
    n1 = len(query.get_claim_relations(cfg).data)
    store.store_claim_relations(target_pid, rels, cfg, "selfcheck")
    n2 = len(query.get_claim_relations(cfg).data)
    check("写关系边", r1.ok)
    check("关系边幂等", n1 == n2, "{} -> {}".format(n1, n2))
    # (4) 孤儿边 → 拒收
    orphan = [{"src_claim": "c_not_exist_1", "dst_claim": claims.data[0]["claim_id"],
               "kind": "use", "polarity": "neutral", "evidence": None, "confidence": 0.5}]
    ro = store.store_claim_relations(target_pid, orphan, cfg, "selfcheck")
    check("孤儿关系边被拒收", (not ro.ok) and ro.error.code == errors.ErrorCode.NOT_FOUND,
          ro.error.code if not ro.ok else "竟然通过了")
else:
    check("有 claims 可测关系边", False)

print("== 6. 导出 ==")
ex = exporters.export_tsv(cfg)
check("导出 TSV", ex.ok and ex.data["files"], ex.data.get("files") if ex.ok else "")
check("papers.tsv 存在", os.path.isfile(os.path.join(cfg.exports_dir, "papers.tsv")))

print("== 7. 定位 ==")
if fig:
    li = locate.get_locate_info(cfg, fig["media_id"])
    check("get_locate_info", li.ok, "page={}".format(li.data.get("page")) if li.ok else li.error.message)
    vl = locate.verify_locate(cfg, fig["media_id"])
    check("verify_locate", vl.ok, vl.data.get("evidence") if vl.ok else "")
    # 无坐标的 media → 明确报错
    allm = query.get_media(cfg, target_pid)
    nocoord = next((x for x in allm.data if not x.get("bbox")), None) if allm.ok else None
    if nocoord:
        ln = locate.get_locate_info(cfg, nocoord["media_id"])
        check("无坐标 media 明确报错", (not ln.ok) and ln.error.code in
              (errors.ErrorCode.EMPTY, errors.ErrorCode.NOT_FOUND))
else:
    check("有 figure 可测定位", False)

print("== 8. 符合性检查 ==")
cf = conformance.check_conformance(cfg)
check("check_conformance 可执行", cf.ok, str(cf.data["summary"]) if cf.ok else "有 FAIL")

print("== 9. 错误机制 ==")
from paperorganization.errors import ErrorCode, fail, fail_from_exception, partial  # noqa: E402
e = fail(ErrorCode.MISSING_FIELD, "msg", location="m.f", hint="h", context={"k": 1})
check("fail 结构完整", (not e.ok) and e.error.code and e.error.location
      and e.error.hint and e.error.context)
try:
    raise ValueError("boom")
except Exception as exc:
    ei = fail_from_exception(exc, location="m.f")
    check("内部异常带堆栈", bool(ei.error.detail and "Traceback" in ei.error.detail))
p = partial({"a": 1}, warnings=["w"])
check("partial 可用且带 data", p.ok and p.status == "partial" and p.data)

print()
print("=" * 50)
print("自检结果：{} 通过 / {} 失败".format(passed, failed))
print("=" * 50)

# 清理临时库（自检不留下任何产物）
try:
    shutil.rmtree(_KB, ignore_errors=True)
except Exception:
    pass

sys.exit(0 if failed == 0 else 1)
