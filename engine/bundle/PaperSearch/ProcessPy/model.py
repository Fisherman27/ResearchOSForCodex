#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
model.py —— 论文的统一数据模型（的落地）

定位
----
五个数据源返回的字段名各不相同：
    arXiv    arxiv_id / summary / published / comment
    OpenAlex openalex_id / abstract_inverted_index / publication_year / authorships
    S2       paperId / externalIds.ArXiv / abstract / year
    Crossref DOI / issued / container-title / author
    Unpaywall best_oa_location / oa_status / version
如果让它们各写各的，下游每接一个源就要加一堆 if。所以这里定义**一个** Paper
结构，各源适配器负责把它填满（缺失字段留空，不用编造）。

三层结构对应
------------------
  Layer 1 Paper Store       → Paper.metadata / abstract / full_text
  Layer 2 Literature Graph  → Paper.citation_graph（出边 references / 入边 cited_by）
  Layer 3 Document Structure→ Paper.document_structure

关于 internal paper_id
----------------------
用它做全流程的主键。生成规则**确定性**（同样的论文永远得到同样的 ID），
且**人类可读**，优先用最强标识符：

    doi:10.1038/nature12373      有 DOI
    arxiv:1603.01121             有 arXiv ID（**去掉版本号**，v1/v2 是同一篇）
    openalex:W2741809807         只有 OpenAlex ID
    s2:649def34...               只有 S2 ID
    title:9f2a1c...              什么都没有，用标题归一化后的哈希

函数索引
─────────────────────────────────────────────────────────────────────────────
  [归一化] normalize_doi / normalize_arxiv / normalize_title / normalize_author
  [判重]   title_similarity(a, b)        标题相似度 0~1
  [主键]   make_paper_id(identity, title) 生成确定性 internal ID
  [模型]   Paper                         统一论文结构（dataclass）
           Paper.to_dict() / from_dict() / merge(other) / add_found_by(...)
  [辅助]   new_paper() / empty_structure()
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field, asdict
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional

# =============================================================================
# 一、标识符归一化
# =============================================================================
# 这些函数被 dedup.py 和所有源适配器共用。规则宁可保守：
# 归一化过头会把不同的东西变成同一个 key。

_DOI_RE = re.compile(r"10\.\d{4,9}/[^\s\"<>]+", re.I)
_ARXIV_NEW = re.compile(r"(\d{4}\.\d{4,5})(v\d+)?")
_ARXIV_OLD = re.compile(r"([a-z][a-z\-]*(?:\.[A-Z]{2})?/\d{7})(v\d+)?")
_PUNCT_RE = re.compile(r"[^\w\s]", re.U)
_WS_RE = re.compile(r"\s+")


def normalize_doi(raw: Any) -> str:
    """把各种写法的 DOI 统一成 `10.xxxx/yyy` 小写形式。

    输入 → 输出：
        'https://doi.org/10.1038/Nature12373'  → '10.1038/nature12373'
        'doi:10.1038/nature12373'              → '10.1038/nature12373'
        '10.1038/nature12373'                  → '10.1038/nature12373'
    ⚠️ DOI **大小写不敏感**，所以统一转小写；不做这一步会导致同一篇被当成两篇。
    返回 "" 表示识别不出 DOI。
    """
    if not raw:
        return ""
    s = str(raw).strip()
    if "doi.org/" in s.lower():
        s = s.split("doi.org/", 1)[1]
    s = re.sub(r"^doi:\s*", "", s, flags=re.I)
    m = _DOI_RE.search(s)
    return m.group(0).lower().rstrip(".,;)") if m else ""


