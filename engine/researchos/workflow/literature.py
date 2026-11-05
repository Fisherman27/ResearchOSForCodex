"""Literature supervisor: the O/S/R closed loop.

Transition table is unchanged.  The difference is that every transition is now
a traced tool call by the ``literature_supervisor`` agent to one of three
sibling agents (Organization / Scheduler / Read), so the coordination claim is
checkable in ``events.jsonl`` and in each agent's ``trace.jsonl``.

State machine:

    created --initialize--> planning
    planning/replanning --scheduler.next_reading_tasks--> tasks_ready | gate
    tasks_ready --read--> reading --organization.apply--> replanning
    (no tasks / exhausted) --organization.review_claim_relations--> completed
    no reader agent --> awaiting_reader (pending packages written)
    no organization agent at gate --> awaiting_relation_review
"""
from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, List, Optional

from ..agents.base import AgentKit, agent_tool
from ..runtime.agent import Agent
from ..runtime.envelope import (error, failed, is_external_pause, ok,
                                partial)

# One implementation, imported under the historical name.
_is_external_pause = is_external_pause
from ..ports.ideation import (filter_proposals, validate_proposals,
                              write_agenda)
from ..ports.sufficiency import assess, compare
from ..runtime.machine import Decision, RouterPolicy
from ..runtime.trace import now
from .state import literature_initial_state


ACTIVE = ("planning", "replanning", "tasks_ready", "reading", "writing_back")
GATE = "awaiting_relation_review"
DONE = ("completed", "blocked_material")


