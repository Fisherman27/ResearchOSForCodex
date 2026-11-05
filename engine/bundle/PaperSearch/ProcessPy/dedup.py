#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
dedup.py —— STEP 4 实体统一（STEP 4 /）

定位
----
同一个研究结果会在多个库里各有一条记录，字段还互有缺失：

    arXiv       arxiv:1603.01121     有 PDF、投稿日期、分类，没被引数
    OpenAlex    W2151123669          有被引数、参考文献、OA 位置，作者名更规范
    S2          649def34...          有 citation context、externalIds 能桥接
    Crossref    10.5555/1838206...   有正式出版信息、license

不合并的话：引用图会出现"同一篇论文三个节点"，被引数被拆散，
排序也就失去意义。所以 STEP 4 是**后面所有步骤的前提**。

匹配优先级（照：DOI → arXiv ID → title + authors）
------------------------------------------------------------------
  第 1 轮  强标识符：任意一个相同（DOI / arXiv / OpenAlex / S2 / OpenReview）即合并
  第 2 轮  弱标识符：标题相似度 ≥ 阈值，**且** 年份一致（可配），
                     有作者信息时再要求首作者姓氏一致

为什么分两轮而不是一把梭
------------------------
强标识符匹配是**确定**的，必须先做完全部确定匹配，再用标题兜底 ——
否则两个都缺 ID 的条目可能因为标题相似先被合并，把后面的 ID 传递性断掉。

保守性取舍
----------
默认阈值 0.92 偏高。理由：**漏合并**只是少一条信息（后面还能靠 DOI 补），
而**错合并**会把两篇不同论文的证据混在一起，之后谁也拆不开。
所以宁可漏。所有合并决策都会写进 merge 报告，可人工复核。

函数索引
─────────────────────────────────────────────────────────────────────────────
  [主入口] resolve(papers, cfg)          去重并返回 (merged, report)
  [匹配]   strong_keys(paper)            取出所有强标识符
  [匹配]   weak_key(paper)               取标题分块键（用于缩小比较范围）
  [工具]   _merge_group(group)           把一组 Paper 合并成一条
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import re
import time
from typing import Dict, List, Tuple

from model import (Paper, normalize_author, make_paper_id, title_similarity,
                   normalize_doi, normalize_arxiv)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


# =============================================================================
# 强标识符
# =============================================================================

def strong_keys(p: Paper) -> List[str]:
    """返回该论文所有的强标识符（形如 `doi:10.1/x`），用于确定性匹配。

    ★ **必须归一化**（实测缺陷 G1）：
      第一版直接拿 identity 里的原始字符串做键，于是同一篇论文的两种写法
      匹配不上，强标识符这一轮完全失效：

          identity["doi"] = "https://doi.org/10.1609/AAAI.V33I01.33012157"
                            ↓ 第一版生成的键
                            "doi:https://doi.org/10.1609/aaai.v33i01.33012157"
          identity["doi"] = "10.1609/aaai.v33i01.33012157"
                            ↓
                            "doi:10.1609/aaai.v33i01.33012157"
          两个键不相等 ⇒ 同一个 DOI 判为两篇 ⇒ 引用图节点分裂、被引数被拆散。

      同样的坑在 arXiv 上更隐蔽：`1809.03057v3` 与 `1809.03057` 是两个键，
      于是**同一条记录会产出两个完全相同的 paper_id**，
      `save_papers()` 按行写 JSONL 时同一 ID 占两行，
      下游按 ID 建索引会互相覆盖 —— 这是最糟的失败形态。

      所以这里用 model 里已有的归一化函数（它们本来就是为此写的，
      只是第一版没调用）：
          DOI   → normalize_doi()   去 URL 前缀、转小写
          arXiv → normalize_arxiv() 去版本号
          OpenAlex/S2/OpenReview/CORE → 去 URL 前缀后小写

    空值会被跳过 —— 否则所有"没有 DOI"的论文会因为 `doi:` 这个空键被合并成一条，
    这是最经典也最致命的去重 bug。
    """
    idt = p.identity or {}
    keys = []
    for k in ("doi", "arxiv_id", "openalex_id", "s2_id", "openreview_id", "core_id"):
        v = (idt.get(k) or "").strip()
        if not v:
            continue
        if k == "doi":
            nv = normalize_doi(v)
        elif k == "arxiv_id":
            nv = normalize_arxiv(v)
        else:
            # OpenAlex 等常给完整 URL（https://openalex.org/W123），
            # 取最后一段并小写。这样 URL 形态与短 ID 形态能匹配上。
            nv = v.rstrip("/").rsplit("/", 1)[-1].strip().lower()
        if nv:
            keys.append("{}:{}".format(k, nv))
    return keys


