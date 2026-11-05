#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
store.py —— Structured Literature Corpus 的落盘与读取（三层）

定位
----
把内存里的 Paper 列表按的三层结构落成**可复核、可增量、可续跑**的产物。
所有产物集中在一次运行的输出目录里，删目录即回滚。

目录布局
--------
    out/
    ├── run.json           本次运行的配置 + 统计 + 时间线（复现入口）
    ├── state.json         断点：每一步是否完成（支持 --resume）
    ├── corpus.jsonl       Layer 1 每行一篇 Paper（完整结构，含溯源）
    ├── graph.jsonl        Layer 2 引用边（每行一条 A→B）
    ├── structures/        Layer 3 结构解析产物
    │   ├── <id>.json         sections/references/图表公式
    │   └── <id>.tei.xml      原始 TEI（证据留存，）
    ├── pdfs/              下载到的 PDF
    ├── report.md          人类可读报告
    └── run.log            过程日志

为什么用 JSONL 而不是一个 JSON 数组
-----------------------------------
- 追加写：跑到一半崩了，已完成的论文不会丢（这是 arxiv_harvest 那条经验）；
- 可流式读：几千篇也不占内存；
- 可 diff/grep：调试时直接看某一行。

函数索引
─────────────────────────────────────────────────────────────────────────────
  [工具]  safe_name(paper_id)              把 paper_id 变成安全的文件名
  [存储]  CorpusStore                      一次运行的产物读写
            .save_papers(papers)           写 Layer 1
            .load_papers()                 读 Layer 1
            .save_graph(papers)            写 Layer 2（引用边）
            .load_graph()                  读 Layer 2
            .save_structure(paper)         写 Layer 3（含 TEI）
            .load_structure(paper_id)      读 Layer 3
            .save_pdf(paper, data)         存 PDF 并回填路径
  [断点]  .mark_step(name, status, **info) / .step_done(name) / .state()
  [元数据] .save_run(cfg, stats, timeline) / .load_run()
  [报告]  .write_report(papers, stats)     生成 report.md
  [统计]  summarize(papers)                语料库层面的统计
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Dict, Iterable, List, Optional

from model import Paper

_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")


def safe_name(paper_id: str) -> str:
    """把 paper_id 变成安全的文件名。

    `doi:10.1038/nature12373` → `doi_10.1038_nature12373`
    paper_id 里含 `:` 和 `/`（DOI 本身就有斜杠），直接当文件名会创建出
    意料之外的子目录，所以统一替换掉。
    """
    return _SAFE_RE.sub("_", (paper_id or "unknown").strip())[:120]


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def build_id_index(papers: Iterable[Paper]) -> Dict[str, str]:
    """建立「各种外部 ID → paper_id」的索引，用于解析引用边。

    ★ 为什么必须要有它（实测踩到的严重缺陷）：
      OpenAlex 给的引用边目标是**它自己的 ID**（`W2006791053`），
      而语料库里的论文主键是 `doi:10.48550/arxiv.1811.00164`。
      不做映射，边和目标就永远 join 不上 ——
      实测 1802 条边里只有 1 条两端都在语料库内（0%），
      整张图事实上是断的，"哪些论文互相引用"根本查不出来。

    索引里放什么（每种都放，命中率最高）：
        paper_id 本身
        openalex_id：W2006791053 / https://openalex.org/W2006791053
        doi：归一化后的小写形式，以及 "doi:" 前缀形式
        arxiv_id：归一化（去版本）后的形式，以及 "arxiv:" 前缀形式
        s2_id
    """
    from model import normalize_doi, normalize_arxiv
    idx: Dict[str, str] = {}

    def put(k: str, pid: str):
        k = (k or "").strip()
        if k and k not in idx:
            idx[k] = pid

    for p in papers:
        pid = p.paper_id
        if not pid:
            continue
        put(pid, pid)
        idt = p.identity or {}
        # OpenAlex：短 ID、完整 URL、带前缀三种写法都收。
        # 大小写也归一（W123 与 w123 是同一个）。
        oa = (idt.get("openalex_id") or "").strip()
        if oa:
            short = oa.rstrip("/").rsplit("/", 1)[-1].lower()
            for form in (short, oa, oa.lower(), "openalex:" + short):
                put(form, pid)
        doi = normalize_doi(idt.get("doi") or "")
        if doi:
            for form in (doi, "doi:" + doi, "https://doi.org/" + doi,
                         "http://dx.doi.org/" + doi):
                put(form, pid)
            # ★ 原始写法（可能是 URL / 带大写）也要能查到 —— 上游数据里
            #   identity 未必都经过归一化（例如测试直接赋值、或旧产物读回）。
            raw = (idt.get("doi") or "").strip()
            if raw:
                put(raw, pid)
                put(raw.lower(), pid)
        ax = normalize_arxiv(idt.get("arxiv_id") or "")
        if ax:
            for form in (ax, "arxiv:" + ax):
                put(form, pid)
            raw = (idt.get("arxiv_id") or "").strip()
            if raw:
                put(raw, pid)
                put(raw.lower(), pid)
        s2 = (idt.get("s2_id") or "").strip()
        if s2:
            for form in (s2, s2.lower(), "s2:" + s2):
                put(form, pid)
    return idx


