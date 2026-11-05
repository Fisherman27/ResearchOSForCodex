"""PaperRead 结果契约校验（机械校验，不是科学正确性证明）。

两层校验：
  1. **结构层**：按 schemas/*.json 做 JSON Schema 校验（无第三方依赖）。
  2. **契约层**：与 PaperOrganization store.py 的语义规则，
     这些是 JSON Schema 表达不了的：
       - evidence 必须显式存在，且不得为伪造 ID 形状之外的空值；
       - 有 evidence 指向**不等于** verification_status=supported（明令）；
       - reading_depth 与产出必须一致（first/second_pass 不得产出 assumptions）；
       - 局限/假设中"作者承认"与"读者推断"必须区分；
       - 无上下文/隔离模式下不得出现 span_id / media_id 锚点。

**重要**：本模块通过只说明"格式与契约合法"，**不**说明理解正确。
所有返回都带 `scientific_validity: "not_assessed"`，防止下游把机械校验当科学证明。
"""

import json
import os

from . import result as R, schema_check
from .errors import make_error

SCHEMA_DIR = os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "schemas"))

ENUMS = {
    "reading_depth": {"unread", "first_pass", "second_pass", "third_pass"},
    "evidence_type": {"theoretical", "empirical", "ablation", "cited"},
    "source_type": {"paper_assertion", "reader_inference", "cross_paper_synthesis"},
    "verification_status": {"not_checked", "supported", "partially_supported",
                            "contradicted", "inconclusive"},
    "claim_relations.kind": {"support", "contradict", "extend", "use"},
    "claim_relations.polarity": {"positive", "negative", "neutral"},
    "understanding_quality.status": {"not_assessed", "partial", "checked", "needs_revision"},
    "absent_reason": {"not_extracted", "not_found", "not_applicable", "unknown", "not_checked"},
    # 隔离实验中间格式
    "exp.source_type": {"author_claim", "derived_inference", "uncertain"},
}

SCHEMA_FOR_MODE = {
    "production": "reading_result.schema.json",
    "experiment": "reading_result_experiment.schema.json",
    "qa": "qa_answer.schema.json",
}

# evidence 锚点的合法形状
_SPAN_PREFIXES = ("span_", "span-")
_MEDIA_PREFIXES = ("fig", "figu", "tab", "table", "formula", "eq")


def _is_span_or_media(anchor: str) -> bool:
    if not isinstance(anchor, str):
        return False
    low = anchor.lower()
    if any(low.startswith(p) for p in _SPAN_PREFIXES):
        return True
    return any(low.startswith(p) for p in _MEDIA_PREFIXES)


def _check_evidence_field(entry, pointer, errors, strict_anchor=True):
    """检查一条条目的 evidence 字段。

    返回 (has_usable_evidence, anchors)。
    """
    if "evidence" not in entry:
        errors.append({"pointer": pointer + "/evidence",
                       "message": "缺少 evidence 字段（契约要求显式存在）"})
        return False, []

    ev = entry["evidence"]
    anchors = ev if isinstance(ev, list) else [ev]
    usable = [a for a in anchors if isinstance(a, str) and a.strip()]

    if not usable:
        # 允许 [null] / []，但必须由 verification_status 或 completeness 说明
        return False, anchors

    if strict_anchor:
        bad = [a for a in usable if not _is_span_or_media(a)]
        if bad:
            errors.append({"pointer": pointer + "/evidence",
                           "message": "evidence 锚点形状非法（应为 span_id 或 media_id）",
                           "actual": bad})
    return True, anchors