def author_overlap(a_authors: List[str], b_authors: List[str]) -> float:
    """两组作者的姓氏集合 Jaccard 相似度（0~1）。

    用**集合**而不是顺序，正是为了处理"不同源里作者顺序不一致"的情况
    （见 config.author_overlap_merge 的说明）。
    姓太短（<2 字符）的丢掉，避免 'Li' / 'Xu' 这类短姓把两组无关作者连起来。
    """
    sa = {normalize_author(x) for x in (a_authors or [])}
    sb = {normalize_author(x) for x in (b_authors or [])}
    sa = {x for x in sa if len(x) >= 2}
    sb = {x for x in sb if len(x) >= 2}
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / float(len(sa | sb))


# 同类强标识符 —— 同一条论文在这类标识符上只能有一个值，出现两个不同值
# 就意味着"这不是同一篇"。DOI / arXiv / OpenAlex / S2 各自独立成类。
_STRONG_CLASSES = ("doi", "arxiv_id", "openalex_id", "s2_id", "openreview_id", "core_id")


def conflicting_strong(a: Paper, b: Paper) -> bool:
    """两条记录的**同类**强标识符是否有冲突（都有值且不相同）。

    例：
        a={arxiv:2001.00001}, b={arxiv:2001.00002}  → True（同类冲突）
        a={arxiv:2001.00001}, b={arxiv:2001.00001}  → False（已由第 1 轮合并）
        a={doi:10.1/x},       b={arxiv:2001.00001}  → False（互补，不冲突）

    ★ 这是"保守去重"原则的具体落实（见模块头部的取舍说明）：
      错合并把两篇的证据混在一起、之后谁也拆不开；漏合并只是少一条信息。
      所以宁可因为冲突而漏合，也不能把带不同 arXiv ID 的两条并成一条。
    """
    for k in _STRONG_CLASSES:
        va = (a.identity.get(k) or "").strip().lower()
        vb = (b.identity.get(k) or "").strip().lower()
        if va and vb and va != vb:
            return True
    return False


def weak_key(p: Paper) -> str:
    """弱匹配用的**分块键**：标题里最长的那个词。

    作用是把 O(n²) 的全量两两比较降到"只在同一个桶里比"。
    用"最长的词"而不是"第一个词"：标题常以 A/An/The/On 开头，
    按首词分桶会把大量无关论文堆进同一个桶。

    ⚠️ 分块键**不能含年份**（这是实测踩到的坑）：
      第一版是 `anchor|year`，结果"预印本 2018 / 正式版 2019"这种
      本该合并的配对**永远进不了同一个桶**，后面写的年份容差逻辑
      根本执行不到 —— 表现为"改了规则但合并数一点没变"。
      年份判断必须在**比较阶段**做（那里才有容差规则），
      不能拿来当分块条件。
    """
    tn = p.metadata.get("title_norm") or ""
    words = [w for w in tn.split() if len(w) > 2]
    return max(words, key=len) if words else ""


# =============================================================================
# 合并
# =============================================================================