def normalize_arxiv(raw: Any) -> str:
    """提取 arXiv ID 并**去掉版本号**。

    为什么去版本号：v1/v2 是同一篇论文的不同修订，检索阶段把它们当成
    两篇会导致引用图里同一个节点分裂。版本信息如需保留，另存 `arxiv_version`。

    输入 → 输出：
        'http://arxiv.org/abs/1603.01121v2' → '1603.01121'
        'hep-th/9901001v1'                  → 'hep-th/9901001'
        'arXiv:2609.01234'                  → '2609.01234'
    返回 "" 表示不是 arXiv ID。
    """
    if not raw:
        return ""
    s = str(raw).strip()
    s = re.sub(r"^arxiv:\s*", "", s, flags=re.I)
    s = s.replace("abs/", " ").replace("pdf/", " ")
    m = _ARXIV_NEW.search(s) or _ARXIV_OLD.search(s)
    return m.group(1) if m else ""


def normalize_title(raw: Any) -> str:
    """标题归一化（用于查重与相似度）。

    做四件事：转小写、去标点、把连字符换成空格、压缩空白。
    ⚠️ **不去停用词**：'Attention Is All You Need' 去掉 is/all/you 之后
    剩下的信息反而更容易和别的论文撞车。
    """
    if not raw:
        return ""
    s = str(raw).lower()
    s = s.replace("-", " ").replace("_", " ")
    s = _PUNCT_RE.sub(" ", s)
    return _WS_RE.sub(" ", s).strip()


def normalize_author(raw: Any) -> str:
    """作者名归一化，取到姓氏即可（用于交叉验证，不用于聚合）。

    输入 'Yoshua Bengio' / 'Bengio, Yoshua' → 都得到 'bengio'
    ⚠️ 多段姓氏（'van der Berg'）会取到最后一段，这对判重足够：
        判重是"标题相似 + 姓氏相同 + 年份相同"三者同时满足，不是单靠姓氏。
    """
    if not raw:
        return ""
    s = str(raw).strip()
    if "," in s:                       # 'Bengio, Yoshua' → 取逗号前
        s = s.split(",", 1)[0]
    parts = _PUNCT_RE.sub(" ", s).split()
    return parts[-1].lower() if parts else ""


