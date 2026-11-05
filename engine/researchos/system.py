"""System assembly: the one place where agents are wired together.

Everything above this file is an agent or a port.  This file is the only place
that knows the whole graph, which makes the coordination topology explicit and
inspectable::

                      Director
                        |
        +---------------+----------------+
        |               |                |
     Research      Literature      Organization
                        |                |
            +-----------+-----------+    |
            |           |           |    |
     Organization   Scheduler      Read  |
                        |                |
                   (planner)        (reader)

Note the two edges into Organization: the Director queries evidence directly
and the Literature supervisor writes knowledge through it.  That asymmetry is
intentional: evidence export is read-only and can be called from anywhere,
while writes are owned by the supervisor's happy path.
"""
from __future__ import annotations

import json
import dataclasses
import os
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

from .agents.base import AgentKit, Ports
from .agents.organization import build_organization_agent
from .agents.read import build_read_agent
from .agents.research import build_research_agent
from .agents.scheduler import build_scheduler_agent
from .config import SystemConfig
from .ports.contracts import PortError
from .runtime.agent import Agent
from .runtime.envelope import error, failed, ok
from .runtime.handoff import (AnswerLedger, HandoffPolicy, HandoffQueue,
                              SessionStore)
from .runtime.policy import Policy
from .runtime.trace import Blackboard, TraceWriter
from .workflow.director import build_director
from .workflow.literature import build_literature_supervisor
from .workflow.prompts import PromptLibrary
from .workflow.state import RunStore


PROMPT_ROOT = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "prompts")


@dataclass
class Agents:
    research: Agent
    organization: Agent
    scheduler: Agent
    read: Agent
    literature: Agent
    director: Agent

    def get(self, name: str) -> Agent:
        try:
            return getattr(self, name)
        except AttributeError:
            raise KeyError("unknown agent: {}".format(name))

    def ids(self) -> Dict[str, str]:
        return {name: agent.id for name, agent in self.__dict__.items()}