def _merge_group(group: List[Paper]) -> Tuple[Paper, List[str], List[str], str]:
    """把一组判定为同一篇的 Paper 合并成一条。

    策略：
      1. 选**标识符最多**的那条作为宿主（信息最全，合并后 ID 最可能稳定）；
      2. 依次 merge 其余成员；
      3. **重算 paper_id** —— 合并后可能拿到了 DOI，ID 应从
         `arxiv:1603.01121` 升级为 `doi:10.5555/...`。
         注意这会让"合并前的 ID"失效，所以返回值里带上别名映射（见 resolve）。

    返回 (合并后的 Paper, 被合并进来的来源, **全部原始 ID**, 合并依据描述)

    ★ 两个必须在**修改之前**做完的快照（实测踩到的 P2 缺陷）：
      1. **全部原始 ID**（包括宿主自己的旧 ID）。
         第一版在执行 `host.paper_id = new_id` 之后才从 members 收集别名，
         此时宿主的 paper_id 已经变了，`m.paper_id != host.paper_id`
         对宿主恒为 False —— 宿主旧 ID 永远进不了 alias，
         导致"旧 ID 重定向"和溯源断链。
      2. **合并依据**。第一版在 merge 之后才调用 source_had_strong()，
         而 merge 会把各成员的标识符合并到一起，于是**靠标题匹配的组
         也会被判成"强标识符一致"**，报告里的理由与实际不符
         （真实冒烟：weak_links=1，组理由却写"强标识符一致"）。
    """
    # ---- 快照 1：合并前的全部 paper_id ----
    original_ids = [p.paper_id for p in group if p.paper_id]
    # ---- 快照 2：合并依据（必须在 merge 之前判定）----
    reason = ("强标识符一致" if source_had_strong(group)
              else "标题相似且年份/作者一致")

    group = sorted(group, key=lambda p: -len(strong_keys(p)))
    host = group[0]
    old_host_id = host.paper_id
    sources = []
    for other in group[1:]:
        host.merge(other, at=_now())
        sources.extend(f.get("source", "") for f in other.provenance.get("found_by", []))
    # 重算 ID：合并后标识符更全，主键升级为最强标识符
    new_id = make_paper_id(host.identity, host.metadata.get("title", ""))
    if new_id:
        host.paper_id = new_id
    host.add_history("dedup", "由 {} 条记录合并而来；合并前 ID: {}；合并后: {}".format(
        len(group), ", ".join(original_ids) or "无", host.paper_id), at=_now())
    return host, sorted(set(s for s in sources if s)), original_ids, reason


# =============================================================================
# 主入口
# =============================================================================

