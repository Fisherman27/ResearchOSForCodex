"""阅读结果落库（元件：store）。

职责：
    接收 PaperRead 的规范化阅读结果 → 校验 → 写 SQLite（活值）+ raw JSON（understanding 段）。

设计原则：
    - 校验不过就拒收，**全有或全无**（事务保证，不落半截数据）。
    - ID 由本模块生成（claim_id/assumption_id/rel_id），PaperRead 不管 ID。
    - 幂等：重跑同一条结果不产生重复行。

对外主函数：
    store_reading_result(result, cfg)    落一篇阅读结果
    store_claim_relations(paper_id, rels, cfg)  落论点间关系
"""
from __future__ import annotations

import json
from typing import Any, Dict, List

from .errors import (ErrorCode, ToolResult, Timer, fail, fail_from_exception, ok)
from .ids import assumption_id, claim_id, relation_id
from .paths import KBConfig
from .schema import connect, safe_id
from .validators import validate_reading_result
from . import writers


# ---------------------------------------------------------------------------
# SQLite 写入（内部）
# ---------------------------------------------------------------------------

def _text_value(value: Any) -> str:
    """SQLite TEXT 的确定性编码；保留列表/对象结构而非 Python repr。"""
    if value is None:
        return ""
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)

def _update_papers(conn, result: Dict[str, Any]):
    """更新 papers 表的理解字段（活值）。"""
    conn.execute(
        """UPDATE papers
           SET gist=?, research_problem=?, contribution=?, method_family=?, reading_depth=?
           WHERE paper_id=?""",
        (_text_value(result.get("gist", "")),
         _text_value(result.get("research_problem", "")),
         _text_value(result.get("contribution", "")), result.get("method_family", ""),
         result.get("reading_depth", "unread"), result["paper_id"]))


def _insert_claims(conn, result: Dict[str, Any]) -> int:
    pid = result["paper_id"]
    n = 0
    for i, c in enumerate(result.get("core_claims") or []):
        cid = claim_id(pid, i + 1)
        ev = c.get("evidence")
        ev_str = json.dumps(ev, ensure_ascii=False) if isinstance(ev, list) else ev
        conn.execute(
            """INSERT OR REPLACE INTO claims
               (claim_id, paper_id, text, evidence, evidence_type, confidence, generated_by)
               VALUES (?,?,?,?,?,?,?)""",
            (cid, pid, c.get("text", ""), ev_str,
             c.get("evidence_type", ""), c.get("confidence"),
             result.get("generated_by", "")))
        n += 1
    return n


def _insert_assumptions(conn, result: Dict[str, Any]) -> int:
    pid = result["paper_id"]
    n = 0
    for i, a in enumerate(result.get("assumptions") or []):
        aid = assumption_id(pid, i + 1)
        conn.execute(
            """INSERT OR REPLACE INTO assumptions
               (assumption_id, paper_id, text, explicit, evidence, confidence, generated_by)
               VALUES (?,?,?,?,?,?,?)""",
            (aid, pid, a.get("text", ""), 1 if a.get("explicit") else 0,
             _text_value(a.get("evidence")), a.get("confidence"),
             result.get("generated_by", "")))
        n += 1
    return n


def _write_understanding(cfg: KBConfig, result: Dict[str, Any]) -> ToolResult:
    """把 understanding 段写进 raw JSON（原话存档）。"""
    r = writers.load_raw_json(cfg, result["paper_id"])
    if r.ok:
        raw = r.data
    else:
        # raw 不存在：给个空壳并记 warning（不阻断落库，但要让调用方知道）
        raw = {"paper_id": result["paper_id"], "understanding": {}}
    # 保存完整、版本化的生产 ReadingResult。SQLite 只承载常用索引字段，
    # raw JSON 是深层结构化知识的权威存档，禁止静默丢弃新 Schema 字段。
    understanding = {
        "gist": "", "research_problem": "", "contribution": [],
        "method": {"what": "", "design_rationale": ""},
        "core_claims": [], "key_results": [], "assumptions": [],
        "limitations": {"acknowledged": [], "unacknowledged": []},
        "method_family": "", "tags": [], "reading_depth": "unread",
        "generated_by": "", "generated_at": "",
    }
    understanding.update({key: value for key, value in result.items()
                          if key != "paper_id"})
    raw["understanding"] = understanding
    out = writers.write_raw_json(cfg, result["paper_id"], raw)
    out.warnings = list(out.warnings) + ([] if r.ok else
                                         ["raw JSON 原不存在，已新建空壳：{}".format(result["paper_id"])])
    return out


# ---------------------------------------------------------------------------
# 对外主函数
# ---------------------------------------------------------------------------

