#!/usr/bin/env python3
"""Deterministic consistency check for one ResearchOS run.

Validates the *artifacts and state on disk*, not scientific correctness and not
whether a subagent was really spawned.  Exit code 0 means the run is internally
consistent with the contracts; non-zero means it is not.

Checks:
  * run state is readable and in a known status;
  * every committed final stage has a matching artifact file and satisfies the
    per-stage required-field contract;
  * ``writing`` is present only when ``audit.verdict == "pass"``;
  * the literature run reached the relation-review gate before ideation;
  * pending work orders and their inbox replies are balanced.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

HERE = os.path.abspath(os.path.dirname(__file__))
ROOT = os.path.dirname(HERE)
ENGINE = os.path.join(ROOT, "engine")
for path in (ENGINE, ROOT):
    if path not in sys.path:
        sys.path.insert(0, path)

from researchos import SystemConfig  # noqa: E402
from researchos.workflow.finalization import (STAGES,  # noqa: E402
                                              validate_stage)
from researchos.workflow.state import RunStore  # noqa: E402


DEFAULT_CONFIG = os.path.join(ROOT, "config", "handoff.json")
KNOWN = {"created", "research_running", "literature_initializing",
         "awaiting_external_agent", "awaiting_stage_agent", "final_synthesis",
         "final_revision", "completed", "blocked_research", "failed"}


def load_json(path: str):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def check(config_path: str, run_id: str) -> dict:
    config = SystemConfig.from_file(config_path)
    store = RunStore(config.runs_dir, run_id)
    problems, warnings, info = [], [], {"run_id": run_id, "run_dir": store.root}
    if not os.path.isfile(store.state_path):
        return {"ok": False, "stage": "state", "problems": ["state.json 不存在"],
                "warnings": [], "info": info}

    state = store.load()
    status = state.get("status")
    info.update({"status": status, "phase": state.get("phase"),
                 "current_stage": state.get("current_stage")})
    if status not in KNOWN:
        problems.append("未知状态：{}".format(status))

    artifacts = state.get("final_artifacts") or {}
    paths = state.get("artifact_paths") or {}
    for stage in artifacts:
        if stage not in STAGES:
            problems.append("未知终段：{}".format(stage))
            continue
        # The canonical location is what the engine writes and what a moved or
        # copied run directory still carries.  ``artifact_paths`` records the
        # absolute path from the machine that produced the run, so it must not
        # be the primary source of truth here.
        path = os.path.join(store.root, "artifacts", stage + ".json")
        if not os.path.isfile(path):
            recorded = paths.get(stage) or "<未记录>"
            problems.append("{}: 状态记录了产物但文件不存在 {}（记录的路径：{}）".format(
                stage, path, recorded))
            continue
        try:
            payload = load_json(path)
        except (OSError, ValueError) as exc:
            problems.append("{}: 产物不可读 {}".format(stage, exc))
            continue
        checked = validate_stage(stage, payload)
        if not checked.get("ok"):
            problems.append("{}: 不满足契约 {}".format(
                stage, (checked.get("error") or {}).get("message")))
        recorded = artifacts.get(stage)
        # ``artifact_paths`` may be absent in older runs; only warn when both
        # sides exist and genuinely disagree.
        if isinstance(recorded, dict) and payload != recorded:
            warnings.append("{}: artifacts/ 与 state.final_artifacts 不一致".format(stage))

    audit = (artifacts.get("audit") or {})
    if "writing" in artifacts and audit.get("verdict") != "pass":
        problems.append("writing 已落地但 audit.verdict != pass")

    pending = (state.get("pending_final_task") or {}).get("stage")
    if status == "awaiting_stage_agent" and pending not in STAGES:
        problems.append("awaiting_stage_agent 但 pending_final_task.stage 非法")

    literature_path = os.path.join(config.runs_dir, run_id + "-literature",
                                   "state.json")
    if os.path.isfile(literature_path):
        literature = load_json(literature_path)
        review = literature.get("relation_review") or {}
        info["literature"] = {"status": literature.get("status"),
                              "cycle": literature.get("cycle"),
                              "relation_review": review.get("status")}
        if literature.get("status") in ("completed", "blocked_material") \
                and review.get("status") != "completed":
            problems.append("文献闭环已结束但未完成关系审阅关卡")
        if review.get("status") == "completed" and not review.get("review_notes"):
            problems.append("关系审阅缺少 review_notes")

    handoff = os.path.join(store.root, "handoff")
    if os.path.isdir(handoff):
        outbox = os.path.join(handoff, "outbox")
        inbox = os.path.join(handoff, "inbox")
        orders = sorted(name[:-5] for name in os.listdir(outbox)
                        if name.endswith(".json")) if os.path.isdir(outbox) else []
        replies = sorted(name[:-5] for name in os.listdir(inbox)
                         if name.endswith(".json")) if os.path.isdir(inbox) else []
        info["orders"] = {"pending": len(orders), "unconsumed_replies": len(replies)}
        if status in ("completed", "blocked_research") and orders:
            warnings.append("已终态但仍有 {} 张未处理工单".format(len(orders)))
        for order_id in replies:
            if order_id not in orders:
                warnings.append("inbox 中有无对应工单的回复：{}".format(order_id))

    return {"ok": not problems, "stage": "consistency", "problems": problems,
            "warnings": warnings, "info": info}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    args = parser.parse_args(argv)
    try:
        result = check(args.config, args.run_id)
    except (OSError, ValueError) as exc:
        result = {"ok": False, "stage": "config", "problems": [str(exc)],
                  "warnings": [], "info": {}}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