class CorpusStore:
    """一次运行的产物目录管理器。

    用法：
        st = CorpusStore(cfg.out_dir)          # 目录不存在会创建
        st.save_papers(papers)
        st.mark_step("search", "done", kept=len(papers))
    """

    def __init__(self, out_dir: str, resume: bool = False,
                 papers_name: str = "corpus.jsonl",
                 graph_name: str = "graph.jsonl"):
        """初始化产物目录。

        papers_name / graph_name : 允许调用方改文件名。
        ★ 为什么需要（实测踩到的破坏性 bug）：
          验收脚本按 test.md的产物清单把论文写成 `papers.jsonl`、
          边写成 `citation_edges.jsonl`，但 CorpusStore 默认读写
          `corpus.jsonl` / `graph.jsonl`。于是"复跑一致性检查"里
          load_papers() 读到 **0 篇**，紧接着 save_graph([]) 把**真实图清空**了
          （651 条边 → 0）。命名不一致导致读空 → 再写就毁数据。
          让文件名成为显式参数，才能保证"读的和写的是同一份"。
        """
        self.papers_name = papers_name
        self.graph_name = graph_name
        self.dir = os.path.abspath(out_dir)
        self.resume = bool(resume)
        os.makedirs(self.dir, exist_ok=True)
        self.structures_dir = os.path.join(self.dir, "structures")
        self.pdf_dir = os.path.join(self.dir, "pdfs")
        for d in (self.structures_dir, self.pdf_dir):
            os.makedirs(d, exist_ok=True)
        self.state_path = os.path.join(self.dir, "state.json")
        self._state = self._load_state() if resume else self._new_state()

    # ---------------- 断点状态 ----------------
    def _new_state(self) -> dict:
        return {"created_at": _now(), "updated_at": _now(), "steps": {}, "notes": []}

    def _load_state(self) -> dict:
        """读已有断点；读不出来就当新运行（不让人卡在一个坏 state 上）。"""
        try:
            with open(self.state_path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:
            return self._new_state()

    def _flush_state(self) -> None:
        self._state["updated_at"] = _now()
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self._state, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, self.state_path)   # 原子写：崩了也不会得到半个文件

    def mark_step(self, name: str, status: str, **info) -> None:
        """记录一步的状态。status: running / done / skipped / failed。

        ⚠️ 每步结束都写盘。这是 arxiv_harvest 那条教训的直接复用：
           断点必须**每步落盘**，否则中断后要么重做全部、要么数据对不上。
        """
        self._state["steps"][name] = dict(
            {"status": status, "at": _now()}, **info)
        self._flush_state()

    def step_done(self, name: str) -> bool:
        """该步是否已成功完成（用于 resume 跳过）。"""
        return (self._state.get("steps", {}).get(name, {}).get("status") == "done")

    def state(self) -> dict:
        return self._state

    def set_scope(self, scope: dict) -> None:
        """记录本次运行的"检索范围配置"快照，供下次 resume 比对漂移。"""
        self._state["scope"] = scope
        self._flush_state()

    def invalidate_steps_from(self, step_name: str) -> int:
        """把 `step_name` 及其之后所有步骤的 done 标记清掉，返回失效数量。

        ★ 为什么需要它（离线边界测试 G13）：
          resume 跳过"已完成"的步骤。若这次换了年份过滤条件，
          做过滤的步骤被跳过 ⇒ 新配置**从不生效**，而 run.json 记的是新配置。
          正确做法是：检测到配置漂移时，**主动失效受影响的步骤**让它重跑。

        用 STEP_ORDER 的顺序判断"之后"，而不是依赖 dict 顺序
        （dict 顺序在 Python 3.7+ 是插入序，但语义上不该拿来当依赖序用）。
        """
        try:
            from pipeline import STEP_ORDER
        except Exception:
            STEP_ORDER = ("STEP1-2_understand", "STEP3_search", "STEP4_dedup",
                          "STEP5_rank_initial", "STEP6_expand", "STEP7_fulltext",
                          "STEP8_structure")
        steps = self._state.get("steps", {})
        if step_name not in STEP_ORDER:
            return 0
        idx = STEP_ORDER.index(step_name)
        n = 0
        for nm in STEP_ORDER[idx:]:
            if nm in steps and steps[nm].get("status") == "done":
                # 不删除记录（保留历史便于排查），只把 status 降级为 invalidated
                steps[nm]["status"] = "invalidated"
                steps[nm]["invalidated_at"] = _now()
                steps[nm]["invalidated_reason"] = "配置漂移：{} 起失效".format(step_name)
                n += 1
        self._flush_state()
        return n

    def add_note(self, text: str) -> None:
        self._state.setdefault("notes", []).append({"at": _now(), "text": text})
        self._flush_state()

    # ---------------- Layer 1：Paper Store ----------------
    def save_papers(self, papers: Iterable[Paper]) -> int:
        """全量重写 corpus.jsonl（每行一篇）。

        为什么是全量重写而不是追加：运行中间的每一步都会改 Paper（合并、评分、
        回填全文），追加会留下同一篇的多个版本、下游不知道该用哪条。
        全量重写 + 原子替换，保证文件里**每个 paper_id 只出现一次**。
        """
        papers = list(papers)
        path = os.path.join(self.dir, self.papers_name)
        if not papers and os.path.isfile(path) and os.path.getsize(path) > 0:
            return -1          # 同上：空输入不覆盖已有非空产物
        tmp = path + ".tmp"
        n = 0
        with open(tmp, "w", encoding="utf-8") as fh:
            for p in papers:
                fh.write(json.dumps(p.to_dict(), ensure_ascii=False) + "\n")
                n += 1
        os.replace(tmp, path)
        return n

    def load_papers(self) -> List[Paper]:
        """读 Layer 1。坏行跳过并计数（不因为一行坏了丢掉整个语料库）。"""
        path = os.path.join(self.dir, self.papers_name)
        out, bad = [], 0
        if not os.path.isfile(path):
            return out
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(Paper.from_dict(json.loads(line)))
                except Exception:
                    bad += 1
        if bad:
            self.add_note("corpus.jsonl 有 {} 行无法解析，已跳过".format(bad))
        return out

    # ---------------- Layer 2：Literature Graph ----------------
    def save_graph(self, papers: Iterable[Paper]) -> int:
        """把引用关系抽成**统一的 citing → cited 边表**写 graph.jsonl。

        ★ 契约（test.md「统一 citing→cited 的 cites 边」）：
          所有边的 `rel` 恒为 "cites"，语义恒为 **from 引用 to**。
          论文自身的 `references` / `cited_by` 只是**视角**，落盘前统一方向：
              P.references 含 X   →  (from=P, to=X)      P 引用 X
              P.cited_by   含 X   →  (from=X, to=P)      X 引用 P    ← 方向翻转
          第一版把 cited_by 存成 (P → X, rel="cited_by")，下游必须知道
          "这条边方向是反的"才不会用错 —— 这类隐式约定是 bug 温床。

        去重键 = **(from, to, rel)**，用**解析后的端点**：
          同一目标可能以 `W2` 和 `doi:10.1234/b` 两种写法返回，解析后指向同一篇；
          用原名做键会留下两条重复边（离线样例可复现）。
          `to_raw_all` 保留全部原始写法作为证据。

        未解析的外部目标：`to` 为空，同时给出**稳定的 unresolved 节点 ID**
        （`unresolved:<raw>`），让外部引用也能作为图节点存在，而不是悬空值。
        多源证据合进 `sources` 列表，不再是一个论文级的单字段。
        """
        index = build_id_index(papers)
        papers = list(papers)
        # ★ 空输入保护：传空列表时不要覆盖已有的非空产物。
        #   这是上面那个"读空 → 写空 → 毁数据"链条的最后一道闸门。
        #   正常结束时 papers 不可能是空的（空语料库本身就是要报 failed 的情形），
        #   所以这个保护不会掩盖真实问题。
        path = os.path.join(self.dir, self.graph_name)
        if not papers and os.path.isfile(path) and os.path.getsize(path) > 0:
            return -1
        tmp = path + ".tmp"
        merged: Dict[tuple, dict] = {}
        order: List[tuple] = []

        def emit(edge: dict) -> None:
            # ⚠️ 未解析目标的 to 是空字符串，**不能**直接拿 (from,to,rel) 当键 ——
            #    那样同一篇论文引用的几十篇外部文献会全部塌缩成一条边
            #    （实测：修完去重后引用边从 1910 掉到 220）。
            #    未解析时用它的稳定节点 ID（unresolved:<raw>）参与去重。
            key = (edge["from"],
                   edge["to"] if edge["to"] else edge.get("unresolved", ""),
                   edge["rel"])
            if key in merged:
                ev = merged[key]
                for r in edge.get("to_raw_all", []):
                    if r and r not in ev["to_raw_all"]:
                        ev["to_raw_all"].append(r)
                for sc in edge.get("sources", []):
                    if sc and sc not in ev["sources"]:
                        ev["sources"].append(sc)
                return
            merged[key] = edge
            order.append(key)

        for p in papers:
            src = p.citation_graph.get("source", "")
            for raw in (p.citation_graph.get("references") or []):
                emit(self._edge(p.paper_id, raw, "cites", index, src, invert=False))
            for raw in (p.citation_graph.get("cited_by") or []):
                emit(self._edge(p.paper_id, raw, "cites", index, src, invert=True))

        with open(tmp, "w", encoding="utf-8") as fh:
            for key in order:
                fh.write(json.dumps(merged[key], ensure_ascii=False) + "\n")
        os.replace(tmp, path)
        return len(order)

    @staticmethod
    def _edge(from_id: str, raw: Any, rel: str, index: dict = None,
              source: str = "", invert: bool = False) -> dict:
        """构造一条统一的 citing→cited 边。

        参数
        ----
        from_id : 本文的 paper_id；invert=False 时作起点，True 时作终点
        raw     : 源里的引用目标（"W2741809807" / "doi:..." / dict）
        invert  : True = cited_by 视角，方向翻转成 (对方 → 本文)

        解析顺序：先原样查索引，再归一化后查（DOI 大小写、arXiv 版本号、
        OpenAlex URL 前缀都会导致原样匹配失败）。

        返回字段：
            from / to / to_raw / to_raw_all / internal / rel /
            sources / source / at （未解析时额外有 unresolved）
        """
        from model import normalize_doi, normalize_arxiv
        if isinstance(raw, dict):
            raw_id = str(raw.get("raw_id") or raw.get("id") or raw.get("paper_id") or "")
            hint = str(raw.get("paper_id") or "")
        else:
            raw_id, hint = str(raw), ""

        idx = index or {}
        # ★ 候选写法要覆盖大小写（实测：OpenAlex ID 是大小写不敏感的，
        #   记录里存成 `w_b`，而引用目标写的是 `W_B` —— 只按原样查会漏掉）。
        #   同理，DOI 已在 normalize_doi 里转小写，但**原始写法**也要试。
        nd = normalize_doi(raw_id)
        na = normalize_arxiv(raw_id)
        short = raw_id.rstrip("/").rsplit("/", 1)[-1] if raw_id else ""
        cands = [hint, raw_id, raw_id.lower(), raw_id.upper(),
                 short, short.lower(),
                 nd, "doi:" + nd if nd else "",
                 na, "arxiv:" + na if na else ""]
        resolved = ""
        for cand in cands:
            if not cand:
                continue
            if cand in idx:
                resolved = idx[cand]
                break
            low = cand.lower()
            if low in idx:
                resolved = idx[low]
                break

        if invert:
            frm = resolved or ("unresolved:" + raw_id if raw_id else "")
            to = from_id
        else:
            frm = from_id
            to = resolved
        internal = bool(resolved)

        edge = {"from": frm, "to": to, "to_raw": raw_id,
                "to_raw_all": [raw_id] if raw_id else [],
                "internal": internal, "rel": rel,
                "sources": [source] if source else [],
                "source": source, "at": _now()}
        if not internal:
            edge["unresolved"] = "unresolved:" + (raw_id or "unknown")
        return edge

    def load_graph(self) -> List[dict]:
        path = os.path.join(self.dir, self.graph_name)
        out = []
        if not os.path.isfile(path):
            return out
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:
                    pass
        return out

    # ---------------- Layer 3：Document Structure ----------------
    def save_structure(self, paper: Paper, tei: str = "") -> str:
        """把结构解析结果写盘，并回填 paper.full_text / document_structure 的路径。

        返回结构 JSON 的路径。TEI 只在 keep_tei 且非空时写（TEI 常有几百 KB）。
        """
        name = safe_name(paper.paper_id)
        sp = os.path.join(self.structures_dir, name + ".json")
        with open(sp, "w", encoding="utf-8") as fh:
            json.dump({"paper_id": paper.paper_id,
                       "title": paper.metadata.get("title", ""),
                       "document_structure": paper.document_structure},
                      fh, ensure_ascii=False, indent=2)
        paper.document_structure["structure_path"] = sp
        if tei:
            tp = os.path.join(self.structures_dir, name + ".tei.xml")
            with open(tp, "w", encoding="utf-8") as fh:
                fh.write(tei)
            paper.document_structure["tei_path"] = tp
        return sp

    def load_structure(self, paper_id: str) -> Optional[dict]:
        p = os.path.join(self.structures_dir, safe_name(paper_id) + ".json")
        if not os.path.isfile(p):
            return None
        with open(p, "r", encoding="utf-8") as fh:
            return json.load(fh)

    # ---------------- 全文 ----------------
    def save_pdf(self, paper: Paper, data: bytes, source: str) -> str:
        """把 PDF 写盘并回填 `paper.full_text`。返回路径。"""
        p = os.path.join(self.pdf_dir, safe_name(paper.paper_id) + ".pdf")
        with open(p, "wb") as fh:
            fh.write(data)
        paper.full_text["pdf_path"] = p
        paper.full_text["pdf_source"] = source
        paper.full_text["pdf_bytes"] = len(data)
        paper.full_text["error"] = ""
        return p

    def has_pdf(self, paper: Paper) -> bool:
        """该论文的 PDF 是否已经在本地产物目录里（resume 时避免重复下载）。"""
        p = paper.full_text.get("pdf_path") or ""
        return bool(p) and os.path.isfile(p)

    def load_pdf_bytes(self, paper: Paper) -> bytes:
        p = paper.full_text.get("pdf_path") or ""
        if p and os.path.isfile(p):
            with open(p, "rb") as fh:
                return fh.read()
        return b""

    # ---------------- 运行元数据 ----------------
    def save_run(self, cfg, stats: dict, timeline: List[dict]) -> None:
        """写 run.json：配置 + 统计 + 时间线。**复现一次检索只需要这个文件。**"""
        path = os.path.join(self.dir, "run.json")
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({
                "finished_at": _now(),
                "config": cfg.to_dict() if hasattr(cfg, "to_dict") else dict(cfg),
                "stats": stats,
                "timeline": timeline,
                "state": self._state,
            }, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)

    def load_run(self) -> dict:
        path = os.path.join(self.dir, "run.json")
        if not os.path.isfile(path):
            return {}
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)

    def log(self, text: str) -> None:
        """追加一行运行日志（同时打到 stdout 由 CLI 决定）。"""
        with open(os.path.join(self.dir, "run.log"), "a", encoding="utf-8") as fh:
            fh.write("[{}] {}\n".format(_now(), text))

    # ---------------- 报告 ----------------
    def write_report(self, papers: List[Paper], stats: dict = None) -> str:
        """生成 report.md（人可读）。

        报告的内容取舍：**只放能支撑"这次检索是否可信"的信息** ——
        来源分布、年份分布、主题分布、引用图规模、全文/结构覆盖率、
        以及排序理由。不堆砌论文列表（那看 corpus.jsonl）。
        """
        st = stats or summarize(papers)
        lines = ["# 检索结果报告", "",
                 "生成时间：{}".format(_now()),
                 "语料库：`{}`".format(os.path.join(self.dir, "corpus.jsonl")), "",
                 "## 一、规模", "",
                 "| 指标 | 值 |", "|---|---|",
                 "| 论文数 | {} |".format(st.get("papers", 0)),
                 "| 有摘要 | {} |".format(st.get("with_abstract", 0)),
                 "| 有 PDF | {} |".format(st.get("with_pdf", 0)),
                 "| 已结构解析 | {} |".format(st.get("with_structure", 0)),
                 "| 引用边 | {} |".format(st.get("edges", 0)),
                 "| 能落到语料库内的引用边 | {} |".format(st.get("edges_internal", 0)),
                 ""]
        lines += ["## 二、来源分布（发现渠道）", "", "| 来源 | 命中次数 |", "|---|---|"]
        for k, v in sorted((st.get("by_source") or {}).items(), key=lambda x: -x[1]):
            lines.append("| {} | {} |".format(k, v))
        lines += ["", "## 三、年份分布", "", "| 年份 | 篇数 |", "|---|---|"]
        for k, v in sorted((st.get("by_year") or {}).items(), reverse=True)[:15]:
            lines.append("| {} | {} |".format(k, v))
        lines += ["", "## 四、主题分布（前 15）", "", "| 主题 | 篇数 |", "|---|---|"]
        for k, v in sorted((st.get("by_topic") or {}).items(), key=lambda x: -x[1])[:15]:
            lines.append("| {} | {} |".format(k, v))
        lines += ["", "## 五、Top 20（按相关度）", "",
                  "| # | 分数 | 年份 | 标题 | 来源 | 全文 |", "|---|---|---|---|---|---|"]
        for i, p in enumerate(papers[:20], 1):
            src = ",".join(sorted({f.get("source", "")
                                   for f in p.provenance.get("found_by", [])}))
            ft = "✓" if p.full_text.get("pdf_path") else ""
            lines.append("| {} | {:.3f} | {} | {} | {} | {} |".format(
                i, p.score.get("total", 0), p.metadata.get("year") or "?",
                (p.metadata.get("title") or "")[:70].replace("|", "/"), src, ft))
        out = os.path.join(self.dir, "report.md")
        with open(out, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        return out


# =============================================================================
# 语料库统计
# =============================================================================

def summarize(papers: List[Paper], edges: List[dict] = None) -> dict:
    """统计语料库，供 report.md 与 CLI 使用。

    返回的键都是"能用来判断检索质量"的：
        papers / with_abstract / with_pdf / with_structure
        edges / edges_internal     引用边总数与其中两端都在语料库内的
        by_source                  发现渠道分布（看是否有源没起作用）
        by_year / by_topic
        avg_score
    """
    st = {"papers": len(papers), "with_abstract": 0, "with_pdf": 0,
          "with_structure": 0, "by_source": {}, "by_year": {}, "by_topic": {},
          "avg_score": 0.0, "edges": 0, "edges_internal": 0,
          "with_citation_graph": 0, "retracted": 0}
    if not papers:
        return st
    ids = {p.paper_id for p in papers}
    score_sum = 0.0
    for p in papers:
        if p.abstract:
            st["with_abstract"] += 1
        if p.full_text.get("pdf_path"):
            st["with_pdf"] += 1
        if (p.document_structure or {}).get("sections"):
            st["with_structure"] += 1
        if p.citation_graph.get("references") or p.citation_graph.get("cited_by"):
            st["with_citation_graph"] += 1
        if p.metadata.get("is_retracted"):
            st["retracted"] += 1
        for f in p.provenance.get("found_by", []):
            k = f.get("source", "?")
            st["by_source"][k] = st["by_source"].get(k, 0) + 1
        y = p.metadata.get("year")
        if y:
            st["by_year"][str(y)] = st["by_year"].get(str(y), 0) + 1
        topics = p.metadata.get("topics") or p.metadata.get("categories") or []
        for t in topics[:3]:
            st["by_topic"][t] = st["by_topic"].get(t, 0) + 1
        score_sum += float(p.score.get("total") or 0)
    st["avg_score"] = round(score_sum / len(papers), 4)
    if edges is None:
        edges = []
        for p in papers:
            for raw in (p.citation_graph.get("references") or []):
                edges.append({"from": p.paper_id, "to": raw})
    st["edges"] = len(edges)
    # 优先用边自带的 internal 标记；没有（旧产物/即时构造）时回退到集合判断
    st["edges_internal"] = sum(
        1 for e in edges
        if e.get("internal") is True
        or (e.get("internal") is None and str(e.get("to") or "") in ids
            and str(e.get("from") or "") in ids))
    st["papers_with_internal_edges"] = len(
        {e["from"] for e in edges if e.get("internal")} |
        {e["to"] for e in edges if e.get("internal") and e.get("to")})
    return st