def title_similarity(a: str, b: str) -> float:
    """两个标题的相似度，0~1。输入会先做 normalize_title。

    取**两个指标的最大值**（都用标准库，无额外依赖）：

      1. `SequenceMatcher.ratio()` —— 字符序列相似度
         擅长：拼写差异、少量增删词（'Deep CFR' vs 'Deep CFR Method'）
         短板：**不抗词序重排**

      2. 词序无关的 token 比例 —— 把词排序后再比
         擅长：词序不同、副标题换位置

    为什么必须两个都算（这不是理论问题，是实测踩到的）：
        'Deep CFR: A Reinforcement Learning Approach'
        'A Reinforcement Learning Approach: Deep CFR'
      只跑 SequenceMatcher 得 **0.786**，低于默认阈值 0.92 ⇒ 同一篇论文会被
      当成两篇、引用图节点分裂。加上词序无关指标后这两条得 1.0，正确合并。

    反过来，只做 token 集合比较也不够：它分不清
        'Deep CFR' 与 'Deep CFR for Poker'（词集包含关系，Jaccard 会偏高）。
    所以是 max 而不是替换 —— 两个短板互补。
    """
    na, nb = normalize_title(a), normalize_title(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    seq = SequenceMatcher(None, na, nb).ratio()
    # 词序无关：排序后再比字符序列
    sorted_ratio = SequenceMatcher(None,
                                   " ".join(sorted(na.split())),
                                   " ".join(sorted(nb.split()))).ratio()
    return max(seq, sorted_ratio)


def make_paper_id(identity: Dict[str, str], title: str = "") -> str:
    """生成确定性 internal paper ID。

    参数
    ----
    identity : {"doi", "arxiv_id", "openalex_id", "s2_id", "openreview_id", "core_id"}
    title    : 兜底用（所有外部 ID 都缺时）

    返回形如 `doi:10.1038/nature12373` 的字符串；**同一篇论文在任何源、
    任何顺序下都会得到相同的 ID**，这是后续合并的前提。
    全空时返回 `title:<sha1前16位>`；连标题都没有则返回 ""。
    """
    idt = identity or {}
    # ★ 归一化后必须**非空**才拿来当主键（实测缺陷 G6）：
    #   第一版写成 `if idt.get("doi"): return "doi:" + normalize_doi(...)`，
    #   而 normalize_doi 对 `10.1/x`（段号不足 4 位）、`not-a-doi` 这类串
    #   返回空串 —— 于是主键变成 `"doi:"` 这个**空主体**。
    #   多篇这样的论文主键完全相同 ⇒ 落盘后按 ID 建索引互相覆盖、
    #   dedup 的强标识符匹配失效。这是"退化值没有校验"的典型。
    #   现在逐级回退：DOI 归一化不出来就用 arXiv，再不行用 OpenAlex……
    #   全都不可用时落到标题哈希（保证唯一），最后才返回空串。
    d = normalize_doi(idt.get("doi") or "")
    if d:
        return "doi:" + d
    a = normalize_arxiv(idt.get("arxiv_id") or "")
    if a:
        return "arxiv:" + a
    oa = str(idt.get("openalex_id") or "").strip()
    if oa:
        short = oa.rstrip("/").rsplit("/", 1)[-1]
        if short:
            return "openalex:" + short
    if idt.get("s2_id"):
        s2 = str(idt["s2_id"]).strip()
        if s2:
            return "s2:" + s2
    if idt.get("openreview_id"):
        orv = str(idt["openreview_id"]).strip()
        if orv:
            return "openreview:" + orv
    nt = normalize_title(title)
    if nt:
        return "title:" + hashlib.sha1(nt.encode("utf-8")).hexdigest()[:16]
    return ""


# =============================================================================
# 二、Paper 数据模型
# =============================================================================

# 结构层的固定字段（即使为空也保留，保证下游读字段不用判断 key 在不在）
def empty_structure() -> dict:
    """Layer 3 的初始结构。

    全字段预置的好处：下游分析可以直接 `paper["document_structure"]["sections"]`
    而不用 `.get(...) or []`，少一层心智负担。
    """
    return {
        "parsed_by": "",        # "grobid" / "pypdf" / ""
        "tei_path": "",         # 原始 TEI XML（证据留存，）
        "sections": [],         # [{"title","level","paragraphs","paragraph_list","text"}]
        "paragraphs_total": 0,
        "figures": 0,
        "tables": 0,
        "equations": 0,
        "references_parsed": [],  # GROBID 从正文抽出的参考文献（含 DOI 时可用于补图）
        # ---- PDF 坐标（需在 process_fulltext 时请求 tei_coordinates）----
        "figure_objects": [],     # 图对象（含 coords 包围盒）
        "table_objects": [],      # 表对象
        "formula_objects": [],    # 公式对象
        "coordinates": {"count": 0, "by_element": {}, "elements": [],
                        "coordinate_space": "", "note": ""},
        # ---- 正文引文标记 ↔ 参考文献条目的连接（test.md STEP7 第 4 条）----
        # ★ 这是**句子级证据链**的基础：知道"第 3 段这句引的是 [12]"，
        #   才能回答"这个方法出自哪篇文献"。下游 PaperOrganization 的
        #   span_map / claims 表要用的就是它。
        #   结构：{"links":[...], "unlinked":[...], "object_links":{...},
        #          "n_refs","n_bibr_refs","n_bib","n_bib_with_id"}
        "citation_links": {},
        # ---- 句子级 span（下游 PaperOrganization 的 spans 表契约）----
        # 每条 {"span_id","section","section_index","paragraph_index",
        #       "sentence_index","char_start","char_end","text",
        #       "coords","coords_level","page_first","page_last"}
        # 供 claims 表做"论点→原文证据"的定位锚点。
        "spans": [],
        "error": "",
    }


@dataclass
class Paper:
    """统一论文结构。

    字段按  的分组组织，其中 `identity` / `metadata` / `full_text` /
    `citation_graph` / `document_structure` / `provenance` 是嵌套 dict，
    其余是顶层标量（方便 JSONL 里直接 grep）。
    """

    paper_id: str = ""
    identity: Dict[str, str] = field(default_factory=lambda: {
        "doi": "", "arxiv_id": "", "openalex_id": "", "s2_id": "",
        "openreview_id": "", "core_id": "",
    })
    # ----B. Bibliographic Metadata ----
    metadata: Dict[str, Any] = field(default_factory=lambda: {
        "title": "", "title_norm": "", "authors": [], "affiliations": [],
        "year": None, "published_date": "", "venue": "", "venue_type": "",
        "categories": [], "keywords": [], "topics": [], "citation_count": None,
        "influential_citation_count": None, "type": "", "language": "",
        "is_retracted": None, "comment": "",
        # 预印本年（仅当预印本与正式版合并且年份不一致时才有值）
        "preprint_year": None,
        # arXiv 版本号（如 "v3"）。★ 为什么单独存（实测缺陷 G3）：
        #   identity.arxiv_id 与 paper_id 都刻意**去掉**版本号（v1/v2 是同一篇，
        #   不去掉会导致同一篇分裂成两个节点）。但"这是第几版"本身是信息 ——
        #   做版本演化分析时要用。第一版把版本号直接丢了，
        #   只有 comment 自由文本里可能残留，等于没有。
        "arxiv_version": "",
    })
    abstract: str = ""
    # ---- 全文（获取策略）----
    full_text: Dict[str, Any] = field(default_factory=lambda: {
        "pdf_path": "", "pdf_url": "", "pdf_source": "", "pdf_bytes": 0,
        "oa_status": "", "version": "", "license": "", "alt_locations": [],
        "text_path": "", "error": "",
    })
    # ----Citation Graph ----
    citation_graph: Dict[str, Any] = field(default_factory=lambda: {
        "references": [],       # 这篇引用的（出边）
        "cited_by": [],         # 引用了这篇的（入边）
        "reference_count": None,
        "source": "",           # "openalex" / "s2"
    })
    # ----Document Structure ----
    document_structure: Dict[str, Any] = field(default_factory=empty_structure)
    # ----C. Search Metadata（溯源）----
    provenance: Dict[str, Any] = field(default_factory=lambda: {
        "found_by": [],   # [{"source","kind","query","detail","at"}]
        "history": [],    # 每次合并/评分的事件流
    })
    # ---- 排序阶段的中间结果（写盘时会一起存，便于解释"为什么它排这么前"）----
    score: Dict[str, Any] = field(default_factory=lambda: {
        "total": 0.0, "parts": {}, "rank": 0,
    })

    # ---------------- 序列化 ----------------
    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Paper":
        """从 dict 还原。

        用**逐字段赋值**而不是 `cls(**d)`：这样旧版本产物里多出来的字段
        不会让新代码崩掉（向前兼容），缺字段也有默认值兜底。
        """
        p = cls()
        p.paper_id = d.get("paper_id", "")
        for k in ("identity", "metadata", "full_text", "citation_graph",
                  "document_structure", "provenance", "score"):
            cur = getattr(p, k)
            inc = d.get(k) or {}
            cur.update(inc)                    # 只覆盖出现的键
        p.abstract = d.get("abstract", "")
        # 嵌套 list/dict 做个浅拷贝，避免调用方改动影响全局常量
        p.document_structure = dict(empty_structure(), **(d.get("document_structure") or {}))
        return p

    # ---------------- 溯源 ----------------
    def _normalize_identity(self) -> None:
        """把 identity 里的标识符统一写成规范形态（就地修改）。

        规范形态：
            doi        → normalize_doi()     `10.1609/aaai.v33i01.33012157`
            arxiv_id   → normalize_arxiv()   `1809.03057`（去版本号）
            其余       → 去 URL 前缀 + 小写   `w1234567890`

        ⚠️ arXiv 的**版本号会被剥离**，这是有意的（v1/v2 是同一篇）。
           需要在合并后仍能回答"当时是第几版"时，看 `metadata.arxiv_version`
           （adapter 会把剥离前的版本号存进去）。
        """
        idt = self.identity or {}
        if idt.get("doi"):
            nd = normalize_doi(idt["doi"])
            if nd:
                idt["doi"] = nd
        if idt.get("arxiv_id"):
            na = normalize_arxiv(idt["arxiv_id"])
            if na:
                idt["arxiv_id"] = na
        for k in ("openalex_id", "s2_id", "openreview_id", "core_id"):
            v = (idt.get(k) or "").strip()
            if v:
                idt[k] = v.rstrip("/").rsplit("/", 1)[-1].lower()
        self.identity = idt

    def add_found_by(self, source: str, kind: str, query: str = "",
                     detail: str = "", at: str = "") -> None:
        """记录"这篇论文是怎么被找到的"（C）。

        kind 取值：
            keyword_search     关键词检索
            citation_expansion 顺着引用找来的
            reference_expansion 顺着参考文献找来的
            recommendation     相似论文推荐
            manual_seed        人工指定
        **同一条来源只记一次**（同一检索式重复命中不刷屏），
        但不同 query / 不同 kind 会各留一条 —— 这正是"为什么它在语料库里"
        的可追溯证据。
        """
        rec = {"source": source, "kind": kind, "query": query,
               "detail": detail, "at": at}
        for old in self.provenance.get("found_by", []):
            if all(old.get(k) == rec[k] for k in ("source", "kind", "query")):
                return
        self.provenance.setdefault("found_by", []).append(rec)

    def add_history(self, event: str, detail: str = "", at: str = "") -> None:
        """记一条处理事件（合并、评分、抓全文……），用于事后复盘。"""
        self.provenance.setdefault("history", []).append(
            {"event": event, "detail": detail, "at": at})

    # ---------------- 合并 ----------------
    def merge(self, other: "Paper", at: str = "") -> List[str]:
        """把另一条记录合并进本记录，返回"被填充/更新的字段名"列表。

        合并策略（保守，**不覆盖已有信息**）：
          - 标量/字符串：本记录为空、对方非空 → 取对方
          - list：取并集（顺序保留本记录在前）
          - dict：逐键递归（citation_graph / full_text 这类）
          - 溯源：追加对方独有的 found_by
          - document_structure：谁有内容用谁（不合并段落，避免错位）
        **只在 paper_id 相同时调用**（判定由 dedup.py 负责）。
        """
        changed: List[str] = []

        def note(msg):
            changed.append(msg)

        # identity：补空 + **写回归一化形态**
        # ★ 为什么补空之后还要再归一化一次（实测缺陷 G2）：
        #   `merge` 只"补空不覆盖"，所以宿主的 `identity["doi"]` 保持它原来的
        #   写法。如果宿主那条形如 `https://doi.org/10.1609/AAAI.V33I01.33012157`，
        #   合并后 identity 里**仍然是 URL 形态** —— 下游每次用 DOI 都要自己
        #   normalize 一次，忘一次就是一次匹配失败。
        #   这里在合并结束时把 identity 统一写成规范形态，让"合并后"
        #   与"新建时"（new_paper 已用 normalize）行为一致。
        for k, v in (other.identity or {}).items():
            if v and not self.identity.get(k):
                self.identity[k] = v
                note("identity." + k)
        self._normalize_identity()

        # metadata：补空 + list 取并集
        for k, v in (other.metadata or {}).items():
            cur = self.metadata.get(k)
            if v in (None, "", [], {}):
                continue
            if isinstance(cur, list) and isinstance(v, list):
                add = [x for x in v if x not in cur]
                if add:
                    self.metadata[k] = cur + add
                    note("metadata." + k)
            elif cur in (None, "", [], {}):
                self.metadata[k] = v
                note("metadata." + k)

        # abstract：本记录空则取对方（取更长的那个，避免摘要被截断版本覆盖）
        if other.abstract and len(other.abstract) > len(self.abstract or ""):
            self.abstract = other.abstract
            note("abstract")

        # full_text：补空；alt_locations 取并集
        for k, v in (other.full_text or {}).items():
            if k == "alt_locations" and isinstance(v, list):
                add = [x for x in v if x not in (self.full_text.get(k) or [])]
                if add:
                    self.full_text[k] = (self.full_text.get(k) or []) + add
                    note("full_text." + k)
            elif v not in (None, "", [], {}) and not self.full_text.get(k):
                self.full_text[k] = v
                note("full_text." + k)

        # citation_graph：references / cited_by 去重并集
        for k in ("references", "cited_by"):
            cur = self.citation_graph.get(k) or []
            add = [x for x in (other.citation_graph.get(k) or []) if x not in cur]
            if add:
                self.citation_graph[k] = cur + add
                note("citation_graph." + k)
        for k in ("reference_count", "source"):
            if other.citation_graph.get(k) and not self.citation_graph.get(k):
                self.citation_graph[k] = other.citation_graph[k]
                note("citation_graph." + k)

        # document_structure：**不做字段级合并**，谁解析过就用谁的
        if (other.document_structure or {}).get("sections") and \
           not (self.document_structure or {}).get("sections"):
            self.document_structure = dict(other.document_structure)
            note("document_structure")

        # 评分取较高的那个（两个源都算过就留更好的解释）
        if (other.score or {}).get("total", 0) > (self.score or {}).get("total", 0):
            self.score = dict(other.score)
            note("score")

        # 溯源
        before = len(self.provenance.get("found_by", []))
        for rec in (other.provenance or {}).get("found_by", []):
            self.add_found_by(rec.get("source", ""), rec.get("kind", ""),
                              rec.get("query", ""), rec.get("detail", ""),
                              rec.get("at", ""))
        if len(self.provenance.get("found_by", [])) > before:
            note("provenance.found_by")

        # ---- 预印本 / 正式版的年份处理 ----
        # 同一篇论文的 arXiv 版与会议版年份不同，合并后必须给出一个**明确**的
        # 出版年，否则下游做趋势分析时会把 2018 的预印本当成 2018 年发表。
        # 规则：取较晚的作为 `year`（正式版通常晚于预印本），较早的存进
        # `preprint_year` 保留信息 —— 不做"谁覆盖谁"的隐式取舍。
        ya, yb = self.metadata.get("year"), other.metadata.get("year")
        if ya and yb and int(ya) != int(yb):
            early, late = sorted((int(ya), int(yb)))
            self.metadata["year"] = late
            self.metadata["preprint_year"] = early
            note("metadata.year(preprint→published)")
            self.add_history("merge_year",
                             "合并时发现年份不一致 {} / {}，取 {} 为出版年，"
                             "{} 记为预印本年".format(early, late, late, early), at=at)

        if changed:
            self.add_history("merge", "合并了 {} 个字段：{}".format(
                len(changed), ",".join(changed[:8])), at=at)
        return changed


def new_paper(identity: Optional[dict] = None, title: str = "") -> Paper:
    """按 identity 造一个空 Paper（paper_id 已算好）。

    源适配器的标准起手式：
        p = new_paper({"arxiv_id": e["arxiv_id"]}, e["title"])
        p.metadata["title"] = e["title"]
        ...
    """
    idt = {"doi": "", "arxiv_id": "", "openalex_id": "", "s2_id": "",
           "openreview_id": "", "core_id": ""}
    idt.update({k: v for k, v in (identity or {}).items() if k in idt and v})
    p = Paper()
    p.identity = idt
    p._normalize_identity()          # 统一形态，避免上游塞进来的是 URL 写法
    p.paper_id = make_paper_id(p.identity, title)
    if title:
        p.metadata["title"] = title
        p.metadata["title_norm"] = normalize_title(title)
    return p
