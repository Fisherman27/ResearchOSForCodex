"""建表与数据库连接（元件：schema）。

表结构的单一来源，对应。
所有其他元件只 import 这里的 connect()，不重复定义建表 SQL。
"""
from __future__ import annotations

import os
import sqlite3
from typing import List, Optional

from .errors import (ErrorCode, ToolResult, fail, fail_from_exception, ok)
from .paths import KBConfig


# ---------------------------------------------------------------------------
# 建表 SQL
# ---------------------------------------------------------------------------

SCHEMA_SQL = """
-- ============ 论文图（一等节点） ============
CREATE TABLE IF NOT EXISTS papers (
    paper_id          TEXT PRIMARY KEY,
    title             TEXT,
    abstract          TEXT,                       -- 摘要（源数据有，必须保留）
    year              INTEGER,
    preprint_year     INTEGER,
    venue             TEXT,
    gist              TEXT,
    research_problem  TEXT,
    contribution      TEXT,
    method_family     TEXT,
    reading_depth     TEXT DEFAULT 'unread',
    citation_count    INTEGER,
    role              TEXT,
    in_corpus         INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS paper_relations (
    rel_id       TEXT PRIMARY KEY,
    src_paper    TEXT,
    dst_paper    TEXT,
    kind         TEXT,
    axis         TEXT,
    evidence     TEXT,
    confidence   REAL,
    generated_by TEXT
);

CREATE TABLE IF NOT EXISTS citations (
    from_paper  TEXT,
    to_paper    TEXT,          -- 解析后的目标：库内=edge.to（paper_id），库外=to_raw
    to_raw      TEXT,          -- 源里的原始 ID（总是在，用于完整性/回查）
    internal    INTEGER,       -- 1=目标在语料库内（可图遍历），0=外部 stub
    source      TEXT,
    PRIMARY KEY (from_paper, to_raw)
);

-- ============ 论点图（二等节点） ============
CREATE TABLE IF NOT EXISTS claims (
    claim_id      TEXT PRIMARY KEY,
    paper_id      TEXT,
    text          TEXT,
    evidence      TEXT,
    evidence_type TEXT,
    confidence    REAL,
    generated_by  TEXT
);

CREATE TABLE IF NOT EXISTS claim_relations (
    rel_id       TEXT PRIMARY KEY,
    src_claim    TEXT,
    dst_claim    TEXT,
    kind         TEXT,
    polarity     TEXT,
    evidence     TEXT,
    confidence   REAL,
    generated_by TEXT
);

CREATE TABLE IF NOT EXISTS assumptions (
    assumption_id TEXT PRIMARY KEY,
    paper_id      TEXT,
    text          TEXT,
    explicit      INTEGER,
    evidence      TEXT,
    confidence    REAL,
    generated_by  TEXT
);

-- ============ 证据对象 ============
CREATE TABLE IF NOT EXISTS media (
    media_id              TEXT PRIMARY KEY,
    paper_id              TEXT,
    kind                  TEXT,
    label                 TEXT,
    caption               TEXT,
    section_index         INTEGER,
    page_first            INTEGER,
    page_last             INTEGER,
    bbox                  TEXT,
    bboxes_per_page       TEXT,
    coords_raw            TEXT,
    coords_n_raw_boxes    INTEGER,
    coords_source         TEXT,
    coords_missing_reason TEXT,
    raw_tei               TEXT
);

CREATE TABLE IF NOT EXISTS span_map (
    span_id    TEXT PRIMARY KEY,
    paper_id   TEXT,
    section    TEXT,
    char_start INTEGER,
    char_end   INTEGER,
    text       TEXT
);

-- ============ 可演化语义索引 ============
CREATE TABLE IF NOT EXISTS semantic_relation_candidates (
    proposal_id        TEXT PRIMARY KEY,
    src_paper          TEXT NOT NULL,
    dst_paper          TEXT NOT NULL,
    relation_type      TEXT NOT NULL,
    relation_summary   TEXT,
    importance         TEXT DEFAULT 'unknown',
    importance_reason  TEXT,
    evidence_refs      TEXT,
    source_type        TEXT DEFAULT 'organization_synthesis',
    confidence         REAL,
    verification_status TEXT DEFAULT 'not_checked',
    status             TEXT DEFAULT 'candidate',
    generated_by       TEXT,
    generated_at       TEXT,
    source_snapshot    TEXT,
    revision           INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS graph_versions (
    version_id      TEXT PRIMARY KEY,
    parent_version  TEXT,
    source_snapshot TEXT,
    trigger         TEXT,
    created_at      TEXT,
    notes           TEXT
);

CREATE INDEX IF NOT EXISTS idx_citations_from ON citations(from_paper);
CREATE INDEX IF NOT EXISTS idx_citations_to   ON citations(to_paper);
CREATE INDEX IF NOT EXISTS idx_media_paper    ON media(paper_id);
CREATE INDEX IF NOT EXISTS idx_claims_paper   ON claims(paper_id);
CREATE INDEX IF NOT EXISTS idx_spans_paper    ON span_map(paper_id);
CREATE INDEX IF NOT EXISTS idx_semantic_src   ON semantic_relation_candidates(src_paper);
CREATE INDEX IF NOT EXISTS idx_semantic_dst   ON semantic_relation_candidates(dst_paper);
CREATE INDEX IF NOT EXISTS idx_semantic_state ON semantic_relation_candidates(status);
"""

