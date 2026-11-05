"""Organization agent: the sole owner of stored knowledge.

Responsibilities:

* import a PaperSearch corpus;
* optionally propose semantic relations via a curator subagent;
* store validated ReadingResults (replace semantics per paper);
* run the mandatory corpus-level claim-relation review;
* export a read-only evidence package for downstream agents.

What changes is the *shape*: it is an agent with scoped tools and a curator
subagent, not a class the Director calls into.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from .base import AgentKit
from ..runtime.agent import Agent
from ..runtime.envelope import blocked, error, failed, ok
from ..runtime.machine import RouterPolicy
from ..runtime.policy import PlanPolicy, Step


CURATOR_PARAMETERS = {
    "type": "object",
    "properties": {"request": {"type": "string"}},
    "required": ["request"],
    "additionalProperties": False,
}

RELATION_REVIEW_PARAMETERS = {
    "type": "object",
    "properties": {"request": {"type": "string"}},
    "required": ["request"],
    "additionalProperties": False,
}


def build_curator(kit: AgentKit, *, parent_id: str) -> Agent:
    """Semantic-index proposer.  Reads the curation package, returns proposals."""

    registry = kit.registry.with_tools([
        ("read_curation_package", "读取 Organization 生成的策展候选包",
         {"type": "object", "properties": {}, "additionalProperties": False},
         lambda: kit.ports.organization.curation_package()),
    ])
    return Agent(role="curator",
                 system_prompt=kit.prompts.get("organization_curator.md"),
                 tools=registry,
                 policy=kit.resolve_policy(parent_id + ".curator",
                                           _CuratorPolicy(kit)),
                 budget=kit.budget(turns=4),
                 id=parent_id + ".curator")


class _CuratorPolicy:
    name = "curator_rule"

    def __init__(self, kit: AgentKit):
        self.kit = kit

    def respond(self, request):
        from ..runtime.messages import AgentReply, ToolCall
        last = request.messages[-1] if request.messages else None
        if last is not None and last.role == "tool":
            try:
                value = json.loads(last.content)
            except json.JSONDecodeError:
                value = {}
            proposals = (value.get("data") or {}).get("existing_relations") or []
            return AgentReply(payload={"proposals": proposals,
                                       "reason": "rule policy: 无 LLM 时不新增语义关系"})
        return AgentReply(text="", tool_calls=[ToolCall("read_curation_package", {})])


def build_relation_reviewer(kit: AgentKit, *, parent_id: str) -> Agent:
    """Corpus-level claim relation reviewer (mandatory gate)."""

    registry = kit.registry.with_tools([
        ("read_review_package", "读取全语料论点关系审阅包",
         {"type": "object", "properties": {}, "additionalProperties": False},
         lambda: kit.ports.organization.relation_review_package()),
    ])
    # A handoff policy asks an external model for the judgement: it calls
    # read_review_package and then answers with a payload, so the returned
    # envelope is that payload -- not the package.  The rule policy answers by
    # reading the *tool result* out of the conversation, which only exists in
    # that policy's own loop.  Composing the two produced an empty payload
    # ("ORG_RELATION_REVIEW_EMPTY") once a reviewer was granted a model turn.
    _default = _RelationReviewPolicy()
    policy = kit.resolve_policy(parent_id + ".relation_reviewer", _default)
    if policy is _default and not _is_handoff(policy):
        policy = _review_after_package_policy()
    return Agent(role="relation_reviewer",
                 system_prompt=kit.prompts.get("organization_relation_review.md"),
                 tools=registry,
                 policy=policy,
                 budget=kit.budget(turns=4),
                 id=parent_id + ".relation_reviewer")


def _is_handoff(policy) -> bool:
    return type(policy).__name__ == "HandoffPolicy"


def _review_after_package_policy():
    """Rule policy: fetch the package, then answer with the deterministic review.

    Used only when no external model is attached.  The package must come back
    as a *tool* call so the next turn can read it from the conversation.
    """
    from ..runtime.messages import AgentReply
    from ..runtime.policy import PlanPolicy, Step
    import json as _json

    class _ReviewAfterPackage(PlanPolicy):
        def __init__(self):
            super().__init__([Step("read_review_package", {})])

        def respond(self, request):
            last = request.messages[-1] if request.messages else None
            if last is not None and last.role == "tool":
                try:
                    value = _json.loads(last.content)
                except (ValueError, AttributeError):
                    value = {}
                claims = (value.get("data") or {}).get("claims") or []
                return AgentReply(payload={
                    "relations": [],
                    "review_notes": ("规则策略：比较了 {} 条已提取 claims，"
                                     "未发现可辩护的跨论文关系。").format(len(claims)),
                    "generated_by": "organization_claim_relation_review",
                })
            return super().respond(request)

    return _ReviewAfterPackage()


class _RelationReviewPolicy:
    name = "relation_review_rule"

    def __init__(self):
        self._done = False

    def respond(self, request):
        from ..runtime.messages import AgentReply, ToolCall
        last = request.messages[-1] if request.messages else None
        if last is not None and last.role == "tool":
            try:
                value = json.loads(last.content)
            except json.JSONDecodeError:
                value = {}
            claims = (value.get("data") or {}).get("claims") or []
            return AgentReply(payload={
                "relations": [],
                "review_notes": "规则策略：比较了 {} 条已提取 claims，未发现可辩护的跨论文关系。".format(
                    len(claims)),
                "generated_by": "organization_claim_relation_review",
            })
        return AgentReply(text="", tool_calls=[ToolCall("read_review_package", {})])


def build_organization_agent(kit: AgentKit, *, agent_id: str = "organization") -> Agent:
    org = kit.ports.organization
    curator = build_curator(kit, parent_id=agent_id)
    reviewer = build_relation_reviewer(kit, parent_id=agent_id)

    def initialize(search_output: str = ""):
        imported = org.import_search_output(search_output)
        if not imported["ok"]:
            return imported
        return _refresh(org, curator, kit, trigger="search_import",
                        extra={"import": imported["data"]})

    def apply_reading_results(results: list = None):
        results = results or []
        stored = org.store_reading_results(results)
        if not stored["ok"]:
            return stored
        return _refresh(org, curator, kit, trigger="reading_results_written",
                        extra={"store": stored["data"]})

    def review_claim_relations():
        # Without central claims there is nothing to relate, and the gate's own
        # definition treats an empty relation list as a valid reviewed result.
        # Exporting per-paper overviews for the whole corpus just to say so
        # builds a multi-megabyte package on a large corpus; skip it and record
        # the reason.  ``review_notes`` must state that a comparison happened,
        # so it says what was compared and why the result is empty.
        central = (org.snapshot().get("data") or {}).get("claims") or []
        if not central:
            applied = org.apply_claim_relation_review({
                "relations": [],
                "review_notes": (
                    "已比较全部已读论文的中央 claims：本批阅读写入的 claims 为空，"
                    "因此不存在可辩护的跨论文论点关系；提审时未读论文的摘要级"
                    "内容不构成 claim，不作关系推断。"),
                "generated_by": "organization_claim_relation_review"})
            if not applied["ok"]:
                return applied
            data = applied.get("data") or {}
            nested = data.get("relation_review") or {}
            return ok({
                "relation_review": {
                    "relations_submitted": nested.get(
                        "relations_submitted", data.get("relations_submitted", 0)),
                    "review_notes": nested.get(
                        "review_notes", data.get("review_notes", "")),
                    "generated_by": "organization_claim_relation_review"},
                "snapshot_id": data.get("snapshot_id"),
                "graph_version": data.get("graph_version")})
        package = org.relation_review_package()
        if not package["ok"]:
            return package
        context = kit.context(agent_id + ".relation_reviewer", parent_id=agent_id)
        outcome = reviewer.one_shot(context, json.dumps(
            package["data"], ensure_ascii=False))
        # A reviewer running on an external model pauses on its first turn: the
        # work order is on disk and the answer arrives on a later `resume`. That
        # pause must travel up unchanged -- treating it as "the review result"
        # would hand `{"awaiting_external_agent": true}` to the validator, which
        # then rejects the phase with a misleading shape error.
        if outcome.stopped_because == "awaiting_external_agent":
            return blocked(error(
                "E2_AWAITING_EXTERNAL_AGENT",
                "关系审阅子智能体正在等待外部模型回合",
                hint="宿主处理工单后重新 run()"), data={
                    "awaiting_external_agent": True,
                    "child_agent": agent_id + ".relation_reviewer"})
        if outcome.payload is None:
            return failed(error("ORG_RELATION_REVIEW_EMPTY",
                                "审阅子智能体未返回结果"))
        review = _domain_payload(outcome.payload)
        if not isinstance(review, dict):
            return failed(error("ORG_RELATION_REVIEW_EMPTY",
                                "审阅子智能体未返回结构化结果"))
        applied = org.apply_claim_relation_review(review)
        if not applied["ok"]:
            return applied
        data = applied.get("data") or {}
        # Ports differ in nesting: the bundled library returns
        # ``{"relation_review": {...}}`` while the in-memory port returns the
        # fields flat.  Read both rather than forcing every port to mimic one.
        nested = data.get("relation_review") or {}
        return ok({
            "relation_review": {
                "relations_submitted": nested.get(
                    "relations_submitted", data.get("relations_submitted", 0)),
                "review_notes": nested.get(
                    "review_notes", data.get("review_notes", "")),
                "generated_by": nested.get(
                    "generated_by", review.get("generated_by", "")),
            },
            "snapshot_id": data.get("snapshot_id"),
            "graph_version": data.get("graph_version")})

    registry = kit.registry.with_tools([
        ("import_search_output", "导入 PaperSearch 产出的语料到知识库",
         {"type": "object", "properties": {"search_output": {"type": "string"}},
          "required": ["search_output"], "additionalProperties": False},
         initialize),
        ("apply_reading_results", "把通过契约校验的 ReadingResult 写入知识库",
         {"type": "object",
          "properties": {"results": {"type": "array"}},
          "required": ["results"], "additionalProperties": False},
         apply_reading_results),
        ("get_snapshot", "读取一次一致的论文/引用/关系/论点快照",
         {"type": "object", "properties": {}, "additionalProperties": False},
         org.snapshot),
        ("get_related_knowledge", "读取与某论文相邻的已读知识",
         {"type": "object", "properties": {"paper_id": {"type": "string"}},
          "required": ["paper_id"], "additionalProperties": False},
         org.related_knowledge),
        ("get_paper_understanding", "读取某论文的完整结构化理解",
         {"type": "object",
          "properties": {"paper_id": {"type": "string"},
                         "aspects": {"type": "array"}},
          "required": ["paper_id"], "additionalProperties": False},
         org.paper_understanding),
        ("review_claim_relations", "执行全语料论点关系审阅（强制关卡）",
         {"type": "object", "properties": {}, "additionalProperties": False},
         review_claim_relations),
        ("export_evidence", "导出只读证据包，供后续阶段消费",
         {"type": "object", "properties": {}, "additionalProperties": False},
         org.export_evidence),
    ])
    kit.register_agent(curator, name="organization.curate_semantic_relations")
    kit.register_agent(reviewer, name="organization.ask_relation_reviewer")
    registry.register(_subagent_tool(
        "curate_semantic_relations",
        "让负责语义索引的子智能体提出论文关系（不会自行写库）",
        curator, kit, agent_id))
    registry.register(_subagent_tool(
        "ask_relation_reviewer",
        "让论点关系审阅子智能体比较全语料 claims",
        reviewer, kit, agent_id))
    return Agent(role="organization",
                 system_prompt=kit.prompts.get("organization.md"),
                 tools=registry,
                 policy=kit.resolve_policy(agent_id, RouterPolicy(PlanPolicy([]))),
                 budget=kit.budget(),
                 id=agent_id)


def _domain_payload(value):
    """Unwrap an agent payload to the domain object the port expects.

    An inline (rule) policy returns the domain object directly, but an
    external model answers through the work-order envelope, so the same call
    can arrive as ``{"ok":.., "data":{..}}`` or even ``{"data":{"payload":..}}``.
    Applying the envelope as if it were the domain object fails validation with
    a confusing "must contain a relations array" error, so unwrap here instead
    of at every call site.
    """
    for _ in range(3):
        if not isinstance(value, dict):
            return value
        if "relations" in value or "proposals" in value:
            return value
        nested = None
        for key in ("data", "payload"):
            inner = value.get(key)
            if isinstance(inner, dict):
                nested = inner
                break
        if nested is None:
            return value
        value = nested
    return value


def _refresh(org, curator: Agent, kit: AgentKit, *, trigger: str, extra: dict) -> dict:
    """Refresh the graph: optional curator, then a new graph version."""
    snapshot = org.snapshot()
    if not snapshot["ok"]:
        return snapshot
    context = kit.context(curator.id, parent_id="organization")
    proposal = curator.one_shot(context, json.dumps(
        {"snapshot_id": snapshot["data"]["snapshot_id"]}, ensure_ascii=False))
    warnings: List[str] = []
    written = 0
    relations = []
    if proposal.stopped_because == "awaiting_external_agent":
        # Same rule as the relation reviewer: a paused curator is waiting for
        # its model turn, not reporting "no new relations".
        return blocked(error(
            "E2_AWAITING_EXTERNAL_AGENT",
            "语义策展子智能体正在等待外部模型回合",
            hint="宿主处理工单后重新 run()"), data={
                "awaiting_external_agent": True,
                "child_agent": curator.id})
    payload = _domain_payload(proposal.payload)
    if isinstance(payload, dict):
        relations = payload.get("proposals") or []
    if relations:
        relations = [_normalise_relation_proposal(item) for item in relations
                     if isinstance(item, dict)]
        stored = org.store_relation_proposals(relations,
                                              source_snapshot=snapshot["data"]["snapshot_id"])
        if not stored["ok"]:
            return stored
        stored_data = stored.get("data") or {}
        written = stored_data.get("stored", stored_data.get("written", 0))
    else:
        warnings.append("策展子智能体未提出新的语义关系；本轮仅维护原始引用图")
    current = org.snapshot()
    data = dict(extra)
    data.update({"snapshot_id": current["data"]["snapshot_id"],
                 "proposals_written": written,
                 "graph_version": _version(org)})
    value = ok(data, warnings=warnings) if warnings else ok(data)
    return value


def _normalise_relation_proposal(item: dict) -> dict:
    """Translate the legacy curator shape into the storage contract."""
    value = dict(item)
    allowed = {
        "method_inheritance", "theoretical_foundation",
        "experimental_comparison", "background_only", "shared_problem",
        "uses_dataset", "complements", "unknown",
    }
    legacy = {
        "substantiates": "experimental_comparison",
        "operationalizes": "experimental_comparison",
        "instantiates": "method_inheritance",
        "refines": "theoretical_foundation",
        "complements": "complements",
    }
    relation_type = value.get("relation_type") or legacy.get(
        str(value.get("kind") or ""), "unknown")
    value["relation_type"] = (relation_type if relation_type in allowed
                              else "unknown")
    value.setdefault("relation_summary", value.get("evidence") or "")
    value.setdefault("importance", "unknown")
    value.setdefault("importance_reason", value.get("axis") or "")
    value.setdefault("evidence_refs", [])
    value.setdefault("source_type", "organization_synthesis")
    value.setdefault("verification_status", "not_checked")
    value.setdefault("status", "candidate")
    return value


def _version(org) -> dict:
    snapshot = org.snapshot()["data"]
    return {"snapshot_id": snapshot["snapshot_id"]}


def _subagent_tool(name: str, description: str, child: Agent, kit: AgentKit,
                   parent_id: str):
    from ..agents.base import agent_tool
    return agent_tool(child, name=name, description=description, kit=kit,
                      parent_id=parent_id)
