"""路径与配置解析（工具库的路径层）。

解决的问题（都是实际踩过的坑）：
    1. 工具被不同工作目录调用时，相对路径解析不一致 → 统一从这里解析。
    2. kb 目录位置散落在各处 → 集中定义、可被调用方覆盖。
    3. 前置条件（kb 是否存在、表是否建好）需要显式检查 → 提供 assert_ready()。

设计：
    - 所有路径要么是绝对路径，要么相对「项目根」解析（自动向上找 ResearchOS/）。
    - 不依赖环境变量（Agent 环境里未必设），但支持显式传参覆盖。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

from .errors import (ErrorCode, ToolResult, Timer, fail, ok)


# ---------------------------------------------------------------------------
# 项目根定位
# ---------------------------------------------------------------------------

def find_project_root(start: Optional[str] = None) -> Optional[str]:
    """从 start（默认本文件所在目录）向上找含 PaperOrganization/ 的目录。

    返回项目根绝对路径（如 ``<project-root>``），找不到返回 None。
    """
    cur = os.path.abspath(start or os.path.dirname(__file__))
    for _ in range(10):                       # 最多向上 10 层
        if os.path.isdir(os.path.join(cur, "PaperOrganization")):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:                     # 到根了
            break
        cur = parent
    return None


def _default_kb(tools_dir: str) -> str:
    """默认 kb 位置：ToolsPy/kb（与工具库同级，便于独立使用）。"""
    return os.path.join(os.path.dirname(tools_dir), "kb")


# ---------------------------------------------------------------------------
# 配置对象
# ---------------------------------------------------------------------------

@dataclass
class KBConfig:
    """知识库配置。

    kb_dir  : 知识库根目录（含 index.sqlite / raw/ / exports/）
    db_path : SQLite 路径（默认 kb_dir/index.sqlite）
    raw_dir : raw/papers 目录
    pdf_dir : raw/pdfs 目录
    """
    kb_dir: str

    @property
    def db_path(self) -> str:
        return os.path.join(self.kb_dir, "index.sqlite")

    @property
    def raw_dir(self) -> str:
        return os.path.join(self.kb_dir, "raw", "papers")

    @property
    def pdf_dir(self) -> str:
        return os.path.join(self.kb_dir, "raw", "pdfs")

    @property
    def exports_dir(self) -> str:
        return os.path.join(self.kb_dir, "exports")

    def ensure_dirs(self):
        """创建所需目录（幂等）。"""
        for d in (self.kb_dir, self.raw_dir, self.pdf_dir, self.exports_dir):
            os.makedirs(d, exist_ok=True)

    def to_dict(self):
        return {
            "kb_dir": self.kb_dir,
            "db_path": self.db_path,
            "raw_dir": self.raw_dir,
            "pdf_dir": self.pdf_dir,
            "exports_dir": self.exports_dir,
        }


def resolve_kb(kb: Optional[str] = None) -> ToolResult:
    """把用户传入的 kb 参数解析成 KBConfig。

    参数 kb：
        None            → 用默认位置（ToolsPy/kb，与工具库同级）
        绝对路径        → 直接用
        相对路径        → 相对**当前工作目录**解析（符合命令行直觉）

    为什么相对路径按 CWD 而不是项目根：
        用户在 ToolsPy 目录下敲 `--kb ./kb`，期望的是 ToolsPy/kb。
        按 CWD 解析才符合直觉，也避免「路径拼到意想不到的位置」这类隐蔽 bug。
    """
    tools_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # ToolsPy/
    if not kb:
        return ok(KBConfig(_default_kb(tools_dir)))
    if os.path.isabs(kb):
        return ok(KBConfig(os.path.normpath(kb)))
    return ok(KBConfig(os.path.normpath(os.path.abspath(kb))))


def assert_ready(cfg: KBConfig, need_tables: bool = True) -> ToolResult:
    """前置条件检查：kb 是否初始化、表是否建好。

    返回 ToolResult；data 为 {"tables":[...], "counts":{...}}。
    未就绪时 ok=False，code=NOT_INITIALIZED，hint 给出修复命令。
    """
    if not os.path.isdir(cfg.kb_dir):
        return fail(ErrorCode.NOT_INITIALIZED,
                    "知识库目录不存在：{}".format(cfg.kb_dir),
                    location="paths.assert_ready",
                    hint="先运行流水线初始化：python cli.py init --kb <kb>，"
                         "或调用 schema.ensure_schema()")
    if not os.path.isfile(cfg.db_path):
        return fail(ErrorCode.NOT_INITIALIZED,
                    "数据库文件不存在：{}".format(cfg.db_path),
                    location="paths.assert_ready",
                    hint="先运行：python cli.py init --kb <kb>")

    import sqlite3
    try:
        conn = sqlite3.connect(cfg.db_path)
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")]
        counts = {}
        for t in tables:
            counts[t] = conn.execute("SELECT COUNT(*) FROM {}".format(t)).fetchone()[0]
        conn.close()
    except Exception as exc:
        return fail(ErrorCode.DB_ERROR, "读取数据库失败：{}".format(exc),
                    location="paths.assert_ready", hint="检查 sqlite 文件是否损坏")

    if need_tables:
        required = {"papers", "media", "citations", "span_map",
                    "claims", "assumptions", "claim_relations", "paper_relations"}
        missing = required - set(tables)
        if missing:
            return fail(ErrorCode.NOT_INITIALIZED,
                        "缺少表：{}".format(sorted(missing)),
                        location="paths.assert_ready",
                        hint="调用 schema.ensure_schema() 建表",
                        context={"tables": tables})
    return ok({"tables": tables, "counts": counts, "config": cfg.to_dict()})