def store_reading_result(result: Dict[str, Any], cfg: KBConfig,
                         strict_evidence: bool = True) -> ToolResult:
    """落一篇论文的阅读结果。

    参数：
        result           PaperRead 的规范化输出
        cfg              知识库配置
        strict_evidence  是否严格校验 evidence 可解析（默认 True）：
                         True  → evidence 指向不存在的 span/media 时**拒收**（防幻觉入库）
                         False → 仅记 warning（宽松模式，存量数据修复时可用）

    返回 ToolResult，data={"paper_id","reading_depth","claims","assumptions",
                          "relations","removed"}

    ★ 四条硬性保证（都是实测发现的问题修复）：
        1. **论文必须已存在**：paper_id 不在 papers 表 → 拒收，不产生孤儿数据。
        2. **evidence 必须可解析**：指向不存在的证据 → 拒收（默认），不写入幻觉证据。
        3. **替换语义**：本次结果覆盖该论文的全部 claims/assumptions，
           多余的旧记录会被**删除**，不残留幽灵数据。
        4. **claim_relations 一并落库**（曾经只校验不写入，导致边被静默丢弃）：
           边里的 src_claim / dst_claim 支持两种写法（见 ids.py）：
             "arxiv:2609.11923#2"   ← 论文ID + 序号（方案A，跨论文）
             "#2"                   ← 省略论文ID，指本论文
             "c_arxiv_2609.11923_2" ← 已是 claim_id，原样使用
    """
    with Timer() as t:
        # ① 校验（结构）
        v = validate_reading_result(result)
        if not v.ok:
            return v          # 直接透传结构化错误

        conn = connect(cfg)
        warnings: List[str] = []
        try:
            # ② 论文必须已存在（防止写孤儿数据）
            pid = result["paper_id"]
            exists = conn.execute(
                "SELECT 1 FROM papers WHERE paper_id=?", (pid,)).fetchone()
            if not exists:
                conn.close()
                return fail(ErrorCode.NOT_FOUND,
                            "论文不存在于知识库：{}".format(pid),
                            location="store.store_reading_result",
                            hint="先导入该论文（pipelines.import_search_output 或 --search-out），"
                                 "再落库其阅读结果；PaperRead 的 paper_id 必须与库内一致",
                            context={"paper_id": pid})

            # ③ evidence 可解析性（默认严格：拒收，防幻觉证据入库）
            dangling = _collect_dangling(conn, result)
            if dangling and strict_evidence:
                conn.close()
                return fail(ErrorCode.NOT_FOUND,
                            "evidence 指向不存在的证据（{} 处）：{}".format(
                                len(dangling), dangling[:5]),
                            location="store.store_reading_result",
                            hint="确认 span_id / media_id 拼写；或该证据尚未导入。"
                                 "如确需宽松写入，用 strict_evidence=False",
                            context={"dangling": dangling})
            if dangling:
                warnings.append("evidence 悬空（宽松模式已写入）：{}".format(dangling[:5]))

            # ④ 写库（事务：全有或全无）
            _update_papers(conn, result)
            # 先删多余旧记录（替换语义）；它可能连带清掉指向本论文旧 claim 的边
            removed, edges_lost = _replace_children(conn, result)
            n_claims = _insert_claims(conn, result)
            n_ass = _insert_assumptions(conn, result)

            # ⑤ 落库关系边
            #   ★ 顺序很关键：必须**先写完 claims** 再解析端点。
            #     因为本论文自己的 "#1" 引用的是本次刚写入的 claim，
            #     若在写 claims 之前预检，自引用必然解析失败（鸡生蛋问题）。
            n_rel, rel_warnings = _insert_relations(conn, result)
            warnings.extend(rel_warnings)

            # ⑥ 严格模式：端点无法解析则整体回滚（避免边被静默丢弃）
            if strict_evidence and rel_warnings:
                conn.rollback()
                conn.close()
                return fail(ErrorCode.NOT_FOUND,
                            "关系边端点无法解析，已回滚本次落库（{} 条边未写入）".format(
                                len(rel_warnings)),
                            location="store.store_reading_result",
                            hint="端点写法应为 '<paper_id>#<序号>'（跨论文，对端须**已落库**）"
                                 "或 '#<本论文序号>'；也接受已是 claim_id 的写法。"
                                 "整篇回滚是为了避免「论文写进去了、边却静默丢了」",
                            context={"reasons": rel_warnings[:3]})

            conn.commit()

            if edges_lost:
                warnings.append(
                    "替换论文 {} 的旧 claims 时，连带清理了 {} 条指向它们的旧关系边"
                    "（这些边可能来自其它论文，需要时可重新建立）".format(pid, edges_lost))
        except Exception as exc:
            conn.rollback()
            conn.close()
            return fail_from_exception(exc, location="store.store_reading_result",
                                       code=ErrorCode.DB_ERROR,
                                       message="落库失败（已回滚）：{}".format(exc),
                                       context={"paper_id": result.get("paper_id")})
        finally:
            try:
                conn.close()
            except Exception:
                pass

        # ⑤ 写 JSON 存档
        jr = _write_understanding(cfg, result)
        if not jr.ok:
            return fail(ErrorCode.WRITE_ERROR,
                        "写 raw JSON 失败：{}".format(jr.error),
                        location="store.store_reading_result",
                        hint="检查 kb/raw/papers 目录写权限")
        warnings.extend(jr.warnings)

    return ok({
        "paper_id": result["paper_id"],
        "reading_depth": result.get("reading_depth", "unread"),
        "claims": n_claims,
        "assumptions": n_ass,
        "relations": n_rel,        # 一并落库的关系边数
        "removed": removed,        # 被替换掉的旧记录数（透明化替换行为）
    }, warnings=warnings, meta={"ms": t.ms})


