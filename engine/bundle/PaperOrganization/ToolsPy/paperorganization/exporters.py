"""导出元件（元件：exporters）。

职责：把知识库导出成**行排序的 TSV**（导出层）。

为什么要这一层：
    SQLite 是二进制、git diff 看不见。导出排序后的 TSV 后，
    git 能精确显示「哪条边/哪篇论文被加了/删了」。

包含：
    export_tsv(cfg)           导出全部表
    export_tables(cfg, spec)  按自定义 spec 导出
"""
from __future__ import annotations

import os
from typing import Dict, List, Optional, Tuple

from .errors import (ErrorCode, Timer, ToolResult, fail, fail_from_exception, ok)
from .paths import KBConfig
from .schema import connect

# 默认导出规格：文件名 → (SQL, 说明)
DEFAULT_EXPORTS: Dict[str, str] = {
    "papers.tsv": ("SELECT paper_id,title,year,preprint_year,venue,reading_depth,"
                   "method_family,citation_count,in_corpus FROM papers ORDER BY paper_id"),
    "media.tsv": ("SELECT media_id,paper_id,kind,label,page_first,page_last,bbox,"
                  "coords_n_raw_boxes FROM media ORDER BY media_id"),
    "citations.tsv": ("SELECT from_paper,to_paper,to_raw,internal,source FROM citations "
                      "ORDER BY from_paper,to_raw"),
    "claims.tsv": ("SELECT claim_id,paper_id,evidence_type,confidence,text FROM claims "
                   "ORDER BY claim_id"),
    "assumptions.tsv": ("SELECT assumption_id,paper_id,explicit,confidence,text FROM assumptions "
                        "ORDER BY assumption_id"),
    "claim_relations.tsv": ("SELECT rel_id,src_claim,dst_claim,kind,polarity,confidence "
                            "FROM claim_relations ORDER BY rel_id"),
    "spans.tsv": ("SELECT span_id,paper_id,section,char_start,char_end FROM span_map "
                  "ORDER BY paper_id,char_start"),
}


def _write_tsv(path: str, rows) -> Tuple[int, Optional[str]]:
    """写 TSV，返回 (行数, 错误)。制表符/换行会被替换，避免破坏结构。"""
    try:
        n = 0
        with open(path, "w", encoding="utf-8") as f:
            for row in rows:
                cells = ["" if v is None else str(v).replace("\t", " ").replace("\n", " ")
                         for v in row]
                f.write("\t".join(cells) + "\n")
                n += 1
        return n, None
    except Exception as exc:
        return 0, str(exc)


def export_tsv(cfg: KBConfig, specs: Optional[Dict[str, str]] = None) -> ToolResult:
    """导出知识库为 TSV。

    参数：
        cfg    知识库配置
        specs  自定义规格（文件名 → SQL）；默认用 DEFAULT_EXPORTS

    返回 data={"files": {name: rows}, "dir": ...}
    """
    specs = specs or DEFAULT_EXPORTS
    with Timer() as t:
        try:
            os.makedirs(cfg.exports_dir, exist_ok=True)
            conn = connect(cfg)
            files = {}
            warnings: List[str] = []
            for name, sql in specs.items():
                try:
                    rows = conn.execute(sql).fetchall()
                except Exception as exc:
                    warnings.append("{} 导出失败（SQL 错误）：{}".format(name, exc))
                    continue
                n, err = _write_tsv(os.path.join(cfg.exports_dir, name), rows)
                if err:
                    warnings.append("{} 写文件失败：{}".format(name, err))
                else:
                    files[name] = n
            conn.close()
        except Exception as exc:
            return fail_from_exception(exc, location="exporters.export_tsv",
                                       code=ErrorCode.WRITE_ERROR,
                                       message="导出失败：{}".format(exc),
                                       context={"dir": cfg.exports_dir})
    return ok({"files": files, "dir": cfg.exports_dir},
              warnings=warnings, meta={"ms": t.ms})