class ResearchSystem:
    """Facade used by the CLI and tests.

    It is deliberately thin: build ports, build agents, delegate one turn to the
    Director.  All real decisions live in agents and their policies.
    """

    def __init__(self, config: SystemConfig, run_id: str, *,
                 ports: Optional[Ports] = None,
                 policy_factory: Optional[Callable[[str, str], Optional[Policy]]] = None,
                 prompt_root: str = PROMPT_ROOT):
        self.config = config
        self.run_id = run_id
        # One task, one directory. In `per_run` layout the run owns its knowledge
        # base, visualization and artifacts, so the whole task can be moved or
        # deleted as a unit and two runs can never interleave writes.
        self.run_dir = os.path.join(config.runs_dir, run_id)
        if config.layout == "per_run":
            self.config = dataclasses.replace(
                config,
                kb_dir=os.path.join(self.run_dir, "kb"),
                visualization_dir=os.path.join(self.run_dir, "viz"))
        self.store = RunStore(config.runs_dir, run_id)
        self.prompts = PromptLibrary(prompt_root)
        self.ports = ports or self._default_ports()
        self.kit = AgentKit(
            config=config, prompts=self.prompts, ports=self.ports,
            run_id=run_id, store=self.store,
            blackboard=Blackboard(os.path.join(self.store.root, "blackboard.json")),
            traces=TraceWriter(self.store.root),
            workspace=self.store.root,
            policy_factory=self._compose_factory(policy_factory))
        self.handoff = (HandoffQueue(os.path.join(self.store.root, "handoff"))
                        if config.mode == "handoff" else None)
        # SessionStore appends its own ``sessions/`` directory, so pass the run
        # root; handoff mode keeps its sessions under the handoff mailbox.
        self.sessions = (SessionStore(os.path.join(self.store.root, "handoff"))
                         if config.mode == "handoff"
                         else SessionStore(self.store.root))
        self.ledger = (AnswerLedger(os.path.join(self.store.root, "handoff"))
                       if config.mode == "handoff" else None)
        self._agents_inflight: Dict[str, int] = {}
        #: Set when an agent turn consumed a handoff reply this cycle, so the
        #: literature loop can be woken even though the mailbox is now empty.
        self._last_handoff_result = False
        self.kit.handoff = self.handoff
        self.agents = self._build()

    # -- construction -----------------------------------------------------
    def _default_ports(self) -> Ports:
        if self.config.port_backend == "memory":
            from .ports.memory import (InMemoryOrganization, InMemoryRead,
                                       InMemoryResearch, InMemoryScheduler)
            return Ports(research=InMemoryResearch(self.config.search_output_dir),
                         organization=InMemoryOrganization(self.config.kb_dir),
                         scheduler=InMemoryScheduler(),
                         read=InMemoryRead())
        if self.config.port_backend == "bundle":
            from .ports.bundle import bundle_ports
            return bundle_ports(self.config)
        raise PortError("unknown port_backend: {}".format(self.config.port_backend))

    #: Roles whose judgement comes from an external model through the
    #: work-order mailbox.
    #:
    #: These are exactly the four module agents. Two roles are absent on purpose:
    #:
    #: * ``director`` -- scheme 4 merged it into the Lead. ``director.py`` is now
    #:   a deterministic top-level controller: no model turn, no session, no work
    #:   order. Handing it off would recreate the second coordinator the scheme
    #:   removes.
    #: * ``finalizer`` -- finalization is driven by the Mode plugin through the
    #:   bridge (acquire -> runTurn -> submit), so it never publishes an order.
    #:
    #: Everything else (the literature state machine, the query planner, the
    #: curator, the relation reviewer, the reading planner) is deterministic
    #: glue: it must NOT cost a model turn, both because it needs none and
    #: because a pause in glue would cascade into one order per nesting level.
    THINKING_AGENTS = ("research", "organization", "scheduler", "read")

    def _hands_off(self, agent_id: str) -> bool:
        """Whether this agent's turns come from an external model.

        Matching is by *module*, so configuring ``read`` also hands off its
        ``read.paper`` subagent.  A subagent that kept a rule policy while its
        parent waited for an external turn would answer ``needs_agent`` instead
        of pausing, and the run would sit on a pending file forever.
        """
        if self.handoff is None:
            return False
        # The DSH Mode's Lead owns orchestration. Director remains only as a
        # compatibility facade around deterministic workflow tools; handing it
        # off would recreate a second, model-driven Lead.
        if agent_id.split(".")[0] == "director":
            return False
        enabled = self.config.handoff_agents or list(self.THINKING_AGENTS)
        # Two spellings are accepted, and the difference matters:
        #
        #   "organization"                  -> the whole module, including its
        #                                      deterministic write-back tools;
        #   "organization.relation_reviewer" -> only that sub-agent.
        #
        # The second form exists because Organization mixes both kinds of work:
        # writing ReadingResults is deterministic and must stay off the model,
        # while judging semantic relations genuinely needs judgement. Naming the
        # sub-agent lets a deployment turn on the judgement without paying a
        # model round-trip for every write-back.
        if agent_id in enabled:
            return True
        return agent_id.split(".")[0] in enabled

    def _policy_for(self, agent_id: str, default: Policy) -> Policy:
        """Only thinking roles get an external turn; glue keeps its rule policy."""
        if self._hands_off(agent_id):
            return HandoffPolicy(self.handoff, agent_id=agent_id,
                                 session=self.sessions, agent_key=agent_id,
                                 ledger=self.ledger)
        return default

    def _compose_factory(self, user_factory):
        """Handoff wins over any injected factory; otherwise defer to it."""
        def factory(agent_id: str, default_name: str):
            if self._hands_off(agent_id):
                return HandoffPolicy(self.handoff, agent_id=agent_id,
                                     session=self.sessions, agent_key=agent_id,
                                     ledger=self.ledger)
            if user_factory is not None:
                return user_factory(agent_id, default_name)
            return None
        return factory

    def _build(self) -> Agents:
        research = build_research_agent(self.kit, agent_id="research")
        organization = build_organization_agent(self.kit, agent_id="organization")
        scheduler = build_scheduler_agent(self.kit, agent_id="scheduler")
        read = build_read_agent(self.kit, agent_id="read")
        literature = build_literature_supervisor(
            self.kit, run_id=self.run_id + "-literature",
            organization=organization, scheduler=scheduler, read=read)
        director = build_director(self.kit, run_id=self.run_id, research=research,
                                  literature=literature,
                                  organization=organization)
        for agent in (research, organization, scheduler, read, literature, director):
            self.kit.register_agent(agent)
        # The Scheduler is a coordinator: it must remember its own judgements
        # across cycles.  Attaching a session here (not in handoff mode) is what
        # turns it from a per-call function into an agent.
        scheduler.persistent_session = self.sessions
        # Research needs the same treatment, for a different reason: scheme 2
        # makes it plan keywords BEFORE retrieving, so one research phase spans
        # several work orders (plan -> search -> conclude). Without a persistent
        # session the conversation is cleared as soon as it returns a payload,
        # so the next order starts from scratch and re-issues the search it just
        # ran -- the phase loops and never advances. `read` is deliberately left
        # per-order: each reading is an isolated turn by design.
        research.persistent_session = self.sessions
        return Agents(research=research, organization=organization,
                      scheduler=scheduler, read=read, literature=literature,
                      director=director)

    # -- public surface ---------------------------------------------------
    def start(self, research_question: str, *,
              search_options: Optional[dict] = None):
        # Lead/controller is local and deterministic, so it initializes the
        # state itself and immediately delegates to Research. The old seed was
        # only needed when an external Director had to answer first; retaining
        # it would leave the controller in awaiting_external_agent before it
        # can publish the Research order.
        return self._invoke("start_research", {
            "research_question": research_question,
            "search_options": dict(search_options or {})})

    def _seed(self, research_question: str, search_options: dict) -> None:
        """Record the task before publishing the first work order.

        Handing a job to an external agent means the job is now in progress,
        not that the run is still uninitialised.  The Director's first
        external turn therefore starts from a recorded question.
        """
        state = self.store.load()
        if state.get("status") != "created":
            return
        state.update({
            "schema_version": "3.0", "status": "awaiting_external_agent",
            "phase": "research", "research_question": research_question.strip(),
            "research_manifest": None, "research_output": None,
            "literature_run_id": self.run_id + "-literature",
            "literature_status": None, "literature_initialized": False,
            "current_stage": None, "final_artifacts": {}, "artifact_paths": {},
            "pending_final_task": None, "final_artifact": None,
            "revision_count": 0, "search_options": dict(search_options),
        })
        self.store.save(state, actor="system")
        self.store.log.append("task_seeded",
                              {"research_question": research_question},
                              actor="system")

    def run(self, *, max_steps: Optional[int] = None):
        arguments = {"max_steps": max_steps} if max_steps else {}
        return self._invoke("run_to_completion", arguments)

    def advance(self):
        if self.handoff is not None:
            # The top-level controller only calls the supervisor; it never
            # consumes replies.  A literature loop parked at a gate (relation
            # review) whose answer has arrived must be woken here, or nothing
            # moves it and the run reports an idle "replanning".
            literature = self.kit.agent_index.get("literature")
            if literature is not None:
                self._wake_literature(literature)
        return self._invoke("advance_phase", {})

    def status(self) -> dict:
        return ok(self.store.load())

    def recover_failed_literature(self) -> dict:
        """Reopen a parent failure when the literature checkpoint is healthy.

        The parent controller used to make a curator contract rejection
        terminal even though the literature store still held a resumable
        ``writing_back``/``awaiting_external_agent`` checkpoint.  Recovery is
        deliberately narrow: it never rewrites literature state, results or
        the knowledge base, and refuses genuinely failed child checkpoints.
        """
        current = self.store.load()
        if current.get("status") != "failed" or current.get("phase") != "literature":
            return failed(error("SYSTEM_RECOVERY_NOT_APPLICABLE",
                                "当前运行不是可恢复的文献阶段失败"))
        literature_store = self._literature_store()
        literature = literature_store.load()
        if literature.get("status") == "failed":
            child_error = literature.get("last_error") or {}
            location = str(child_error.get("location") or "")
            recoverable_relation_error = (
                child_error.get("code") in {
                    "E_INPUT_MISSING_FIELD", "E_INPUT_INVALID_ENUM",
                    "E_INPUT_INVALID_VALUE",
                }
                and "semantic." in location
                and bool(literature.get("pending_tasks"))
            )
            if not recoverable_relation_error:
                return failed(error("SYSTEM_RECOVERY_CHILD_FAILED",
                                    "文献子流程本身已失败，不能自动重开"))
            # Reading results are already cached by task id.  Re-enter the
            # normal tasks_ready path so the engine reuses those files and
            # retries only Organization write-back with the corrected schema.
            literature["status"] = "tasks_ready"
            literature.pop("last_error", None)
            literature_store.save(literature, actor="lead_recovery")
            literature_store.log.append("recoverable_writeback_reopened", {
                "previous_error": child_error,
                "pending_tasks": [item.get("task_id") for item in
                                  literature.get("pending_tasks") or []],
            }, actor="lead_recovery")
        previous = current.pop("last_error", None)
        current["status"] = "literature_running"
        current["literature_status"] = {
            "status": literature.get("status"),
            "cycle": literature.get("cycle", 0),
        }
        self.store.save(current, actor="lead")
        self.store.log.append("literature_failure_reopened", {
            "previous_error": previous,
            "checkpoint_status": literature.get("status"),
        }, actor="lead")
        return ok(current, warnings=["已从文献检查点重开父级失败"])

    def submit_final_result(self, stage: str, payload: dict):
        return self._invoke("submit_final_result",
                            {"stage": stage, "payload": payload})

    def submit_reading_result(self, task_id: str, result: dict):
        return self.agents.literature.tools.call("submit_reading_result",
                                                 {"task_id": task_id,
                                                  "result": result})

    def submit_relation_review(self, review: dict):
        return self.agents.literature.tools.call("submit_relation_review",
                                                 {"review": review})

    def resume(self) -> dict:
        """One idempotent step of the handoff loop.

        The loop is deliberately *single-step and idempotent* because two
        timelines meet here: the supervisor's state machine and the external
        agents' answers.  Each call does exactly one of:

        1. consume every reply that is ready (driving those agents);
        2. advance the literature state machine one transition;
        3. publish at most one new work order.

        Calling it again immediately is safe: if nothing is ready and the
        machine cannot move, nothing happens.
        """
        if self.handoff is None:
            return failed(error("SYSTEM_NOT_IN_HANDOFF_MODE",
                                "当前配置不是 handoff 模式"))
        handled = self._consume_replies()
        literature = self.kit.agent_index.get("literature")
        advanced = None
        if literature is not None:
            self._wake_literature(literature)
            if handled:
                # One step is one transition.  A consumed answer can leave the
                # loop packed (tasks_ready) with its next order already
                # published; advancing again in the same step made that look
                # idle and the orchestrator stopped there.  Only step when this
                # call did not itself consume an answer.
                state = self._literature_store().load()
                advanced = {"status": state.get("status"), "ok": True}
            else:
                value = literature.tools.call("advance_literature", {})
                advanced = {"status": value.get("status"),
                            "ok": value.get("ok")}
        current = self.store.load()
        if handled and current.get("status") == "awaiting_external_agent" \
                and current.get("phase") == "research":
            current["status"] = "research_running"
            self.store.save(current, actor="lead")
        # The Lead now owns top-level progression. After applying subordinate
        # replies, run the deterministic controller once so Research completion,
        # literature termination, and finalization entry are reflected in the
        # parent state without asking a duplicate Director model.
        top = self.advance()
        orders = self.handoff.pending()
        return ok({"state": self.store.load(), "handled": handled,
                   "advanced": advanced,
                   "top_level": {"status": top.get("status"),
                                  "ok": top.get("ok")},
                   "orders": [{"order_id": row["order_id"],
                               "agent_id": row["agent_id"]}
                              for row in orders],
                   "handoff_dir": self.handoff.root},
                  warnings=([] if not orders else
                            ["仍有 {} 个待处理工单".format(len(orders))]))

    def _consume_replies(self) -> list:
        """Drive every agent that has an answer waiting.  Deepest first."""
        handled = []
        for _ in range(200):
            ready = [row for row in self.handoff.pending()
                     if self.handoff.has_reply(row["order_id"])]
            if not ready:
                break
            ready.sort(key=lambda row: (-row["agent_id"].count("."),
                                        row["seq"]))
            row = ready[0]
            tool = (row.get("metadata") or {}).get("tool")
            if tool == "propose_research_directions":
                # Ideation is a one-shot answer, not a conversation: claim the
                # reply and hand it to the ideation path directly.  Driving the
                # agent here would ask it for a *new* decision and publish
                # another order instead of applying the answer.
                reply = self.handoff.claim(row["order_id"])
                literature = self.kit.agent_index.get("literature")
                if reply is not None and literature is not None:
                    payload = reply.get("payload")
                    if isinstance(payload, dict):
                        applied = literature.tools.call(
                            "submit_research_directions", {"payload": payload})
                        if applied.get("ok"):
                            handled.append({"order_id": row["order_id"],
                                            "agent_id": row["agent_id"],
                                            "stopped": "applied"})
                        else:
                            # The order is already claimed, so this answer cannot
                            # be replayed. Dropping the failure used to leave
                            # `ideation` at `pending` with no order outstanding:
                            # the mailbox looked idle and the run parked forever.
                            # Record the verdict so completion can close the
                            # phase — an ideation failure must never cost the
                            # reading that already succeeded.
                            self._mark_ideation_failed(applied)
                            handled.append({"order_id": row["order_id"],
                                            "agent_id": row["agent_id"],
                                            "stopped": "rejected"})
                self._last_handoff_result = True
                continue
            if row["agent_id"] == "organization.relation_reviewer":
                # The gate's answer arrives as a one-shot payload, not as a
                # conversation the reviewer should finish.  Applying it through
                # ``submit_relation_review`` is the only path that runs the gate
                # (organisation write + ideation + phase close); re-driving the
                # reviewer would just re-answer and leave the phase pending.
                reply = self.handoff.claim(row["order_id"])
                literature = self.kit.agent_index.get("literature")
                payload = (reply or {}).get("payload")
                if literature is not None and isinstance(payload, dict):
                    applied = literature.tools.call("submit_relation_review",
                                                    {"review": payload})
                    if applied.get("ok"):
                        handled.append({"order_id": row["order_id"],
                                        "agent_id": row["agent_id"],
                                        "stopped": "applied"})
                    else:
                        # The answer is already claimed, so it cannot be replayed.
                        # Silently dropping the failure parked the run at
                        # `replanning` with a `pending` review and an empty outbox
                        # — indistinguishable from healthy work from outside.
                        # Re-open the gate so a fresh order is published, and give
                        # up loudly after a bounded number of bad answers.
                        self._reopen_relation_review(applied)
                        handled.append({"order_id": row["order_id"],
                                        "agent_id": row["agent_id"],
                                        "stopped": "rejected"})
                self._last_handoff_result = True
                continue
            agent = self.kit.agent_index.get(row["agent_id"])
            if agent is None:
                break
            context = self.kit.context(row["agent_id"])
            # The order carries the metadata the agent was originally asked
            # with (`metadata.initial`: the tool arguments, run id, search
            # options). Driving the agent with an EMPTY metadata dict makes a
            # rule policy lose its original arguments, so it re-issues the very
            # tool call it just got a result for -- research loops forever and
            # the phase never advances. Forward what the order recorded.
            replay_metadata = dict(row.get("metadata") or {})
            # The routed argument dict is the authoritative record of *this*
            # request.  The order also flattens its arguments into metadata, so
            # an older ``arguments`` key from a previous turn can survive
            # alongside them; drop it and let ``initial.arguments`` (written by
            # agent_tool on every delegation) decide, or keep the flat form.
            initial = replay_metadata.get("initial") or {}
            if isinstance(initial.get("arguments"), dict):
                replay_metadata.pop("arguments", None)
            # A decision turn is answered by a *payload*; if this order is a
            # delegation of one capability, the answer must be routed with that
            # capability's arguments or the module re-asks itself.
            if replay_metadata.get("order_kind") in ("decision", "knowledge") \
                    and replay_metadata.get("title", "").startswith(
                        row["agent_id"] + ": "):
                replay_metadata.pop("tool", None)
            # Keep `metadata.tool`: `HandoffPolicy.respond` uses its presence to
            # decide whether this is a *delegation* (idempotent, recorded in the
            # AnswerLedger so a parent's `_ask` can retrieve it) or a *decision*
            # turn (always fresh). Dropping it made every delegation look like a
            # decision, so the answer never reached the ledger and the parent
            # director re-asked forever.
            outcome = agent.one_shot(context, "", metadata=replay_metadata)
            handled.append({"order_id": row["order_id"],
                            "agent_id": row["agent_id"],
                            "stopped": outcome.stopped_because})
            # A paused turn is NOT an answer.  `one_shot` returns
            # `{"awaiting_external_agent": True, "via": ...}` whenever the agent
            # published another order instead of finishing, and persisting that
            # placeholder as a reading poisons the result cache: the write-back
            # then fails contract validation on `paper_id` / `reading_depth` /
            # `generated_by`, which is exactly how a transient pause used to end
            # a whole research run.
            paused = outcome.stopped_because == "awaiting_external_agent"
            if (row.get("agent_id") == "read" and not paused
                    and isinstance(outcome.payload, dict)):
                # The reader answered an order.  Persist the reading against its
                # task now: the dispatch path re-checks this cache, so a retry
                # after a pause reuses the answer instead of asking again.
                # The task id lives in the order's metadata: either flat (when
                # the supervisor passed it explicitly) or under ``task`` (when
                # the whole ReadingTask was forwarded).
                metadata = row.get("metadata") or {}
                task_id = (outcome.payload.get("reading_task_id")
                           or metadata.get("task_id")
                           or (metadata.get("task") or {}).get("task_id"))
                if not task_id:
                    try:
                        task_id = (json.loads(row.get("question") or "{}")
                                   .get("task") or {}).get("task_id")
                    except (ValueError, AttributeError):
                        task_id = None
                if task_id:
                    child = self._literature_store()
                    if child is not None:
                        child.write_result(task_id, outcome.payload)
            # Any consumed answer unblocks the loop: the waiting agent may be
            # Organization (import / write-back / review) rather than the
            # Scheduler.  Recording the *decision* is separate and only happens
            # when the payload actually looks like one.
            self._last_handoff_result = True
            self._record_scheduler_decision(outcome.payload)
            if outcome.stopped_because == "awaiting_external_agent":
                break
        return handled

    #: How many malformed reviewer answers are tolerated before the run fails.
    REVIEW_ATTEMPTS = 3

    def _reopen_relation_review(self, outcome: dict) -> None:
        """Retry a rejected review answer, or fail the run visibly.

        `_begin_relation_review` re-publishes the order whenever the stored
        review is not `completed`, so clearing the status back to `not_started`
        is all a retry needs. After `REVIEW_ATTEMPTS` the gate fails the run
        with the last error instead of looping forever.
        """
        child = self._literature_store()
        if child is None:
            return
        state = child.load()
        review = state.setdefault("relation_review", {})
        attempts = int(review.get("failed_attempts") or 0) + 1
        review["failed_attempts"] = attempts
        review["last_error"] = outcome.get("error") or {
            "message": "relation review rejected"}
        if attempts >= self.REVIEW_ATTEMPTS:
            review["status"] = "failed"
            state["status"] = "failed"
            state["last_error"] = review["last_error"]
            child.log.append("relation_review_failed",
                             {"attempts": attempts,
                              "error": review["last_error"]}, actor="system")
        else:
            # Not `completed` and not `pending`: the planner will ask again.
            review["status"] = "not_started"
            child.log.append("relation_review_retry",
                             {"attempt": attempts,
                              "error": review["last_error"]}, actor="system")
        child.save(state, actor="system")

    def _mark_ideation_failed(self, outcome: dict) -> None:
        """Record a rejected ideation answer so the phase can still close.

        `_complete` treats `ok` / `unavailable` / `failed` as finished and only
        `pending` as "waiting for an order". Turning a rejected submission into
        `failed` is what stops a bad answer from becoming a permanent stall.
        """
        child = self._literature_store()
        if child is None:
            return
        state = child.load()
        state["ideation"] = {
            "status": "failed",
            "error": outcome.get("error") or {"message": "ideation rejected"},
        }
        child.save(state, actor="system")
        child.log.append("ideation_submission_rejected",
                         {"error": outcome.get("error")}, actor="system")

    def _record_scheduler_decision(self, payload) -> None:
        """Write a consumed Scheduler answer into the literature state.

        This is the join point between two timelines: the external agent has
        answered, and the state machine needs that answer as data.  Writing it
        here (rather than letting planning re-derive it) is what makes the
        handoff loop terminate.
        """
        if not isinstance(payload, dict):
            return
        selected = payload.get("selected")
        if not isinstance(selected, list):
            return
        child = self._literature_store()
        if child is None:
            return
        state = child.load()
        # The decision is recorded *after* the agent answered, so the planner
        # must assemble it once, not ask again: store it as consumed (the
        # supervisor sets ``assembled`` at its single assembly point).  A
        # ``consumed=False`` entry made the planner take the "not assembled"
        # branch, which assembled against an empty situation -- the snapshot id
        # then vanished and every selected paper was rejected as 不在快照中.
        # Use the *current* cycle, not whatever the caller last saw: the
        # decision belongs to the cycle that is about to be planned.
        state["pending_decision"] = {
            "cycle": state.get("cycle"),
            "selected": [str(item) for item in selected],
            "reasons": {str(key): str(value) for key, value in
                        (payload.get("reasons") or {}).items()},
            "note": str(payload.get("note") or ""),
            "reader_groups": payload.get("reader_groups") or [],
            "situation": {},
            "consumed": True,
            # Answered, not yet assembled: the supervisor assembles at its
            # single assembly point and sets ``assembled``.  Writing
            # ``consumed=False`` here made the planner answer the agent again.
            "assembled": False,
            "source": "handoff",
        }
        child.save(state, actor="system")

    def _wake_literature(self, literature) -> None:
        """Move the literature loop from ``awaiting_external_agent`` to planning.

        Woken when either an answer is waiting *or* an agent has just consumed
        one and produced a result.  Checking only the mailbox is not enough:
        by the time we look, the reply has been claimed and the mailbox is
        empty again, so the loop would stay parked forever.
        """
        child = self._literature_store()
        if child is None:
            return
        state = child.load()
        pending = self.handoff.pending()
        # A gate pause is recognised by the review it already holds, not by the
        # order's metadata (the reviewer's own policy publishes that order, so
        # the metadata carries no ``tool``).
        gate_pending = (state.get("relation_review") or {}).get(
            "status") == "pending"
        if state.get("status") == "replanning" and not gate_pending:
            # A gate pause is the exception: the review left the loop in
            # ``replanning`` with nothing to plan, so its answer must still wake
            # it.  Any other ``replanning`` is advanced by ``step`` itself.
            return
        if state.get("status") not in ("awaiting_external_agent", "replanning"):
            return
        if state.get("status") == "replanning":
            # (Gate-pause re-entry handled above.)  ``replanning`` normally
            # means "about to ask the Scheduler".  But
            # the loop can also be here with no decision to assemble -- e.g. a
            # gate pause (relation review) whose answer has now arrived, or a
            # consumed reply woken a previous step.  Storing ``replanning`` on
            # wake made the loop reachable but unadvanceable: nothing moves it
            # forward without a pending decision, so the run stalled silently.
            # Leaving the status as it was lets ``step`` see the real condition
            # (a pending review, packed tasks, or planning) and take the right
            # transition.  Nothing is discarded: ``_last_handoff_result`` stays
            # set until a transition consumes it.
            return
        waiting = any(self.handoff.has_reply(row["order_id"])
                      for row in self.handoff.pending())
        answered = bool(self._last_handoff_result)
        if not (waiting or answered):
            return
        state["status"] = "replanning"
        child.save(state, actor="system")
        self._last_handoff_result = False

    def _literature_store(self):
        from .workflow.state import RunStore
        path = os.path.join(self.config.runs_dir, self.run_id + "-literature")
        if not os.path.isdir(path):
            return None
        return RunStore(self.config.runs_dir, self.run_id + "-literature")

    def _invoke(self, tool: str, arguments: dict) -> dict:
        context = self.kit.context(self.agents.director.id)
        result = self.agents.director.one_shot(
            context, _director_seed(self.store.load(), tool, arguments),
            metadata={"tool": tool, "arguments": arguments})
        if result.stopped_because == "awaiting_external_agent":
            return self._pause(result)
        if result.payload is None:
            return failed(error("SYSTEM_NO_PAYLOAD",
                                "Director 未返回结果（{}）".format(
                                    result.stopped_because),
                                hint="检查 policy 与工具返回值"))
        value = result.payload
        if not isinstance(value, dict) or "status" not in value:
            return failed(error("SYSTEM_BAD_PAYLOAD", "Director 返回了非信封结果"))
        value.setdefault("diagnostics", {})
        value["diagnostics"].setdefault("agent_trace", result.trace_path)
        return value

    def _pause(self, result) -> dict:
        """Return a restartable envelope describing what the host must answer."""
        orders = self.handoff.pending() if self.handoff else []
        return ok({"state": self.store.load(),
                   "awaiting_external_agent": True,
                   "orders": [{"order_id": row["order_id"],
                               "agent_id": row["agent_id"],
                               "role": row.get("role")} for row in orders],
                   "handoff_dir": self.handoff.root if self.handoff else None},
                  warnings=["等待外部 Agent 回合：{} 个待处理工单".format(
                      len(orders))],
                  diagnostics={"agent_trace": result.trace_path,
                               "mode": self.config.mode})

    def actors(self) -> list:
        return self.store.actors_seen()


def _director_seed(state: dict, tool: str, arguments: dict) -> str:
    """Human-readable seed message for the Director's first external turn.

    This lands in the work order's ``messages``, so the host agent's LLM sees a
    concrete task instead of an empty user turn.
    """
    import json
    question = state.get("research_question") or arguments.get("research_question") or ""
    phase = state.get("phase") or "research"
    status = state.get("status") or "created"
    return json.dumps({
        "task": "advance the research run",
        "requested_tool": tool,
        "research_question": question,
        "phase": phase,
        "status": status,
        # Whether a corpus already exists is the one fact that decides between
        # "start the research phase" and "advance the current one". Omitting it
        # left a host unable to tell those apart and re-asking to start.
        "research_output": state.get("research_output"),
        "literature": state.get("literature_status"),
        "literature_status": state.get("literature_status"),
        "current_stage": state.get("current_stage"),
        "artifacts": sorted((state.get("final_artifacts") or {}).keys()),
        "hint": "调用一个工具推进，或在阶段完成时给出 payload",
    }, ensure_ascii=False)