def resolve(papers: List[Paper], cfg=None) -> Tuple[List[Paper], dict]:
    """STEP 4 主入口：把多源候选合并成唯一论文列表。

    参数
    ----
    papers : 多源检索得到的原始列表（同一篇可能有多条）
    cfg    : PipelineConfig；用到 title_similarity / require_year_match

    返回
    ----
    (merged, report)
      merged : 去重后的 Paper 列表
      report : {
        "before", "after", "merged_groups",
        "groups": [{"members": [paper_id...], "reason": "...", "kept": paper_id,
                    "id_changed": bool}],
        "alias": {旧ID: 新ID},          ← 供引用边重定向
        "elapsed_sec"
      }

    ★ 为什么报告里要保留完整的分组与理由：
      去重是**不可逆**的信息损失。留下决策记录，出问题才能回溯
      （"这两篇为什么被合并了？"）。这也是强调保留证据的一部分。
    """
    t0 = time.time()
    thr = getattr(cfg, "title_similarity", 0.92)
    need_year = getattr(cfg, "require_year_match", True)
    near = getattr(cfg, "near_duplicate_title", 0.97)
    ytol = getattr(cfg, "preprint_year_tolerance", 2)

    # ---- 并查集 ----
    n = len(papers)
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra
            return True
        return False

    # ---- 第 1 轮：强标识符 ----
    by_key: Dict[str, int] = {}
    strong_links = 0
    for i, p in enumerate(papers):
        for k in strong_keys(p):
            if k in by_key:
                if union(by_key[k], i):
                    strong_links += 1
            else:
                by_key[k] = i

    # ---- 第 2 轮：标题 + 年份（+ 首作者）----
    # 只在还没被强匹配合并过的组之间比，且先分块降低比较量。
    blocks: Dict[str, List[int]] = {}
    for i, p in enumerate(papers):
        blocks.setdefault(weak_key(p), []).append(i)

    weak_links = 0
    blocked_by_conflict: List[dict] = []
    for _, idxs in blocks.items():
        if len(idxs) < 2:
            continue
        for x in range(len(idxs)):
            for y in range(x + 1, len(idxs)):
                i, j = idxs[x], idxs[y]
                if find(i) == find(j):
                    continue
                a, b = papers[i], papers[j]

                # ★ 强标识符冲突检查（实测缺陷 G5）
                #   两个**互相不同**的强标识符是"这不是同一篇"的**反证**，
                #   必须优先于标题相似度。
                #   第一版不看这个：两条各带不同 arXiv ID 的记录，
                #   只要标题+年份+作者吻合就被合并，其中一个 ID 被静默丢弃
                #   （Paper.merge 对 identity 只补空，宿主已占的位置不覆盖）。
                #   注意"冲突"只针对**同类**标识符：
                #     arXiv:A vs arXiv:B → 冲突（同一类，两个不同值）
                #     DOI:X   vs arXiv:Y → 不冲突（互补，正是要合并的情形）
                if conflicting_strong(a, b):
                    blocked_by_conflict.append({
                        "a": a.paper_id, "b": b.paper_id,
                        "a_identity": {k: v for k, v in a.identity.items() if v},
                        "b_identity": {k: v for k, v in b.identity.items() if v},
                        "reason": "同类强标识符冲突（不同值）⇒ 判定为不同论文，不合并",
                    })
                    continue

                ta, tb = a.metadata.get("title", ""), b.metadata.get("title", "")
                if not ta or not tb:
                    continue
                sim = title_similarity(ta, tb)
                if sim < thr:
                    continue

                # ---- 作者证据（两种，任一成立即可）----
                aa = (a.metadata.get("authors") or [])
                ab = (b.metadata.get("authors") or [])
                # a) 首作者吻合（最常用）
                first_ok = True
                if aa and ab:
                    first_ok = normalize_author(aa[0]) == normalize_author(ab[0])
                # b) 作者**集合**高度重叠（≥2 位作者）
                #    用于不同源里作者顺序不一致的情况 —— 实测 'Generalized Sampling
                #    and Variance in CFR' 在两个源里顺序相反，只看首作者会漏合。
                set_ok = (len(aa) >= 2 and len(ab) >= 2
                          and author_overlap(aa, ab)
                          >= getattr(cfg, "author_overlap_merge", 0.5))
                # ⚠️ 注意这里**不能**因为首作者不一致就 continue：
                #    那样 (b) 规则永远执行不到（第一版就是这么写的，
                #    表现为"加了规则但一篇都没多合并"）。
                if (aa and ab) and not (first_ok or set_ok):
                    continue

                # ---- 年份证据 ----
                ya, yb = a.metadata.get("year"), b.metadata.get("year")
                if need_year and ya and yb and int(ya) != int(yb):
                    # 标题几乎完全相同的前提下才允许年份不一致，且分两档：
                    #   a) 首作者吻合 + 差距在预印本容差内（预印本 → 正式版）
                    #   b) 作者集合高度重叠（年份元数据本身可能就错）
                    ok_a = (sim >= near and first_ok
                            and abs(int(ya) - int(yb)) <= ytol)
                    ok_b = (sim >= near and set_ok)
                    if not (ok_a or ok_b):
                        continue
                if union(i, j):
                    weak_links += 1

    # ---- 按组归并 ----
    groups: Dict[int, List[int]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)

    merged, report_groups, alias = [], [], {}
    for _, idxs in groups.items():
        members = [papers[i] for i in idxs]
        if len(members) == 1:
            merged.append(members[0])
            continue
        host, sources, original_ids, reason = _merge_group(members)
        # 别名映射：合并前的**所有** ID → 合并后的 ID
        # 用 _merge_group 返回的快照，而不是 members 的当前值 ——
        # 宿主的 paper_id 已经被改写，从 members 现读会漏掉宿主旧 ID。
        for old in original_ids:
            if old and old != host.paper_id:
                alias[old] = host.paper_id
        report_groups.append({
            "members": original_ids,
            "kept": host.paper_id,
            "reason": reason,
            "sources": sources,
            "size": len(members),
        })
        merged.append(host)

    report = {
        "before": n, "after": len(merged),
        "merged_groups": len(report_groups),
        "strong_links": strong_links, "weak_links": weak_links,
        "groups": report_groups,
        "alias": alias,
        # 被"强标识符冲突"挡下的配对：**记录下来而不是静默丢弃**，
        # 便于事后复核"这两条为什么没合并"。
        "blocked_by_conflict": blocked_by_conflict,
        "elapsed_sec": round(time.time() - t0, 2),
    }
    return merged, report