def _collect_dangling(conn, result: Dict[str, Any]) -> List[str]:
    """收集 result 里所有指向不存在证据的 ID（claims / assumptions / relations）。"""
    dangling: List[str] = []

    def check(ev):
        d = _evidence_ok(conn, ev)
        if d:
            dangling.append(d)

    for c in (result.get("core_claims") or []):
        check(c.get("evidence"))
    for a in (result.get("assumptions") or []):
        check(a.get("evidence"))
    for rel in (result.get("claim_relations") or []):
        check(rel.get("evidence"))
    for row in ((result.get("critical_appraisal") or {}).get(
            "quality_assessments") or []):
        check(row.get("evidence"))
    for row in (result.get("evidence_quotes") or []):
        check(row.get("span_id"))
    return dangling


def _replace_children(conn, result: Dict[str, Any]):
    """替换语义：删除该论文多余的旧 claims/assumptions，并清理因此产生的孤儿关系边。

    落库用 INSERT OR REPLACE，只能覆盖「前 N 条」；若本次结果条目比上次少，
    多出来的旧记录会残留成幽灵数据。这里先把该论文的全部子记录删掉，
    再按本次结果重新写入 —— 保证「库内状态 == 本次结果」。

    ★ 连带清理：删掉的 claim 可能被 claim_relations 引用。若不处理，
      会留下指向不存在 claim 的孤儿边。这里同步清除。

    返回 (被删除的行数, 被连带清理的关系边数)。
    边数单独返回，是为了让调用方知道「有别的论文建立的边被牵扯进来了」。
    """
    pid = result["paper_id"]
    n = 0
    cur = conn.execute("DELETE FROM claims WHERE paper_id=?", (pid,))
    n += cur.rowcount or 0
    cur = conn.execute("DELETE FROM assumptions WHERE paper_id=?", (pid,))
    n += cur.rowcount or 0
    # 清理孤儿边：两端任一 claim 已不存在的边
    cur = conn.execute(
        """DELETE FROM claim_relations
           WHERE NOT EXISTS (SELECT 1 FROM claims c WHERE c.claim_id = src_claim)
              OR NOT EXISTS (SELECT 1 FROM claims c WHERE c.claim_id = dst_claim)""")
    edges_lost = cur.rowcount or 0
    n += edges_lost
    return n, edges_lost


def _insert_relations(conn, result: Dict[str, Any]):
    """落库本论文声明的 claim_relations。

    支持两种端点写法（见 ids.resolve_claim_ref）：
        "arxiv:2609.11923#2"   论文ID + 序号（方案A，跨论文）
        "#2"                   省略论文ID，指本论文
        "c_arxiv_2609.11923_2" 已是 claim_id，原样使用

    返回 (写入条数, warnings)。无法解析的端点记为 warning 并跳过该条边
    （不因一条边失败而回滚整篇论文的落库）。
    """
    from .ids import resolve_claim_ref, relation_id
    rels = result.get("claim_relations") or []
    if not rels:
        return 0, []

    pid = result["paper_id"]
    gen = result.get("generated_by", "")
    n = 0
    warnings: List[str] = []
    for i, rel in enumerate(rels):
        src = resolve_claim_ref(rel.get("src_claim") or "", pid, conn)
        dst = resolve_claim_ref(rel.get("dst_claim") or "", pid, conn)
        if not src or not dst:
            bad = []
            if not src:
                bad.append("src_claim={!r}".format(rel.get("src_claim")))
            if not dst:
                bad.append("dst_claim={!r}".format(rel.get("dst_claim")))
            warnings.append(
                "claim_relations[{}] 端点无法解析（{}），已跳过。"
                "写法应为 '<paper_id>#<序号>' 或 '#<本论文序号>'；"
                "跨论文的边要求对端论文**已落库**".format(i, "，".join(bad)))
            continue
        rid = relation_id(src, dst, rel.get("kind", ""), rel.get("polarity", ""))
        conn.execute(
            """INSERT OR REPLACE INTO claim_relations
               (rel_id, src_claim, dst_claim, kind, polarity, evidence, confidence, generated_by)
               VALUES (?,?,?,?,?,?,?,?)""",
            (rid, src, dst, rel.get("kind", ""), rel.get("polarity", ""),
             _text_value(rel.get("evidence")), rel.get("confidence"), gen))
        n += 1
    return n, warnings


