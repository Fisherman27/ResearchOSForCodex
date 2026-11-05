#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
query.py —— STEP 1-2 查询理解与检索式生成（STEP 1/2）

定位
----
    STEP 1  把研究问题拆成：核心概念 / 同义词 / 缩写 / 相关概念 / 子问题
    STEP 2  产生**多个**检索式（而不是只搜一个关键词）

为什么必须有这一步
------------------
直接用研究问题原文去搜，会踩三个坑：

  1. **缩写搜不到**：搜 "CFR" 得到的多是金融/法规论文（Cost and Freight、
     Code of Federal Regulations），而真正要的 "counterfactual regret minimization"
     一篇都搜不到；反过来只搜全称又会漏掉用缩写的论文。
  2. **单检索式召回不足**：的例子很典型 —— 关键词检索可能只拿到
     20 篇，靠引用网络才能恢复整个脉络。多检索式是扩大入口的第一步。
  3. **各源语法不同**：arXiv 用 `all:"..."` / `cat:`，OpenAlex 用 `search=`，
     S2 用 `query=`。同一组概念要翻译成三个方言。

方法：规则 + 内置术语表（V1 不接 LLM）
--------------------------------------
明确检索阶段不做语义判断，且这一阶段的目标是"别漏"。
规则方法零依赖、可复现、可解释，且对本领域（AI/博弈）的关键缩写覆盖得动 ——
因为术语表是**可维护的**，发现漏了就加一条，比调模型可预期得多。

函数索引
─────────────────────────────────────────────────────────────────────────────
  [STEP 1]  extract_concepts(question)     研究问题 → 概念表（含缩写展开）
  [STEP 1]  expand_synonyms(concepts)      按术语表补充同义词/相关词
  [STEP 2]  build_queries(question, cfg)   生成多检索式 + 各源方言
  [方言]    to_arxiv_query / to_openalex_query / to_s2_query
  [工具]    split_question(question)       拆出子问题（按连接词/标点）
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import re
from typing import Dict, List, Tuple

# =============================================================================
# 领域术语表
# =============================================================================
# 只收"本领域确实会混用的写法"。每条是双向的：
#   提到任一项 → 把所有项都当成同义概念去搜。
# 维护原则：**只加高置信度的**。加错会把无关论文灌进候选池，
# 比漏搜更难清理（因为排序阶段未必能救回来）。
TERM_GROUPS: List[Tuple[str, ...]] = [
    ("cfr", "counterfactual regret minimization", "counterfactual regret"),
    ("deep cfr", "deep counterfactual regret minimization"),
    ("mccfr", "monte carlo counterfactual regret minimization",
     "monte-carlo cfr"),
    ("marl", "multi-agent reinforcement learning"),
    ("rl", "reinforcement learning"),
    ("nfsp", "neural fictitious self-play"),
    ("fsp", "fictitious self-play", "fictitious play"),
    ("nash", "nash equilibrium"),
    ("ne", "nash equilibrium"),
    ("imperfect information", "imperfect-information game"),
    ("imperfect information game", "extensive-form game", "extensive form game"),
    ("poker", "texas hold'em", "holdem", "leduc poker"),
    ("mcts", "monte carlo tree search"),
    ("ppo", "proximal policy optimization"),
    ("dqn", "deep q-network", "deep q learning"),
    ("self-play", "selfplay"),
    ("game theory", "game-theoretic"),
    ("equilibrium", "equilibria"),
    ("regret matching", "regret minimization"),
    ("multi-agent", "multiagent"),
    ("opponent modeling", "opponent modelling"),
    ("policy gradient", "policy-gradient"),
    ("llm", "large language model", "large language models"),
    ("benchmark", "evaluation", "empirical study"),
    ("survey", "review", "overview"),
]

# 检索时应当被丢掉的词（出现在问题里会让 query 变长且没信息）
STOPWORDS = set("""
a an the of for in on to and or with without by from into over under
is are was were be been being do does did doing have has had having
we i you they he she it this that these those there here
how what why when where which who whom whose
can could should would may might must will shall
study paper research work approach method methods
using use used uses about toward towards via based
""".split())

# 太泛、单独出现毫无区分度的词 —— 它们**不能**进检索式。
# 典型：搜 "deep" 会拉回半个深度学习领域；搜 "learning" 同理。
# 注意：这些词作为**短语的一部分**是好的（"deep cfr"），只有单独出现才丢。
GENERIC_SINGLE = set("""
deep learning model models method methods approach approaches
data network networks system systems algorithm algorithms
analysis study performance results problem problems
framework training inference neural
""".split())

