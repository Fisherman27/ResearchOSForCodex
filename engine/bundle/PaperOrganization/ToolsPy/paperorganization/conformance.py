"""符合性检查元件（元件：conformance）。

职责：逐项对照  检查知识库的组织结果与形式，返回结构化报告。
Agent 可用它做「落库后自检」，也可用于回归。

检查维度：
    A 目录结构
    B raw JSON 结构（：A/B/B2/C 四段字段）
    C SQLite 表结构（：表名+列名）
    D 数据质量（证据可解析、枚举合法、ID 规则、坐标覆盖）
    E 导出层
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List

from .errors import (ErrorCode, Timer, ToolResult, fail, fail_from_exception, ok, partial)
from .paths import KBConfig
from .schema import ALL_TABLES, connect, safe_id
from .validators import EVIDENCE_TYPES, POLARITIES, READING_DEPTHS

#要求的目录/文件
REQUIRED_DIRS = ("raw/papers", "exports")
REQUIRED_FILES = ("index.sqlite",)

#raw JSON 字段
RAW_TOP_FIELDS = ("paper_id", "metadata", "spans", "media", "understanding")
RAW_METADATA_FIELDS = ("title_variants", "authors", "year", "preprint_year", "venue",
                       "source_ids", "categories", "citation_count",
                       "citation_source", "citation_fetched_at")
RAW_SPAN_FIELDS = ("span_id", "section", "char_start", "char_end", "text")
RAW_MEDIA_FIELDS = ("media_id", "kind", "label", "caption", "section_index", "coords",
                    "bboxes_per_page", "coords_raw_boxes", "coords_n_raw_boxes",
                    "coords_source", "raw_tei")
RAW_UNDERSTANDING_FIELDS = ("gist", "research_problem", "contribution", "method",
                            "core_claims", "key_results", "assumptions", "limitations",
                            "method_family", "tags", "reading_depth",
                            "generated_by", "generated_at")

#表结构
REQUIRED_SCHEMA = {
    "papers": ["paper_id", "title", "abstract", "year", "preprint_year", "venue", "gist",
               "research_problem", "contribution", "method_family", "reading_depth",
               "citation_count", "role", "in_corpus"],
    "paper_relations": ["rel_id", "src_paper", "dst_paper", "kind", "axis",
                        "evidence", "confidence", "generated_by"],
    "citations": ["from_paper", "to_paper", "to_raw", "internal", "source"],
    "claims": ["claim_id", "paper_id", "text", "evidence", "evidence_type",
               "confidence", "generated_by"],
    "claim_relations": ["rel_id", "src_claim", "dst_claim", "kind", "polarity",
                        "evidence", "confidence", "generated_by"],
    "assumptions": ["assumption_id", "paper_id", "text", "explicit", "evidence",
                    "confidence", "generated_by"],
    "media": ["media_id", "paper_id", "kind", "label", "caption", "section_index",
              "page_first", "page_last", "bbox", "bboxes_per_page", "coords_raw",
              "coords_n_raw_boxes", "coords_source", "coords_missing_reason", "raw_tei"],
    "span_map": ["span_id", "paper_id", "section", "char_start", "char_end", "text"],
}

REQUIRED_EXPORTS = ("papers.tsv", "media.tsv", "citations.tsv")


def check_conformance(cfg: KBConfig) -> ToolResult:
    """执行全部对照检查。

    返回 data={
        "summary": {"pass":n,"fail":n,"warn":n},
        "checks": [{"level","item","detail"}, ...],
        "failures": [...], "warnings": [...]
    }
    """
    with Timer() as t:
        checks: List[Dict[str, str]] = []

        def rec(level, item, detail=""):
            checks.append({"level": level, "item": item, "detail": detail})

        # ---------- A 目录结构 ----------
        for sub in REQUIRED_DIRS:
            p = os.path.join(cfg.kb_dir, sub)
            rec("PASS" if os.path.isdir(p) else "FAIL", "目录存在: kb/{}".format(sub), p)
        for f in REQUIRED_FILES:
            p = os.path.join(cfg.kb_dir, f)
            rec("PASS" if os.path.isfile(p) else "FAIL", "文件存在: kb/{}".format(f))

        # 取一篇有完整证据的论文作为 raw JSON 样本
        sample = None
        if os.path.isdir(cfg.raw_dir):
            for fn in sorted(os.listdir(cfg.raw_dir)):
                if not fn.endswith(".json"):
                    continue
                try:
                    raw = json.load(open(os.path.join(cfg.raw_dir, fn), encoding="utf-8"))
                except Exception:
                    continue
                if raw.get("spans") and raw.get("media"):
                    sample = raw
                    break
        if sample is None:
            # 退而求其次：任意一个 JSON
            if os.path.isdir(cfg.raw_dir):
                for fn in sorted(os.listdir(cfg.raw_dir)):
                    if fn.endswith(".json"):
                        try:
                            sample = json.load(open(os.path.join(cfg.raw_dir, fn),
                                                    encoding="utf-8"))
                            break
                        except Exception:
                            pass
        if sample is None:
            rec("FAIL", "raw JSON 样本", "没有任何可读的 raw JSON")
        else:
            for k in RAW_TOP_FIELDS:
                rec("PASS" if k in sample else "FAIL", "raw 顶层字段: {}".format(k))
            md = sample.get("metadata") or {}
            for k in RAW_METADATA_FIELDS:
                rec("PASS" if k in md else "FAIL", "raw.metadata.{}".format(k))
            # source_ids 必须在 metadata 内
            if "source_ids" in sample and "source_ids" not in md:
                rec("FAIL", "metadata.source_ids 位置", "放在了顶层，要求在 metadata 内")
            if sample.get("spans"):
                for k in RAW_SPAN_FIELDS:
                    rec("PASS" if k in sample["spans"][0] else "FAIL", "raw.spans[0].{}".format(k))
            if sample.get("media"):
                for k in RAW_MEDIA_FIELDS:
                    rec("PASS" if k in sample["media"][0] else "FAIL", "raw.media[0].{}".format(k))
            u = sample.get("understanding") or {}
            for k in RAW_UNDERSTANDING_FIELDS:
                rec("PASS" if k in u else "FAIL", "raw.understanding.{}".format(k))

        # ---------- C SQLite 表结构 ----------
        try:
            conn = connect(cfg)
            existing = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            for tbl, cols in REQUIRED_SCHEMA.items():
                if tbl not in existing:
                    rec("FAIL", "表存在: {}".format(tbl), "缺失")
                    continue
                rec("PASS", "表存在: {}".format(tbl))
                actual = {r[1] for r in conn.execute("PRAGMA table_info({})".format(tbl))}
                for c in cols:
                    rec("PASS" if c in actual else "FAIL", "  {}.{}".format(tbl, c))

            # ---------- D 数据质量 ----------
            span_ids = {r[0] for r in conn.execute("SELECT span_id FROM span_map")}
            media_ids = {r[0] for r in conn.execute("SELECT media_id FROM media")}

            # D1 evidence 可解析
            bad_ev = []
            for r in conn.execute("SELECT claim_id, evidence FROM claims"):
                ev = r["evidence"]
                if not ev:
                    continue
                try:
                    ids = json.loads(ev) if str(ev).startswith("[") else [ev]
                except Exception:
                    ids = [ev]
                for i in ids:
                    if i and i not in span_ids and i not in media_ids:
                        bad_ev.append((r["claim_id"], i))
            rec("PASS" if not bad_ev else "FAIL",
                "claims.evidence 可解析到 span/media",
                "{} 条悬空".format(len(bad_ev)) if bad_ev else "全部命中")

            # D2 枚举
            bad = conn.execute(
                "SELECT COUNT(*) FROM claims WHERE evidence_type != '' AND evidence_type NOT IN "
                "('theoretical','empirical','ablation','cited')").fetchone()[0]
            rec("PASS" if bad == 0 else "FAIL", "claims.evidence_type 枚举合法",
                "{} 条非法".format(bad))

            bad = conn.execute(
                "SELECT COUNT(*) FROM claim_relations WHERE polarity NOT IN "
                "('positive','negative','neutral')").fetchone()[0]
            rec("PASS" if bad == 0 else "FAIL", "claim_relations.polarity 枚举合法",
                "{} 条非法".format(bad))

            bad = conn.execute(
                "SELECT COUNT(*) FROM papers WHERE reading_depth NOT IN "
                "('unread','first_pass','second_pass','third_pass')").fetchone()[0]
            rec("PASS" if bad == 0 else "FAIL", "papers.reading_depth 枚举合法",
                "{} 条非法".format(bad))

            # D3 ID 规则
            bad = conn.execute("SELECT COUNT(*) FROM claims WHERE claim_id NOT LIKE 'c\\_%' ESCAPE '\\'").fetchone()[0]
            rec("PASS" if bad == 0 else "FAIL", "claim_id 命名 (c_<safe_id>_<n>)", "{} 条不符".format(bad))
            bad = conn.execute("SELECT COUNT(*) FROM assumptions WHERE assumption_id NOT LIKE 'a\\_%' ESCAPE '\\'").fetchone()[0]
            rec("PASS" if bad == 0 else "FAIL", "assumption_id 命名 (a_<safe_id>_<n>)", "{} 条不符".format(bad))
            bad = conn.execute("SELECT COUNT(*) FROM claim_relations WHERE rel_id NOT LIKE 'r\\_%' ESCAPE '\\'").fetchone()[0]
            rec("PASS" if bad == 0 else "FAIL", "rel_id 命名 (r_<hash>)", "{} 条不符".format(bad))

            # D4 坐标覆盖 + 缺失原因
            tot = conn.execute("SELECT COUNT(*) FROM media").fetchone()[0]
            withb = conn.execute("SELECT COUNT(*) FROM media WHERE bbox IS NOT NULL").fetchone()[0]
            rec("PASS", "media 坐标覆盖", "{}/{} 有 bbox".format(withb, tot))
            noreason = conn.execute(
                "SELECT COUNT(*) FROM media WHERE bbox IS NULL AND "
                "(coords_missing_reason IS NULL OR coords_missing_reason='')").fetchone()[0]
            rec("PASS" if noreason == 0 else "FAIL",
                "无坐标的 media 有 missing_reason", "{} 条缺原因".format(noreason))

            # D5 reading_depth 一致性
            bad = conn.execute("""
                SELECT COUNT(*) FROM papers p JOIN assumptions a ON p.paper_id=a.paper_id
                WHERE p.reading_depth IN ('unread','first_pass','second_pass')""").fetchone()[0]
            rec("PASS" if bad == 0 else "FAIL",
                "reading_depth 与产出一致（second_pass 无 assumptions）", "{} 篇违反".format(bad))

            # D5b 孤立性检查（曾漏检：孤儿 claim/assumption/边 会让图不完整）
            orphan_claims = conn.execute(
                """SELECT COUNT(*) FROM claims c
                   WHERE NOT EXISTS (SELECT 1 FROM papers p WHERE p.paper_id=c.paper_id)"""
            ).fetchone()[0]
            rec("PASS" if orphan_claims == 0 else "FAIL",
                "claims 都有对应论文（无孤儿）", "{} 条孤儿".format(orphan_claims))

            orphan_ass = conn.execute(
                """SELECT COUNT(*) FROM assumptions a
                   WHERE NOT EXISTS (SELECT 1 FROM papers p WHERE p.paper_id=a.paper_id)"""
            ).fetchone()[0]
            rec("PASS" if orphan_ass == 0 else "FAIL",
                "assumptions 都有对应论文（无孤儿）", "{} 条孤儿".format(orphan_ass))

            orphan_rel = conn.execute(
                """SELECT COUNT(*) FROM claim_relations r
                   WHERE NOT EXISTS (SELECT 1 FROM claims c WHERE c.claim_id=r.src_claim)
                      OR NOT EXISTS (SELECT 1 FROM claims c WHERE c.claim_id=r.dst_claim)"""
            ).fetchone()[0]
            rec("PASS" if orphan_rel == 0 else "FAIL",
                "claim_relations 两端 claim 都存在（无孤儿边）", "{} 条孤儿边".format(orphan_rel))

            # D6 表非空检查
            for tname in ALL_TABLES:
                n = conn.execute("SELECT COUNT(*) FROM {}".format(tname)).fetchone()[0]
                if tname == "paper_relations":
                    rec("WARN" if n == 0 else "PASS", "{} 状态".format(tname),
                        "{} 行（跨论文脉络，可能未产出）".format(n))
                else:
                    rec("PASS" if n > 0 else "WARN", "{} 非空".format(tname), "{} 行".format(n))

            conn.close()
        except Exception as exc:
            return fail_from_exception(exc, location="conformance.check_conformance",
                                       code=ErrorCode.DB_ERROR,
                                       message="检查数据库失败：{}".format(exc))

        # ---------- E 导出层 ----------
        for f in REQUIRED_EXPORTS:
            p = os.path.join(cfg.exports_dir, f)
            rec("PASS" if os.path.isfile(p) else "FAIL", "exports/{}".format(f))
        ptsv = os.path.join(cfg.exports_dir, "papers.tsv")
        if os.path.isfile(ptsv):
            col = [l.split("\t")[0] for l in open(ptsv, encoding="utf-8") if l.strip()]
            rec("PASS" if col == sorted(col) else "FAIL", "papers.tsv 行排序",
                "已排序" if col == sorted(col) else "未排序")

        # ---------- 汇总 ----------
        n_pass = sum(1 for c in checks if c["level"] == "PASS")
        n_fail = sum(1 for c in checks if c["level"] == "FAIL")
        n_warn = sum(1 for c in checks if c["level"] == "WARN")

    failures = [c for c in checks if c["level"] == "FAIL"]
    warnings = [c for c in checks if c["level"] == "WARN"]
    data = {
        "summary": {"pass": n_pass, "fail": n_fail, "warn": n_warn},
        "checks": checks,
        "failures": failures,
        "warnings": warnings,
    }
    if n_fail:
        return fail(ErrorCode.SCHEMA_MISMATCH,
                    "符合性检查未通过：{} 项 FAIL".format(n_fail),
                    location="conformance.check_conformance",
                    hint="查看 data.failures 逐项修正；多为字段缺失或位置错误",
                    context={"summary": data["summary"], "failures": failures[:20]},
                    warnings=["{} 项 WARN".format(n_warn)] if n_warn else None)
    # 把 data 带在成功结果里（fail 分支的 data 会被覆盖，这里重新构造）
    res = ok(data, warnings=["{} 项 WARN".format(n_warn)] if n_warn else [])
    res.meta["ms"] = t.ms
    return res