def build_literature_supervisor(kit: AgentKit, *, run_id: str,
                                organization: Agent, scheduler: Agent,
                                read: Agent) -> Agent:
    # The literature loop keeps its own state file, exactly as the
    # ``<run>-literature`` child run does.  Sharing the Director's store would
    # merge two state machines into one file.
    from .state import RunStore
    store = RunStore(kit.config.runs_dir, run_id)
    agent_id = "literature"

    # -- tools: siblings are tools of the supervisor ---------------------
    def get_state():
        return ok(_load())

    supervisor_proxy = {"registry": None}

    def dispatch(agent: Agent, tool_name: str, arguments: dict,
                 *, note: str = "") -> dict:
        """Ask a subordinate agent to use one of *its* capabilities.

        The supervisor does not reach into a sibling's registry; it calls the
        agent-tool that represents that sibling and names the capability via
        ``__tool__``.  The subordinate therefore appears as a nested agent with
        its own trace, and swapping its policy changes behaviour without the
        supervisor knowing.
        """
        envelope_to = {"organization": "ask_organization",
                       "scheduler": "ask_scheduler",
                       "read": "ask_reader"}[agent.id.split(".")[0]]
        payload = dict(arguments)
        payload["__tool__"] = tool_name
        value = supervisor_proxy["registry"].call(envelope_to, payload)
        store.log.append("supervisor_dispatch", {
            "note": note, "target_agent": agent.id, "target_tool": tool_name,
            "via": envelope_to,
            "arguments": {key: item for key, item in arguments.items()
                          if key != "__tool__"},
            "status": value.get("status"), "ok": value.get("ok"),
            "child_trace": (value.get("meta") or {}).get("child_trace")},
            actor=agent_id)
        return value

    def initialize(search_output: str = "", research_goal: str = ""):
        state = _load()
        if state["status"] != "created":
            return failed(error("SYSTEM_ALREADY_INITIALIZED",
                                "run 已初始化，使用 run 或创建新的 run_id"))
        state["search_output"] = search_output
        state["research_goal"] = str(research_goal or "").strip()
        store.save(state, actor=agent_id)
        store.log.append("initialization_started",
                         {"search_output": search_output}, actor=agent_id)
        imported = dispatch(organization, "import_search_output",
                            {"search_output": search_output},
                            note="导入检索语料")
        if not imported["ok"]:
            if (imported.get("error") or {}).get("code") == \
                    "E2_AWAITING_EXTERNAL_AGENT":
                # The Organization agent is external, so importing is itself a
                # pause.  Marking the run failed here would make an ordinary
                # handoff look like a crash.
                state["status"] = "awaiting_external_agent"
                store.save(state, actor=agent_id)
                return ok(state, warnings=["等待 Organization 的外部回合"])
            return _fail_state(state, "organization_initialize_failed", imported)
        data = imported["data"]
        state.update({"knowledge_snapshot": data["snapshot_id"],
                      "graph_version": (data.get("graph_version") or {}).get(
                          "version_id"),
                      # Set in the SAME save as the snapshot it describes. Two
                      # consecutive saves left a window where an interruption
                      # persisted the snapshot without the flag, and `step` then
                      # re-ran the import forever.
                      "organization_imported": True,
                      "status": "planning"})
        store.save(state, actor=agent_id)
        store.log.append("organization_initialized", data, actor=agent_id)
        return _plan(state)

    def _load() -> dict:
        return store.load(initial=literature_initial_state(run_id))

    def _plan(state: dict) -> dict:
        """Ask the Scheduler to decide the next batch.

        Planning always starts from the *stored* state.  A caller may hold a
        snapshot from before a write-back, and planning against it would compute
        the situation from stale reading depths -- which is how a run scheduled
        an already-read paper a second time.

        The Scheduler is a persistent agent: it keeps its conversation across
        cycles, so it can remember what it already judged and revise it.  The
        supervisor's job here is only to hand it the current facts and record
        its decision -- not to decide for it.
        """
        state = _load()
        # 1. Read the situation *from the knowledge base*, in-process.  This is
        #    a local query, not a model turn: asking the Scheduler agent to fetch
        #    it would spend an external round trip on a lookup, and in handoff
        #    mode it would publish a work order that carries no decision.
        # Idempotency: a supervisor may call ``advance_literature`` more than
        # once in ``replanning`` (resume loops do).  Planning must not re-ask
        # the agent each time, or a single cycle would consume several external
        # turns and record duplicate decisions.
        cached = state.get("pending_decision")
        if cached and cached.get("cycle") == state.get("cycle"):
            if not cached.get("assembled"):
                # An empty external Scheduler selection is the explicit stop
                # signal, exactly like an inline decision below.  Routing it
                # through `_plan_tasks` treated it as a bad material choice;
                # because readable papers still existed, the engine discarded
                # the stop decision and re-asked Scheduler forever.
                if not cached.get("selected"):
                    cached["assembled"] = True
                    cached["consumed"] = True
                    state["pending_decision"] = cached
                    state.setdefault("scheduler_decisions", []).append({
                        "cycle": cached.get("cycle"),
                        "selected": [],
                        "reasons": cached.get("reasons") or {},
                        "note": cached.get("note", ""),
                        "counts": {},
                        "source": cached.get("source", "handoff"),
                    })
                    store.log.append("scheduler_decided", {
                        "cycle": cached.get("cycle"),
                        "selected": [],
                        "reasons": cached.get("reasons") or {},
                        "note": cached.get("note", ""),
                        "source": cached.get("source", "handoff"),
                    }, actor=agent_id)
                    store.save(state, actor=agent_id)
                    unavailable = [
                        paper_id for paper_id, item in
                        (_situation_now(kit, state).get("data") or {})
                        .get("papers", {}).items()
                        if item.get("unread") and
                        item.get("pdf_available") is False
                    ]
                    terminal = ("blocked_material" if unavailable
                                and not state.get("completed_paper_ids")
                                else "completed")
                    return _begin_relation_review(
                        state, terminal_status=terminal,
                        unavailable=unavailable,
                        warnings=["Scheduler 判断当前证据覆盖已足够"])
                # The answer was already consumed from the mailbox and stored
                # here.  Assemble it exactly once; asking the agent again would
                # publish a duplicate order.  ``assembled`` (not ``consumed``)
                # marks this, because ``consumed`` means "the agent has answered"
                # and is set before assembly has happened.
                cached["assembled"] = True
                # The audit trail is written here for handoff answers, because
                # their decision arrived from outside this function.  Without
                # this the run would have decisions in ``pending_decision`` but
                # an empty ``scheduler_decisions`` history -- and "why did it
                # read this?" would be unanswerable.
                state.setdefault("scheduler_decisions", []).append({
                    "cycle": cached.get("cycle"),
                    "selected": cached.get("selected") or [],
                    "reasons": cached.get("reasons") or {},
                    "note": cached.get("note", ""),
                    "counts": {},
                    "source": cached.get("source", "handoff"),
                })
                store.log.append("scheduler_decided", {
                    "cycle": cached.get("cycle"),
                    "selected": cached.get("selected") or [],
                    "reasons": cached.get("reasons") or {},
                    "note": cached.get("note", ""),
                    "source": cached.get("source", "handoff")}, actor=agent_id)
                store.save(state, actor=agent_id)
                # Pass the situation this decision was made against, not an
                # empty stub: assembly reads the snapshot from it, and a stub
                # turns the whole snapshot into None (AttributeError on resume).
                return _plan_tasks(state, cached.get("selected") or [],
                                   cached.get("reasons") or {},
                                   cached.get("reader_groups") or [],
                                   _situation_for_assembly(kit, cached),
                                   reading_depths=cached.get("reading_depths") or {})
            if cached.get("consumed"):
                # This cycle has already been planned and assembled.  Re-read
                # the state rather than returning the stale object the caller
                # passed in: assembly has already moved it to ``tasks_ready``,
                # and returning the old snapshot makes the caller think nothing
                # happened.
                current = _load()
                if current.get("status") == "tasks_ready":
                    return _read_and_commit(current)
                # The decision produced nothing durable — its tasks were dropped
                # (missing material) or the write-back fell through. Keeping it
                # makes every later plan short-circuit here, so the cycle could
                # never ask again and the run parked forever at `replanning`.
                # A consumed decision that yielded no work is spent: drop it so
                # planning asks a fresh question.
                current.pop("pending_decision", None)
                store.save(current, actor=agent_id)
                store.log.append("stale_decision_dropped", {
                    "cycle": current.get("cycle")}, actor=agent_id)
                return _plan(current)
            # A decision was made but not yet assembled (a resume can re-enter
            # planning between the two steps): assemble it now.  ``assembled``
            # is set *before* assembly, so an interruption inside assembly is
            # retried below rather than silently dropping the decision.
            state["pending_decision"]["consumed"] = True
            state["pending_decision"]["assembled"] = True
            store.save(state, actor=agent_id)
            return _plan_tasks(state, cached.get("selected") or [],
                               cached.get("reasons") or {},
                               cached.get("reader_groups") or [],
                               _situation_for_assembly(kit, cached),
                               reading_depths=cached.get("reading_depths") or {})

        situation = _situation_now(kit, state)
        if not situation["ok"]:
            return _fail_state(state, "planning_failed", situation)

        # 2. Give the agent a turn to decide.  It sees its own prior turns plus
        #    the situation, and answers with a selection and reasons.
        #    In handoff mode this publishes a work order and pauses; the same
        #    order must not be republished on every resume, so check first.
        # Only an *unanswered* order means "still waiting".  Once a reply is in
        # the inbox the decision can proceed; treating any outstanding file as
        # "waiting" would deadlock, because the file stays until it is claimed.
        pending_orders = kit.handoff.pending() if getattr(kit, "handoff", None) else []
        waiting = [row for row in pending_orders
                   if row.get("agent_id") in ("scheduler", "scheduler.planner")
                   and (row.get("metadata") or {}).get("tool")
                   in (None, "", "next_reading_tasks")
                   and not kit.handoff.has_reply(row["order_id"])]
        if waiting:
            state["status"] = "awaiting_external_agent"
            store.save(state, actor=agent_id)
            return ok(state, warnings=["已有待处理的 Scheduler 工单"])
        store.log.append("scheduler_situation", {
            "cycle": state.get("cycle"),
            "counts": situation["data"].get("counts")}, actor=agent_id)
        decision = _decide_reading(kit, state, situation["data"])
        if not decision["ok"]:
            if (decision.get("error") or {}).get("code") == \
                    "SCHEDULER_AWAITING_EXTERNAL":
                # In handoff mode the Scheduler answered by publishing a work
                # order.  That is a pause, not a failure: returning it as an
                # error would mark the whole literature run failed while an
                # agent is merely waiting.
                state["status"] = "awaiting_external_agent"
                store.save(state, actor=agent_id)
                return ok(state, warnings=["等待 Scheduler 的外部决策"])
            return _fail_state(state, "planning_failed", decision)
        selected = decision["data"]["selected"]
        reasons = decision["data"]["reasons"]
        reader_groups = decision["data"]["reader_groups"]
        reading_depths = decision["data"].get("reading_depths") or {}
        snapshot_of_situation = situation["data"]
        state["pending_decision"] = {
            "cycle": state["cycle"], "selected": selected,
            "reasons": reasons, "reader_groups": reader_groups,
            "reading_depths": reading_depths,
            "situation": snapshot_of_situation}
        state.setdefault("scheduler_decisions", []).append({
            "cycle": state["cycle"],
            "selected": selected,
            "reasons": reasons,
            "reader_groups": reader_groups,
            "reading_depths": reading_depths,
            "note": decision["data"].get("note", ""),
            "counts": snapshot_of_situation.get("counts") or {},
        })
        store.log.append("scheduler_decided", {
            "cycle": state["cycle"], "selected": selected,
            "reasons": reasons,
            "note": decision["data"].get("note", "")}, actor=agent_id)

        if not selected:
            # The agent decided there is nothing left worth reading.  This is
            # the same signal as an exhausted queue, so it goes through the
            # mandatory relation-review gate rather than looping in replanning.
            unavailable = [
                paper_id for paper_id, item in (situation["data"].get("papers")
                                                or {}).items()
                if item.get("unread") and item.get("pdf_available") is False]
            terminal = "blocked_material" if unavailable else "completed"
            return _begin_relation_review(state, terminal_status=terminal,
                                          unavailable=unavailable,
                                          warnings=["Scheduler 判断没有可读论文"])

        return _plan_tasks(state, selected, reasons, reader_groups, situation,
                           reading_depths=reading_depths)

    def _material_dead_end(state: dict, papers: dict, unreadable: list,
                           *, warnings: list) -> dict:
        """Decide what an unusable selection means.

        Two very different situations hide behind "nothing readable was chosen":

        * the corpus still holds readable unread papers — the model simply
          picked a wrong one, so ask again once rather than blocking a healthy
          run;
        * nothing readable remains — the phase is genuinely out of material.

        Re-planning is bounded: repeating the same dead end blocks the phase, so
        a model that keeps insisting cannot spin the loop forever.
        """
        # The stale decision is why this dead end repeats: `_plan` reuses a
        # cached `pending_decision` instead of asking the Scheduler again, so a
        # decision that picked unreadable material would be re-applied forever.
        # Dropping it is what makes the re-plan an actual new question.
        state.pop("pending_decision", None)
        available = [paper_id for paper_id, item in papers.items()
                     if item.get("unread")
                     and item.get("pdf_available") is not False]
        replanned = int(state.get("material_replans") or 0)
        # Each corrective Scheduler turn receives rejected ids as a hard
        # constraint below. Keep the retry bounded, but do not declare a
        # material shortage after a single bad model choice while readable
        # papers still exist.
        if available and replanned < 3:
            state["material_replans"] = replanned + 1
            state["status"] = "replanning"
            store.save(state, actor=agent_id)
            store.log.append("material_replan", {
                "unreadable": unreadable, "available": len(available),
                "available_paper_ids": available},
                actor=agent_id)
            return ok(state, warnings=list(warnings) + [
                "语料中仍有 {} 篇可读论文，重新规划".format(len(available))])
        terminal = "blocked_material" if unreadable else "completed"
        return _begin_relation_review(state, terminal_status=terminal,
                                      unavailable=unreadable, warnings=warnings)

    def _plan_tasks(state: dict, selected: list, reasons: dict,
                    reader_groups: list,
                    situation: dict, reading_depths: dict = None) -> dict:
        # Work from the stored state, but keep the decision bookkeeping the
        # caller just wrote: ``_load()`` is a fresh object, so a plain
        # reassignment would silently drop ``pending_decision`` and the loop
        # would never know the decision was consumed.
        latest = _load()
        decision = latest.get("pending_decision")
        state = latest
        if decision is not None:
            state["pending_decision"] = decision
        # Material availability is the ENGINE's fact, not the model's. The
        # Scheduler may be an external model and can name a paper the corpus
        # has no PDF for; dispatching that task asks a reader to read nothing,
        # so its answer can never arrive and the loop re-asks the same order
        # forever (observed: 1435 identical blocked `read_paper` calls).
        # Drop unreadable selections here; if nothing readable is left, the
        # phase ends as `blocked_material` instead of spinning.
        papers = (situation.get("data") or {}).get("papers") or {}
        readable, unreadable = [], []
        for paper_id in selected:
            item = papers.get(paper_id) or {}
            (unreadable if item.get("pdf_available") is False else readable).append(
                paper_id)
        if unreadable:
            known = set(state.get("missing_material_paper_ids") or [])
            state["missing_material_paper_ids"] = sorted(known | set(unreadable))
        if not readable:
            return _material_dead_end(state, papers, unreadable,
                                      warnings=["Scheduler 选中的论文没有可读材料"])
        selected = readable
        # 3. Turn the decision into concrete tasks.
        #    Assembly is *deterministic* work, not a decision, so it must run
        #    locally even when the Scheduler is a handoff agent.  Routing this
        #    through the agent would ask an external model to build ReadingTask
        #    objects out of a decision it has already made -- and in handoff
        #    mode it would publish another work order instead of returning tasks.
        planned = _assemble_tasks(scheduler, {
            "selected": selected,
            "reasons": reasons,
            "reader_groups": reader_groups,
            "reading_depths": dict(reading_depths or {}),
            "batch_size": kit.config.batch_size,
            "context_strategy": kit.config.context_strategy,
            "context_budget_tokens": kit.config.context_budget_tokens,
            "reading_strategy_id": kit.config.reading_strategy_id,
            "reread_budget": kit.config.reread_budget,
            "max_rereads_per_paper": kit.config.max_rereads_per_paper,
            "reread_history": dict(state.get("reread_counts") or {}),
        })
        if not planned["ok"]:
            return _fail_state(state, "planning_failed", planned)
        data = planned["data"]
        tasks = data.get("tasks") or []
        # Cap enforcement lives here, not in the Scheduler, because the
        # supervisor owns the authoritative count.  In handoff mode an external
        # LLM can call ``next_reading_tasks`` without ``reread_history``; if the
        # cap were only checked inside the Scheduler, omitting the argument
        # would allow unlimited re-reads.  Dropping excess re-read tasks here
        # makes the limit un-evadable.
        counts = dict(state.get("reread_counts") or {})
        kept, dropped = [], []
        for task in tasks:
            if task.get("task_type") != "reread":
                kept.append(task)
                continue
            done = int((counts.get(task["target_paper"]) or {}).get("count", 0))
            if done >= kit.config.max_rereads_per_paper:
                dropped.append(task["target_paper"])
                continue
            kept.append(task)
        if dropped:
            state.setdefault("reread_rejected", []).append({
                "cycle": state["cycle"], "paper_ids": dropped,
                "reason": "达到单篇重读上限"})
            store.log.append("reread_rejected", {
                "paper_ids": dropped,
                "max_rereads_per_paper": kit.config.max_rereads_per_paper},
                actor=agent_id)
        tasks = kept
        if not tasks:
            unavailable = (data.get("plan") or {}).get("unavailable_material") or []
            # A planner that selects nothing is exhausted, not stuck: the
            # original pipeline has the same rule.  Missing-material papers
            # decide the terminal.
            terminal = "blocked_material" if unavailable else "completed"
            return _begin_relation_review(state, terminal_status=terminal,
                                          unavailable=unavailable,
                                          warnings=planned.get("warnings"))
        state["pending_tasks"] = tasks
        state["knowledge_snapshot"] = data.get("snapshot_id")
        state["status"] = "tasks_ready"
        store.save(state, actor=agent_id)
        store.log.append("tasks_planned", {
            "plan": data.get("plan"),
            "task_ids": [task["task_id"] for task in tasks]}, actor=agent_id)
        return ok(state, warnings=planned.get("warnings"))


    def _read_and_commit(state: dict) -> dict:
        # Always work from the stored state: planning and reading are separate
        # transitions, and a caller may hold a snapshot from before the plan.
        state = _load()
        tasks = state.get("pending_tasks") or []
        if not tasks:
            return ok(state)
        # Material is checked at dispatch, not only at planning: a task that was
        # persisted before this guard existed (or by an older version) would
        # otherwise be re-dispatched forever. `_attachments` is the engine's own
        # definition of what a reader is given, so an empty list means the reader
        # has nothing to read no matter which policy asks it.
        from ..agents.read import _attachments
        readable_tasks, unreadable_ids = [], []
        for task in tasks:
            if _attachments(task):
                readable_tasks.append(task)
            else:
                unreadable_ids.append(task.get("target_paper"))
        if unreadable_ids:
            known = set(state.get("missing_material_paper_ids") or [])
            state["missing_material_paper_ids"] = sorted(
                known | {pid for pid in unreadable_ids if pid})
        if not readable_tasks:
            state["pending_tasks"] = []
            store.save(state, actor=agent_id)
            # Retire the orders already published for these tasks. They can never
            # be answered (the reader has nothing to read), and an outstanding
            # order blocks the host from advancing at all: `step` refuses to move
            # while anything in the mailbox is unanswered. Dropping the task
            # without its order leaves the run wedged.
            _retire_task_orders(kit, [pid for pid in unreadable_ids if pid],
                                [t.get("task_id") for t in tasks if t.get("task_id")])
            situation = _situation_now(kit, state)
            papers = ((situation.get("data") or {}).get("papers") or {}
                      if situation.get("ok") else {})
            return _material_dead_end(
                state, papers, [pid for pid in unreadable_ids if pid],
                warnings=["阅读任务缺少材料，未派发"])
        tasks = readable_tasks
        state["status"] = "reading"
        store.save(state, actor=agent_id)
        results = []
        for task in tasks:
            # A task whose reading already landed (possibly during an earlier
            # attempt that was interrupted) must not be re-dispatched: the
            # reader's answer is on disk, and asking again in handoff mode would
            # publish a second work order for the same paper.
            cached_result = store.read_result(task["task_id"])
            if cached_result is not None:
                results.append((task, cached_result))
                continue
            outcome = dispatch(read, "read_paper", {"task": task},
                               note="阅读 {}（策略 {}）".format(
                                   task["target_paper"],
                                   task.get("reading_strategy_id")))
            if outcome.get("status") == "blocked":
                code = (outcome.get("error") or {}).get("code")
                if code == "E2_AWAITING_EXTERNAL_AGENT":
                    # An external reader is working on it.  Writing pending
                    # packages here would ask a human to redo what an agent is
                    # already doing, and the run would never resume by itself.
                    state["status"] = "awaiting_external_agent"
                    store.save(state, actor=agent_id)
                    return ok(state, warnings=["等待阅读智能体的外部回合"])
                return _prepare_pending(state, outcome)
            if not outcome["ok"]:
                state["failed_tasks"].append({"task_id": task["task_id"],
                                              "error": outcome.get("error")})
                return _fail_state(state, "reading_failed", outcome)
            result = outcome["data"]
            store.write_result(task["task_id"], result)
            results.append((task, result))
        state["status"] = "writing_back"
        store.save(state, actor=agent_id)
        written = dispatch(organization, "apply_reading_results",
                           {"results": [result for _, result in results]},
                           note="写回 {} 篇阅读结果".format(len(results)))
        if not written["ok"]:
            if _is_external_pause(written):
                # Write-back triggers the semantic curator, which runs on an
                # external model. Its first attempt pauses with a work order on
                # disk; the readings are already stored, so this is a wait, not
                # a failed write-back. `resume` drives the curator next cycle.
                state["status"] = "awaiting_external_agent"
                store.save(state, actor=agent_id)
                return ok(state, warnings=["等待语义策展智能体的外部回合"])
            return _fail_state(state, "organization_writeback_failed", written)
        for task, _ in results:
            if task["task_id"] not in state["completed_task_ids"]:
                state["completed_task_ids"].append(task["task_id"])
            if task["target_paper"] not in state["completed_paper_ids"]:
                state["completed_paper_ids"].append(task["target_paper"])
            if task.get("task_type") == "reread":
                counts = state.setdefault("reread_counts", {})
                record = dict(counts.get(task["target_paper"]) or {})
                record["count"] = int(record.get("count", 0)) + 1
                record["signature"] = task.get("deficiency_signature") or []
                record["last_task"] = task["task_id"]
                # Compare the gaps we dispatched against the gaps that remain:
                # this is what distinguishes a productive re-read from a
                # pointless one, and it is recorded rather than inferred later.
                outcome_of_reread = compare(
                    task.get("deficiency") or [],
                    assess(task["target_paper"], result))
                record["last_outcome"] = outcome_of_reread
                if outcome_of_reread["sufficient_after"]:
                    record["state"] = "resolved"
                elif outcome_of_reread["progress"]:
                    record["state"] = "improving"
                else:
                    # No gap closed.  Whether or not the same gaps remain, the
                    # reader is not converging on what it was asked to fix, so
                    # further passes would repeat this outcome.
                    record["state"] = "stalled"
                counts[task["target_paper"]] = record
                state.setdefault("reread_log", []).append({
                    "paper_id": task["target_paper"],
                    "task_id": task["task_id"],
                    "reread_index": task.get("reread_index"),
                    "reading_focus": task.get("reading_focus") or [],
                    "deficiency": task.get("deficiency") or [],
                    "outcome": outcome_of_reread,
                    "snapshot": task.get("knowledge_snapshot"),
                })
                store.log.append("reread_completed", {
                    "paper_id": task["target_paper"],
                    "task_id": task["task_id"],
                    "state": record["state"],
                    "resolved": outcome_of_reread["resolved"],
                    "still_open": outcome_of_reread["still_open"],
                }, actor=agent_id)
        state["pending_tasks"] = []
        state["cycle"] += 1
        # A decision belongs to the cycle that produced it.  Clearing it here is
        # what lets the next cycle ask a fresh question: a leftover decision from
        # the previous cycle would be reused (it is still "not assembled" from
        # the new cycle's point of view) and the same paper would be chosen.
        state.pop("pending_decision", None)
        state["knowledge_snapshot"] = written["data"]["snapshot_id"]
        state["graph_version"] = (written["data"].get("graph_version") or {}).get(
            "version_id")
        state["status"] = "replanning"
        store.save(state, actor=agent_id)
        store.log.append("cycle_committed", {
            "cycle": state["cycle"],
            "snapshot_id": state["knowledge_snapshot"]}, actor=agent_id)
        return ok({"state": state, "written": written["data"]},
                  warnings=written.get("warnings"))

    def _prepare_pending(state: dict, outcome: dict) -> dict:
        """Write one pending package per task and wait."""
        files = []
        for task in state.get("pending_tasks") or []:
            package = kit.ports.read.prepare(task)
            files.append(store.write_pending(task, package.get("data") or {}))
        state["status"] = "awaiting_reader"
        store.save(state, actor=agent_id)
        store.log.append("reader_input_required", {"files": files}, actor=agent_id)
        return ok(state, warnings=["等待 Reader 结果：{}".format(files)])

    def _begin_relation_review(state: dict, *, terminal_status: str,
                               unavailable: List[str],
                               warnings: Optional[List[str]]) -> dict:
        review = state.setdefault("relation_review", {})
        if review.get("status") == "completed":
            return _complete(state, terminal_status, unavailable, warnings)
        review["status"] = "pending"
        review["terminal_status"] = terminal_status
        state["pending_tasks"] = []
        # The Scheduler's empty selection ends the reading loop.  A second
        # resume could already have published another reading-decision order
        # before this gate was persisted; leaving that stale order beside the
        # relation-review order makes the host answer a decision the workflow
        # has explicitly closed.  Withdraw only reading-decision orders.
        if getattr(kit, "handoff", None) is not None:
            for row in list(kit.handoff.pending()):
                metadata = row.get("metadata") or {}
                if (row.get("agent_id") in ("scheduler", "scheduler.planner")
                        and metadata.get("order_kind") == "reading_decision"):
                    kit.handoff.retire(str(row.get("order_id") or ""))
        if unavailable:
            state["missing_material_paper_ids"] = unavailable
        # Persist BEFORE dispatching. `_commit_relation_review` reloads the state
        # from disk (the answer may have arrived while the loop was parked), so an
        # in-memory-only `terminal_status` / `missing_material_paper_ids` is lost
        # and the phase closes as `completed` even when the corpus was blocked on
        # missing material. The inline path hit this every time.
        store.save(state, actor=agent_id)
        reviewed = dispatch(organization, "review_claim_relations", {},
                            note="全语料论点关系审阅（强制关卡）")
        if not reviewed["ok"]:
            if _is_external_pause(reviewed):
                # The reviewer runs on an external model, so the first attempt
                # pauses with its work order already published. That is a wait,
                # not a failure: keep the review pending and let `resume` drive
                # it on the next cycle.  The status stays ``replanning`` so the
                # answering reply can wake the loop: as
                # ``awaiting_external_agent`` the wake path stopped early (it
                # is reserved for decisions and readings), and the phase
                # stalled with nothing to plan.
                store.save(state, actor=agent_id)
                return ok(state, warnings=["等待关系审阅智能体的外部回合"])
            return _fail_state(state, "claim_relation_review_failed", reviewed)
        return _commit_relation_review(state, reviewed)

    def _commit_relation_review(state: dict, outcome: dict) -> dict:
        # The review may have been answered while the loop was parked, so the
        # stored state (not the caller's snapshot) is the one to complete.
        state = _load()
        data = outcome.get("data") or {}
        review_data = data.get("relation_review") or {
            "relations_submitted": data.get("relations_submitted", 0),
            "review_notes": data.get("review_notes", ""),
            "generated_by": data.get("generated_by", "")}
        review = state.setdefault("relation_review", {})
        review.update({"status": "completed", "pending_path": None,
                       "reviewed_at": now(),
                       "relations_submitted": review_data.get("relations_submitted", 0),
                       "review_notes": review_data.get("review_notes", ""),
                       "generated_by": review_data.get("generated_by", "")})
        if data.get("snapshot_id"):
            state["knowledge_snapshot"] = data["snapshot_id"]
        if isinstance(data.get("graph_version"), dict):
            state["graph_version"] = data["graph_version"].get("version_id")
        store.log.append("claim_relation_review_completed", review_data,
                         actor=agent_id)
        return _complete(state, review.get("terminal_status") or "completed",
                         state.get("missing_material_paper_ids") or [],
                         outcome.get("warnings"))

    def _complete(state: dict, terminal_status: str, unavailable: List[str],
                  warnings: Optional[List[str]]) -> dict:
        # Ideation runs once, after the knowledge base is final and before the
        # literature phase closes.  Its failure must never change the phase
        # outcome: a run whose reading succeeded is not a failed run just
        # because no direction could be proposed.  That is why the result is
        # recorded as a warning instead of a state transition.
        notes = list(warnings or [])
        status_of_ideation = (state.get("ideation") or {}).get("status")
        if status_of_ideation == "pending":
            # The corpus is final and an ideation question has already been
            # asked.  Re-running it would publish another order; the answer must
            # come through ``submit_research_directions``.
            state["status"] = "awaiting_external_agent"
            store.save(state, actor=agent_id)
            return ok(state, warnings=["等待 ideation 的外部回合"])
        finished = status_of_ideation in ("ok", "unavailable", "failed")
        if terminal_status == "completed" and not finished:
            ideation = _run_ideation(state)
            state["ideation"] = ideation
            if ideation.get("status") == "ok":
                store.log.append("ideation_completed", {
                    "strategy_id": ideation.get("strategy_id"),
                    "proposals": len(ideation.get("proposals") or []),
                    "agenda_path": ideation.get("agenda_path")}, actor=agent_id)
            elif ideation.get("status") in ("unavailable", "pending"):
                store.log.append("ideation_unavailable",
                                 {"reason": ideation.get("reason")},
                                 actor=agent_id)
                if ideation.get("status") == "pending":
                    # An external ideation order is outstanding.  Closing the
                    # phase now would finalise a run whose last step is still
                    # unanswered, so pause instead: the host answers it and the
                    # supervisor re-enters completion.
                    state["status"] = "awaiting_external_agent"
                    state["ideation"] = {"status": "pending"}
                    store.save(state, actor=agent_id)
                    return ok(state, warnings=["等待 ideation 的外部回合"])
            else:
                store.log.append("ideation_failed",
                                 {"error": ideation.get("error")}, actor=agent_id)
                notes.append("方向提出失败：{}".format(
                    (ideation.get("error") or {}).get("message")))
            # Persist the ideation outcome whatever it was: leaving it in the
            # in-memory object lets the next ``_complete`` call see ``None`` and
            # run ideation a second time.
            store.save(state, actor=agent_id)
        state["status"] = terminal_status
        state["pending_tasks"] = []
        store.save(state, actor=agent_id)
        if terminal_status == "blocked_material":
            store.log.append("workflow_blocked_material",
                             {"paper_ids": unavailable}, actor=agent_id)
        else:
            store.log.append("workflow_completed", {"cycle": state["cycle"]},
                             actor=agent_id)
        return ok(state, warnings=notes)

    def _run_ideation(state: dict) -> dict:
        """Ask the Scheduler to synthesise the corpus into research directions.

        The prompts, not just the call arguments, must reach the external agent.
        Routing this through the agent's policy would publish an order whose
        messages are empty -- the host would be asked to answer a question it
        cannot see -- so the assembled prompt package is used directly when no
        inline executor is attached.
        """
        if getattr(kit, "handoff", None) is not None \
                and getattr(kit, "ideation_executor", None) is None:
            return _ideation_handoff(kit, state, agent_id, store)
        outcome = dispatch(scheduler, "propose_research_directions", {
            "strategy_id": kit.config.ideation_strategy,
            "research_goal": state.get("research_goal") or kit.config.research_goal,
            "max_proposals": kit.config.max_proposals,
        }, note="综合已有研究，提出研究方向")
        if not outcome["ok"]:
            code = (outcome.get("error") or {}).get("code")
            if code == "E2_AWAITING_EXTERNAL_AGENT":
                # The Scheduler is external, so ideation is a pause too.  It is
                # recorded as pending rather than failed, and the host answers
                # it through ``submit_research_directions``.
                return {"status": "pending", "error": None,
                        "reason": "external ideation order published"}
            return {"status": "failed", "error": outcome.get("error")}
        data = outcome["data"] or {}
        if data.get("awaiting_ideation_agent"):
            # No model attached: keep the assembled prompts so a host agent can
            # answer them, exactly like the reading path does.
            path = store.write_pending(
                {"task_id": "ideation"},
                {"task_type": "ideation",
                 "strategy_id": data.get("strategy_id"),
                 "messages": data.get("messages"),
                 "strategy_prompt_sha256": data.get(
                     "strategy_prompt_sha256"),
                 "payload": data.get("payload")})
            return {"status": "unavailable", "reason": "no ideation executor",
                    "strategy_id": data.get("strategy_id"),
                    "pending_path": path}
        return {"status": "ok",
                "strategy_id": data.get("strategy_id"),
                "strategy_prompt_sha256": data.get("strategy_prompt_sha256"),
                "proposals": data.get("proposals") or [],
                "opportunities": data.get("opportunities") or [],
                # Two distinct things: ``rejected`` is what the strategy itself
                # considered and dropped; ``rejected_proposals`` is what this
                # system refused because it could not be grounded.
                "rejected": data.get("rejected") or [],
                "rejected_proposals": data.get("rejected_proposals") or [],
                "filter": data.get("filter") or {},
                "landscape_summary": data.get("landscape_summary") or {},
                "agenda_path": data.get("agenda_path")}

    def _fail_state(state: dict, event: str, result: dict) -> dict:
        state["status"] = "failed"
        state["last_error"] = result.get("error")
        store.save(state, actor=agent_id)
        store.log.append(event, {"error": result.get("error")}, actor=agent_id)
        return result

    def step():
        """Advance exactly one transition.  Returns the new state envelope."""
        state = _load()
        status = state["status"]
        if status in DONE:
            return ok(state)
        if status in ("created",):
            return failed(error("SYSTEM_NOT_INITIALIZED",
                                "先调用 initialize(search_output)"))
        if status == GATE:
            return ok(state, warnings=["等待 Organization 论点关系审阅结果"])
        if (status == "awaiting_external_agent"
                and (state.get("ideation") or {}).get("status") == "pending"):
            if _has_ideation_order(kit):
                # Still outstanding: re-planning would publish a duplicate.
                return ok(state, warnings=["等待 ideation 的外部回合"])
            # The order was answered.  Re-enter completion so the agenda is
            # stored and the phase can close.
            return _complete(state, "completed", [], [])
        if (status == "awaiting_external_agent"
                and not state.get("organization_imported")
                and not state.get("knowledge_snapshot")
                and state.get("search_output")):
            retried = initialize(state.get("search_output"))
            if retried["ok"]:
                return retried
            return ok(state, warnings=["等待 Organization 的外部回合"])
        if status == "awaiting_external_agent" and (
                state.get("relation_review") or {}).get("status") == "pending" \
                and not any((row.get("metadata") or {}).get("tool") in (
                    "review_claim_relations", "ask_relation_reviewer")
                    for row in (kit.handoff.pending()
                                if getattr(kit, "handoff", None) else [])):
            # A reviewer answered while the loop was parked.  The review gate
            # must now run again: its pause left it *pending*, and after the
            # answer the next call is what applies the result and closes the
            # phase.  Without this branch the re-entry fell through to the
            # generic "wait for an external turn" case and the phase stalled
            # with a pending review that would never be committed.
            return _begin_relation_review(
                state,
                terminal_status=state["relation_review"].get("terminal_status")
                or "completed",
                unavailable=state.get("missing_material_paper_ids") or [],
                warnings=["关系审阅已作答，重新进入关卡"])
        if status == "awaiting_external_agent":
            # Two different pauses share this status:
            #  * waiting for a *decision* (no tasks yet) -- nothing to do;
            #  * waiting for a *reading* (tasks already assembled) -- the read
            #    must be retried, or the assembled task is never consumed and
            #    the loop publishes another order for the same paper.
            if state.get("pending_tasks"):
                return _read_and_commit(state)
            return ok(state, warnings=["等待外部 Agent 回合"])
        if status in ("planning", "replanning"):
            # A state with tasks already assembled is mid-transition: reading
            # was interrupted (typically by an external pause) after planning
            # succeeded.  Re-planning would discard the assembled tasks and
            # schedule the same paper again, so finish the read instead.
            if state.get("pending_tasks"):
                return _read_and_commit(state)
            planned = _plan(state)
            if not planned["ok"] or planned["status"] != "ok":
                return planned
            # ``_plan`` may have closed the phase (gate, ideation pause) or
            # assembled tasks.  Re-read and stop when it is not about to read,
            # otherwise the stale ``replanning`` snapshot is carried further and
            # overwrites what planning just recorded.
            state = _load()
            if state.get("status") not in ("tasks_ready",):
                return ok(state)
        if state["status"] == "tasks_ready":
            return _read_and_commit(state)
        return ok(_load())

    def run(max_cycles: int = 0, research_goal: str = ""):
        if str(research_goal or "").strip():
            state = _load()
            if state.get("research_goal") != str(research_goal).strip():
                state["research_goal"] = str(research_goal).strip()
                store.save(state, actor=agent_id)
                store.log.append("research_goal_synchronized", {
                    "research_goal": state["research_goal"]}, actor=agent_id)
        limit = max_cycles or kit.config.max_cycles
        for _ in range(limit):
            state = _load()
            if state["status"] in DONE:
                return ok(state)
            if state["status"] == GATE:
                return ok(state, warnings=["等待 Organization 论点关系审阅结果"])
            outcome = step()
            if not outcome["ok"]:
                if _is_external_pause(outcome):
                    state["status"] = "awaiting_external_agent"
                    store.save(state, actor=agent_id)
                    return ok(state, warnings=[
                        "等待外部 Agent 回合（文献闭环）"])
                return outcome
            state = _load()
            if state["status"] in DONE:
                return ok(state)
        state = _load()
        return partial(state, warnings=["达到本次运行上限；状态已保存"])

    def submit_result(task_id: str = "", result: dict = None):
        state = _load()
        task = next((item for item in state.get("pending_tasks", [])
                     if item.get("task_id") == task_id), None)
        if not task:
            return failed(error("SYSTEM_TASK_NOT_PENDING",
                                "待处理任务不存在：{}".format(task_id)))
        validated = kit.ports.read.validate(result or {}, task)
        if not validated["ok"]:
            return validated
        return _commit_results(state, [(task, validated["data"])])

    def _commit_results(state: dict, pairs) -> dict:
        written = dispatch(organization, "apply_reading_results",
                           {"results": [item for _, item in pairs]},
                           note="外部提交的阅读结果写回")
        if not written["ok"]:
            return _fail_state(state, "organization_writeback_failed", written)
        for task, _ in pairs:
            state["completed_task_ids"].append(task["task_id"])
            state["completed_paper_ids"].append(task["target_paper"])
        state["pending_tasks"] = []
        state["cycle"] += 1
        state["status"] = "replanning"
        store.save(state, actor=agent_id)
        return ok(state)

    def submit_relation_review(review: dict = None):
        state = _load()
        # A handoff reviewer answers while the loop is parked (``replanning``
        # with a pending review); only a run with no review in flight is
        # rejected.
        pending_review = (state.get("relation_review") or {}).get(
            "status") == "pending"
        if state.get("status") != GATE and not pending_review:
            return failed(error("SYSTEM_RELATION_REVIEW_NOT_PENDING",
                                "当前没有待提交的论点关系审阅任务"))
        applied = kit.ports.organization.apply_claim_relation_review(review or {})
        if not applied["ok"]:
            return applied
        # The Organization bundle nests its review summary under
        # `data.relation_review` (it merges an `extra` dict), so reading
        # `data["relations_submitted"]` raised `KeyError: 'relations_submitted'`
        # — and the caller swallowed that failure, leaving the gate `pending`
        # with its order already claimed. Accept both shapes.
        data = applied.get("data") or {}
        summary = data.get("relation_review") or {}
        wrapped = ok({"relation_review": {
            "relations_submitted": summary.get(
                "relations_submitted", data.get("relations_submitted", 0)),
            "review_notes": summary.get("review_notes")
            or data.get("review_notes") or (review or {}).get("review_notes", ""),
            "generated_by": summary.get("generated_by")
            or data.get("generated_by") or "organization_claim_relation_review"},
            "snapshot_id": data.get("snapshot_id"),
            "graph_version": data.get("graph_version")})
        return _commit_relation_review(state, wrapped)

    def submit_research_directions(strategy_id: str = "",
                                   payload: dict = None):
        """Accept externally produced ideation output.

        Mirrors ``submit_relation_review``: when no model is attached, the
        Scheduler writes the assembled prompts to ``pending/ideation.json`` and
        a host agent answers them.  This is how that answer re-enters the
        system, through the same validation and filtering as an inline run.
        """
        state = _load()
        ideation = state.get("ideation") or {}
        # Two ways to arrive here: no model was attached (``unavailable``), or a
        # handoff order was published (``pending``).  Both expect an external
        # answer; accepting only one of them silently dropped the other.
        if ideation.get("status") not in ("unavailable", "pending"):
            return failed(error("SYSTEM_IDEATION_NOT_PENDING",
                                "当前没有待提交的研究方向任务"))
        chosen = strategy_id or kit.config.ideation_strategy
        landscape = _landscape_for(kit)
        if not landscape["ok"]:
            return landscape
        # An external agent may legitimately conclude that nothing here is worth
        # proposing. `validate_proposals` demands a non-empty list, which is right
        # for our own strategies but wrong for a host answer: rejecting it left
        # the order already claimed and `ideation` stuck at `pending` with no
        # order left to answer, so the whole run parked forever at `replanning`.
        # An empty list is an ANSWER — record it and close the phase.
        if isinstance(payload, dict) and payload.get("proposals") == []:
            record = {
                "status": "ok",
                "strategy_id": chosen,
                "strategy_prompt_sha256": ideation.get("strategy_prompt_sha256"),
                "proposals": [],
                "opportunities": payload.get("opportunities") or [],
                "rejected": payload.get("rejected") or [],
                "rejected_proposals": [],
                "filter": {"accepted": 0, "rejected": 0},
                "landscape_summary": ideation.get("landscape_summary") or {},
                "reason": "外部智能体判断当前语料没有值得提出的方向",
            }
            path = write_agenda(os.path.join(store.root, "ideation"), record)
            record["agenda_path"] = path
            state["ideation"] = record
            store.save(state, actor=agent_id)
            store.log.append("ideation_empty", {
                "strategy_id": chosen, "submitted_externally": True,
                "agenda_path": path}, actor=agent_id)
            return _complete(state, "completed", [], [])
        validated = validate_proposals(payload or {}, landscape["data"])
        if not validated["ok"]:
            return validated
        filtered = filter_proposals(validated["data"]["proposals"],
                                    max_proposals=kit.config.max_proposals)
        record = {"status": "ok", "strategy_id": chosen,
                  "strategy_prompt_sha256": ideation.get(
                      "strategy_prompt_sha256"),
                  "proposals": filtered["proposals"],
                  "opportunities": validated["data"].get("opportunities") or [],
                  "rejected": validated["data"].get("rejected") or [],
                  "rejected_proposals":
                      validated["data"].get("rejected_proposals") or [],
                  "filter": {key: value for key, value in filtered.items()
                             if key != "proposals"},
                  "landscape_summary": ideation.get("landscape_summary") or {}}
        path = write_agenda(os.path.join(store.root, "ideation"), record)
        record["agenda_path"] = path
        state["ideation"] = record
        store.save(state, actor=agent_id)
        store.log.append("ideation_completed", {
            "strategy_id": chosen, "proposals": len(filtered["proposals"]),
            "submitted_externally": True, "agenda_path": path}, actor=agent_id)
        # The agenda was the last outstanding question; close the phase now.
        return _complete(state, "completed", [], [])

    def get_research_agenda():
        """The current agenda, if ideation has produced one."""
        state = _load()
        ideation = state.get("ideation") or {}
        if ideation.get("status") != "ok":
            return ok({"available": False, "status": ideation.get("status")},
                      warnings=["尚未产生研究方向"])
        return ok({"available": True, "agenda_path": ideation.get("agenda_path"),
                   "proposals": ideation.get("proposals") or [],
                   "opportunities": ideation.get("opportunities") or [],
                   "strategy_id": ideation.get("strategy_id")})

    # -- registry: siblings as tools ------------------------------------
    registry = kit.registry.with_tools([
        ("get_literature_state", "读取文献闭环当前状态",
         {"type": "object", "properties": {}, "additionalProperties": False},
         get_state),
        ("initialize_literature", "导入语料并进入规划",
         {"type": "object",
          "properties": {"search_output": {"type": "string"},
                         "research_goal": {"type": "string"}},
          "required": ["search_output"], "additionalProperties": False},
         initialize),
        ("advance_literature", "推进一次文献闭环转移",
         {"type": "object", "properties": {}, "additionalProperties": False},
         step),
        ("run_literature", "连续推进文献闭环直到终态或关卡",
         {"type": "object",
          "properties": {"max_cycles": {"type": "integer"},
                         "research_goal": {"type": "string"}},
          "additionalProperties": False},
         run),
        ("submit_reading_result", "提交外部产生的阅读结果",
         {"type": "object",
          "properties": {"task_id": {"type": "string"},
                         "result": {"type": "object"}},
          "required": ["task_id", "result"], "additionalProperties": False},
         submit_result),
        ("submit_relation_review", "提交外部完成的论点关系审阅",
         {"type": "object", "properties": {"review": {"type": "object"}},
          "required": ["review"], "additionalProperties": False},
         submit_relation_review),
        ("submit_research_directions", "提交外部完成的 ideation 结果",
         {"type": "object",
          "properties": {"strategy_id": {"type": "string"},
                         "payload": {"type": "object"}},
          "required": ["payload"], "additionalProperties": False},
         submit_research_directions),
        ("get_research_agenda", "读取已产生的研究方向",
         {"type": "object", "properties": {}, "additionalProperties": False},
         get_research_agenda),
    ])
    for name, description, child in (
            ("ask_organization", "让 Organization 智能体处理知识库事务", organization),
            ("ask_scheduler", "让 Scheduler 智能体规划下一批阅读", scheduler),
            ("ask_reader", "让 Read 智能体阅读指定任务", read)):
        registry.register(agent_tool(child, name=name, description=description,
                                     kit=kit, parent_id=agent_id))

    supervisor_proxy["registry"] = registry
    rules = [
        lambda s: (Decision("run_literature", {}, note="推进文献闭环")
                   if s.get("status") in ("planning", "replanning")
                   else None),
    ]
    return Agent(role="literature_supervisor",
                 system_prompt=kit.prompts.get("literature_supervisor.md"),
                 tools=registry,
                 policy=kit.resolve_policy(agent_id, RouterPolicy(_SupervisorPolicy(run))),
                 budget=kit.budget(turns=4),
                 id=agent_id)