def validate_reading_result(data, mode="experiment", strict_anchor=None, schema_dir=None):
    """校验一份 ReadingResult。

    参数：
        data          : 解析后的 dict（或 JSON 字符串）
        mode          : 'production' | 'experiment'
        strict_anchor : evidence 是否必须是 span_id/media_id 形状。
                        None → 按 mode 自动（production=True，experiment=False）
        schema_dir    : Schema 目录覆盖

    返回信封，data = {"errors": [...], "warnings": [...], "schema": path}。
    结构错误与契约错误都进 errors；`diagnostics` 里给出可读摘要。
    """
    if isinstance(data, (str, bytes)):
        try:
            data = json.loads(data)
        except json.JSONDecodeError as exc:
            return R.fail(make_error(
                "E_JSON_PARSE", "输出不是合法 JSON：{}".format(exc),
                details=[{"line": exc.lineno, "column": exc.colno}]))

    if mode not in SCHEMA_FOR_MODE:
        return R.fail(make_error(
            "E_ARG", "未知 mode '{}'，应为 {}".format(mode, sorted(SCHEMA_FOR_MODE))))

    if strict_anchor is None:
        strict_anchor = (mode == "production")

    sdir = schema_dir or SCHEMA_DIR
    schema_path = os.path.join(sdir, SCHEMA_FOR_MODE[mode])
    schema, err = schema_check.load_schema(schema_path)
    if err:
        return R.fail(make_error("E_SCHEMA_INVALID", err))

    struct_errors, struct_warnings = schema_check.validate(data, schema)
    errors = list(struct_errors)
    warnings = list(struct_warnings)

    # ---------------- 契约层规则 ----------------
    if mode == "production":
        _contract_production(data, errors, warnings, strict_anchor)
    elif mode == "experiment":
        _contract_experiment(data, errors, warnings)

    diag = {
        "mode": mode,
        "schema": schema_path,
        "error_count": len(errors),
        "warning_count": len(warnings),
        "strict_anchor": strict_anchor,
        "scientific_validity": "not_assessed",
    }
    meta = {
        "note": "机械校验通过不代表理解正确；科学正确性需独立评估",
    }

    if errors:
        return R.fail(
            make_error("E_VALIDATION",
                       "结果不满足 {} Schema / 契约（{} 处）".format(mode, len(errors)),
                       details=errors),
            data={"errors": errors, "warnings": warnings, "schema": schema_path},
            diagnostics=diag, meta=meta)
    if warnings:
        return R.ok(data={"errors": [], "warnings": warnings, "schema": schema_path},
                    warnings=warnings, diagnostics=diag, meta=meta,
                    status=R.STATUS_PARTIAL)
    return R.ok(data={"errors": [], "warnings": [], "schema": schema_path},
                diagnostics=diag, meta=meta)


# ---------------------------------------------------------------------------
# production 契约
# ---------------------------------------------------------------------------

def _contract_production(data, errors, warnings, strict_anchor):
    rd = data.get("reading_depth")

    # 1. evidence 必须显式存在（对齐 store.py 的校验语义）
    for i, c in enumerate(data.get("core_claims") or []):
        usable, _ = _check_evidence_field(c, "/core_claims/{}".format(i), errors, strict_anchor)
        # 2. 有 evidence 不等于 supported（明令）
        if c.get("verification_status") == "supported" and not usable:
            errors.append({
                "pointer": "/core_claims/{}/verification_status".format(i),
                "message": "verification_status=supported 但没有可用 evidence 锚点",
            })
    for i, r in enumerate(data.get("key_results") or []):
        _check_evidence_field(r, "/key_results/{}".format(i), errors, strict_anchor)
    for i, a in enumerate(data.get("assumptions") or []):
        _check_evidence_field(a, "/assumptions/{}".format(i), errors, strict_anchor)
        # 隐含假设必须标 reader_inference
        if a.get("explicit") is False and a.get("source_type") == "paper_assertion":
            errors.append({
                "pointer": "/assumptions/{}/source_type".format(i),
                "message": "explicit=false（读者推断的隐含假设）不得标为 paper_assertion",
            })
    for i, rel in enumerate(data.get("claim_relations") or []):
        _check_evidence_field(rel, "/claim_relations/{}".format(i), errors, strict_anchor)
    for i, ev in enumerate(data.get("method_evolution") or []):
        if ev.get("source_type") == "paper_assertion":
            warnings.append(
                "method_evolution[{}] 标为 paper_assertion：跨篇演化通常至少包含读者综合，"
                "请确认作者原文确实直接陈述了该演化关系".format(i))

    # 3. reading_depth 与产出一致（对齐 store.py）
    if rd in ("unread", "first_pass", "second_pass"):
        if data.get("assumptions"):
            errors.append({
                "pointer": "/assumptions",
                "message": "reading_depth={} 不应产出 assumptions（只有 third_pass 读假设）".format(rd),
            })
        unack = (data.get("limitations") or {}).get("unacknowledged")
        if unack:
            errors.append({
                "pointer": "/limitations/unacknowledged",
                "message": "reading_depth={} 不应产出 unacknowledged limitations".format(rd),
            })
    if rd == "third_pass":
        if not data.get("reading_basis"):
            warnings.append(
                "旧版 third_pass 未声明 reading_basis；新阅读应填写 fulltext/fulltext_and_visuals")
        elif data.get("reading_basis") not in ("fulltext", "fulltext_and_visuals"):
            errors.append({"pointer": "/reading_basis",
                           "message": "third_pass 不得把 title_abstract 声称为全文精读"})
        appraisal = data.get("critical_appraisal")
        if not isinstance(appraisal, dict):
            warnings.append(
                "旧版 third_pass 缺少 critical_appraisal；新阅读应提交主张—证据审计")
        else:
            dimensions = {row.get("dimension") for row in
                          (appraisal.get("quality_assessments") or [])
                          if isinstance(row, dict)}
            expected = {"design", "measurement", "comparison", "statistics",
                        "robustness", "reproducibility", "external_validity"}
            missing = sorted(expected - dimensions)
            if missing:
                errors.append({
                    "pointer": "/critical_appraisal/quality_assessments",
                    "message": "精读质量审查缺少维度：{}".format(missing),
                })
        for i, quote in enumerate(data.get("evidence_quotes") or []):
            if quote.get("review_status") == "rejected" and not quote.get("review_reason"):
                errors.append({
                    "pointer": "/evidence_quotes/{}/review_reason".format(i),
                    "message": "被拒绝的逐页证据必须说明原因",
                })
    elif data.get("critical_appraisal"):
        warnings.append("非 third_pass 提交了 critical_appraisal；请确认阅读覆盖足以支持质量审计")

    # 4. 空字段必须有 completeness 解释
    absent = {e.get("field") for e in (data.get("completeness") or {}).get("absent_fields") or []}
    for field, value in (
        ("gist", data.get("gist")),
        ("research_problem", data.get("research_problem")),
    ):
        if (value is None or value == "") and field not in absent:
            warnings.append(
                "字段 '{}' 为空但 completeness.absent_fields 未说明原因".format(field))
    for field in ("contribution", "core_claims", "key_results", "assumptions"):
        if not data.get(field) and field not in absent:
            warnings.append(
                "字段 '{}' 为空数组但 completeness.absent_fields 未说明原因".format(field))

    # 5. 证据锚点数量与"有据可依"的一致性
    for i, c in enumerate(data.get("core_claims") or []):
        vs = c.get("verification_status")
        if vs in ("supported", "partially_supported") and not c.get("evidence"):
            warnings.append("core_claims[{}] 声称 {} 但 evidence 为空".format(i, vs))
        # 跨篇综合必须有跨篇结构支撑，否则来源类型是空口声明
        if c.get("source_type") == "cross_paper_synthesis":
            if not (data.get("method_evolution") or data.get("claim_relations")):
                warnings.append(
                    "core_claims[{}] 标为跨篇综合，但本结果没有跨篇结构可支撑该来源".format(i))