# 全部表名（供前置检查/清空用）
ALL_TABLES = ("papers", "paper_relations", "citations", "claims",
              "claim_relations", "assumptions", "media", "span_map",
              "semantic_relation_candidates", "graph_versions")

# 增量迁移（CREATE TABLE IF NOT EXISTS 不会给已有表补列，必须显式 ADD COLUMN）
# 格式：(表名, 列名, 列定义)
MIGRATIONS = (
    ("papers", "abstract", "TEXT"),
)


# ---------------------------------------------------------------------------
# 连接
# ---------------------------------------------------------------------------

def connect(cfg: KBConfig) -> sqlite3.Connection:
    """打开数据库连接（不建表）。调用方负责关闭。

    行工厂设为 sqlite3.Row，可用列名访问。
    """
    os.makedirs(os.path.dirname(cfg.db_path), exist_ok=True)
    conn = sqlite3.connect(cfg.db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _apply_migrations(conn) -> List[str]:
    """给已有库补列（幂等）。返回本次新增的列描述。"""
    added = []
    for table, col, decl in MIGRATIONS:
        # 表不存在则跳过（新建库由 SCHEMA_SQL 负责）
        if not conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table,)).fetchone():
            continue
        cols = {r[1] for r in conn.execute("PRAGMA table_info({})".format(table))}
        if col not in cols:
            conn.execute("ALTER TABLE {} ADD COLUMN {} {}".format(table, col, decl))
            added.append("{}.{}".format(table, col))
    return added


def ensure_schema(cfg: KBConfig) -> ToolResult:
    """建表 + 增量迁移（幂等）。返回 data={"tables": [...], "migrated": [...]}。"""
    try:
        cfg.ensure_dirs()
        conn = connect(cfg)
        conn.executescript(SCHEMA_SQL)
        migrated = _apply_migrations(conn)
        conn.commit()
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")]
        conn.close()
    except Exception as exc:
        return fail_from_exception(exc, location="schema.ensure_schema",
                                   code=ErrorCode.DB_ERROR,
                                   message="建表失败：{}".format(exc),
                                   hint="检查 kb 目录写权限与 sqlite 可用性")
    return ok({"tables": tables, "migrated": migrated}, meta={"kb_dir": cfg.kb_dir})


def clear_tables(cfg: KBConfig, tables=None) -> ToolResult:
    """清空指定表（默认全部），用于幂等重建。"""
    tables = tables or ALL_TABLES
    try:
        conn = connect(cfg)
        for t in tables:
            conn.execute("DELETE FROM {}".format(t))
        conn.commit()
        conn.close()
    except Exception as exc:
        return fail_from_exception(exc, location="schema.clear_tables",
                                   code=ErrorCode.DB_ERROR,
                                   message="清空表失败：{}".format(exc))
    return ok({"cleared": list(tables)})


# ---------------------------------------------------------------------------
# ID 工具
# ---------------------------------------------------------------------------

def safe_id(paper_id: str) -> str:
    """把 paper_id 的 ':' 和 '/' 换成 '_'，用于文件名和 ID 片段。"""
    return (paper_id or "").replace(":", "_").replace("/", "_")