def _evidence_ok(conn, evidence) -> str:
    """返回空串表示 OK；否则返回悬空 ID 描述。"""
    from .validators import parse_evidence
    for i in parse_evidence(evidence):
        hit = conn.execute("SELECT 1 FROM span_map WHERE span_id=?", (i,)).fetchone()
        if not hit:
            hit = conn.execute("SELECT 1 FROM media WHERE media_id=?", (i,)).fetchone()
        if not hit:
            return i
    return ""


def store_claim_relations(paper_id: str, relations: List[Dict[str, Any]],
                          cfg: KBConfig, generated_by: str = "") -> ToolResult:
    """落论点间关系（跨论文，常读多篇后补齐）。

    relations: [{"src_claim","dst_claim","kind","polarity","evidence","confidence"}, ...]
    返回 data={"written": n}
    """
    from .validators import POLARITIES, REL_KINDS

    errors = []
    for i, rel in enumerate(relations):
        if rel.get("kind") not in REL_KINDS:
            errors.append("relations[{}].kind 非法 '{}'，应为 {}".format(
                i, rel.get("kind"), list(REL_KINDS)))
        if rel.get("polarity") not in POLARITIES:
            errors.append("relations[{}].polarity 非法 '{}'，应为 {}".format(
                i, rel.get("polarity"), list(POLARITIES)))
        if "evidence" not in rel:
            errors.append("relations[{}] 缺少 evidence".format(i))
    if errors:
        return fail(ErrorCode.INVALID_VALUE, "关系边校验未通过（{} 项）".format(len(errors)),
                    location="store.store_claim_relations",
                    hint="逐项修正 context.errors 后重试",
                    context={"errors": errors})

    conn = connect(cfg)
    n = 0
    try:
        from .ids import resolve_claim_ref
        # ★ 解析端点：支持「论文ID#序号」（方案A）与「已是 claim_id」两种写法；
        #   省略论文ID 的 "#2" 使用传入的 paper_id 作为归属论文。
        resolved = []
        missing = []
        for i, rel in enumerate(relations):
            src = resolve_claim_ref(rel.get("src_claim") or "", paper_id, conn)
            dst = resolve_claim_ref(rel.get("dst_claim") or "", paper_id, conn)
            if not src:
                missing.append("relations[{}].src_claim={!r}".format(
                    i, rel.get("src_claim")))
            if not dst:
                missing.append("relations[{}].dst_claim={!r}".format(
                    i, rel.get("dst_claim")))
            resolved.append((src, dst, rel))

        if missing:
            conn.close()
            return fail(ErrorCode.NOT_FOUND,
                        "关系边端点无法解析（{} 处）".format(len(missing)),
                        location="store.store_claim_relations",
                        hint="端点写法应为 '<paper_id>#<序号>'（跨论文）或 "
                             "'#<本论文序号>'；跨论文的边要求对端论文**已落库**。"
                             "也接受已是 claim_id 的写法",
                        context={"unresolved": missing[:5]})

        for src, dst, rel in resolved:
            rid = relation_id(src, dst, rel.get("kind", ""), rel.get("polarity", ""))
            conn.execute(
                """INSERT OR REPLACE INTO claim_relations
                   (rel_id, src_claim, dst_claim, kind, polarity, evidence, confidence, generated_by)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (rid, src, dst, rel.get("kind", ""), rel.get("polarity", ""),
                 _text_value(rel.get("evidence")), rel.get("confidence"), generated_by))
            n += 1
        conn.commit()
    except Exception as exc:
        conn.rollback()
        conn.close()
        return fail_from_exception(exc, location="store.store_claim_relations",
                                   code=ErrorCode.DB_ERROR,
                                   message="写 claim_relations 失败：{}".format(exc))
    conn.close()
    return ok({"written": n})
