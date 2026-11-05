#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
rank.py —— STEP 5 排序与筛选（STEP 5）

定位
----
多源检索拿回来的候选是"广撒网"，必须缩小到一个能真正读完的集合。
 要求考虑的维度：

    topic relevance      概念是否命中
    abstract relevance   摘要是否真的在讲这个
    citation importance  被引（要抑制高被引垄断）
    recency              新鲜度
    venue                发表处
    diversity            避免清一色同一主题

设计上的两个坚持
----------------
1. **完全可解释**：每篇论文的分数要能拆成 parts 并说明理由。
   "为什么这篇排第 3？" 必须能回答 —— 否则调参就是玄学，
   而且下游（论文理解模块）也无从判断该不该信这个顺序。

2. **不用模型**：V1 不接 LLM/嵌入。原因：①  明确检索阶段不做语义判断；
   ② 排序规则一旦依赖模型，复现性就没了；③ 这个阶段的目标是"别漏、别太离谱"，
   不是"精准排序"。规则评分对"缩小候选集"这个目标足够，且零依赖、零成本。

函数索引
─────────────────────────────────────────────────────────────────────────────
  [概念]  concept_hits(text, concepts)       统计概念命中（支持多词短语）
  [评分]  score_paper(paper, concepts, cfg)  返回 {"total","parts","why"}
  [主入口] rank_papers(papers, concepts, cfg) 打分 + 过滤 + 多样性挑选
  [多样性] diversify(ranked, cfg)            按主题做贪心去冗余
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import math
import time
from typing import Dict, List, Tuple

from model import Paper, normalize_title

# 常见的有名 venue —— 只用来给一点点加权，**不做硬过滤**
# （硬过滤会漏掉大量有价值的预印本和 workshop 论文）
KNOWN_VENUES = (
    "neurips", "nips", "icml", "iclr", "aaai", "ijcai", "acl", "emnlp", "naacl",
    "cvpr", "iccv", "eccv", "kdd", "sigir", "www", "wsdm", "uai", "aistats",
    "colt", "jmlr", "tpami", "nature", "science", "pnas", "journal of",
    "transactions on", "conference on", "proceedings of",
)

# 预印本类 venue 关键词 —— 不扣分，只是不给 venue 加分
PREPRINT_HINTS = ("arxiv", "preprint", "biorxiv", "ssrn", "openreview", "techrxiv")


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


# =============================================================================
# 概念命中
# =============================================================================

def concept_hits(text: str, concepts: List[str]) -> Tuple[float, List[str]]:
    """统计文本里命中了哪些概念。

    参数
    ----
    text     : 要检查的文本（标题或摘要）；内部会做小写+连字符归一
    concepts : 概念词列表，**支持多词短语**（如 "counterfactual regret minimization"）

    返回 (覆盖率 0~1, 命中的概念列表)

    实现要点：
      - 多词短语按整串匹配，命中一个短语得 1 分（而不是按词数得 3 分）——
        否则长短语会因为"词多"而主导分数；
      - 命中的概念**去重**，同一个概念出现多次不重复加分（避免关键词堆砌刷分）；
      - 除以概念总数得到覆盖率，这样不同数量的概念之间可比。
    """
    if not text or not concepts:
        return 0.0, []
    t = " " + normalize_title(text) + " "
    hit = []
    for c in concepts:
        nc = normalize_title(c)
        if nc and nc in t and c not in hit:
            hit.append(c)
    return (len(hit) / float(len(concepts)) if concepts else 0.0), hit


# =============================================================================
# 单篇评分
# =============================================================================

def score_paper(paper: Paper, concepts: List[str], cfg) -> dict:
    """给一篇论文打分，返回可解释的分项。

    参数
    ----
    concepts : STEP 1-2 产生的概念/关键词列表
    cfg      : PipelineConfig（权重 w_* 与 recency_half_life_years）

    返回
    ----
    {"total": float, "parts": {维度: 分数}, "why": "人类可读的说明"}

    各维度算法与理由：
      topic     = 标题的概念覆盖率          —— 标题命中是最强信号
      abstract  = 摘要的概念覆盖率 × 0.8    —— 系数 0.8：摘要常用同义改写，
                                              命中率不如标题可靠
      citation  = log10(1+被引) / log10(1+1000)  封顶 1.0
                  ★ 取对数、并封顶，是为了**抑制高被引垄断**：
                    1000 次被引不该比 100 次重要 10 倍，否则经典老论文
                    会把所有相关的新工作挤出候选集。
      recency   = 0.5 ** (年龄 / 半衰期)    —— 指数衰减，半衰期默认 4 年
      venue     = 1.0 命中已知 venue；预印本 0.0；未知 0.3（不惩罚未知）
    """
    parts: Dict[str, float] = {}
    why: List[str] = []

    # ---- topic ----
    title = paper.metadata.get("title", "")
    topic_cov, topic_hit = concept_hits(title, concepts)
    parts["topic"] = topic_cov
    if topic_hit:
        why.append("标题命中 {}".format("/".join(topic_hit[:4])))

    # ---- abstract ----
    abs_cov, abs_hit = concept_hits(paper.abstract or "", concepts)
    parts["abstract"] = abs_cov * 0.8
    if abs_hit:
        why.append("摘要命中 {} 个概念".format(len(abs_hit)))

    # ---- citation ----
    cites = paper.metadata.get("citation_count")
    if cites is None:
        parts["citation"] = 0.0            # 未知不给分也不扣分
    else:
        try:
            parts["citation"] = min(1.0, math.log10(1 + max(0, int(cites))) / 3.0)
        except Exception:
            parts["citation"] = 0.0
    if cites:
        why.append("被引 {}".format(cites))

    # ---- recency ----
    hl = getattr(cfg, "recency_half_life_years", 4.0) or 4.0
    year = paper.metadata.get("year")
    if year:
        age = max(0, time.gmtime().tm_year - int(year))
        parts["recency"] = 0.5 ** (age / hl)
        if age <= 2:
            why.append("近 {} 年".format(age) if age else "当年")
    else:
        parts["recency"] = 0.3             # 年份缺失给中性偏低值

    # ---- venue ----
    venue = (paper.metadata.get("venue") or "").lower()
    if any(v in venue for v in PREPRINT_HINTS):
        parts["venue"] = 0.0
    elif any(v in venue for v in KNOWN_VENUES):
        parts["venue"] = 1.0
        why.append("venue: {}".format(paper.metadata.get("venue", "")[:30]))
    else:
        parts["venue"] = 0.3 if venue else 0.1

    weights = {
        "topic": getattr(cfg, "w_topic", 0.45),
        "abstract": getattr(cfg, "w_abstract", 0.25),
        "citation": getattr(cfg, "w_citation", 0.15),
        "recency": getattr(cfg, "w_recency", 0.10),
        "venue": getattr(cfg, "w_venue", 0.05),
    }
    total = sum(parts[k] * weights[k] for k in weights)
    return {"total": round(total, 4),
            "parts": {k: round(parts[k], 4) for k in parts},
            "weights": weights,
            "why": "; ".join(why)}


