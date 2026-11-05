"""结果适配：隔离实验中间格式 → 生产 ReadingResult。

首轮实验用最小中间格式（`exp-1.0`）跑，生产 Schema（`2.0-draft`）字段更多。
本模块做**可审计的确定性映射**，而不是让模型一次产出两种格式。

映射原则：
  - 中间格式里没有的信息**不编造**：新字段给空数组 / null，并在 `completeness`
    里注明 `not_extracted`，说明"是本轮没提取，不是论文没有"。
  - `paper_evidence_locator` 是**软定位**，映射到 `soft_locator`；
    `evidence` 保持 `[None]`，`verification_status` 一律 `not_checked`——
    软定位不等于强语义支持，绝不允许冒充 span_id / media_id。
  - `author_claim` → `source_type=paper_assertion`；
    `derived_inference` → `reader_inference`；`uncertain` → `reader_inference`
    且 `verification_status=inconclusive`。
"""

from . import result as R
from .errors import make_error

SCHEMA_VERSION = "2.0-draft"
_SOURCE_MAP = {
    "author_claim": "paper_assertion",
    "derived_inference": "reader_inference",
    "uncertain": "reader_inference",
}

# 中间格式没有、生产格式需要的字段
NOT_EXTRACTED_PRODUCTION_FIELDS = [
    "method.mechanism.components",
    "method.mechanism.key_equations",
    "method.mechanism.algorithm_steps",
    "method.mechanism.dependencies",
    "method.mechanism.differences_from_prior_work",
    "key_results.metric",
    "key_results.baseline",
    "key_results.scope",
    "understanding_quality.checks",
]