class _SupervisorPolicy:
    """Default policy: delegate the whole loop to ``run_literature``."""

    name = "supervisor"

    def __init__(self, run):
        self._run = run
        self._done = False

    def reset(self):
        self._done = False

    def respond(self, request):
        if not self._done:
            self._done = True
            from ..runtime.messages import AgentReply, ToolCall
            return AgentReply(text="", tool_calls=[ToolCall("run_literature", {})])
        last = request.messages[-1] if request.messages else None
        try:
            return _reply_from(last)
        except Exception:
            return _reply_from(None)


def _reply_from(message):
    from ..runtime.messages import AgentReply
    if message is not None and message.role == "tool":
        try:
            return AgentReply(payload=json.loads(message.content))
        except json.JSONDecodeError:
            return AgentReply(payload=None)
    return AgentReply(payload=None)


def _landscape_for(kit_):
    """Project the evidence package for an external ideation answer."""
    from ..ports.ideation import build_landscape
    evidence = kit_.ports.organization.export_evidence()
    if not evidence["ok"]:
        return evidence
    return {"ok": True, "status": "ok", "data": build_landscape(
        evidence["data"]), "error": None, "warnings": [],
        "diagnostics": {}, "meta": {}}


def _decide_reading(kit_: AgentKit, state: dict, situation: dict) -> dict:
    """One decision turn by the Scheduler, with its conversation persisted.

    This is the difference between a planner and an agent: the Scheduler sees
    its own previous judgements, so it can say "last cycle I decided X because
    of Y; the new gap changes that".  The supervisor records the answer and
    never substitutes its own choice.
    """
    import json as _json
    scheduler_agent = kit_.agent_index.get("scheduler")
    if scheduler_agent is None:
        return failed(error("SYSTEM_SCHEDULER_MISSING",
                            "找不到 scheduler agent"))
    # The message is *pure JSON*.  An external agent receives this verbatim, and
    # mixing prose with the payload forces every host to write a parser for the
    # prose before it can parse the data.  The instruction belongs in the system
    # prompt, which already says what to do with a decision turn.
    prompt = _json.dumps({
        "turn": "reading_decision",
        "cycle": state.get("cycle"),
        "research_goal": state.get("research_goal") or kit_.config.research_goal,
        "situation": situation,
        "material_constraints": {
            "forbidden_paper_ids": state.get("missing_material_paper_ids") or [],
            "instruction": (
                "不得选择 forbidden_paper_ids；只能选择 situation.papers 中 "
                "pdf_available=true 的未读论文，或 reread_allowed=true 的论文。"
            ),
        },
        "state": {
            "pending_tasks": [task.get("task_id")
                              for task in state.get("pending_tasks") or []],
            "completed_papers": state.get("completed_paper_ids") or [],
        },
    }, ensure_ascii=False, indent=1)
    # A persistent agent keeps this conversation between cycles; the seed above
    # is appended to it rather than replacing it.
    context = kit_.context("scheduler", parent_id="literature")
    # A decision turn is self-contained: the situation carries everything the
    # agent needs.  Resuming the persistent conversation here would make the
    # *latest user message* an earlier cycle's question, so a handoff order
    # would carry stale context while its metadata described the new call.
    # Discard any answer left unclaimed by an earlier cycle: a decision turn
    # must ask a *new* question, not receive the previous answer again.  This
    # is the last link in the loop that made cycle 1 re-select cycle 0's paper.
    policy = getattr(scheduler_agent, "policy", None)
    if policy is not None and hasattr(policy, "discard_unclaimed"):
        policy.discard_unclaimed("scheduler")
    session = getattr(scheduler_agent, "persistent_session", None)
    # A decision is self-contained, so the turn must not inherit history from
    # the policy's own session either: the resumed conversation would make the
    # *last user message* an earlier cycle's question, and the work order would
    # carry that stale context under new metadata.
    policy_session = getattr(policy, "session", None) if policy is not None else None
    if policy is not None and hasattr(policy, "session"):
        policy.session = None
    scheduler_agent.persistent_session = None
    try:
        outcome = scheduler_agent.one_shot(
            context, prompt,
            metadata={"decision_turn": True, "situation": situation,
                      "batch_size": kit_.config.batch_size})
    finally:
        scheduler_agent.persistent_session = session
        if policy is not None and policy_session is not None:
            policy.session = policy_session
    if outcome.stopped_because == "awaiting_external_agent":
        return failed(error("SCHEDULER_AWAITING_EXTERNAL",
                            "Scheduler 已发布工单，等待外部决策"))
    if outcome.stopped_because != "final":
        return failed(error("SCHEDULER_DECISION_INCOMPLETE",
                            "Scheduler 未完成决策（{}）".format(
                                outcome.stopped_because),
                            hint="提高 max_agent_turns 或检查工具调用"))
    payload = outcome.payload
    if not isinstance(payload, dict):
        return failed(error("SCHEDULER_DECISION_INVALID",
                            "Scheduler 的决策必须是 JSON 对象"))
    selected = payload.get("selected")
    if selected is None:
        selected = []
    if not isinstance(selected, list):
        return failed(error("SCHEDULER_DECISION_INVALID",
                            "selected 必须是数组"))
    reasons = payload.get("reasons") or {}
    if not isinstance(reasons, dict):
        reasons = {}
    reader_groups = payload.get("reader_groups") or []
    if not isinstance(reader_groups, list):
        return failed(error("SCHEDULER_DECISION_INVALID",
                            "reader_groups 必须是数组"))
    reading_depths = payload.get("reading_depths") or {}
    if not isinstance(reading_depths, dict) or any(
            value not in ("summary", "skim", "deep")
            for value in reading_depths.values()):
        return failed(error("SCHEDULER_DECISION_INVALID",
                            "reading_depths 必须把 paper_id 映射到 summary/skim/deep"))
    return ok({"selected": [str(item) for item in selected],
               "reasons": {str(key): str(value)
                           for key, value in reasons.items()},
               "reader_groups": reader_groups,
               "reading_depths": {str(key): str(value)
                                   for key, value in reading_depths.items()},
               "note": str(payload.get("note") or "")})