def source_had_strong(members: List[Paper]) -> bool:
    """这组里是否至少有一对共享强标识符（用于给合并理由下结论）。"""
    seen = {}
    for i, p in enumerate(members):
        for k in strong_keys(p):
            if k in seen:
                return True
            seen[k] = i
    return False


def rekey(papers: List[Paper]) -> Dict[str, str]:
    """按当前 identity 重算所有 paper_id，返回 {旧ID: 新ID} 别名表。

    ★ 为什么需要它（抽查发现的真实缺陷）：
      `make_paper_id` 的优先级是 **doi > arxiv > openalex > s2 > title 哈希**。
      但 DOI 常常是**后来才补上的** —— `enrich_crossref` 在 `dedup.resolve()`
      **之后**才跑，Unpaywall 也会补标识符。补完之后没人重算 paper_id，
      于是出现了"既有 DOI、主键却还是 `openalex:W...`"的记录：
          identity = {"doi": "10.65109/qfxi4541", "openalex_id": "W2187559597"}
          paper_id = "openalex:W2187559597"     ← 违反自己的规则
      后果：主键取决于"DOI 是哪一步补上的"这种偶然因素，不稳定；
            同一篇论文在不同运行里可能得到不同主键；别名映射也随之失真。

    做法：重算 → 收集变化 → 用 apply_alias 把引用边里的旧 ID 一并重定向。
    **冲突保护**：若重算出的新 ID 与另一篇论文的现有 ID 撞车（正常情况下
    不该发生，因为那意味着它们本就该合并），保留旧 ID 并跳过 —— 宁可主键
    不够"最优"，也不能让两篇论文共用一个主键（那会静默丢数据）。
    """
    existing = {p.paper_id for p in papers}
    alias: Dict[str, str] = {}
    for p in papers:
        new_id = make_paper_id(p.identity, p.metadata.get("title", ""))
        if not new_id or new_id == p.paper_id:
            continue
        if new_id in existing and new_id not in alias.values():
            # 目标 ID 已被别篇占用：不重命名，避免主键碰撞
            continue
        alias[p.paper_id] = new_id
        existing.discard(p.paper_id)
        existing.add(new_id)
    # 应用别名（同时改写论文自身的 paper_id 和引用边端点）
    for p in papers:
        if p.paper_id in alias:
            p.paper_id = alias[p.paper_id]
    if alias:
        apply_alias(papers, alias)
    return alias


def apply_alias(papers: List[Paper], alias: Dict[str, str]) -> int:
    """用别名表把语料库内部的旧 ID 重定向到合并后的新 ID。

    需要它的原因：合并会**重算 paper_id**（例如 arxiv:xxx 升级为 doi:yyy），
    而在此之前写下的引用边用的还是旧 ID。不重定向就会产生一堆指向
    "语料库里不存在的节点"的边，图就断了。

    返回被改写的边数。
    """
    if not alias:
        return 0
    n = 0
    for p in papers:
        for key in ("references", "cited_by"):
            vals = p.citation_graph.get(key) or []
            new_vals = []
            for v in vals:
                if isinstance(v, str) and v in alias:
                    new_vals.append(alias[v])
                    n += 1
                else:
                    new_vals.append(v)
            p.citation_graph[key] = new_vals
        if p.paper_id in alias:
            p.paper_id = alias[p.paper_id]
    return n
