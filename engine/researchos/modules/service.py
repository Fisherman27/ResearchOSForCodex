"""Module services: one facade per research module, shared by both modes.

`ModuleServices` is the *only* place that knows how a logical module maps onto
the ported tool libraries. The orchestrator and the tests all call these
methods, so "one implementation, many callers" is a structural fact: there is
exactly one function body per capability, and no user-facing wrapper around it.

Each method returns the unified `ModuleResult` from `contracts.py`, carrying
`artifact_ids` rather than inlining large payloads -- the caller fetches detail
through `read_artifact`. That keeps a phase's model context small even when the
knowledge base is large (guide section 9).
"""
from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, Iterable, List, Optional

from .contracts import (ExecutionContext, completed, blocked, failed, partial,
                        idempotency_key, module_result)
from ..runtime.envelope import is_usable


class ArtifactStore:
    """Content-addressed store for module outputs.

    Artifacts are referenced by id everywhere else, so a module result stays
    small and a retry can reuse an earlier output instead of recomputing.
    """

    def __init__(self, root: str, *, task_id: str):
        self.root = os.path.join(os.path.abspath(root), "artifacts", task_id)
        os.makedirs(self.root, exist_ok=True)

    def put(self, kind: str, value: Any, *, key: Optional[str] = None) -> str:
        stem = key or "{}-{}".format(kind, int(time.time() * 1000))
        artifact_id = "{}/{}".format(kind, stem)
        path = os.path.join(self.root, kind)
        os.makedirs(path, exist_ok=True)
        target = os.path.join(path, stem + ".json")
        temp = target + ".tmp"
        with open(temp, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, target)
        return artifact_id

    def path_of(self, artifact_id: str) -> str:
        kind, _, stem = artifact_id.partition("/")
        return os.path.join(self.root, kind, stem + ".json")

    def get(self, artifact_id: str) -> Any:
        path = self.path_of(artifact_id)
        if not os.path.isfile(path):
            return None
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)

    def exists(self, artifact_id: str) -> bool:
        return os.path.isfile(self.path_of(artifact_id))


