"""Reading-feedback sufficiency assessment.

This is the judgement behind "schedule a re-read".  It is deliberately
deterministic and lives outside the agents: deciding *whether* a paper was read
well enough is a domain rule, not a model opinion.  The Scheduler agent consumes
the result and decides what to do with it (re-read now, defer, or accept).

The core distinction:

* **re-readable gaps** -- the reader had the material but did not extract or
  verify something (``not_extracted``, ``not_checked``, ``unknown``), or the
  reader itself flagged the understanding as ``partial`` / ``needs_revision``.
  A second pass with targeted focus can plausibly fix these.
* **terminal gaps** -- the fact is absent from the paper (``not_found``) or does
  not apply (``not_applicable``).  Re-reading cannot help; scheduling another
  pass would burn budget and, worse, teach the system to re-read forever.

Only the first category produces `ReadingGap` entries that can schedule a
re-read.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


#: Absent-field reasons a re-read could plausibly resolve.
RETRYABLE_REASONS = ("not_extracted", "not_checked", "unknown")

#: Reasons that mean "the paper genuinely does not contain this".
TERMINAL_REASONS = ("not_found", "not_applicable")

#: Understanding states that justify another pass.
RETRYABLE_QUALITY = ("partial", "needs_revision")

#: Fields whose absence blocks downstream synthesis most severely.
FIELD_PRIORITY = {
    "core_claims": 3,
    "limitations": 3,
    "method": 2,
    "key_results": 2,
    "assumptions": 2,
    "contribution": 1,
    "research_problem": 1,
}


@dataclass
class ReadingGap:
    """One concrete reason a reading is not good enough yet."""

    field: str
    reason: str
    detail: str
    priority: int = 1
    focus: str = ""

    def to_dict(self) -> dict:
        return {"field": self.field, "reason": self.reason,
                "detail": self.detail, "priority": self.priority,
                "focus": self.focus}


@dataclass
class Assessment:
    paper_id: str
    sufficient: bool
    gaps: List[ReadingGap] = field(default_factory=list)
    terminal_gaps: List[ReadingGap] = field(default_factory=list)
    reading_depth: str = ""
    quality_status: str = ""

    @property
    def retryable(self) -> bool:
        return bool(self.gaps)

    @property
    def focus(self) -> List[str]:
        """Human/LLM-readable focus list to attach to the next ReadingTask."""
        ordered = sorted(self.gaps, key=lambda gap: -gap.priority)
        return [gap.focus for gap in ordered if gap.focus]

    def to_dict(self) -> dict:
        return {"paper_id": self.paper_id, "sufficient": self.sufficient,
                "reading_depth": self.reading_depth,
                "quality_status": self.quality_status,
                "gaps": [gap.to_dict() for gap in self.gaps],
                "terminal_gaps": [gap.to_dict() for gap in self.terminal_gaps],
                "focus": self.focus}


def _field_text(result: dict, name: str) -> str:
    value = result.get(name)
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (list, dict)):
        return "" if not value else "present"
    return str(value).strip()


def assess(paper_id: str, reading_result: Optional[dict]) -> Assessment:
    """Judge whether one ReadingResult is good enough to build on."""
    if not reading_result:
        # No understanding at all: the paper is not read, which is a scheduling
        # concern, not an insufficiency in what was read.
        return Assessment(paper_id=paper_id, sufficient=False,
                          reading_depth="unread",
                          gaps=[ReadingGap(
                              field="understanding", reason="not_extracted",
                              detail="该论文尚无结构化理解",
                              priority=FIELD_PRIORITY["core_claims"],
                              focus="完成一次完整阅读并输出结构化理解")])

    quality = reading_result.get("understanding_quality") or {}
    status = str(quality.get("status") or "")
    depth = str(reading_result.get("reading_depth") or "")
    gaps: List[ReadingGap] = []
    terminal: List[ReadingGap] = []

    # 1. The reader's own verdict.
    if status in RETRYABLE_QUALITY:
        gaps.append(ReadingGap(
            field="understanding_quality", reason=status,
            detail="阅读器自评理解状态为 {}".format(status),
            priority=3,
            focus="复核该论文的关键论断与证据链，补齐未证实部分"))

    # 2. Unresolved questions are explicit re-read targets.
    for question in (quality.get("unresolved_questions") or [])[:5]:
        text = str(question or "").strip()
        if text:
            gaps.append(ReadingGap(
                field="unresolved_questions", reason="not_checked",
                detail=text, priority=2,
                focus="回答：{}".format(text)))

    # 3. Declared absences, split by whether another pass could help.
    completeness = reading_result.get("completeness") or {}
    for item in completeness.get("absent_fields") or []:
        name = str(item.get("field") or "").strip()
        reason = str(item.get("reason") or "").strip()
        if not name:
            continue
        notes = str(item.get("notes") or "").strip()
        gap = ReadingGap(
            field=name, reason=reason,
            detail=notes or "字段 {} 缺失（{}）".format(name, reason),
            priority=FIELD_PRIORITY.get(name, 1),
            focus="补齐字段 {}（{}）".format(name, reason))
        if reason in RETRYABLE_REASONS:
            gaps.append(gap)
        elif reason in TERMINAL_REASONS:
            terminal.append(gap)

    # 4. Fields that are structurally required and empty without explanation.
    for name, priority in (("core_claims", 3), ("limitations", 3),
                           ("method", 2)):
        text = _field_text(reading_result, name)
        if text:
            continue
        if any(gap.field == name for gap in gaps + terminal):
            continue
        gaps.append(ReadingGap(
            field=name, reason="not_extracted",
            detail="字段 {} 为空且未在 completeness 中说明".format(name),
            priority=priority,
            focus="明确给出 {}，若确实不适用则在 completeness 中说明".format(name)))

    # 5. A superficial pass over a paper with declared depth below full reading.
    if depth in ("unread", "", "first_pass") and status not in ("checked",) \
            and not gaps:
        gaps.append(ReadingGap(
            field="reading_depth", reason="not_checked",
            detail="阅读深度为 {}".format(depth or "未标注"),
            priority=1,
            focus="完成第二遍阅读，覆盖方法与论证细节"))

    # 6. Critical-reading audit gaps.  A rejected quote is retained for audit,
    # but it cannot support synthesis; missing appraisal information is a
    # concrete re-read target when the Reader itself has not resolved it.
    appraisal = reading_result.get("critical_appraisal") or {}
    for item in (appraisal.get("missing_information") or [])[:5]:
        text = str(item or "").strip()
        if text and status in RETRYABLE_QUALITY:
            gaps.append(ReadingGap(
                field="critical_appraisal", reason="not_checked", detail=text,
                priority=2, focus="核查精读审计缺口：{}".format(text)))
    rejected = [quote for quote in (reading_result.get("evidence_quotes") or [])
                if quote.get("review_status") == "rejected"]
    if rejected and not any(quote.get("review_status") == "accepted"
                            for quote in reading_result.get("evidence_quotes") or []):
        gaps.append(ReadingGap(
            field="evidence_quotes", reason="not_checked",
            detail="所有逐页引文均被拒绝，当前没有可接受的原文证据",
            priority=3, focus="重新定位并逐字核对可支持核心主张的页码引文"))

    sufficient = not gaps
    return Assessment(paper_id=paper_id, sufficient=sufficient, gaps=gaps,
                      terminal_gaps=terminal, reading_depth=depth,
                      quality_status=status)


def compare(dispatched: List[dict], after: Assessment) -> dict:
    """Did a re-read actually close the gaps it was sent to close?

    ``dispatched`` is the task's ``deficiency`` list (what the Scheduler saw when
    it scheduled the pass).  Comparing it with the post-read assessment answers
    the only question that matters operationally: is another pass worth it?

    Resolution is matched on ``(field, reason)``: a gap counts as resolved when
    that pair no longer appears.  A gap whose reason *changed* (say
    ``not_extracted`` -> ``not_found``) is reported separately, because the
    reader made progress but discovered the fact is absent from the paper.
    """
    before = {(str(item.get("field")), str(item.get("reason")))
              for item in dispatched or []}
    current = {(gap.field, gap.reason) for gap in after.gaps}
    terminal = {(gap.field, gap.reason) for gap in after.terminal_gaps}
    current_fields = {field for field, _ in current}
    terminal_fields = {field for field, _ in terminal}
    before_fields = {field for field, _ in before}
    # A gap resolved when the *field* is no longer an open gap.  Matching on the
    # (field, reason) pair alone would misreport a field whose reason changed --
    # e.g. "not_extracted" becoming "not_found" means the reader looked and the
    # fact is absent, which is progress of a different kind, not a resolution.
    resolved = sorted(pair for pair in before
                      if pair[0] not in current_fields
                      and pair[0] not in terminal_fields)
    still_open = sorted(pair for pair in before if pair in current)
    became_terminal = sorted(
        pair for pair in before
        if pair[0] in terminal_fields and pair[0] not in current_fields)
    new_gaps = sorted(pair for pair in current if pair[0] not in before_fields)
    return {
        "resolved": [list(pair) for pair in resolved],
        "still_open": [list(pair) for pair in still_open],
        "became_terminal": [list(pair) for pair in became_terminal],
        "new": [list(pair) for pair in new_gaps],
        "progress": bool(resolved),
        "converged": not still_open,
        "sufficient_after": after.sufficient,
    }


def summarise(assessments: List[Assessment]) -> dict:
    """Aggregate view for logging and for the planner's input."""
    retryable = [item for item in assessments if item.retryable]
    return {
        "assessed": len(assessments),
        "insufficient": len(retryable),
        "papers": [item.to_dict() for item in retryable],
        "terminal_only": [item.paper_id for item in assessments
                          if not item.retryable and item.terminal_gaps],
    }
