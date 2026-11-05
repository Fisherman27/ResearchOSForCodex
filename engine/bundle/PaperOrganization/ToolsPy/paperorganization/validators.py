"""校验元件（元件：validators）。

职责：校验 PaperRead 的阅读结果是否符合契约（PaperRead）。
**纯函数、不碰 IO**，便于单测和 Agent 单独调用做「预检」。

包含：
    validate_reading_result(result)  → ToolResult（错误列表在 data["errors"]）
    check_evidence_resolvable(...)   → 校验 evidence 指向的 span/media 是否存在

枚举常量集中在这里，供其他模块 import。
"""
from __future__ import annotations

import json
from typing import Any, Dict, List

from .errors import ErrorCode, ToolResult, fail, ok

# ---------------------------------------------------------------------------
# 枚举（对齐）
# ---------------------------------------------------------------------------

EVIDENCE_TYPES = ("theoretical", "empirical", "ablation", "cited")
POLARITIES = ("positive", "negative", "neutral")
REL_KINDS = ("support", "contradict", "extend", "use")
READING_DEPTHS = ("unread", "first_pass", "second_pass", "third_pass")


# ---------------------------------------------------------------------------
# 阅读结果校验
# ---------------------------------------------------------------------------

def validate_reading_result(result: Dict[str, Any]) -> ToolResult:
    """校验 PaperRead 返回的 result。

    返回 ToolResult：
        ok=True  → data={"errors": []}
        ok=False → code=E_INPUT_INVALID_VALUE，data={"errors":[{code,location,message,hint}]}
                   error 里是首条错误的摘要，完整列表在 data["errors"]。

    校验规则：
        1. paper_id / reading_depth / generated_by 必填
        2. 枚举合法性（evidence_type / polarity / kind / reading_depth）
        3. claim / key_result / assumption / claim_relation 必须有 evidence 字段
        4. reading_depth 与产出一致（second_pass 不许带 assumptions / unacknowledged）
    """
    errors: List[Dict[str, str]] = []

    def add(code, location, message, hint=""):
        errors.append({"code": code, "location": location,
                       "message": message, "hint": hint})

    if not isinstance(result, dict):
        return fail(ErrorCode.INVALID_TYPE, "result 必须是 dict",
                    location="validators.validate_reading_result",
                    hint="PaperRead 应返回一个 dict（见）")

    # 1. 必填
    for key in ("paper_id", "reading_depth", "generated_by"):
        if not result.get(key):
            add(ErrorCode.MISSING_FIELD, "result.{}".format(key),
                "缺少必填字段 '{}'".format(key),
                "PaperRead 输出必须包含 {}".format(key))

    # 2. reading_depth 枚举
    rd = result.get("reading_depth")
    if rd and rd not in READING_DEPTHS:
        add(ErrorCode.INVALID_ENUM, "result.reading_depth",
            "非法值 '{}'".format(rd),
            "应为 {}".format(list(READING_DEPTHS)))

    # 3. claims
    for i, c in enumerate(result.get("core_claims") or []):
        if "evidence" not in c:
            add(ErrorCode.MISSING_FIELD, "result.core_claims[{}].evidence".format(i),
                "缺少 evidence 字段",
                "每条 claim 必须带 evidence（span_id 或 media_id）；给不出就写 null")
        et = c.get("evidence_type")
        if et and et not in EVIDENCE_TYPES:
            add(ErrorCode.INVALID_ENUM, "result.core_claims[{}].evidence_type".format(i),
                "非法值 '{}'".format(et), "应为 {}".format(list(EVIDENCE_TYPES)))

    # 4. key_results
    for i, r in enumerate(result.get("key_results") or []):
        if "evidence" not in r:
            add(ErrorCode.MISSING_FIELD, "result.key_results[{}].evidence".format(i),
                "缺少 evidence 字段", "结果常以图表为证，evidence 应指向 media_id")
        et = r.get("evidence_type")
        if et and et not in EVIDENCE_TYPES:
            add(ErrorCode.INVALID_ENUM, "result.key_results[{}].evidence_type".format(i),
                "非法值 '{}'".format(et), "应为 {}".format(list(EVIDENCE_TYPES)))

    # 5. assumptions
    for i, a in enumerate(result.get("assumptions") or []):
        if "explicit" not in a:
            add(ErrorCode.MISSING_FIELD, "result.assumptions[{}].explicit".format(i),
                "缺少 explicit 字段",
                "explicit 必须区分 true(作者明说) / false(读者推断)")

    # 6. claim_relations
    from .ids import is_ref, is_claim_id
    for i, rel in enumerate(result.get("claim_relations") or []):
        kind = rel.get("kind")
        if kind and kind not in REL_KINDS:
            add(ErrorCode.INVALID_ENUM, "result.claim_relations[{}].kind".format(i),
                "非法值 '{}'".format(kind), "应为 {}".format(list(REL_KINDS)))
        pol = rel.get("polarity")
        if pol and pol not in POLARITIES:
            add(ErrorCode.INVALID_ENUM, "result.claim_relations[{}].polarity".format(i),
                "非法值 '{}'".format(pol), "应为 {}".format(list(POLARITIES)))
        if "evidence" not in rel:
            add(ErrorCode.MISSING_FIELD, "result.claim_relations[{}].evidence".format(i),
                "缺少 evidence 字段", "关系边必须带原文依据位置")
        # 端点写法校验（结构层面；能否解析成真实 claim_id 由 store 查库决定）
        for key in ("src_claim", "dst_claim"):
            v = rel.get(key)
            if not v:
                add(ErrorCode.MISSING_FIELD,
                    "result.claim_relations[{}].{}".format(i, key),
                    "端点为空",
                    "应为 '<paper_id>#<序号>'（跨论文）或 '#<本论文序号>'，"
                    "也接受已是 claim_id 的写法")
            elif not (is_ref(v) or is_claim_id(v)):
                add(ErrorCode.INVALID_VALUE,
                    "result.claim_relations[{}].{}".format(i, key),
                    "端点写法无法识别：{!r}".format(v),
                    "应为 '<paper_id>#<序号>'（如 arxiv:2609.11923#2）、"
                    "'#2'（本论文第2个主张），或 claim_id（c_...)")

    # 7. reading_depth 与产出一致性
    if rd in ("unread", "first_pass", "second_pass"):
        if result.get("assumptions"):
            add(ErrorCode.INCONSISTENT, "result.assumptions",
                "reading_depth={} 不应产出 assumptions".format(rd),
                "假设是 third_pass 的产物；若确实读了假设，请把 reading_depth 改为 third_pass")
        unack = (result.get("limitations") or {}).get("unacknowledged")
        if unack:
            add(ErrorCode.INCONSISTENT, "result.limitations.unacknowledged",
                "reading_depth={} 不应产出 unacknowledged limitations".format(rd),
                "未承认局限是 third_pass 的产物；确实有则改 reading_depth")

    # 8. 精读必须留下可审计的主张—证据检查，而不是只把覆盖级别写成 third_pass。
    if rd == "third_pass":
        if result.get("reading_basis") and result.get("reading_basis") not in (
                "fulltext", "fulltext_and_visuals"):
            add(ErrorCode.INCONSISTENT, "result.reading_basis",
                "third_pass 不得基于 title_abstract", "填写 fulltext 或 fulltext_and_visuals")
        appraisal = result.get("critical_appraisal")
        if isinstance(appraisal, dict):
            actual = {row.get("dimension") for row in
                      (appraisal.get("quality_assessments") or [])
                      if isinstance(row, dict)}
            expected = {"design", "measurement", "comparison", "statistics",
                        "robustness", "reproducibility", "external_validity"}
            missing = sorted(expected - actual)
            if missing:
                add(ErrorCode.MISSING_FIELD,
                    "result.critical_appraisal.quality_assessments",
                    "缺少质量维度：{}".format(missing),
                    "七个维度都必须给 verdict；无法判断时用 unknown/not_applicable")

    if errors:
        return fail(ErrorCode.INVALID_VALUE,
                    "阅读结果校验未通过（{} 项）".format(len(errors)),
                    location="validators.validate_reading_result",
                    hint="逐项修正 data['errors'] 里列出的问题后重试",
                    context={"errors": errors})
    return ok({"errors": []})