def experiment_to_production(exp: dict, generated_by=None, reading_task_id=None,
                             context_snapshot=None, reading_depth=None,
                             generated_at="") -> dict:
    """把中间格式 dict 映射为生产 ReadingResult dict。

    参数：
        exp              : 通过 experiment Schema 的 dict
        generated_by     : 生成者标识；默认在中间格式基础上加 "+exp-1.0" 溯源
        reading_task_id  : 隔离实验为 None
        context_snapshot : 隔离实验为 None
        reading_depth    : 未给定则按内容推断（有 assumptions → third_pass）
        generated_at     : 生成时间戳。本适配器**不自动取当前时间**，
                           以保证映射是确定性的、可复现的；未提供时会告警。

    返回信封；data 为生产格式 dict（**仍需再跑 production 校验**）。
    """
    if not isinstance(exp, dict):
        return R.fail(make_error("E_ARG", "exp 必须是 dict"))

    method = exp.get("method") or {}
    assumptions_raw = exp.get("assumptions") or []
    claims_raw = exp.get("core_claims") or []

    # 中间格式的 assumptions 是字符串数组，且无法区分显式/隐含。
    # 不能把所有假设都标 explicit=true（明令禁止）。
    assumptions = []
    for a in assumptions_raw:
        if isinstance(a, dict):
            assumptions.append(a)
        else:
            assumptions.append({
                "text": str(a),
                "explicit": False,          # 保守：中间格式未区分，按读者推断处理
                "evidence": [None],
                "confidence": None,
                "source_type": "reader_inference",
                "verification_status": "not_checked",
            })

    core_claims = []
    for c in claims_raw:
        st_raw = c.get("source_type")
        st = _SOURCE_MAP.get(st_raw, "reader_inference")
        vs = "inconclusive" if st_raw == "uncertain" else "not_checked"
        locator = c.get("paper_evidence_locator")
        locator = locator.strip() if isinstance(locator, str) and locator.strip() else None
        entry = {
            "text": c.get("claim", ""),
            # evidence 是 Organization 级锚点；隔离实验没有锚点 → [null]。
            # 软定位单独放 soft_locator，绝不允许冒充 span_id / media_id。
            "evidence": [None],
            "soft_locator": locator,
            "evidence_type": "empirical",   # 中间格式未标注证据性质 → 保守默认
            "confidence": None,
            "scope": {"conditions": [c["scope"]] if c.get("scope") else [],
                      "objects": [], "settings": []},
            "premises": [],
            "reasoning": c.get("support", ""),
            "source_type": st,
            "verification_status": vs,
            "verification_notes": (
                "来源为隔离实验中间格式；原文定位为 soft_locator（页码/引文/图表号），"
                "未经 Organization 的 span_id / media_id 锚定，也未做独立核验，"
                "因此 evidence 为 [null] 且状态保持 {}。".format(vs)),
        }
        core_claims.append(entry)

    # 实验格式允许 Reader 在读到后继论文时声明跨篇论点关系。生产契约的
    # evidence 只能放 Organization 的 span_id / media_id；实验格式仅有页节等
    # 软定位，因此保留关系、端点、类型与置信度，但把强证据锚点明确写成
    # [None]，绝不把软定位冒充为可解析证据。
    claim_relations = []
    for rel in (exp.get("claim_relations") or []):
        claim_relations.append({
            "src_claim": rel.get("src_claim", ""),
            "dst_claim": rel.get("dst_claim", ""),
            "kind": rel.get("kind", ""),
            "polarity": rel.get("polarity", "neutral"),
            "evidence": [None],
            "confidence": rel.get("confidence"),
        })

    if reading_depth is None:
        reading_depth = "third_pass" if assumptions else "second_pass"

    absences = [{"field": f, "reason": "not_extracted",
                 "notes": "本轮隔离实验的最小中间格式不含该字段；非论文本身缺失。"}
                for f in NOT_EXTRACTED_PRODUCTION_FIELDS]
    absences.append({
        "field": "method.mechanism.notes",
        "reason": "not_extracted" if not method.get("mechanism") else "unknown",
        "notes": ("中间格式的 mechanism 文本已迁移到该字段。"
                  if method.get("mechanism") else "模型未给出方法机制说明。"),
    })
    if not exp.get("key_results"):
        absences.append({"field": "key_results", "reason": "not_found",
                         "notes": "模型在本轮输出中未给出关键结果。"})
    if not exp.get("limitations"):
        absences.append({"field": "limitations", "reason": "not_found",
                         "notes": "模型在本轮输出中未给出局限。"})

    prod = {
        "schema_version": SCHEMA_VERSION,
        "paper_id": exp.get("paper_id", ""),
        "reading_depth": reading_depth,
        "generated_by": generated_by or "paperread-exp-adapter",
        "generated_at": generated_at,
        "reading_task_id": reading_task_id,
        "context_snapshot": context_snapshot,

        "gist": exp.get("gist", ""),
        "research_problem": exp.get("research_problem", ""),
        "contribution": list(exp.get("contribution") or []),

        "method": {
            "what": method.get("what", ""),
            "design_rationale": method.get("design_rationale", ""),
            "mechanism": {
                "notes": method.get("mechanism", ""),
                "inputs": [],
                "outputs": [],
                "components": [],
                "key_equations": [],
                "algorithm_steps": [],
                "dependencies": [],
                "differences_from_prior_work": [],
            },
        },
        "core_claims": core_claims,
        "key_results": [
            {"text": str(k), "evidence": [None], "evidence_type": "empirical",
             "metric": None, "dataset_or_setting": None, "baseline": None,
             "comparison": None,
             "scope": {"conditions": [], "objects": [], "settings": []},
             "verification_status": "not_checked", "soft_locator": None}
            for k in (exp.get("key_results") or [])
        ],
        "assumptions": assumptions,
        "limitations": {
            "acknowledged": [str(x) for x in (exp.get("limitations") or [])],
            "unacknowledged": [],
        },
        "method_family": "",
        "tags": [],
        "claim_relations": claim_relations,
        "method_evolution": [],
        "understanding_quality": {
            "status": "not_assessed",
            "checks": [],
            "unresolved_questions": list(exp.get("unresolved_questions") or []),
        },
        "completeness": {"absent_fields": absences},
    }

    warnings = [
        "适配结果把中间格式的软定位放进 soft_locator 而非 evidence："
        "**软定位不等于强语义支持**，evidence 保持 [null]，verification_status 统一为 not_checked。",
        "中间格式不区分假设的显式/隐含，全部按 explicit=false（读者推断）处理，"
        "以避免伪造作者明示。",
        "method.mechanism 的结构化子字段（components/key_equations/algorithm_steps）"
        "标记为 not_extracted，而非填造。",
    ]
    if claim_relations:
        warnings.append(
            "实验格式中的 claim_relations 已保留；其页节说明属于软定位，"
            "不能写入生产 evidence，故 evidence=[null]，关系仍需后续锚点核验。")
    if not generated_at:
        warnings.append("generated_at 为空：请由调用方写入真实运行时间，适配器不自动取当前时间。")
    return R.ok(data=prod, warnings=warnings,
                diagnostics={"schema_version": SCHEMA_VERSION,
                             "reading_depth": reading_depth,
                             "claims": len(core_claims),
                             "assumptions": len(assumptions)})
