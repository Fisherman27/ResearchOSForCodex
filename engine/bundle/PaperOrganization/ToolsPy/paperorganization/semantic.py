"""可演化语义关系候选的校验、存储与版本记录。"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Dict, List

from .errors import ErrorCode, ToolResult, fail, fail_from_exception, ok
from .schema import connect


RELATION_TYPES = {
    "method_inheritance", "theoretical_foundation", "experimental_comparison",
    "background_only", "shared_problem", "uses_dataset", "complements", "unknown",
}
STATUSES = {"candidate", "accepted", "rejected", "superseded", "needs_review"}
IMPORTANCE = {"high", "medium", "low", "unknown"}
VERIFICATION = {"not_checked", "supported", "inconclusive", "contradicted"}


def _proposal_id(item: Dict[str, Any]) -> str:
    key = "|".join(str(item.get(k, "")) for k in
                   ("src_paper", "dst_paper", "relation_type"))
    return "sr_" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def validate_relation_proposal(item: Dict[str, Any]) -> ToolResult:
    """机械校验 RelationProposal；不宣称语义判断科学正确。"""
    if not isinstance(item, dict):
        return fail(ErrorCode.INVALID_TYPE, "关系提议必须是对象",
                    location="semantic.validate_relation_proposal")
    missing = [k for k in ("src_paper", "dst_paper", "relation_type")
               if not item.get(k)]
    if missing:
        return fail(ErrorCode.MISSING_FIELD, "缺少字段：{}".format(missing),
                    location="semantic.validate_relation_proposal")
    checks = (("relation_type", RELATION_TYPES), ("status", STATUSES),
              ("importance", IMPORTANCE),
              ("verification_status", VERIFICATION))
    for field, allowed in checks:
        value = item.get(field)
        if value is not None and value not in allowed:
            return fail(ErrorCode.INVALID_ENUM,
                        "{} 非法：{}".format(field, value),
                        location="semantic.validate_relation_proposal",
                        hint="允许值：{}".format(sorted(allowed)))
    confidence = item.get("confidence")
    if confidence is not None and (not isinstance(confidence, (int, float)) or
                                   not 0 <= confidence <= 1):
        return fail(ErrorCode.INVALID_VALUE, "confidence 必须在 0..1",
                    location="semantic.validate_relation_proposal")
    return ok(item)


def store_relation_proposals(cfg, proposals: List[Dict[str, Any]], *,
                             source_snapshot: str,
                             generated_by: str = "organization-agent") -> ToolResult:
    """事务式保存关系提议，不改写 raw citations。"""
    validated = []
    for index, item in enumerate(proposals or []):
        result = validate_relation_proposal(item)
        if not result.ok:
            result.error.location += ".proposals[{}]".format(index)
            return result
        validated.append(dict(item))
    if not validated:
        return ok({"written": 0, "proposal_ids": []},
                  warnings=["没有关系提议需要保存"])
    now = datetime.now(timezone.utc).isoformat()
    try:
        conn = connect(cfg)
        known = {r[0] for r in conn.execute("SELECT paper_id FROM papers")}
        missing = sorted({p[k] for p in validated for k in ("src_paper", "dst_paper")
                          if p[k] not in known})
        if missing:
            conn.close()
            return fail(ErrorCode.NOT_FOUND, "关系端点论文不存在：{}".format(missing),
                        location="semantic.store_relation_proposals",
                        hint="先把关系两端论文导入 Organization")
        ids = []
        for item in validated:
            pid = item.get("proposal_id") or _proposal_id(item)
            ids.append(pid)
            existing = conn.execute(
                "SELECT revision FROM semantic_relation_candidates WHERE proposal_id=?",
                (pid,)).fetchone()
            revision = (existing[0] + 1) if existing else 1
            conn.execute(
                """INSERT OR REPLACE INTO semantic_relation_candidates
                   (proposal_id,src_paper,dst_paper,relation_type,relation_summary,
                    importance,importance_reason,evidence_refs,source_type,confidence,
                    verification_status,status,generated_by,generated_at,
                    source_snapshot,revision)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (pid, item["src_paper"], item["dst_paper"], item["relation_type"],
                 item.get("relation_summary", ""), item.get("importance", "unknown"),
                 item.get("importance_reason", ""),
                 json.dumps(item.get("evidence_refs") or [], ensure_ascii=False),
                 item.get("source_type", "organization_synthesis"),
                 item.get("confidence"), item.get("verification_status", "not_checked"),
                 item.get("status", "candidate"), item.get("generated_by", generated_by),
                 item.get("generated_at", now), source_snapshot, revision))
        conn.commit()
        conn.close()
    except Exception as exc:
        return fail_from_exception(exc, location="semantic.store_relation_proposals",
                                   code=ErrorCode.DB_ERROR, message=str(exc))
    return ok({"written": len(ids), "proposal_ids": ids,
               "source_snapshot": source_snapshot})


def list_relation_proposals(cfg, statuses=None) -> ToolResult:
    """读取候选/精选关系，供可视化和 Scheduler 适配。"""
    try:
        conn = connect(cfg)
        sql = "SELECT * FROM semantic_relation_candidates"
        params = []
        if statuses:
            marks = ",".join("?" for _ in statuses)
            sql += " WHERE status IN ({})".format(marks)
            params = list(statuses)
        sql += " ORDER BY proposal_id"
        rows = [dict(r) for r in conn.execute(sql, params)]
        conn.close()
        for row in rows:
            try:
                row["evidence_refs"] = json.loads(row.get("evidence_refs") or "[]")
            except ValueError:
                pass
    except Exception as exc:
        return fail_from_exception(exc, location="semantic.list_relation_proposals",
                                   code=ErrorCode.DB_ERROR, message=str(exc))
    return ok(rows, warnings=[] if rows else ["尚无语义关系提议"])


def create_graph_version(cfg, *, source_snapshot: str, trigger: str,
                         notes: str = "") -> ToolResult:
    """记录一次语义图发布/修订事件。"""
    now = datetime.now(timezone.utc).isoformat()
    try:
        conn = connect(cfg)
        parent = conn.execute(
            "SELECT version_id FROM graph_versions ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        seed = "{}|{}|{}".format(source_snapshot, trigger, now)
        version_id = "graph_" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]
        conn.execute(
            "INSERT INTO graph_versions VALUES (?,?,?,?,?,?)",
            (version_id, parent[0] if parent else None, source_snapshot,
             trigger, now, notes))
        conn.commit()
        conn.close()
    except Exception as exc:
        return fail_from_exception(exc, location="semantic.create_graph_version",
                                   code=ErrorCode.DB_ERROR, message=str(exc))
    return ok({"version_id": version_id,
               "parent_version": parent[0] if parent else None,
               "source_snapshot": source_snapshot})