def _retire_task_orders(kit_, paper_ids: list, task_ids: list) -> None:
    """Move unanswered orders for abandoned tasks out of the mailbox.

    Kept out of `done/` on purpose: an archived order implies an answer that was
    applied. These go to `cancelled/` so the record stays honest.
    """
    handoff = getattr(kit_, "handoff", None)
    outbox = getattr(handoff, "outbox", None)
    if not outbox or not os.path.isdir(outbox):
        return
    cancelled = os.path.join(os.path.dirname(outbox), "cancelled")
    needles = [needle for needle in list(task_ids) + list(paper_ids) if needle]
    if not needles:
        return
    os.makedirs(cancelled, exist_ok=True)
    for name in list(os.listdir(outbox)):
        if not name.endswith(".json"):
            continue
        path = os.path.join(outbox, name)
        try:
            with open(path, encoding="utf-8") as handle:
                text = handle.read()
        except OSError:
            continue
        if not any(needle in text for needle in needles):
            continue
        try:
            os.replace(path, os.path.join(cancelled, name))
        except OSError:
            continue


def _situation_now(kit_, state: dict) -> dict:
    """The current reading situation, computed by the Scheduler's own logic.

    Delegated rather than re-implemented: the supervisor must not hold a second
    opinion about what the agent was shown.
    """
    from ..agents.scheduler import compute_situation
    return compute_situation(
        kit_, reread_history=dict(state.get("reread_counts") or {}),
        reread_budget=kit_.config.reread_budget,
        max_rereads_per_paper=kit_.config.max_rereads_per_paper)


