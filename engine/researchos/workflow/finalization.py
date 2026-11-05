"""Finalization stages as agents.

Stage list, roles, instructions and required fields are copied verbatim from
the original fixed pipeline, so the loop and its gates are unchanged.  What
changes: each stage is an Agent with a scoped ``get_evidence`` tool and a
validation contract enforced at the agent boundary.
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

from ..agents.base import AgentKit
from ..runtime.agent import Agent
from ..runtime.envelope import error, failed, ok
from ..runtime.messages import AgentReply


STAGES = (
    "synthesis", "gap_analysis", "hypothesis", "methodology",
    "execution", "analysis", "audit", "writing",
)

AGENT_ROLES = {
    "synthesis": "synthesizer",
    "gap_analysis": "gap_analyst",
    "hypothesis": "hypothesis_agent",
    "methodology": "methodologist",
    "execution": "executor",
    "analysis": "analyst",
    "audit": "auditor",
    "writing": "writer",
}

STAGE_INSTRUCTIONS = {
    "synthesis": "Synthesize the supplied literature evidence. Do not propose or execute new experiments.",
    "gap_analysis": "Identify evidence-bounded gaps. Do not turn an absence in this corpus into a universal absence claim.",
    "hypothesis": "Form falsifiable hypotheses from the selected gap. Do not report them as findings.",
    "methodology": "Design procedures and evaluation. Do not claim that planned actions were executed.",
    "execution": "Execute the approved methodology with runtime tools. Record only actions and artifacts that actually occurred.",
    "analysis": "Analyze recorded execution artifacts and literature evidence. Do not alter execution records.",
    "audit": "Independently audit traceability, limitations, and completion gates. Do not repair artifacts silently.",
    "writing": "Write only from audited artifacts. Preserve limitations, references, and evidence traceability.",
}

REQUIRED = {
    "synthesis": ("summary", "consensus", "disagreements", "unknowns", "evidence_refs"),
    "gap_analysis": ("gaps", "selected_gap", "evidence_refs"),
    "hypothesis": ("hypotheses", "selected_hypothesis", "falsification_conditions"),
    "methodology": ("design", "inputs", "procedure", "evaluation", "risks"),
    "execution": ("actions", "artifacts", "failures"),
    "analysis": ("results", "uncertainties", "robustness", "evidence_refs"),
    "audit": ("verdict", "checks", "required_revisions"),
    "writing": ("title", "abstract", "findings", "limitations", "references", "evidence_trace"),
}

_LIST_FIELDS = {
    "synthesis": ("consensus", "disagreements", "unknowns", "evidence_refs"),
    "gap_analysis": ("gaps", "evidence_refs"),
    "hypothesis": ("hypotheses", "falsification_conditions"),
    "methodology": ("inputs", "procedure", "evaluation", "risks"),
    "execution": ("actions", "artifacts", "failures"),
    "analysis": ("results", "uncertainties", "robustness", "evidence_refs"),
    "audit": ("checks", "required_revisions"),
    "writing": ("findings", "limitations", "references", "evidence_trace"),
}

_NONEMPTY = {
    "execution": ("actions", "artifacts"),
    "analysis": ("results", "evidence_refs"),
    "audit": ("checks",),
    "writing": ("findings", "evidence_trace"),
}


def validate_stage(stage: str, payload: Any) -> dict:
    """The single completion gate every stage payload must pass."""
    if stage not in REQUIRED:
        return failed(error("FINAL_STAGE_UNKNOWN", "未知阶段：{}".format(stage)))
    if not isinstance(payload, dict):
        return failed(error("FINAL_OUTPUT_INVALID", "阶段输出必须是 JSON object"))
    missing = [key for key in REQUIRED[stage]
               if key not in payload or payload[key] is None or payload[key] == ""]
    if missing:
        return failed(error("FINAL_OUTPUT_INVALID",
                            "缺少字段：{}".format(", ".join(missing))))
    if stage == "audit" and payload["verdict"] not in {"pass", "revise", "blocked"}:
        return failed(error("FINAL_OUTPUT_INVALID",
                            "audit.verdict 必须为 pass/revise/blocked"))
    invalid = [key for key in _LIST_FIELDS.get(stage, ())
               if not isinstance(payload.get(key), list)]
    if invalid:
        return failed(error("FINAL_OUTPUT_INVALID",
                            "字段必须为数组：{}".format(", ".join(invalid))))
    empty = [key for key in _NONEMPTY.get(stage, ()) if not payload.get(key)]
    if empty:
        return failed(error("FINAL_OUTPUT_INVALID",
                            "完成阶段不得为空：{}".format(", ".join(empty))))
    if stage == "execution":
        required_artifact = {"artifact_id", "kind", "uri", "produced_by",
                             "verification_status"}
        invalid_artifacts = [index for index, item in enumerate(payload["artifacts"])
                             if not isinstance(item, dict)
                             or not required_artifact.issubset(item)]
        if invalid_artifacts:
            return failed(error("FINAL_OUTPUT_INVALID",
                                "execution.artifacts 缺少可审计字段，索引：{}".format(
                                    invalid_artifacts)))
    if (stage == "audit" and payload["verdict"] == "pass"
            and payload["required_revisions"]):
        return failed(error("FINAL_OUTPUT_INVALID",
                            "audit=pass 时 required_revisions 必须为空"))
    return ok(payload)


def stage_package(stage: str, *, research_question: str,
                  artifacts: Optional[dict] = None,
                  research_manifest: Optional[dict] = None,
                  literature_status: Optional[dict] = None,
                  evidence: Optional[dict] = None) -> dict:
    if stage not in REQUIRED:
        raise ValueError("unknown stage: {}".format(stage))
    return {
        "stage": stage, "agent_role": AGENT_ROLES[stage],
        "stage_instruction": STAGE_INSTRUCTIONS[stage],
        "required_fields": list(REQUIRED[stage]),
        "research_question": research_question,
        "research_manifest": research_manifest or {},
        "literature_status": literature_status or {},
        "prior_artifacts": artifacts or {},
        "evidence": evidence or {},
        "available_system_tools": ["get_evidence"],
    }


def evidence_tool(evidence: dict, *, snapshot_id: Optional[str] = None):
    def get_evidence(paper_id: Optional[str] = None):
        if paper_id is None:
            return evidence
        papers = [item for item in evidence.get("papers", [])
                  if item.get("overview", {}).get("paper_id") == paper_id]
        return {"snapshot_id": evidence.get("snapshot_id"), "papers": papers}
    return get_evidence


def build_stage_agent(kit: AgentKit, stage: str, *, agent_id: str = "") -> Agent:
    """One stage = one agent with one read-only evidence tool."""
    if stage not in REQUIRED:
        raise ValueError("unknown stage: {}".format(stage))
    role = AGENT_ROLES[stage]
    holder: Dict[str, Any] = {"evidence": {}}

    def get_evidence(paper_id: Optional[str] = None):
        return evidence_tool(holder["evidence"])(paper_id)

    registry = kit.registry.with_tools([
        ("get_evidence", "读取 Organization 导出的只读证据包",
         {"type": "object",
          "properties": {"paper_id": {"type": ["string", "null"]}},
          "additionalProperties": False},
         get_evidence),
    ])
    agent = Agent(role=role,
                  system_prompt=kit.prompts.get("finalization.md") + "\n" +
                                STAGE_INSTRUCTIONS[stage],
                  tools=registry,
                  policy=kit.resolve_policy(agent_id or role, _PendingPolicy()),
                  budget=kit.budget(),
                  id=agent_id or role)
    agent.stage = stage  # type: ignore[attr-defined]
    agent.evidence_holder = holder  # type: ignore[attr-defined]
    return agent


class _PendingPolicy:
    """Placeholder policy: the stage must be driven by an injected executor.

    When no agent is supplied the Director writes a pending package and waits,
    rather than inventing a stage result.
    """

    name = "pending"

    def respond(self, request):
        return AgentReply(text="", payload=None)


def run_stage(agent: Agent, kit: AgentKit, *, package: dict,
              context_agent_id: str) -> dict:
    """Drive one stage agent and validate its output against the contract."""
    holder = getattr(agent, "evidence_holder", None)
    if holder is not None:
        holder["evidence"] = package.get("evidence") or {}
    context = kit.context(context_agent_id, parent_id="director")
    result = agent.one_shot(context, json.dumps(package, ensure_ascii=False),
                            metadata={"stage": package["stage"]})
    if result.payload is None:
        return failed(error("FINAL_AGENT_REQUIRED",
                            "阶段 {} 需要可执行策略".format(package["stage"]),
                            hint="注入 {} Agent 或提交外部阶段结果".format(
                                package["agent_role"])))
    return validate_stage(package["stage"], result.payload)


def write_artifact(root: str, stage: str, payload: dict) -> str:
    path = os.path.join(root, stage + ".json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    return path