# =============================================================================
# 主入口
# =============================================================================

def rank_papers(papers: List[Paper], concepts: List[str], cfg,
                stage: str = "initial") -> Tuple[List[Paper], dict]:
    """打分 → 过滤 → 排序 → 多样性挑选。

    参数
    ----
    stage : "initial"（STEP 5）或 "expanded"（STEP 6 之后）——
            决定用 cfg.keep_top 还是 cfg.expansion_keep

    返回 (kept_papers, report)
      kept_papers 已按分数降序，且 `paper["score"]["rank"]` 已回填。

    过滤规则（顺序很重要）：
      1. 已撤稿（is_retracted）→ 直接剔除（不该进研究语料库）
      2. 需要年份范围时按年份过滤（年份缺失的**保留**，不做无根据的丢弃）
      3. min_score 以下的剔除
      4. 排序后取前 keep_top
      5. 多样性挑选
    """
    t0 = time.time()
    dropped = {"retracted": 0, "year": 0, "low_score": 0, "over_limit": 0}

    # ---- 1. 撤稿 ----
    kept = []
    for p in papers:
        if p.metadata.get("is_retracted"):
            dropped["retracted"] += 1
            continue
        kept.append(p)

    # ---- 2. 年份 ----
    yf, yt = getattr(cfg, "year_from", 0), getattr(cfg, "year_to", 0)
    if yf or yt:
        tmp = []
        for p in kept:
            y = p.metadata.get("year")
            if not y:                       # 年份未知：保留（不因缺字段而丢）
                tmp.append(p)
                continue
            try:
                yi = int(y)
            except Exception:
                tmp.append(p)
                continue
            if (yf and yi < yf) or (yt and yi > yt):
                dropped["year"] += 1
                continue
            tmp.append(p)
        kept = tmp

    # ---- 3. 打分 ----
    for p in kept:
        p.score = score_paper(p, concepts, cfg)
    kept.sort(key=lambda x: -float(x.score.get("total") or 0))

    # ---- 4. min_score ----
    ms = getattr(cfg, "min_score", 0.0) or 0.0
    if ms > 0:
        before = len(kept)
        kept = [p for p in kept if float(p.score.get("total") or 0) >= ms]
        dropped["low_score"] = before - len(kept)

    # ---- 5. 取前 N ----
    limit = (getattr(cfg, "keep_top", 60) if stage == "initial"
             else getattr(cfg, "expansion_keep", 60))
    if limit and len(kept) > limit:
        dropped["over_limit"] = len(kept) - limit
        kept = kept[:limit]

    # ---- 6. 多样性 ----
    kept = diversify(kept, cfg)

    for i, p in enumerate(kept, 1):
        p.score["rank"] = i

    report = {"stage": stage, "in": len(papers), "out": len(kept),
              "dropped": dropped, "concepts": len(concepts),
              "elapsed_sec": round(time.time() - t0, 2)}
    return kept, report


def diversify(ranked: List[Paper], cfg) -> List[Paper]:
    """按主题做贪心去冗余，避免前 N 篇全是同一个子方向。

    算法（简化的 MMR）：按分数从高到低扫，维护每个主题桶的配额；
    超出配额的先放一边，扫完再补回来（**只调整顺序，不丢论文**）。

    参数
    ----
    cfg.diversify_by : "primary_topic" / "venue" / ""（关闭）
                       topic 取 metadata.topics 或 categories 的第一项

    为什么只调顺序不丢：
      多样性是"想先看到什么"的问题，不是"什么不该要"。
      丢论文会让语料库出现难以解释的缺口，调序则无副作用。
    """
    dim = getattr(cfg, "diversify_by", "") or ""
    if not dim or len(ranked) < 6:
        return ranked

    def key_of(p: Paper) -> str:
        if dim == "venue":
            return (p.metadata.get("venue") or "unknown").lower()[:40]
        topics = p.metadata.get("topics") or p.metadata.get("categories") or []
        return (topics[0] if topics else "unknown")

    quota = max(2, len(ranked) // max(1, len({key_of(p) for p in ranked})) + 1)
    counts: Dict[str, int] = {}
    primary, deferred = [], []
    for p in ranked:
        k = key_of(p)
        if counts.get(k, 0) < quota:
            counts[k] = counts.get(k, 0) + 1
            primary.append(p)
        else:
            deferred.append(p)
    return primary + deferred