class ModuleServices:
    """The four research modules, callable independently or under orchestration.

    Construction takes the already-built `Ports` bundle, so this class contains
    no import of the bundled libraries and can therefore be unit-tested with the
    in-memory ports as well.
    """

    def __init__(self, ports, config, *, artifacts: ArtifactStore,
                 registry=None):
        self.ports = ports
        self.config = config
        self.artifacts = artifacts
        self.registry = registry

    # -- shared helpers -------------------------------------------------
    @staticmethod
    def _envelope_ok(value: Optional[dict]) -> bool:
        return is_usable(value)

    @staticmethod
    def _err(value: Optional[dict]) -> List[str]:
        value = value or {}
        err = value.get("error") or {}
        if isinstance(err, dict) and err.get("message"):
            return ["{}: {}".format(err.get("code", "ERROR"), err["message"])]
        return []

    def _gate(self, context: ExecutionContext, tool: str) -> Optional[dict]:
        """Refuse a call the execution context did not authorize.

        This is the runtime half of "mode changes what a module may do": a
        scoped module call cannot reach a tool outside its grant, even though
        the same code path serves the orchestrator's own calls.
        """
        if context.tool_allowed(tool):
            return None
        return blocked(
            errors=["tool '{}' is outside this call's allowed_tools for "
                    "role '{}'".format(tool, context.role_id)],
            summary="调用被执行上下文拒绝：{} 不在授权工具列表内".format(tool),
            meta={"denied_tool": tool, "allowed_tools": context.allowed_tools},
        )

    # -- Research / PaperSearch ----------------------------------------
    def research_run(self, context: ExecutionContext, question: str, *,
                     run_id: str, query: str = "", reason: str = "",
                     options: Optional[Dict[str, Any]] = None,
                     idempotency: Optional[str] = None) -> dict:
        """One traceable retrieval run. Produces a corpus, not a conclusion."""
        denied = self._gate(context, "research.run")
        if denied:
            return denied
        key = idempotency or idempotency_key(
            context.task_id, context.role_id, "research.run",
            {"question": question, "run_id": run_id})
        # Idempotency: an identical retry reuses the earlier run instead of
        # re-downloading the same corpus (guide section 10).
        existing = self.artifacts.get("research/{}-{}".format(run_id, key[:8]))
        if existing is not None:
            return completed(
                artifact_ids=[("research/{}-{}".format(run_id, key[:8]))],
                summary="复用已完成的检索运行（幂等命中）",
                data={"corpus": existing.get("corpus"),
                      "count": existing.get("count", 0)},
                provenance=existing.get("provenance", []),
                meta={"idempotent_replay": True},
            )
        value = self.ports.research.run(
            question, run_id=run_id, query=query or question,
            reason=reason, options=dict(options or {}))
        if not self._envelope_ok(value):
            status = "blocked" if (value or {}).get("status") == "blocked" else "failed"
            return module_result(
                status,
                errors=self._err(value),
                summary="检索未完成",
                data=(value or {}).get("data"),
                meta={"envelope_status": (value or {}).get("status")},
            )
        data = value.get("data") or {}
        artifact = self.artifacts.put(
            "research", {"corpus": data, "provenance": data.get("provenance", [])},
            key="{}-{}".format(run_id, key[:8]))
        # The bundle's real field is `search_output` (a directory); the engine
        # never inlines the corpus. Pass through whichever the adapter produced
        # so this stays a translation, not a guess.
        corpus = (data.get("corpus") or data.get("output_dir")
                  or data.get("search_output"))
        return completed(
            artifact_ids=[artifact],
            summary="检索完成：{} 条记录".format(
                data.get("count", len(data.get("papers") or []))),
            data={"corpus": corpus, "count": data.get("count", 0)},
            provenance=data.get("provenance", []),
            meta={"run_id": run_id, "idempotency_key": key},
        )

    # -- PaperRead ------------------------------------------------------
    def read_prepare(self, context: ExecutionContext, task: dict, *,
                     material: Optional[dict] = None) -> dict:
        denied = self._gate(context, "read.prepare")
        if denied:
            return denied
        value = self.ports.read.prepare(task, material=material)
        if not self._envelope_ok(value):
            return blocked(errors=self._err(value),
                           summary="阅读材料缺失，无法准备阅读包")
        data = value.get("data") or {}
        artifact = self.artifacts.put("read-package", data)
        return completed(artifact_ids=[artifact],
                         summary="阅读包已准备（策略 {}）".format(
                             task.get("reading_strategy_id", "")),
                         data={"messages": (data or {}).get("messages"),
                               "task_id": task.get("task_id")})

    def read_validate(self, context: ExecutionContext, result: dict,
                      task: dict) -> dict:
        """Gate a model-produced ReadingResult before it may be stored."""
        denied = self._gate(context, "read.validate")
        if denied:
            return denied
        value = self.ports.read.validate(result, task)
        if not self._envelope_ok(value):
            return failed(errors=self._err(value),
                          summary="阅读结果未通过生产契约",
                          meta={"diagnostics": (value or {}).get("diagnostics")})
        artifact = self.artifacts.put("reading-result", value.get("data"))
        return completed(artifact_ids=[artifact],
                         summary="阅读结果通过契约校验",
                         data={"paper_id": (value.get("data") or {}).get("paper_id")})

    # -- PaperOrganization ---------------------------------------------
    def organization_import(self, context: ExecutionContext,
                            search_output: str) -> dict:
        denied = self._gate(context, "organization.import")
        if denied:
            return denied
        value = self.ports.organization.import_search_output(search_output)
        if not self._envelope_ok(value):
            return failed(errors=self._err(value), summary="导入检索产物失败")
        artifact = self.artifacts.put("organization", value.get("data"))
        return completed(artifact_ids=[artifact],
                         summary="知识库导入完成",
                         data={"snapshot": (value.get("data") or {})})

    def organization_snapshot(self, context: ExecutionContext) -> dict:
        denied = self._gate(context, "organization.snapshot")
        if denied:
            return denied
        value = self.ports.organization.snapshot()
        if not self._envelope_ok(value):
            return failed(errors=self._err(value), summary="读取知识图快照失败")
        artifact = self.artifacts.put("kb-snapshot", value.get("data"))
        return completed(artifact_ids=[artifact], summary="知识图快照",
                         data=value.get("data"))

    def organization_store_readings(self, context: ExecutionContext,
                                    results: Iterable[dict]) -> dict:
        denied = self._gate(context, "organization.store")
        if denied:
            return denied
        value = self.ports.organization.store_reading_results(results)
        if not self._envelope_ok(value):
            return failed(errors=self._err(value), summary="写入阅读结果失败")
        artifact = self.artifacts.put("organization-write", value.get("data"))
        return completed(artifact_ids=[artifact],
                         summary="阅读结果已写入知识库",
                         data={"snapshot": value.get("data")})

    def organization_query(self, context: ExecutionContext, paper_id: str) -> dict:
        denied = self._gate(context, "organization.query")
        if denied:
            return denied
        value = self.ports.organization.paper_understanding(paper_id)
        if not self._envelope_ok(value):
            return blocked(errors=self._err(value),
                           missing_dependencies=[paper_id],
                           summary="知识库中没有该论文的可用理解")
        artifact = self.artifacts.put("paper-understanding", value.get("data"),
                                      key=paper_id)
        return completed(artifact_ids=[artifact],
                         summary="论文 {} 的知识状态".format(paper_id),
                         data=value.get("data"))

    # -- PaperScheduler -------------------------------------------------
    def scheduler_plan(self, context: ExecutionContext, *,
                       completed_papers: Optional[List[str]] = None,
                       batch_size: Optional[int] = None) -> dict:
        denied = self._gate(context, "scheduler.plan")
        if denied:
            return denied
        snapshot = self.ports.organization.snapshot()
        if not self._envelope_ok(snapshot):
            return blocked(errors=self._err(snapshot),
                           missing_dependencies=["knowledge_graph"],
                           summary="无法读取知识图，不能制定阅读计划")
        value = self.ports.scheduler.plan(
            snapshot.get("data") or {},
            strategy=self.config.planner_strategy,
            batch_size=batch_size or self.config.batch_size,
            completed=completed_papers or [])
        if not self._envelope_ok(value):
            return failed(errors=self._err(value), summary="生成阅读计划失败")
        data = value.get("data") or {}
        artifact = self.artifacts.put("reading-plan", data)
        # The scheduler's own vocabulary is `selected`/`ordered`, not `tasks`.
        # Exposing the real field is what keeps this adapter a translation
        # rather than a second implementation.
        selected = data.get("selected") or []
        ordered = data.get("ordered") or []
        envelope_status = (value or {}).get("status")
        status = "partial" if envelope_status == "partial" else (
            "completed" if envelope_status in ("ok", "empty") else "failed")
        return module_result(
            status,
            artifact_ids=[artifact],
            summary="阅读计划：{} 篇入选 / {} 篇待读{}".format(
                len(selected), len(ordered),
                "" if ordered else "（当前没有可读候选：材料缺失或已全部读过）"),
            data={"selected": selected, "ordered": ordered,
                  "reasons": data.get("reasons") or {},
                  "unavailable_material": data.get("unavailable_material") or []},
            warnings=list((value or {}).get("warnings") or []),
            meta={"snapshot_id": data.get("snapshot_id"),
                  "strategy": data.get("strategy")})

    def scheduler_select_context(self, context: ExecutionContext, *,
                                 target_paper: str) -> dict:
        denied = self._gate(context, "scheduler.context")
        if denied:
            return denied
        related = self.ports.organization.related_knowledge(target_paper)
        knowledge = (related.get("data") or {}).get("items") if self._envelope_ok(
            related) else []
        value = self.ports.scheduler.select_context(
            target_paper, knowledge or [],
            strategy=self.config.context_strategy,
            budget_tokens=self.config.context_budget_tokens)
        if not self._envelope_ok(value):
            return partial(errors=self._err(value),
                           summary="上下文选择降级：使用空上下文",
                           data={"context": []})
        data = value.get("data") or {}
        artifact = self.artifacts.put("reading-context", data, key=target_paper)
        return completed(artifact_ids=[artifact],
                         summary="已为 {} 选择前置知识".format(target_paper),
                         data=data)

    # -- task lifecycle -------------------------------------------------
    def archive_task(self, task_id: str) -> dict:
        """Archive sessions after a task; artifacts are never deleted (R2/R3)."""
        touched = []
        if self.registry is not None:
            touched = self.registry.archive_task(task_id)
        return completed(
            summary="任务 {} 已归档 {} 个会话（产物保留）".format(task_id, len(touched)),
            data={"archived": [row.session_id for row in touched]},
        )