# 子问题的切分触发词
SUBQ_SPLIT = re.compile(
    r"(?:[?;；]|(?:以及|并且|同时|另外|此外|and also|as well as))", re.I)


# =============================================================================
# 文本工具
# =============================================================================

def _tokens(text: str) -> List[str]:
    """切词：保留连字符（imperfect-information）和撇号（hold'em）。"""
    return re.findall(r"[A-Za-z][A-Za-z0-9'\-]*", text or "")


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower()).strip()


def split_question(question: str) -> List[str]:
    """把研究问题拆成子问题。

    'CFR 在多智能体场景下的收敛性如何？以及有哪些加速方法？'
      → ['CFR 在多智能体场景下的收敛性如何', '有哪些加速方法']

    用途：每个子问题都能派生额外的检索式，扩大入口（STEP 2 的要求）。
    中英文标点都处理。
    """
    parts = [p.strip() for p in SUBQ_SPLIT.split(question or "") if p and p.strip()]
    return parts if len(parts) > 1 else ([question.strip()] if question.strip() else [])


# =============================================================================
# STEP 1：概念抽取与扩展
# =============================================================================

def extract_concepts(question: str) -> List[str]:
    """研究问题 → 概念列表（去停用词、保短语、按术语表展开缩写）。

    参数
    ----
    question : 用户的研究问题，中英文皆可（英文术语会原样保留）

    返回概念列表，**已去重、已展开**。例如：
        'Deep CFR 在多人扑克中的收敛性'
        → ['deep cfr', 'deep counterfactual regret minimization',
           '多人扑克', 'multi-agent', 'poker', ...]

    实现说明：
      - 中文片段整体保留（本阶段不做中文分词 —— 引入分词器就引入了依赖，
        而且中文关键词对英文数据库的检索帮助有限，主要靠英文术语）；
      - 英文走"相邻词成短语"：连续的非停用词组成 n-gram，
        长度 1~4 都收，交给术语表和后续的命中率自然筛选。
    """
    q = _norm(question)
    if not q:
        return []

    raw: List[str] = []

    # 中文片段（含中文的连续串）
    for m in re.finditer(r"[\u4e00-\u9fff][\u4e00-\u9fffA-Za-z0-9]*", question or ""):
        s = m.group(0)
        if len(s) >= 2:
            raw.append(s)

    # 英文 n-gram（1~4 词），跨停用词断开
    toks = _tokens(q)
    idx = 0
    while idx < len(toks):
        if toks[idx] in STOPWORDS:
            idx += 1
            continue
        for n in range(1, 5):
            seg = toks[idx:idx + n]
            if len(seg) < n:
                break
            if any(t in STOPWORDS for t in seg):
                break
            raw.append(" ".join(seg))
        idx += 1

    # 只保留"有意义"的：长度 >=2，或本身就是已知缩写
    known = {t for grp in TERM_GROUPS for t in grp}
    concepts = []
    for c in raw:
        c = c.strip()
        if not c:
            continue
        if len(c) < 2 and c not in known:
            continue
        if c not in concepts:
            concepts.append(c)

    return expand_synonyms(concepts)


def expand_synonyms(concepts: List[str]) -> List[str]:
    """按术语表补充同义词/相关写法。

    规则：只要某个概念**等于或包含**术语组里任一项，就把整组都加进来。
    例：'deep cfr' → 增补 'deep counterfactual regret minimization'。

    ★ 为什么用"包含"而不是"等于"：
       用户写的是 'Deep CFR method'，词表里是 'deep cfr'，
       要求完全相等就永远匹配不上。
    """
    out = list(concepts)
    low = [c.lower() for c in concepts]
    for grp in TERM_GROUPS:
        hit = False
        for term in grp:
            if term in low:
                hit = True
                break
            if len(term) >= 3 and any(term in c for c in low):
                hit = True
                break
        if hit:
            for t in grp:
                if t not in out:
                    out.append(t)
    return out


def _is_cjk(s: str) -> bool:
    """是否含中日韩字符。"""
    return bool(re.search(r"[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]", s or ""))