def _has_external_reply(kit_: AgentKit) -> bool:
    """Whether the handoff mailbox has an answer waiting for this run."""
    queue = getattr(kit_, "handoff", None)
    if queue is None:
        queue = getattr(kit.store, "handoff", None)
    if queue is None:
        return False
    try:
        return any(queue.has_reply(row["order_id"]) for row in queue.pending())
    except Exception:
        return False


def _situation_for_assembly(kit, cached: dict) -> dict:
    """The situation envelope to re-assemble a cached decision against.

    A decision stored by a resume may carry the full situation it was made
    against (snapshot_id + papers + counts + caps), a situation without a
    snapshot id, or no situation at all.  Assembly only reads ``snapshot_id``,
    ``papers``, ``counts`` and ``caps``, so a complete envelope is rebuilt here
    for the first two cases -- otherwise ``selected`` could never be matched
    against the snapshot and every task would be rejected as "不在快照中".

    When no situation is available at all, ask for it again.  That is a read of
    facts, not a new model turn, and it keeps the run honest: assembly never
    invents a snapshot id.
    """
    situation = cached.get("situation") or {}
    if situation.get("snapshot_id"):
        return ok(situation)
    fresh = _situation_now(kit, {"reread_counts": cached.get("reread_counts")})
    if fresh["ok"]:
        return fresh
    return ok(situation)


