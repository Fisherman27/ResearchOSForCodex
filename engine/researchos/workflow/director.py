"""Deterministic top-level controller used by the DSH Mode Lead.

The historical module name is retained for storage/API compatibility, but the
Mode never hands this controller to a model and never creates a Director DSH
Session. The parent DSH Agent is the sole Lead; this object only exposes the
deterministic transitions that Lead drives:

* its tools are its subordinates (``ask_research``, ``ask_literature``,
  ``ask_organization``) and a small number of state transitions;
* it never touches a port, a knowledge base or a paper;
* every hop is a traced tool call carrying a task envelope;
* the phase table and completion gates are unchanged.

Phase table (unchanged):

    research -> literature -> finalization(synthesis..writing) -> completed

Completion gates (unchanged):

    * writing only lands when ``audit.verdict == "pass"``
    * ``audit=revise`` returns to methodology, ``audit=blocked`` -> blocked_research
    * an exhausted reading queue ends the literature phase, not the run
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

from ..agents.base import AgentKit, agent_tool
from ..runtime.agent import Agent, AgentResult
from ..runtime.envelope import error, failed, ok, partial
from ..runtime.envelope import is_external_pause as _is_external_pause
from ..runtime.trace import now
from .finalization import AGENT_ROLES, STAGES, build_stage_agent, run_stage, stage_package, validate_stage, write_artifact


FINAL_STATUSES = {"completed", "failed", "blocked_research"}


def build_director(kit: AgentKit, *, run_id: str, research: Agent,
                   literature: Agent, organization: Agent) -> Agent:
    store = kit.store
    agent_id = "director"
    artifacts_dir = os.path.join(store.root, "artifacts")
    stage_agents = {stage: build_stage_agent(kit, stage)
                    for stage in STAGES}

    director_proxy = {"registry": None}

    def _ask(tool_name: str, arguments: dict, *, target: Agent,
             tool: str) -> dict:
        """Dispatch to a subordinate through its agent-tool.

        The Director never calls a subordinate's internal tools directly: it
        asks the subordinate, and the subordinate decides which of its own
        capabilities to use.  That indirection is the whole point of the
        refactor, so it is centralised here rather than duplicated per phase.
        """
        arguments = dict(arguments)
        arguments.setdefault("__tool__", tool)
        value = director_proxy["registry"].call(tool_name, arguments)
        store.log.append("director_dispatch", {
            "target_agent": target.id, "target_tool": tool,
            "requested_tool": tool_name,
            "status": value.get("status"), "ok": value.get("ok"),
            "child_trace": (value.get("meta") or {}).get("child_trace")},
            actor=agent_id)
        return value

    def state() -> dict:
        return store.load()

    def _literature_state_from_disk() -> Optional[dict]:
        """Read the literature sub-run's state directly.

        A large corpus makes the supervisor's state outgrow the tool-result
        limit, so its envelope arrives truncated (``data=None``).  The status we
        need to decide the phase transition lives in the sub-run's state file,
        which is the same record the supervisor would have reported.
        """
        path = os.path.join(kit.config.runs_dir,
                            run_id + "-literature", "state.json")
        try:
            with open(path, encoding="utf-8") as handle:
                value = json.load(handle)
        except (OSError, ValueError):
            return None
        return value if isinstance(value, dict) else None

    def save(value: dict, event: Optional[str] = None, data: Optional[dict] = None):
        store.save(value, actor=agent_id)
        if event:
            store.log.append(event, data or {}, actor=agent_id)

    # -- phase: research -------------------------------------------------
    def run_research(research_question: str = "", search_options: dict = None):
        current = state()
        if current["status"] != "research_running":
            return ok(current)
        outcome = _ask("ask_research", {
            "research_question": research_question,
            "run_id": run_id,
            "query": "",
            "reason": "",
            "options": search_options or {},
        }, target=research, tool="run_search_pipeline")
        if not outcome["ok"]:
            if _is_external_pause(outcome):
                return _pause(current, outcome)
            return _transition_failure(current, "research_failed", outcome)
        # `agent_tool` forwards a child's envelope verbatim ("transparent
        # delegation"), so the result can be at three depths:
        #   * outcome.data == {"search_output": ...}          (bare result)
        #   * outcome.data == {"data": {"search_output": ...}} (nested envelope)
        #   * outcome.data == {"payload": {...}}               (final payload)
        # Unwrap all three instead of only the first, or a successful retrieval
        # is reported as `DIRECTOR_RESEARCH_OUTPUT_MISSING`.
        data = outcome.get("data") or {}
        for _ in range(3):
            if "search_output" in data:
                break
            for key in ("data", "payload"):
                inner = data.get(key)
                if isinstance(inner, dict):
                    data = inner
                    break
            else:
                break
        if "search_output" not in data:
            return _transition_failure(current, "research_failed", failed(error(
                "DIRECTOR_RESEARCH_OUTPUT_MISSING",
                "Research 未返回 search_output",
                hint="检索结果里没有 search_output；检查 Research port 的返回形状",
                context={"observed_keys": sorted((outcome.get("data") or {}).keys())})))
        current["research_manifest"] = (outcome.get("diagnostics") or {}).get(
            "query_plan", data.get("query_plan") or {})
        current["research_output"] = data["search_output"]
        current["phase"] = "literature"
        current["status"] = "literature_initializing"
        save(current, "research_completed", {
            "search_output": current["research_output"],
            "query_plan": current["research_manifest"]})
        return ok(current)

    # -- phase: literature -----------------------------------------------
    def run_literature():
        current = state()
        if current.get("phase") != "literature":
            return ok(current)
        if not current.get("literature_initialized"):
            initialized = _ask("ask_literature", {
                "search_output": current["research_output"],
                # The literature run has its own store/configuration. Passing
                # the question explicitly is what makes it available to the
                # Scheduler instead of silently falling back to an empty
                # process-wide default.
                "research_goal": current["research_question"]},
                target=literature, tool="initialize_literature")
            # The supervisor is routed to ``initialize_literature``; make sure
            # the argument actually reaches it.
            if not initialized["ok"]:
                if _is_external_pause(initialized):
                    return _pause(current, initialized)
                return _transition_failure(current, "literature_failed", initialized)
            current["literature_initialized"] = True
            current["literature_status"] = initialized["data"]
            save(current)
        before = _literature_state_from_disk() or {}
        # The supervisor call is only an *advance*: its own status tells "moved"
        # from "parked at a gate", and that distinction must come from the
        # stored state, not from whichever envelope happened to come back
        # (a caller may advance again later, and must not look like a pause).
        outcome = _ask("ask_literature", {
            # Also synchronize on every resume so runs created by an older
            # version (whose child state lacks research_goal) self-heal.
            "research_goal": current["research_question"]}, target=literature,
                       tool="run_literature")
        current = state()
        after = _literature_state_from_disk() or {}
        if _is_external_pause(outcome) and (after.get("status") or "") in (
                before.get("status") or "", "replanning", "failed"):
            # Genuinely parked: the stored state did not move either.  Report
            # the wait without ever replacing the child's status with a pause
            # marker of our own.
            return _pause(current, outcome)
        advance_ok = (after.get("status") or "") == (before.get("status") or "")
        if not outcome["ok"] and not advance_ok:
            if _is_external_pause(outcome):
                return _pause(current, outcome)
            return _transition_failure(current, "literature_failed", outcome)
        child = outcome.get("data")
        if not isinstance(child, dict):
            # The supervisor's own state grows with the corpus and can exceed
            # the tool-result limit, in which case the envelope is truncated to
            # ``data=None``.  The status is still authoritative on disk, so read
            # it from the literature store rather than dereferencing None.
            child = _literature_state_from_disk() or {}
        current["literature_status"] = {"status": child.get("status"),
                                        "cycle": child.get("cycle")}
        if child.get("status") in ("completed", "blocked_material"):
            current["phase"] = "finalization"
            current["current_stage"] = "synthesis"
            current["status"] = "final_synthesis"
            save(current, "literature_completed", {"status": child.get("status")})
            return ok(current)
        current["status"] = child.get("status") or outcome["status"]
        save(current)
        return ok(current, warnings=outcome.get("warnings"))

    # -- phase: finalization ---------------------------------------------
    def advance_stage():
        current = state()
        if current.get("phase") != "finalization":
            return ok(current)
        stage = current["current_stage"]
        role = AGENT_ROLES[stage]
        exported = _ask("ask_organization", {}, target=organization,
                        tool="export_evidence")
        if not exported["ok"]:
            return _transition_failure(current, "evidence_export_failed", exported)
        evidence = exported["data"]
        agent = stage_agents[stage]
        package = stage_package(
            stage, research_question=current["research_question"],
            artifacts=current.get("final_artifacts"),
            research_manifest=current.get("research_manifest"),
            literature_status=current.get("literature_status"),
            evidence=evidence)
        result = run_stage(agent, kit, package=package,
                           context_agent_id="director." + role)
        store.log.append("director_dispatch", {
            "target_agent": agent.id, "target_tool": "stage:" + stage,
            "status": result.get("status"), "ok": result.get("ok")},
            actor=agent_id)
        if not result["ok"]:
            if _is_external_pause(result):
                return _pause(current, result)
            if (result.get("error") or {}).get("code") == "FINAL_AGENT_REQUIRED":
                path = store.write_pending({"task_id": "final-" + stage}, package)
                current["status"] = "awaiting_stage_agent"
                current["pending_final_task"] = {"stage": stage,
                                                 "agent_role": role,
                                                 "path": path}
                save(current)
                return ok(current, warnings=["待处理包：{}".format(path)])
            return _transition_failure(current, "finalization_failed", result)
        path = write_artifact(artifacts_dir, stage, result["data"])
        return _commit_finalization(current, stage, result["data"], path)

    def submit_final_result(stage: str = "", payload: dict = None):
        current = state()
        if (current.get("phase") != "finalization"
                or current.get("current_stage") != stage):
            return failed(error("DIRECTOR_STAGE_NOT_PENDING",
                                "当前待处理阶段不是 {}".format(stage)))
        checked = validate_stage(stage, payload or {})
        if not checked["ok"]:
            return checked
        path = write_artifact(artifacts_dir, stage, checked["data"])
        return _commit_finalization(current, stage, checked["data"], path)

    def _commit_finalization(current: dict, stage: str, artifact: dict,
                             path: str) -> dict:
        current.setdefault("final_artifacts", {})[stage] = artifact
        current.setdefault("artifact_paths", {})[stage] = path
        current["pending_final_task"] = None
        if stage == "audit":
            if artifact["verdict"] == "blocked":
                current["status"] = "blocked_research"
                save(current, "audit_blocked", {"artifact": path})
                return ok(current)
            if artifact["verdict"] == "revise":
                current["revision_count"] = current.get("revision_count", 0) + 1
                # Audit may find defects in any upstream artifact. Restarting
                # at methodology made synthesis/gap/hypothesis immutable and
                # could trap the run in a revise loop.
                current["current_stage"] = "synthesis"
                current["status"] = "final_revision"
                save(current, "audit_revise", {"artifact": path})
                return ok(current)
        if stage == "writing":
            if current["final_artifacts"].get("audit", {}).get("verdict") != "pass":
                return _transition_failure(current, "completion_gate_failed",
                                           failed(error("DIRECTOR_AUDIT_REQUIRED",
                                                        "最终成果缺少通过的审计")))
            current["phase"] = "completed"
            current["status"] = "completed"
            current["current_stage"] = None
            current["final_artifact"] = path
            save(current, "workflow_completed", {
                "final_artifact": path, "completed_at": now()})
            return ok(current)
        index = STAGES.index(stage)
        current["current_stage"] = STAGES[index + 1]
        current["status"] = "final_{}".format(current["current_stage"])
        save(current, "final_stage_committed", {"stage": stage})
        return ok(current)

    def _pause(current: dict, result: dict) -> dict:
        """Record that an external LLM turn is required, and stop cleanly."""
        current["status"] = "awaiting_external_agent"
        current["pending_external"] = result.get("data") or {}
        save(current, "awaiting_external_agent",
             {"awaiting": current["pending_external"]})
        return ok(current, warnings=["等待外部 Agent 回合：{}".format(
            (result.get("data") or {}).get("child_agent", "?"))])

    def _transition_failure(current: dict, event: str, result: dict) -> dict:
        current["status"] = "failed"
        current["last_error"] = result.get("error")
        save(current, event, {"error": result.get("error")})
        return result

    # -- tools -----------------------------------------------------------
    def start(research_question: str = "", search_options: dict = None):
        current = state()
        # In handoff mode ``system.start`` seeds the run before the first
        # external turn, so the state is no longer "created" when the host
        # finally answers.  Accepting the seeded state keeps start idempotent
        # without weakening the "already initialized" guard for real reruns.
        allowed = ("created", "awaiting_external_agent", "research_running")
        if current.get("status") not in allowed:
            return failed(error("DIRECTOR_ALREADY_INITIALIZED",
                                "顶层研究 run 已初始化"))
        if (current.get("status") != "created"
                and current.get("research_output")):
            return failed(error("DIRECTOR_ALREADY_INITIALIZED",
                                "顶层研究 run 已初始化"))
        if not str(research_question or "").strip():
            return failed(error("DIRECTOR_QUESTION_MISSING",
                                "research_question 不能为空"))
        current.update({
            "schema_version": "3.0", "status": "research_running",
            "phase": "research", "research_question": research_question.strip(),
            "research_manifest": None, "research_output": None,
            "literature_run_id": run_id + "-literature",
            "literature_status": None, "literature_initialized": False,
            "current_stage": None, "final_artifacts": {}, "artifact_paths": {},
            "pending_final_task": None, "final_artifact": None,
            "revision_count": 0,
            "search_options": dict(search_options or {}),
        })
        save(current, "director_started",
             {"research_question": research_question})
        return run_research(research_question, search_options)

    def run(max_steps: int = 0):
        limit = max_steps or 0
        if not limit:
            limit = 1
        current = state()
        if current.get("status") in FINAL_STATUSES:
            return ok(current)
        if current.get("status") == "created":
            return failed(error("DIRECTOR_NOT_INITIALIZED",
                                "先用 start(question) 初始化"))
        if current.get("status") == "awaiting_external_agent":
            # While literature is open, an external pause belongs to that
            # sub-machine, not to the run: the supervisor may have already
            # consumed the answer and moved to completion.  Re-entering it is
            # idempotent (the supervisor and the handoff policy both refuse to
            # publish a duplicate order), so polling here is what lets the run
            # leave the pause and enter finalization.
            if current.get("phase") != "literature":
                return ok(current, warnings=["等待外部 Agent 回合"])
            return run_literature()
        phase = current.get("phase")
        if phase == "research":
            return run_research(current["research_question"],
                                current.get("search_options"))
        if phase == "literature":
            return run_literature()
        if phase == "finalization":
            return advance_stage()
        return ok(current)

    def cycle(max_steps: int = 0):
        limit = max_steps or kit.config.max_cycles
        for _ in range(limit):
            current = state()
            if current.get("status") in FINAL_STATUSES:
                return ok(current)
            if current.get("status") in ("awaiting_stage_agent",
                                         "awaiting_reader",
                                         "awaiting_relation_review",
                                         "awaiting_external_agent"):
                return ok(current)
            outcome = run()
            if not outcome["ok"]:
                if _is_external_pause(outcome):
                    return _pause(current, outcome)
                return outcome
        return partial(state(), warnings=["达到本次推进上限；状态已保存"])

    def get_state():
        return ok(state())

    registry = kit.registry.with_tools([
        ("get_director_state", "读取顶层运行状态",
         {"type": "object", "properties": {}, "additionalProperties": False},
         get_state),
        ("start_research", "初始化研究并启动 Research 阶段",
         {"type": "object",
          "properties": {"research_question": {"type": "string"},
                         "search_options": {"type": "object"}},
          "required": ["research_question"], "additionalProperties": False},
         start),
        ("advance_phase", "推进一个阶段",
         {"type": "object", "properties": {}, "additionalProperties": False},
         run),
        ("run_to_completion", "推进到终态或人工关卡",
         {"type": "object", "properties": {"max_steps": {"type": "integer"}},
          "additionalProperties": False},
         cycle),
        ("submit_final_result", "提交外部完成的终段结果",
         {"type": "object",
          "properties": {"stage": {"type": "string", "enum": list(STAGES)},
                         "payload": {"type": "object"}},
          "required": ["stage", "payload"], "additionalProperties": False},
         submit_final_result),
    ])
    for name, description, child in (
            ("ask_research", "把研究问题交给 Research 智能体检索", research),
            ("ask_literature", "把语料交给 Literature 监督智能体闭环阅读",
             literature),
            ("ask_organization", "直接向 Organization 智能体查询证据", organization)):
        registry.register(agent_tool(child, name=name,
                                     description=description, kit=kit,
                                     parent_id=agent_id))
    director_proxy["registry"] = registry
    return Agent(role="director",
                 system_prompt=kit.prompts.get("director.md"),
                 tools=registry,
                 policy=kit.resolve_policy(agent_id, _DirectorPolicy()),
                 budget=kit.budget(turns=6),
                 id=agent_id)


def run_agent(agent: Agent, kit: AgentKit, *, metadata: dict) -> AgentResult:
    context = kit.context(agent.id, parent_id=None)
    return agent.one_shot(context, json.dumps(metadata, ensure_ascii=False),
                          metadata=metadata)


class _DirectorPolicy:
    """Default Director policy: ask to run to completion, then return state.

    Equivalent to calling the pipeline runner once from the caller.
    An LLM policy would instead inspect ``get_director_state`` and decide which
    subordinate to ask next.
    """

    name = "director"

    def __init__(self):
        self._started = False

    def reset(self):
        self._started = False

    def respond(self, request):
        from ..runtime.messages import AgentReply, ToolCall
        if not self._started:
            self._started = True
            metadata = dict(request.metadata or {})
            tool = metadata.get("tool") or "run_to_completion"
            arguments = dict(metadata.get("arguments") or {})
            if tool == "run_to_completion" and "max_steps" in metadata:
                arguments.setdefault("max_steps", int(metadata["max_steps"]))
            return AgentReply(text="", tool_calls=[ToolCall(tool, arguments)])
        last = request.messages[-1] if request.messages else None
        if last is not None and last.role == "tool":
            try:
                return AgentReply(payload=json.loads(last.content))
            except json.JSONDecodeError:
                return AgentReply(payload=None)
        return AgentReply(payload=None)