def canonical_concepts(concepts: List[str], question: str = "") -> List[str]:
    """挑出**用于检索**的核心概念（去掉过长/过泛/无用的）。

    规则（按优先级排序，保证结果稳定可复现）：
      1. 术语表里的已知术语（cfr / marl / ...）                    —— 最优先
      2. **含 CJK 的概念被剔除**：arXiv/OpenAlex/S2 的检索以英文为主，
         中文短语发过去基本返回空，还会白占名额。
         （中文概念仍保留在 `concepts` 里用于打分，只是不进检索式）
      3. 2~5 个词的短语 —— 信息量足够且能搜到
      4. 单个词：必须不在 GENERIC_SINGLE 里，且长度 >= 4 以过滤噪音
      5. >5 个词的短语丢掉（数据库多半搜不到这么长的串）
    最多保留 12 个，**长短语优先**（`deep cfr` 比 `cfr` 更有区分度）。
    """
    known = {t for grp in TERM_GROUPS for t in grp}
    scored = []
    for c in concepts:
        if _is_cjk(c):                       # 规则 2：中文不进检索式
            continue
        n = len(c.split())
        if c in known:                       # 规则 1
            scored.append((0, -n, c))
        elif 2 <= n <= 5:                    # 规则 3
            scored.append((1, -n, c))
        elif n == 1 and len(c) >= 4 and c not in GENERIC_SINGLE:   # 规则 4
            scored.append((2, -n, c))
        # 其余（>5 词、泛词、过短）直接不进
    scored.sort()
    return [c for _, _, c in scored[:12]]


# =============================================================================
# STEP 2：检索式生成
# =============================================================================

def build_queries(question: str, cfg=None) -> dict:
    """STEP 2 主入口：产出概念表与多源检索式。

    返回
    ----
    {
      "question": ...,
      "sub_questions": [...],
      "concepts": [...],            # 全部概念（含同义词），用于 STEP 5 打分
      "core_concepts": [...],       # 用于检索的核心概念
      "queries": {                  # 每个源一份检索式列表
          "arxiv":    ["all:\"deep cfr\"", ...],
          "openalex": ["deep cfr", ...],
          "s2":       ["deep cfr", ...],
      },
      "human": [...]                # 给人工在 Google Scholar 补查用的
    }

    检索式构造策略（三层，从精确到宽泛）：
      1. **短语精确**：核心短语加引号，先拿高相关的；
      2. **概念组合**：两个核心概念 AND，拿到交集里的工作；
      3. **单概念宽搜**：只用最强的那个概念，兜底召回。
    三层都跑，靠 STEP 4/5 的去重与排序收敛 —— 入口宁宽勿窄。
    """
    cfg = cfg or _DummyCfg()
    subqs = split_question(question)
    all_concepts: List[str] = []
    for sq in subqs or [question]:
        for c in extract_concepts(sq):
            if c not in all_concepts:
                all_concepts.append(c)

    core = canonical_concepts(all_concepts, question)
    if not core:
        core = [_norm(question)] if question else []

    # ---- 手工指定的检索式优先 ----
    manual = list(getattr(cfg, "queries", None) or [])

    phrases, combos, singles = [], [], []
    # 1. 短语精确（最多 4 条，避免检索式爆炸）
    for c in core[:4]:
        phrases.append('"{}"'.format(c))
    # 2. 概念组合（最多 4 对）
    for i in range(min(2, len(core))):
        for j in range(i + 1, min(4, len(core))):
            if len(combos) >= 4:
                break
            combos.append('"{}" AND "{}"'.format(core[i], core[j]))
    # 3. 单概念宽搜（最多 3 条）
    for c in core[:3]:
        singles.append('"{}"'.format(c))

    plain = [c for c in core]                  # 各源 search 参数直接用的裸短语
    arxiv_q = phrases + combos + singles
    return {
        "question": question,
        "sub_questions": subqs,
        "concepts": all_concepts,
        "core_concepts": core,
        "queries": {
            "arxiv": (manual or arxiv_q),
            "openalex": (manual or plain[:6] + combos[:2]),
            "s2": (manual or plain[:6]),
        },
        "human": (manual or ['"{}"'.format(c) for c in core[:5]]),
    }


class _DummyCfg:
    """build_queries 不传 cfg 时的占位（只需要 queries 属性为空）。"""
    queries: List[str] = []


# =============================================================================
# 各源方言
# =============================================================================

def to_arxiv_query(concepts: List[str], field: str = "all") -> str:
    """把概念拼成 arXiv 检索式：`all:"a" AND all:"b"`。

    field 取值见 arxiv.py：all / ti / abs / au / cat。
    """
    if not concepts:
        return ""
    return " AND ".join('{}:"{}"'.format(field, c) for c in concepts[:4])


def to_openalex_query(concepts: List[str]) -> str:
    """OpenAlex 的 search 参数是自由文本，直接空格连接即可（它自己做分词）。"""
    return " ".join(concepts[:5])


def to_s2_query(concepts: List[str]) -> str:
    """S2 的 query 也是自由文本；它的检索偏弱，短语少一点召回更好。"""
    return " ".join(concepts[:4])