def _assemble_tasks(scheduler_agent, arguments: dict) -> dict:
    """Call the Scheduler's task assembler directly, bypassing its policy.

    The distinction this preserves is the whole point of the two-phase design:
    *deciding* is the agent's job (and may be delegated to an external model),
    while *assembling* a decided choice into ReadingTask objects is deterministic
    bookkeeping.  Sending assembly through the agent would, in handoff mode,
    replace a task list with yet another work order.
    """
    tools = scheduler_agent.tools
    if "next_reading_tasks" not in tools:
        return failed(error("SCHED_ASSEMBLER_MISSING",
                            "Scheduler 缺少任务装配工具"))
    # ``ToolRegistry.call`` routes through the agent's policy.  In handoff mode
    # that policy pauses with a work order instead of assembling, so the
    # decision would never become tasks and the cycle would stall.  Assembly is
    # deterministic: invoke the tool itself, which is what this function
    # exists to guarantee.
    target = (getattr(tools, "_tools", None) or {}).get("next_reading_tasks")
    if target is None:
        return failed(error("SCHED_ASSEMBLER_MISSING",
                            "Scheduler 缺少任务装配工具"))
    # Invoke the handler, not ``Tool.invoke``: the latter enforces the
    # result-size ceiling and, for a 221-paper corpus, replaces the assembled
    # task list with ``data=None`` ("已截断").  That ceiling exists to protect
    # an external agent's context; here the result never leaves the process and
    # is consumed immediately, so truncating it would only break assembly.
    try:
        value = target.handler(**dict(arguments or {}))
    except Exception as exc:  # noqa: BLE001 - boundary, same shape as Tool.invoke
        import traceback
        return failed(error("E2_TOOL_RAISED", "{}: {}".format(
            type(exc).__name__, exc), hint="查看工具实现与输入",
            location="tools.next_reading_tasks",
            cause=traceback.format_exc()[-2000:]))
    if not isinstance(value, dict):
        value = ok({"tasks": []})
    value.setdefault("warnings", [])
    value.setdefault("diagnostics", {})
    return value


