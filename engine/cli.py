#!/usr/bin/env python3
"""ResearchOS handoff CLI — the only program a DSH agent has to call.

DSH supplies the thinking: one short-lived subagent per work order, reading
`order.json` and replying with `{"tool_calls": ...}` or `{"payload": ...}`.
This program supplies everything else: typed tools, contract checks, state
machines, gates and the on-disk record of who decided what.

The agent-facing commands are all JSON-in/JSON-out so that a caller never has
to parse prose:

    init      seed a run and publish its first work order
    orders    list the orders that are waiting for a reply
    show      render one order for a host agent: role prompt, history, tool
              schemas, attachments, reply contract and answer guidance
    reply     validate and file the answer to one order
    pending   print the run's progress in a few lines
    step      the pump: consume replies, advance the machine, publish the next
              order (single-step and idempotent; safe to call repeatedly)
    run       loop `step` until the run stops or a work order is outstanding
    submit    file a deterministic terminal-stage result (see `stage`)
    stage     show the packet a finalization stage would receive
    status    dump the raw top-level run state
    agents    print the agent topology and per-agent tool grants
    install   link this product's skills into the DSH project root (once)
    selftest  run the bundled test suite and report pass/fail

Exit codes: 0 success, 1 the engine reported failure, 2 bad usage/state.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import traceback


HERE = os.path.abspath(os.path.dirname(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from researchos import ResearchSystem, SystemConfig  # noqa: E402
from researchos import host_guide  # noqa: E402
from researchos.runtime.envelope import failed, error  # noqa: E402
from researchos.runtime.handoff import inbox_instructions, read_order  # noqa: E402
from researchos.workflow.finalization import STAGES, stage_package  # noqa: E402


ENGINE_ACTIVITY_FILE = ".engine-active.json"
ENGINE_ACTIVITY_INTERVAL = 2.0
ENGINE_ACTIVITY_MAX_AGE = 10.0


def _atomic_json(path: str, value: dict) -> None:
    """Write a tiny coordination record without exposing partial JSON."""
    fd, temporary = tempfile.mkstemp(prefix="engine-active-", suffix=".json",
                                     dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _read_engine_activity(root: str):
    """Return a fresh activity lease, ignoring crash debris."""
    path = os.path.join(root, ENGINE_ACTIVITY_FILE)
    try:
        with open(path, encoding="utf-8") as handle:
            value = json.load(handle)
        heartbeat = float(value.get("heartbeat_at") or 0)
        if heartbeat <= 0 or time.time() - heartbeat > ENGINE_ACTIVITY_MAX_AGE:
            return None
        return value
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None


@contextlib.contextmanager
def _engine_activity(system: ResearchSystem, operation: str):
    """Expose a live heartbeat while a synchronous engine operation is running."""
    path = os.path.join(system.store.root, ENGINE_ACTIVITY_FILE)
    stopped = threading.Event()
    started = time.time()

    def beat() -> None:
        while not stopped.is_set():
            try:
                _atomic_json(path, {
                    "schema_version": "1.0",
                    "operation": operation,
                    "pid": os.getpid(),
                    "started_at": started,
                    "heartbeat_at": time.time(),
                })
            except OSError:
                # Observability must never stop the research operation itself.
                pass
            stopped.wait(ENGINE_ACTIVITY_INTERVAL)

    worker = threading.Thread(target=beat, name="researchos-activity",
                              daemon=True)
    worker.start()
    try:
        yield
    finally:
        stopped.set()
        worker.join(timeout=ENGINE_ACTIVITY_INTERVAL + 0.5)
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass


def _emit(value, *, code: int = 0) -> int:
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))
    return code


def _fail(code: str, message: str, *, hint: str = "") -> int:
    return _emit(failed(error(code, message, hint=hint)), code=2)


def _run_state_path(config: SystemConfig, run_id: str) -> str:
    """Where one run's state.json lives, without touching the filesystem.

    Constructing a `ResearchSystem` runs `RunStore.__init__`, which *creates*
    `<runs_dir>/<id>/{pending,results}`. That made a read-only query on a run id
    that does not exist leave a phantom run directory behind — and the plugin
    then bound the Session to it, so the workbench showed "找不到该研究运行".
    """
    return os.path.join(os.path.abspath(config.runs_dir), run_id, "state.json")


def _require_run(args, *, config: SystemConfig = None) -> int:
    """Fail before any directory is created when the run does not exist."""
    config = config or SystemConfig.from_file(args.config)
    if os.path.isfile(_run_state_path(config, args.run_id)):
        return 0
    return _fail("CLI_RUN_NOT_FOUND",
                 "找不到研究运行：{}".format(args.run_id),
                 hint="用 init 创建，或确认 --config 指向该 run 所在的 runs_dir")


def _system(args) -> ResearchSystem:
    config = SystemConfig.from_file(args.config)
    return ResearchSystem(config, args.run_id)


def _require_handoff(system: ResearchSystem) -> int:
    if system.handoff is None:
        return _fail("CLI_NOT_HANDOFF_MODE",
                     "当前配置不是 handoff 模式",
                     hint="使用 --config config/handoff.json")
    return 0


def _teammate_name(order_id: str, agent_id: str) -> str:
    """A kebab-case teammate name for one order.

    DSH team members are addressed by name/target, and two members must not
    share one; deriving the name from the order id keeps it unique per order
    without the caller inventing conventions.
    """
    safe = "".join(ch if ch.isalnum() else "-" for ch in agent_id.lower())
    safe = "-".join(part for part in safe.split("-") if part) or "role"
    return "ros-{}-{}".format(safe, order_id.split("-")[0])


def _orders(system: ResearchSystem) -> list:
    rows = system.handoff.pending() if system.handoff else []
    out = []
    for row in rows:
        guide = host_guide.describe(row)
        order_id = row["order_id"]
        out.append({"order_id": order_id,
                    "seq": row.get("seq"),
                    "agent_id": row["agent_id"],
                    "role": row.get("role"),
                    "agent_kind": guide["kind"],
                    "title": guide["title"],
                    "requested_tool": guide["requested_tool"],
                    "tools": [tool.get("name") for tool in row.get("tools") or []],
                    "has_reply": system.handoff.has_reply(order_id),
                    # Host plumbing for DSH: where the reply goes and which
                    # team member should write it.  Both are conveniences --
                    # `reply` re-validates whatever the member produced.
                    "reply_file": os.path.join(system.handoff.inbox,
                                               order_id + ".json"),
                    "suggested_teammate": _teammate_name(
                        order_id, row["agent_id"])})
    return out


def _pending_view(system: ResearchSystem) -> dict:
    state = system.store.load()
    orders = _orders(system)
    activity = _read_engine_activity(system.store.root)
    view = {
        "run_id": system.run_id,
        "mode": system.config.mode,
        "status": state.get("status"),
        "phase": state.get("phase"),
        "current_stage": state.get("current_stage"),
        "literature_status": state.get("literature_status"),
        "artifacts": sorted((state.get("final_artifacts") or {}).keys()),
        "pending_final_stage": (state.get("pending_final_task") or {}).get("stage"),
        "orders": orders,
        "handoff_dir": system.handoff.root if system.handoff else None,
    }
    if activity:
        view["engine_active"] = {
            "operation": activity.get("operation") or "step",
            "started_at": activity.get("started_at"),
            "heartbeat_at": activity.get("heartbeat_at"),
        }
    awaiting = str(state.get("status") or "").startswith("awaiting_") \
        or state.get("status") == "relation_review"
    if awaiting and not orders and not activity:
        view["recovery"] = {
            "needed": True,
            "action": "resume",
            "reason": "状态等待 Agent，但没有未答工单或活动中的引擎 step",
        }
    # A host that only polls `pending` must still be able to see *what* the run
    # is waiting for, otherwise a stage pause looks like an idle run.
    if state.get("status") == "awaiting_stage_agent":
        view["stage_task"] = _stage_task(system)
    return view


TERMINAL = ("completed", "failed", "blocked_research", "blocked_material",
            "cancelled")


def _stage_task(system: ResearchSystem) -> dict:
    """The packet a finalization stage needs, rendered as a host deliverable.

    The eight finalization stages are deliberately *not* work orders: their
    payloads are validated by a deterministic contract, and a caller may either
    write them itself or have a model write them.  This function is the one
    place that turns the engine's pending pointer into instructions a host can
    follow without reading the engine source.
    """
    state = system.store.load()
    pending = state.get("pending_final_task") or {}
    stage = pending.get("stage") or state.get("current_stage")
    if not stage:
        return {}
    exported = system.ports.organization.export_evidence()
    evidence = exported.get("data") if exported.get("ok") else {}
    packet = stage_package(
        stage,
        research_question=state.get("research_question") or "",
        artifacts=state.get("final_artifacts"),
        research_manifest=state.get("research_manifest"),
        literature_status=state.get("literature_status"),
        evidence=evidence)
    return {
        "stage": stage,
        "agent_role": packet["agent_role"],
        "packet": packet,
        "instructions": [
            "阅读 packet：stage_instruction、required_fields 与 evidence。",
            "只写 packet.required_fields 要求的字段，不要编造引用或实验。",
            "把 payload 存成 JSON 文件，然后运行 `python cli.py submit "
            "--run-id {} --stage {} --file <payload.json>`。".format(
                system.run_id, stage),
            "提交后再次运行 `python cli.py step` 继续下一阶段。",
        ],
    }


def _pump(system: ResearchSystem, *, advance: bool = True) -> dict:
    """One orchestration step: consume replies, then let the engine move.

    Two timelines meet here.  ``system.resume()`` consumes whatever the host
    has written and advances the literature state machine (single-step and
    idempotent).  When the mailbox holds no unanswered order and the run is
    not terminal, the Director gets one turn, which publishes the next order
    or reports a pending finalization stage.  Keeping both halves here means a
    host only ever has to call ``step`` in a loop.
    """
    before = {row["order_id"] for row in _orders(system)}
    outcome = system.resume()
    # Work may already be committed before a buggy resume branch returns None.
    # Treat that as a contract failure instead of hiding the progress behind an
    # ``AttributeError: 'NoneType' object has no attribute 'get'`` below.
    if outcome is None:
        outcome = failed(error(
            "CLI_RESUME_NO_RESULT",
            "resume 已执行，但没有返回状态信封",
            hint="运行状态仍保留；Lead 应再次 step/resume 读取磁盘状态"))
    elif not isinstance(outcome, dict):
        outcome = failed(error(
            "CLI_RESUME_BAD_RESULT",
            "resume 返回了非对象结果：{}".format(type(outcome).__name__),
            hint="运行状态仍保留；检查 resume 的返回契约"))
    after = _orders(system)
    fresh = [row for row in after if row["order_id"] not in before]
    state = system.store.load()
    director = None
    # `resume()` always publishes at most one order.  If it published one, the
    # machine already took its step and asking the Director again would publish
    # a *second* order for the same transition.
    unanswered = [row for row in after if not row["has_reply"]]
    may_advance = (advance and not unanswered and state.get("status") not in TERMINAL
                   and state.get("status") != "awaiting_stage_agent")
    if may_advance and fresh:
        may_advance = False
    if may_advance and state.get("phase") == "finalization":
        # Finalization is deterministic orchestration: advance_stage reads the
        # next stage, exports evidence and builds the packet.  The *thinking*
        # is the stage payload itself, so asking the Director for a tool call
        # here would spend an LLM turn on a fixed transition.
        try:
            director = system.agents.director.tools.call("advance_phase", {})
        except Exception as exc:  # noqa: BLE001 - CLI boundary
            director = failed(error("CLI_STAGE_FAILED",
                                    "{}: {}".format(type(exc).__name__, exc)))
        state = system.store.load()
        after = _orders(system)
        fresh = [row for row in after if row["order_id"] not in before]
        may_advance = False
    if may_advance:
        try:
            director = system.run(max_steps=1)
        except Exception as exc:  # noqa: BLE001 - CLI boundary
            director = failed(error("CLI_DIRECTOR_FAILED",
                                    "{}: {}".format(type(exc).__name__, exc)))
        state = system.store.load()
        after = _orders(system)
        fresh = [row for row in after if row["order_id"] not in before]
    view = {
        "ok": bool(outcome.get("ok", True)),
        "status": state.get("status"),
        "phase": state.get("phase"),
        "current_stage": state.get("current_stage"),
        "resolved": sorted(before - {row["order_id"] for row in after}),
        "new_orders": fresh,
        "orders": after,
        "handled": (outcome.get("data") or {}).get("handled") or [],
        "advanced": (outcome.get("data") or {}).get("advanced"),
        "warnings": list(outcome.get("warnings") or [])
                    + list((director or {}).get("warnings") or []),
    }
    if not view["ok"] and outcome.get("error"):
        # Keep the concrete recovery evidence in the CLI response. Without it
        # the host only saw ``ok: false`` and the Lead had nothing to reason from.
        view["error"] = outcome["error"]
    if state.get("status") == "awaiting_stage_agent":
        view["stage_task"] = _stage_task(system)
    return view


def cmd_init(args) -> int:
    system = _system(args)
    code = _require_handoff(system)
    if code:
        return code
    outcome = system.start(args.question,
                           search_options=_search_options(args))
    if not outcome.get("ok"):
        return _emit(outcome, code=1)
    view = _pending_view(system)
    view["first_order"] = (view.get("orders") or [None])[0]
    return _emit(view)


def cmd_orders(args) -> int:
    system = _system(args)
    code = _require_handoff(system)
    if code:
        return code
    return _emit({"run_id": system.run_id, "orders": _orders(system)})


def cmd_show(args) -> int:
    """Render one order.

    ``full`` (default) is the host document a subagent reads; ``json`` is the
    raw file for debugging; ``brief`` is prose for a human.
    """
    system = _system(args)
    code = _require_handoff(system)
    if code:
        return code
    path = os.path.join(system.handoff.outbox, args.order + ".json")
    if not os.path.isfile(path):
        return _fail("CLI_ORDER_NOT_FOUND", "工单不存在：{}".format(path))
    order = read_order(path)
    if args.format == "json":
        return _emit(order)
    if args.format == "brief":
        print(inbox_instructions(order))
        return 0
    # The default is what a host agent actually needs: one JSON document with
    # the reply instructions attached, so a fresh subagent has no other file
    # to read.
    # Pass the live state: the order's own question is a snapshot from the start
    # of its turn and may already be stale (a director turn can start the
    # research phase and pause in literature before publishing).
    guide = host_guide.describe(order, system.store.load())
    return _emit({
        "order_id": order["order_id"],
        "agent_id": order["agent_id"],
        "role": order.get("role"),
        "agent_kind": guide["kind"],
        "title": guide["title"],
        "requested_tool": guide["requested_tool"],
        "guidance": guide["guidance"],
        "suggested_reply": guide["suggested_reply"],
        "system_prompt": order.get("system_prompt") or "",
        "question": order.get("question") or "",
        "messages": order.get("messages") or [],
        "tools": order.get("tools") or [],
        "attachments": order.get("attachments") or [],
        "metadata": order.get("metadata") or {},
        "reply_contract": order.get("reply_contract") or {},
        "inbox_path": os.path.join(system.handoff.inbox,
                                   order["order_id"] + ".json"),
        "instructions": [
            "只返回 JSON：{\"tool_calls\": [...]} 或 {\"payload\": {...}}。",
            "tool_calls 的名字和参数必须符合 tools 的 JSON Schema。",
            "不要伪造工具结果；看不到的数据就说看不到。",
            "payload 是这张工单的最终答复，给出后不再调用工具。",
            "把 JSON 交给 `python cli.py reply --order {}` 或直接写入 inbox_path。"
            .format(order["order_id"]),
        ],
    })


def _load_reply(args) -> dict:
    if args.file:
        with open(args.file, encoding="utf-8") as handle:
            return json.load(handle)
    if args.json:
        return json.loads(args.json)
    raise ValueError("需要 --file 或 --json 提供回复")


def cmd_reply(args) -> int:
    system = _system(args)
    code = _require_handoff(system)
    if code:
        return code
    path = os.path.join(system.handoff.outbox, args.order + ".json")
    if not os.path.isfile(path):
        return _fail("CLI_ORDER_NOT_FOUND",
                     "工单不存在或已被消费：{}".format(args.order))
    try:
        reply = _load_reply(args)
    except (OSError, ValueError) as exc:
        return _fail("CLI_REPLY_UNREADABLE",
                     "回复不是合法 JSON：{}".format(exc))
    # Shape validation belongs to the engine (``HandoffPolicy._interpret``);
    # running it here fails *before* writing so a malformed answer never
    # reaches the inbox and blocks the next step.
    problem = _reply_problem(system, args.order, reply)
    if problem:
        return _fail("CLI_REPLY_INVALID", problem)
    written = system.handoff.reply(args.order, reply)
    return _emit({"ok": True, "order_id": args.order, "written": written,
                  "shape": "tool_calls" if "tool_calls" in reply else "payload"})


def _reply_problem(system: ResearchSystem, order_id: str, reply) -> str:
    from researchos.runtime.handoff import HandoffPolicy
    if not isinstance(reply, dict):
        return "回复必须是 JSON object"
    if "tool_calls" not in reply and "payload" not in reply:
        return "回复必须包含 tool_calls 或 payload"
    if "tool_calls" in reply:
        calls = reply.get("tool_calls")
        if not isinstance(calls, list) or not calls:
            return "tool_calls 必须是非空数组"
        order = read_order(os.path.join(system.handoff.outbox,
                                        order_id + ".json"))
        allowed = {tool["name"] for tool in order.get("tools") or []}
        for item in calls:
            if not isinstance(item, dict) or not item.get("name"):
                return "每个 tool call 都需要 name"
            if allowed and item["name"] not in allowed:
                return "工具 {} 未被授权给 {}；可用：{}".format(
                    item["name"], order.get("agent_id"),
                    ", ".join(sorted(allowed)))
    else:
        # Exercise the same interpreter the engine uses, so a payload that
        # cannot become an AgentReply is rejected now rather than at claim time.
        try:
            HandoffPolicy._interpret(reply)
        except ValueError as exc:
            return str(exc)
    return ""


def cmd_pending(args) -> int:
    # Strictly read-only: a query must never create a run.
    code = _require_run(args)
    if code:
        return code
    system = _system(args)
    return _emit(_pending_view(system))


def cmd_step(args) -> int:
    system = _system(args)
    code = _require_handoff(system)
    if code:
        return code
    try:
        with _engine_activity(system, "step"):
            return _emit(_pump(system))
    except Exception as exc:  # noqa: BLE001 - preserve CLI boundary evidence
        return _emit(failed(error(
            "CLI_STEP_RAISED",
            "{}: {}".format(type(exc).__name__, exc),
            hint="状态已保留；Lead 可再次调用 step/resume",
            cause=traceback.format_exc()[-8000:])), code=1)


def cmd_run(args) -> int:
    system = _system(args)
    code = _require_handoff(system)
    if code:
        return code
    steps = []
    for _ in range(max(1, args.max_steps)):
        state = system.store.load()
        if state.get("status") in TERMINAL:
            break
        # A pending finalization stage is work the host owes; pumping again
        # would just re-report the same packet.
        if state.get("status") == "awaiting_stage_agent" or \
                state.get("pending_final_task"):
            break
        if any(not row["has_reply"] for row in _orders(system)):
            break
        before = (state.get("status"), state.get("current_stage"),
                  len(_orders(system)))
        outcome = _pump(system)
        steps.append({"status": outcome.get("status"),
                      "phase": outcome.get("phase"),
                      "current_stage": outcome.get("current_stage"),
                      "new_orders": [row["order_id"]
                                     for row in outcome.get("new_orders") or []]})
        state = system.store.load()
        after = (state.get("status"), state.get("current_stage"),
                 len(_orders(system)))
        if after == before:
            break
    view = _pending_view(system)
    view["steps"] = steps
    if view["status"] == "awaiting_stage_agent":
        view["stage_task"] = _stage_task(system)
    return _emit(view)


def cmd_run_dir(args) -> int:
    """Print this run's directory, resolved through the configured config.

    The authority for "where does this run live" must be one function. Two
    callers used to hard-code `<engine>/runs/<id>` instead, so a config with a
    different `runs_dir` had its stage payloads written to an orphan directory.
    """
    config = SystemConfig.from_file(args.config)
    return _emit({"ok": True,
                  "run_dir": os.path.dirname(_run_state_path(config, args.run_id))})


def cmd_status(args) -> int:
    # Same rule: asking about a run must not create one.
    code = _require_run(args)
    if code:
        return code
    system = _system(args)
    return _emit({"ok": True, "status": "ok", "data": system.store.load()})


def cmd_stage(args) -> int:
    """Show what one finalization stage is allowed to see and must produce."""
    system = _system(args)
    state = system.store.load()
    if args.name not in STAGES:
        return _fail("CLI_STAGE_UNKNOWN", "未知阶段：{}".format(args.name),
                     hint="可用阶段：" + ", ".join(STAGES))
    exported = system.ports.organization.export_evidence()
    evidence = exported.get("data") if exported.get("ok") else {}
    packet = stage_package(
        args.name,
        research_question=state.get("research_question") or "",
        artifacts=state.get("final_artifacts"),
        research_manifest=state.get("research_manifest"),
        literature_status=state.get("literature_status"),
        evidence=evidence)
    return _emit({"stage": args.name, "packet": packet,
                  "current_stage": state.get("current_stage"),
                  "status": state.get("status"),
                  "remaining_stages": list(STAGES)[
                      list(STAGES).index(args.name) + 1:] if args.name in STAGES
                  else []})


def cmd_submit(args) -> int:
    """File one finalization payload through the engine's own gate.

    This is the deterministic route for a stage whose work a host performed
    itself, and the recovery route when a model turn is lost.  The payload is
    validated by ``validate_stage`` before it can advance the run.
    """
    system = _system(args)
    if args.stage not in STAGES:
        return _fail("CLI_STAGE_UNKNOWN", "未知阶段：{}".format(args.stage),
                     hint="可用阶段：" + ", ".join(STAGES))
    with open(args.file, encoding="utf-8") as handle:
        payload = json.load(handle)
    # Submit through the Director's own tool rather than `system.submit_final_result`:
    # in handoff mode the latter would publish *another work order* (the Director's
    # turn is external), while committing a stage payload is deterministic work the
    # engine must do in-process.  The tool is the same capability, same validation.
    outcome = system.agents.director.tools.call(
        "submit_final_result", {"stage": args.stage, "payload": payload})
    if not outcome.get("ok"):
        return _emit(outcome, code=1)
    state = system.store.load()
    return _emit({"ok": True, "stage": args.stage,
                  "status": state.get("status"),
                  "phase": state.get("phase"),
                  "current_stage": state.get("current_stage"),
                  "committed": sorted((state.get("final_artifacts") or {}).keys()),
                  "next": "运行 `python cli.py step` 继续"})


def cmd_agents(args) -> int:
    """Print the topology.  Inspection must not leave run directories behind."""
    import dataclasses
    import tempfile

    if args.run_id:
        system = _system(args)
        scratch = None
    else:
        scratch = tempfile.mkdtemp(prefix="researchos-inspect-")
        config = dataclasses.replace(SystemConfig.from_file(args.config),
                                     runs_dir=scratch)
        system = ResearchSystem(config, "inspect")
    rows = []
    for name, agent_id in system.agents.ids().items():
        agent = system.agents.get(name)
        # `kind` is the honest distinction. Scheme 4 merged Director into the
        # Lead, so `director` is a deterministic top-level controller: listing
        # it beside model-driven agents as if it were a peer is what kept two
        # architecture stories alive and risked it leaking back into handoff.
        if name == "director":
            kind = "controller"
        elif system._hands_off(agent_id):
            kind = "agent"
        else:
            kind = "deterministic"
        rows.append({"name": name, "id": agent_id, "role": agent.role,
                     "kind": kind, "tools": agent.tools.names()})
    value = {"agents": rows,
             "model_agents": [row["id"] for row in rows if row["kind"] == "agent"],
             "controllers": [row["id"] for row in rows
                             if row["kind"] == "controller"],
             "deterministic_modules": [row["id"] for row in rows
                                       if row["kind"] == "deterministic"],
             "thinking_agents": list(system.THINKING_AGENTS),
             "handoff_enabled": bool(system.handoff),
             "note": ("`director` is a deterministic controller, not a model "
                      "agent: scheme 4 merged it into the Lead. It publishes no "
                      "work order and owns no session.")}
    if args.run_id and os.path.isdir(system.store.root):
        value["actors"] = system.actors()
    if scratch is not None:
        import shutil
        shutil.rmtree(scratch, ignore_errors=True)
    return _emit(value)


def _project_root(start: str) -> str:
    """Nearest ancestor with ``.git`` (DSH's own project-root rule)."""
    current = os.path.abspath(start)
    while True:
        if os.path.exists(os.path.join(current, ".git")):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            return os.path.abspath(start)
        current = parent


def cmd_install(args) -> int:
    """Make the skills visible to DSH without duplicating their source.

    DSH discovers skills at ``<projectRoot>/.agents/skills`` and
    ``<projectRoot>/.dsh/skills``, where the project root is the nearest
    ancestor containing ``.git``.  A product nested inside a larger repository
    therefore has to be linked into that root once.  Symlinks keep this
    directory the single source of truth; ``--copy`` is for filesystems that
    cannot follow links.
    """
    import shutil

    source = os.path.join(HERE, ".agents", "skills")
    if not os.path.isdir(source):
        return _fail("CLI_NO_SKILLS", "缺少 .agents/skills：{}".format(source))
    root = os.path.abspath(args.project or _project_root(HERE))
    target = os.path.join(root, ".agents", "skills")
    os.makedirs(target, exist_ok=True)
    installed = []
    for name in sorted(os.listdir(source)):
        src = os.path.join(source, name)
        if not os.path.isdir(src):
            continue
        dst = os.path.join(target, name)
        if os.path.lexists(dst):
            if os.path.islink(dst) and os.path.realpath(dst) == os.path.realpath(src):
                installed.append({"name": name, "status": "already-linked",
                                  "path": dst})
                continue
            installed.append({"name": name, "status": "skipped-exists",
                              "path": dst})
            continue
        if args.copy:
            shutil.copytree(src, dst)
            installed.append({"name": name, "status": "copied", "path": dst})
        else:
            os.symlink(src, dst, target_is_directory=True)
            installed.append({"name": name, "status": "linked", "path": dst})
    return _emit({
        "ok": True,
        "project_root": root,
        "skills_dir": target,
        "installed": installed,
        "agents_md": os.path.join(HERE, "AGENTS.md"),
        "note": ("技能已出现在项目技能根；DSH 会在下一个会话目录里加载。"
                 "AGENTS.md 仍从会话工作目录沿祖先链加载。"),
    })


def cmd_selftest(args) -> int:
    command = [sys.executable, "-m", "unittest", "discover",
               "-s", os.path.join(HERE, "tests"), "-t", HERE, "-q"]
    if args.pattern:
        command += ["-p", args.pattern]
    completed = subprocess.run(command, cwd=HERE, text=True,
                               capture_output=True)
    tail = (completed.stderr or completed.stdout or "").strip().splitlines()
    return _emit({"ok": completed.returncode == 0,
                  "returncode": completed.returncode,
                  "command": " ".join(command),
                  "output_tail": tail[-25:]},
                 code=0 if completed.returncode == 0 else 1)


def _search_options(args) -> dict:
    options: dict = {}
    if args.papers:
        with open(args.papers, encoding="utf-8") as handle:
            value = json.load(handle)
        options["papers"] = value.get("papers", value) if isinstance(
            value, dict) else value
    if args.search_output:
        options["search_output"] = os.path.abspath(args.search_output)
    if args.max_per_source:
        options["max_per_source"] = args.max_per_source
    if args.fetch_top:
        options["fetch_top"] = args.fetch_top
    if args.parse_top:
        options["parse_top"] = args.parse_top
    if args.expand_top:
        options["expand_top"] = args.expand_top
    return options


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cli.py",
        description="ResearchOS handoff driver for DeepSeek Harness")
    parser.add_argument("--config",
                        default=os.path.join(HERE, "config", "mode.json"))
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="初始化 run 并发布第一张工单")
    init.add_argument("--run-id", required=True)
    init.add_argument("--question", required=True)
    init.add_argument("--papers", help="离线语料 JSON（papers 数组或对象）")
    init.add_argument("--search-output", help="复用一个已存在的检索产物目录")
    init.add_argument("--max-per-source", type=int)
    init.add_argument("--fetch-top", type=int)
    init.add_argument("--parse-top", type=int)
    init.add_argument("--expand-top", type=int)
    init.set_defaults(func=cmd_init)

    for name, func, help_text in (
            ("orders", cmd_orders, "列出待处理工单"),
            ("pending", cmd_pending, "一行式进度与待处理工单"),
            ("step", cmd_step, "消费回复、推进一次、发布下一张工单"),
            ("status", cmd_status, "打印顶层 state.json"),
            ("run-dir", cmd_run_dir, "打印该 run 的目录（按 config 解析）")):
        command = sub.add_parser(name, help=help_text)
        command.add_argument("--run-id", required=True)
        command.set_defaults(func=func)

    show = sub.add_parser("show", help="渲染一张工单（默认给宿主的完整文档）")
    show.add_argument("--run-id", required=True)
    show.add_argument("--order", required=True)
    show.add_argument("--format", choices=("full", "json", "brief"),
                      default="full", help="full=给宿主的完整文档；json=原始工单；brief=人读")
    show.set_defaults(func=cmd_show)

    reply = sub.add_parser("reply", help="写入并校验一张工单的回复")
    reply.add_argument("--run-id", required=True)
    reply.add_argument("--order", required=True)
    reply.add_argument("--file")
    reply.add_argument("--json")
    reply.set_defaults(func=cmd_reply)

    run = sub.add_parser("run", help="在没有未回复工单时连续推进")
    run.add_argument("--run-id", required=True)
    run.add_argument("--max-steps", type=int, default=200)
    run.set_defaults(func=cmd_run)

    stage = sub.add_parser("stage", help="查看某个终段的输入包")
    stage.add_argument("--run-id", required=True)
    stage.add_argument("--name", required=True)
    stage.set_defaults(func=cmd_stage)

    submit = sub.add_parser("submit", help="提交某个终段的 payload")
    submit.add_argument("--run-id", required=True)
    submit.add_argument("--stage", required=True, choices=list(STAGES))
    submit.add_argument("--file", required=True)
    submit.set_defaults(func=cmd_submit)

    agents = sub.add_parser("agents", help="打印智能体拓扑与工具授权")
    agents.add_argument("--run-id", default="")
    agents.set_defaults(func=cmd_agents)

    install = sub.add_parser(
        "install", help="把本目录的技能链接到 DSH 项目根（一次即可）")
    install.add_argument("--project", help="DSH 项目根；默认向上查找 .git")
    install.add_argument("--copy", action="store_true",
                         help="复制而不是建立符号链接")
    install.set_defaults(func=cmd_install)

    selftest = sub.add_parser("selftest", help="运行仓库测试套件")
    selftest.add_argument("--pattern", default="test_*.py")
    selftest.set_defaults(func=cmd_selftest)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except FileNotFoundError as exc:
        return _fail("CLI_FILE_MISSING", str(exc))
    except ValueError as exc:
        return _fail("CLI_INVALID_INPUT", str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