# ---------------------------------------------------------------------------
# experiment 契约（首轮隔离实验的最小中间格式）
# ---------------------------------------------------------------------------

def _contract_experiment(data, errors, warnings):
    """隔离实验格式的语义规则。

    这一层的关键职责是**防止把软定位伪装成强证据**，以及防止在无 Organization
    的环境里凭空造出 span_id / media_id。
    """
    claims = data.get("core_claims") or []
    for i, c in enumerate(claims):
        ptr = "/core_claims/{}".format(i)
        st = c.get("source_type")
        loc = c.get("paper_evidence_locator")

        # 隔离模式不应出现 span_id / media_id（本轮没有 Organization）
        if isinstance(loc, str) and _is_span_or_media(loc):
            errors.append({
                "pointer": ptr + "/paper_evidence_locator",
                "message": "隔离实验模式不得生成 span_id / media_id（本轮无 Organization 锚点）",
                "actual": loc,
            })

        # 推断类主张不应声称有精确原文定位
        if st == "derived_inference" and isinstance(loc, str) and loc.strip():
            warnings.append(
                "core_claims[{}] 标为 derived_inference 却给出了原文定位 '{}'："
                "请确认该定位确实支持推断，而非支撑被推断的作者陈述".format(i, loc))

        # uncertain 不该给出定位
        if st == "uncertain" and isinstance(loc, str) and loc.strip():
            warnings.append(
                "core_claims[{}] 标为 uncertain 但有定位：若定位可靠，来源类型不应是 uncertain".format(i))

    # 空字段应进 unresolved_questions 或明确说明（实验格式无 completeness 段）
    for field in ("contribution", "key_results", "assumptions", "limitations"):
        if not data.get(field):
            warnings.append(
                "字段 '{}' 为空；请确认这属于'确实未发现'还是'尚未核查'，"
                "并在 unresolved_questions 中体现".format(field))

    if not (data.get("method") or {}).get("mechanism"):
        warnings.append("method.mechanism 为空：这会让后续 Agent 无法重建方法机制")