def _has_ideation_order(kit_) -> bool:
    """Whether an ideation work order is still waiting for its answer.

    Takes the kit explicitly: this helper is defined at module scope, so it has
    no closure over the supervisor's ``kit`` and must not pretend it does.
    """
    queue = getattr(kit_, "handoff", None)
    if queue is None:
        return False
    for row in queue.pending():
        if (row.get("metadata") or {}).get("tool") == "propose_research_directions":
            return True
    return False


def _ideation_handoff(kit_, state: dict, actor: str, store_) -> dict:
    """Publish an ideation work order that carries the real prompt."""
    from ..agents.scheduler import _reading_state
    from ..ports.ideation import build_landscape, prepare_payload
    from ..ports.ideation_strategies import build_ideation_messages
    evidence = kit_.ports.organization.export_evidence()
    if not evidence["ok"]:
        return {"status": "failed", "error": evidence.get("error")}
    landscape = build_landscape(evidence["data"])
    if not landscape["paper_count"]:
        return {"status": "failed",
                "error": {"code": "IDEATION_NO_CORPUS",
                          "message": "语料为空，无法提出研究方向"}}
    payload = prepare_payload(
        landscape, research_goal=(state.get("research_goal")
                                  or kit_.config.research_goal),
        reading_state=_reading_state(kit_))
    built = build_ideation_messages(kit_.config.ideation_strategy, payload)
    if not built["ok"]:
        return {"status": "failed", "error": built.get("error")}
    from ..runtime.handoff import WorkOrder
    from ..runtime.messages import Message
    from ..runtime.policy import PolicyRequest as _PR
    request = _PR(
        agent_id="scheduler", role="scheduler", system_prompt="",
        messages=[Message.system(built["data"]["messages"][0]["content"]),
                  Message.user(built["data"]["messages"][1]["content"]),
                  Message.user(built["data"]["messages"][2]["content"])],
        tools=[], metadata={"tool": "propose_research_directions",
                            "strategy_id": built["data"]["strategy_id"]})
    order = WorkOrder.build(kit_.handoff.next_seq(), request)
    kit_.handoff.publish(order)
    return {"status": "pending", "reason": "external ideation order published"}