# ---------------------------------------------------------------------------
# 证据可解析性校验
# ---------------------------------------------------------------------------

def parse_evidence(evidence: Any) -> List[str]:
    """把 evidence 字段归一化成 ID 列表。

    支持：None / 字符串 / JSON 字符串数组 / Python list。
    """
    if evidence is None:
        return []
    if isinstance(evidence, list):
        return [str(x) for x in evidence if x]
    if isinstance(evidence, str):
        s = evidence.strip()
        if s.startswith("["):
            try:
                v = json.loads(s)
                if isinstance(v, list):
                    return [str(x) for x in v if x]
            except Exception:
                pass
        return [s] if s else []
    return [str(evidence)]


def check_evidence_resolvable(conn, evidence: Any) -> ToolResult:
    """检查 evidence 里的 ID 是否能在 span_map / media 表里找到。

    返回 data={"ids":[...], "resolved":[...], "dangling":[...]}
    有悬空 ID 时 ok=False（code=E_STATE_NOT_FOUND）。
    """
    ids = parse_evidence(evidence)
    if not ids:
        return ok({"ids": [], "resolved": [], "dangling": []})

    resolved, dangling = [], []
    for i in ids:
        hit = conn.execute("SELECT 1 FROM span_map WHERE span_id=?", (i,)).fetchone()
        if not hit:
            hit = conn.execute("SELECT 1 FROM media WHERE media_id=?", (i,)).fetchone()
        (resolved if hit else dangling).append(i)

    if dangling:
        return fail(ErrorCode.NOT_FOUND,
                    "evidence 指向的证据不存在：{}".format(dangling),
                    location="validators.check_evidence_resolvable",
                    hint="确认 ID 拼写；或该 span/media 尚未导入（先跑 import 流水线）",
                    context={"dangling": dangling, "resolved": resolved})
    return ok({"ids": ids, "resolved": resolved, "dangling": []})
