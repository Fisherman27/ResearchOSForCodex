"""Research ideation: the third capability inside the Scheduler.

Positioning
-----------
``PaperScheduler`` is the coordinating agent.  Internally it has three
capabilities that can be replaced independently:

    ReadingPlanner    decide what to read next      (agents/scheduler.py)
    ContextSelector   decide what context to supply (agents/scheduler.py)
    ResearchIdeation  synthesise, find opportunities, propose directions
                      (this module)

Keeping them in separate modules is what makes ablation possible: a run with
``ideation_strategy = I0`` and a run with ``I1`` differ by exactly one prompt,
because the assembly, validation and filtering below are shared.

Two principles encoded here, both from the design discussion:

1. **The Scheduler argues for a direction; it does not certify it.**  Every
   proposal carries ``unverified`` items, and novelty is never reported as
   established.  A closed corpus cannot prove novelty.
2. **Ideas must be traceable to the corpus.**  ``evidence_refs`` are checked
   against the actual paper and claim ids; a proposal that cites nothing real
   is rejected rather than quietly accepted.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from ..runtime.envelope import error, failed, ok, partial
from .ideation_strategies import build_ideation_messages


#: Fields a proposal must carry to be considered well-formed.
PROPOSAL_FIELDS = ("proposal_id", "title", "research_question", "gap",
                   "from_opportunity", "core_hypothesis", "proposed_mechanism",
                   "validation_plan", "evidence_refs")

OPPORTUNITY_KINDS = ("limitation_aggregation", "contradiction",
                     "complementarity", "assumption_relaxation",
                     "unexplored_intersection", "transfer",
                     "problem_reframing", "other")

#: Dimensions scored by the deterministic filter.  Novelty is deliberately
#: absent: it cannot be established from a closed corpus.
FILTER_DIMENSIONS = ("evidence_grounding", "specificity", "mechanism_clarity",
                     "falsifiability")


def build_landscape(evidence: dict) -> dict:
    """Project the evidence package into a compact, problem-centric payload.

    This is deterministic on purpose.  Feeding raw ``ReadingResult`` objects to
    the model wastes context and makes it impossible to say what the model was
    actually shown.  The projection keeps what ideation needs and drops the
    rest:

    * per paper: problem, contribution, method family, declared limitations,
      explicit assumptions, unresolved questions;
    * the claim graph: which claims contradict, extend or support each other;
    * aggregate counts so the model can see whether a theme is common or
      incidental.
    """
    papers = evidence.get("papers") or []
    claims = evidence.get("claims") or []
    claim_relations = evidence.get("claim_relations") or []
    claim_index = {claim.get("claim_id"): claim for claim in claims
                   if claim.get("claim_id")}
    paper_ids = {item.get("overview", {}).get("paper_id")
                 for item in papers}
    paper_ids.discard(None)

    rows = []
    for item in papers:
        overview = item.get("overview") or {}
        understanding = item.get("understanding") or {}
        paper_id = overview.get("paper_id")
        limitations = understanding.get("limitations") or {}
        method = understanding.get("method") or {}
        mechanism = method.get("mechanism") or {}
        rows.append({
            "paper_id": paper_id,
            "title": overview.get("title"),
            "year": overview.get("year"),
            "reading_depth": overview.get("reading_depth"),
            "research_problem": understanding.get("research_problem")
            or overview.get("research_problem"),
            "contribution": understanding.get("contribution")
            or overview.get("contribution"),
            "method_family": understanding.get("method_family")
            or overview.get("method_family"),
            "method": {
                "what": method.get("what"),
                "design_rationale": method.get("design_rationale"),
                # Mechanism detail is what assumption-relaxation and
                # complementarity reasoning actually operate on: you cannot ask
                # "what if we remove component X" without knowing the parts.
                "inputs": mechanism.get("inputs") or [],
                "outputs": mechanism.get("outputs") or [],
                "components": _components(mechanism.get("components")),
                "dependencies": _texts(mechanism.get("dependencies")),
                "differences_from_prior_work": _texts(
                    mechanism.get("differences_from_prior_work")),
            },
            "method_what": method.get("what"),
            "acknowledged_limitations": _texts(
                limitations.get("acknowledged")),
            "unacknowledged_limitations": _texts(
                limitations.get("unacknowledged")),
            "assumptions": _assumptions(understanding.get("assumptions")),
            "claims": [{"claim_id": claim.get("claim_id"),
                        "text": claim.get("text"),
                        "scope": claim.get("scope"),
                        "premises": claim.get("premises") or []}
                       for claim in claims
                       if claim.get("paper_id") == paper_id],
            "unresolved_questions": ((understanding.get("understanding_quality")
                                      or {}).get("unresolved_questions") or []),
            "screening": understanding.get("screening") or {},
            "critical_appraisal": understanding.get("critical_appraisal") or {},
            "accepted_evidence_quotes": [
                quote for quote in (understanding.get("evidence_quotes") or [])
                if quote.get("review_status") in ("accepted", "pending")],
            "method_evolution": _evolution(understanding.get("method_evolution")),
        })

    relations = []
    for relation in claim_relations:
        src = claim_index.get(relation.get("src_claim")) or {}
        dst = claim_index.get(relation.get("dst_claim")) or {}
        relations.append({
            "kind": relation.get("kind"),
            "polarity": relation.get("polarity"),
            "src_claim": relation.get("src_claim"),
            "dst_claim": relation.get("dst_claim"),
            "src_paper": src.get("paper_id"),
            "dst_paper": dst.get("paper_id"),
            "src_text": src.get("text"),
            "dst_text": dst.get("text"),
        })

    # Paper-level semantic relations.  These are the backbone of the
    # evolution/backcasting and complementarity strategies: claim relations say
    # what two claims do to each other, paper relations say how one *method*
    # builds on another.
    paper_relations = []
    for relation in evidence.get("paper_relations") or []:
        paper_relations.append({
            "src_paper": relation.get("src_paper"),
            "dst_paper": relation.get("dst_paper"),
            "kind": relation.get("kind"),
            "axis": relation.get("axis"),
            "evidence": _texts(relation.get("evidence")),
            "confidence": relation.get("confidence"),
            "generated_by": relation.get("generated_by"),
        })

    # Aggregate so the model can distinguish "one paper said this" from "this
    # keeps coming up", without having to count by hand.
    limitation_counts: Dict[str, int] = {}
    for row in rows:
        for text in (row["acknowledged_limitations"]
                     + row["unacknowledged_limitations"]):
            key = text.strip().lower()[:120]
            if key:
                limitation_counts[key] = limitation_counts.get(key, 0) + 1
    recurring = [{"text": key, "papers": count}
                 for key, count in sorted(limitation_counts.items(),
                                          key=lambda pair: -pair[1])
                 if count > 1]

    return {
        "snapshot_id": evidence.get("snapshot_id"),
        "paper_count": len([row for row in rows if row["paper_id"]]),
        "papers": rows,
        "claim_relations": relations,
        "paper_relations": paper_relations,
        "timeline": _timeline(rows),
        "assumption_clusters": _assumption_clusters(rows),
        "unresolved_clusters": _unresolved_clusters(rows),
        "contradiction_count": len([rel for rel in relations
                                    if rel["kind"] == "contradict"]),
        "recurring_limitations": recurring[:20],
        "known_paper_ids": sorted(paper_ids),
        "known_claim_ids": sorted(claim_index),
    }


def _texts(items) -> List[str]:
    out = []
    for item in items or []:
        if isinstance(item, str):
            text = item.strip()
        elif isinstance(item, dict):
            text = str(item.get("text") or "").strip()
        else:
            text = str(item).strip()
        if text:
            out.append(text)
    return out


def validate_proposals(payload: Any, landscape: dict) -> dict:
    """Structural and traceability checks, independent of any strategy.

    This is the part that makes I0 and I1 comparable: both must clear the same
    bar, so a difference in output quality is attributable to the mechanism.
    """
    if not isinstance(payload, dict):
        return failed(error("IDEATION_OUTPUT_INVALID",
                            "ideation 输出必须是 JSON 对象"))
    proposals = payload.get("proposals")
    if not isinstance(proposals, list) or not proposals:
        return failed(error("IDEATION_OUTPUT_INVALID",
                            "proposals 必须是非空数组",
                            hint="即使没有好方向，也要说明为何 rejected"))
    opportunities = payload.get("opportunities") or []
    if not isinstance(opportunities, list):
        return failed(error("IDEATION_OUTPUT_INVALID",
                            "opportunities 必须是数组"))
    opportunity_ids = {str(item.get("opportunity_id"))
                       for item in opportunities if isinstance(item, dict)}
    known_papers = set(landscape.get("known_paper_ids") or [])
    known_claims = set(landscape.get("known_claim_ids") or [])

    problems: List[str] = []
    accepted: List[dict] = []
    for index, proposal in enumerate(proposals):
        if not isinstance(proposal, dict):
            problems.append("proposal[{}] 不是对象".format(index))
            continue
        missing = [field for field in PROPOSAL_FIELDS
                   if not proposal.get(field)]
        if missing:
            problems.append("proposal[{}] 缺字段：{}".format(
                index, ", ".join(missing)))
            continue
        if opportunity_ids and str(proposal["from_opportunity"]) not in \
                opportunity_ids:
            problems.append("proposal[{}] 引用了不存在的 opportunity {}".format(
                index, proposal["from_opportunity"]))
            continue
        anchors = _anchors(proposal.get("evidence_refs"))
        known = [ref for ref in anchors
                 if ref in known_papers or ref in known_claims]
        unknown = [ref for ref in anchors if ref not in known_papers
                   and ref not in known_claims]
        if not known:
            problems.append(
                "proposal[{}] 的 evidence_refs 无法锚定到语料中的任何论文/论点".format(
                    index))
            continue
        cleaned = dict(proposal)
        cleaned["evidence_refs"] = known
        cleaned["unanchored_refs"] = unknown
        cleaned["unverified"] = _unverified(proposal, unknown)
        accepted.append(cleaned)

    if not accepted:
        return failed(error("IDEATION_NO_GROUNDED_PROPOSAL",
                            "没有任何提案能锚定到语料证据",
                            hint="检查 evidence_refs 是否使用 paper_id/claim_id",
                            context={"problems": problems}),
                      data={"rejected": problems, "raw": payload})
    value = {"proposals": accepted, "rejected_proposals": problems,
             "rejected": payload.get("rejected") or [],
             "opportunities": opportunities,
             "literature_review": payload.get("literature_review") or {},
             "gap_analysis": payload.get("gap_analysis") or {},
             "landscape_summary": {
                 "papers": landscape.get("paper_count"),
                 "contradictions": landscape.get("contradiction_count"),
                 "recurring_limitations": len(
                     landscape.get("recurring_limitations") or []),
             }}
    if problems:
        return partial(value, warnings=problems)
    return ok(value)


def _anchors(refs) -> List[str]:
    out = []
    for ref in refs or []:
        if isinstance(ref, dict):
            value = ref.get("paper_id") or ref.get("claim_id") or ref.get("ref")
        else:
            value = ref
        text = str(value or "").strip()
        if text:
            out.append(text)
    return out


def _unverified(proposal: dict, unknown: List[str]) -> List[str]:
    """Things this corpus cannot establish.  Never empty by construction.

    Novelty is always unverified in a closed corpus; that is a property of the
    setting, not a failure of the model.
    """
    items = [str(item).strip() for item in proposal.get("unverified") or []
             if str(item).strip()]
    items.append("新颖性未经外部文献检索确认（闭世界语料无法证明）")
    if unknown:
        items.append("引用了语料中不存在的证据：{}".format(", ".join(unknown)))
    return items


def filter_proposals(proposals: List[dict], *, max_proposals: int = 5,
                     min_mechanisms: int = 2) -> dict:
    """Deterministic convergent filter.

    Scores only what can be checked mechanically: whether the proposal is
    anchored, whether its mechanism is described concretely, whether it states
    a falsifiable plan, and whether it is distinct from another proposal.
    Scientific value and novelty are *not* scored -- they cannot be, here.
    """
    scored = []
    for proposal in proposals:
        anchors = len(proposal.get("evidence_refs") or [])
        mechanism = str(proposal.get("proposed_mechanism") or "")
        plan = proposal.get("validation_plan") or []
        hypothesis = str(proposal.get("core_hypothesis") or "")
        scores = {
            "evidence_grounding": min(1.0, anchors / 3.0),
            "specificity": min(1.0, len(mechanism) / 200.0),
            "mechanism_clarity": 1.0 if mechanism and
            proposal.get("why_it_might_work") else 0.4,
            "falsifiability": min(1.0, len(plan) / 2.0) if hypothesis else 0.2,
        }
        total = sum(scores.values()) / len(FILTER_DIMENSIONS)
        scored.append(dict(proposal, scores=scores, total=round(total, 3)))
    scored.sort(key=lambda item: -item["total"])

    # Diversity: do not return five near-identical refinements.  Deduplicate on
    # (opportunity, mechanism head) and require more than one kind of opportunity
    # when the corpus offers more than one.
    chosen: List[dict] = []
    seen = set()
    for proposal in scored:
        key = (str(proposal.get("from_opportunity")),
               str(proposal.get("proposed_mechanism") or "")[:80].lower())
        if key in seen:
            continue
        seen.add(key)
        chosen.append(proposal)
        if len(chosen) >= max_proposals:
            break

    kinds = {str(item.get("from_opportunity")) for item in chosen}
    warnings = []
    if len(kinds) < min_mechanisms and len(chosen) > 1:
        warnings.append(
            "已选方向只来自 {} 个 opportunity；多样性不足".format(len(kinds)))
    return {"proposals": chosen,
            "dropped_for_similarity": len(scored) - len(chosen),
            "distinct_opportunities": len(kinds),
            "warnings": warnings}


def prepare_payload(landscape: dict, *, research_goal: str = "",
                    reading_state: Optional[dict] = None) -> dict:
    """The exact JSON a strategy is given.  Deliberately one shape for all arms."""
    return {
        "research_goal": research_goal,
        "corpus": landscape,
        "reading_state": reading_state or {},
    }


def run_ideation(payload: Any, *, strategy_id: str, executor,
                 landscape: dict, directory: Optional[str] = None,
                 max_proposals: int = 5) -> dict:
    """One ideation turn, from strategy resolution to a filtered result."""
    built = build_ideation_messages(strategy_id, payload, directory=directory)
    if not built["ok"]:
        return built
    messages = built["data"]["messages"]
    try:
        raw = executor(messages) if callable(executor) else executor.run(
            messages=messages)
    except Exception as exc:  # noqa: BLE001 - agent boundary
        return failed(error("IDEATION_AGENT_FAILED",
                            "{}: {}".format(type(exc).__name__, exc)))
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            return failed(error("IDEATION_OUTPUT_NOT_JSON", str(exc)))
    validated = validate_proposals(raw, landscape)
    if not validated["ok"]:
        return validated
    filtered = filter_proposals(validated["data"]["proposals"],
                                max_proposals=max_proposals)
    value = dict(validated["data"])
    value.update({
        "proposals": filtered["proposals"],
        "strategy_id": built["data"]["strategy_id"],
        "strategy_prompt_sha256": built["data"]["strategy_prompt_sha256"],
        "common_contract_sha256": built["data"]["common_contract_sha256"],
        "filter": {key: value_ for key, value_ in filtered.items()
                   if key != "proposals"},
    })
    warnings = list(validated.get("warnings") or []) + filtered["warnings"]
    if warnings:
        return partial(value, warnings=warnings)
    return ok(value)


def write_agenda(root: str, value: dict) -> str:
    """Persist the research agenda so later stages can consume it."""
    import os
    os.makedirs(root, exist_ok=True)
    path = os.path.join(root, "agenda.json")
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    return path


def _components(items) -> List[dict]:
    """Keep component name/role: the handle for thought experiments."""
    out = []
    for item in items or []:
        if isinstance(item, str):
            out.append({"name": item, "role": ""})
            continue
        if isinstance(item, dict) and item.get("name"):
            out.append({"name": str(item["name"]),
                        "role": str(item.get("role") or "")})
    return out


def _assumptions(items) -> List[dict]:
    """Assumptions keep their explicit/implicit flag and the claims they hold up.

    ``explicit=False`` is the interesting case for assumption relaxation: the
    author never stated it, so removing it is a genuine change of setting.
    """
    out = []
    for item in items or []:
        if isinstance(item, str):
            out.append({"text": item, "explicit": None, "used_by_claims": []})
            continue
        if isinstance(item, dict):
            text = str(item.get("text") or "").strip()
            if not text:
                continue
            out.append({"text": text,
                        "explicit": item.get("explicit"),
                        "used_by_claims": item.get("used_by_claims") or []})
    return out


def _evolution(items) -> List[dict]:
    out = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        out.append({
            "from_paper": item.get("from_paper"),
            "to_paper": item.get("to_paper"),
            "inherited": _texts(item.get("inherited")),
            "modified": [{"component": entry.get("component"),
                          "before": entry.get("before"),
                          "after": entry.get("after")}
                         for entry in item.get("modified") or []
                         if isinstance(entry, dict)],
            "new_assumptions": _texts(item.get("new_assumptions")),
            "removed_assumptions": _texts(item.get("removed_assumptions")),
        })
    return out


def _timeline(rows) -> List[dict]:
    """Papers ordered by first public appearance, with what each changed.

    Backcasting needs this: to ask "what did the next step look like from here",
    you first need to know what "here" was.
    """
    ordered = sorted((row for row in rows if row.get("paper_id")),
                     key=lambda row: (row.get("year") or 9999,
                                      row.get("paper_id")))
    return [{"paper_id": row["paper_id"], "year": row.get("year"),
             "research_problem": row.get("research_problem"),
             "method_family": row.get("method_family"),
             "contribution": row.get("contribution"),
             "removed_assumptions": [
                 str(assumption)
                 for evolution in row.get("method_evolution") or []
                 for assumption in evolution.get("removed_assumptions") or []],
             } for row in ordered]


def _assumption_clusters(rows) -> List[dict]:
    """Group papers by shared assumption text.

    A shared assumption that nobody has tested is a natural relaxation target;
    a shared assumption that one paper already dropped is a natural transfer.
    """
    buckets: Dict[str, List[str]] = {}
    for row in rows:
        for assumption in row.get("assumptions") or []:
            text = str(assumption.get("text") or "").strip()
            if text:
                buckets.setdefault(text[:120], []).append(row["paper_id"])
    return [{"assumption": text, "papers": papers}
            for text, papers in sorted(buckets.items(),
                                       key=lambda pair: -len(pair[1]))
            if len(papers) > 1]


def _unresolved_clusters(rows) -> List[dict]:
    """Recurring unresolved questions across papers.

    One paper's unanswered question is a gap in that paper.  The same question
    in three papers is a gap in the field, which is what ideation trades in.
    """
    buckets: Dict[str, List[str]] = {}
    for row in rows:
        for question in row.get("unresolved_questions") or []:
            text = str(question or "").strip()
            if text:
                buckets.setdefault(text[:120], []).append(row["paper_id"])
    return [{"question": text, "papers": papers}
            for text, papers in sorted(buckets.items(),
                                       key=lambda pair: -len(pair[1]))
            if len(papers) > 1]
