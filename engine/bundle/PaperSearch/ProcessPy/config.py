#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
config.py —— 流程配置（所有可调参数集中在这里）

定位
----
的 8 步流水线有大量可调项：搜几个源、每个源要多少条、
怎么排序、要不要抓全文、要不要解析结构……这些**不能散落在代码里**，
否则调参要翻十个文件，而且没法写进配置文件交给别人复现。

设计原则
--------
1. **不依赖任何配置文件格式**：就是 dataclass + 环境变量 + 命令行覆盖，
   不引入 yaml/toml 依赖（ExamplePy 也是零第三方依赖，保持一致）；
2. **每个参数都有"为什么"的注释**：尤其是默认值的取舍理由；
3. **可序列化**：一次运行的完整配置会存进 state.json —— 复现一次检索
   只需要那个文件，不用回忆当时敲了什么。

函数索引
─────────────────────────────────────────────────────────────────────────────
  [配置]  PipelineConfig                所有可调项（dataclass）
  [配置]  PipelineConfig.to_dict()      存进 run 元数据
  [配置]  default_out_dir()             默认产物目录（本目录下 out/）
  [自检]  describe(cfg)                 打印人类可读的配置摘要
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, asdict
from typing import List, Optional

_HERE = os.path.dirname(os.path.abspath(__file__))


def default_out_dir() -> str:
    """默认产物目录：<ProcessPy>/out/。

    为什么不放系统临时目录：要求"尽可能保留原始证据"，
    产物是要留着给后续模块吃的，放临时目录会被清理掉。
    """
    return os.path.join(_HERE, "out")


@dataclass
class PipelineConfig:
    """一次检索运行的完整配置。

    分组：路径 / 检索 / 排序 / 扩展 / 全文 / 解析 / 限流
    """

    # ---- 路径 --------------------------------------------------------------
    out_dir: str = field(default_factory=default_out_dir)
    examplepy: str = ""              # ExamplePy 目录；留空 = 自动定位
    resume: bool = False             # 从已有 run 目录续跑

    # ---- STEP 1-2：检索式 ---------------------------------------------------
    research_question: str = ""
    # 手工指定的检索式；为空则用 query.py 从 research_question 自动生成
    queries: List[str] = field(default_factory=list)
    # 每个检索式在最上游至少要命中的候选数（各源合计，用于判断"够不够扩"）
    target_candidates: int = 200

    # ---- STEP 3：多源检索 ---------------------------------------------------
    use_arxiv: bool = True
    use_openalex: bool = True
    use_semantic_scholar: bool = True   # ⚠️ 无 S2_API_KEY 时会自动降级跳过
    use_crossref_search: bool = True    # DOI 出版物补召回；限制查询数避免拖慢主流程
    crossref_query_limit: int = 3
    per_source_limit: int = 60          # 每个源、每个检索式取多少条
    # 只要这些分类下的论文（arXiv 用 cat: 语法；OpenAlex 用 topics 过滤后筛）
    categories: List[str] = field(default_factory=list)
    year_from: int = 0                  # 0 = 不限
    year_to: int = 0

    # ---- STEP 4：实体统一 ---------------------------------------------------
    # 标题归一化后相似度阈值（0~1）。0.92 是保守值：宁可漏合并，
    # 不要把两篇不同论文合成一条（合并错了后面全错，拆不开）
    title_similarity: float = 0.92
    require_year_match: bool = True      # 标题相同时年份也必须一致才合并
    # ★ 预印本 / 正式版：同一篇论文的 arXiv 版与会议版常常差 1~2 年。
    #   实测踩到：'Variance Reduction in MCCFR'（arXiv 2018 / AAAI 2019）与
    #   'D2CFR'（arXiv 2021 / TNNLS 2023）标题相似度都是 1.000、首作者姓氏也相同，
    #   却被"年份必须一致"挡在门外，语料库里出现两篇同名论文、引用图节点分裂。
    #   所以当标题**几乎完全相同**且作者吻合时，放宽年份到 ±N 年。
    preprint_year_tolerance: int = 2
    near_duplicate_title: float = 0.97   # 触发上述放宽的标题相似度门槛
    # ★ 作者集合重叠阈值：当标题**几乎完全相同**且两边作者集合高度重叠时，
    #   即使首作者顺序不同、年份差得离谱也判为同一篇。
    #   实测动机：'Generalized Sampling and Variance in CFR' 这一篇，
    #   两个源的记录作者顺序不同（Gibson/Lanctot vs Lanctot/Gibson）、
    #   年份一个写成 2021 一个写成 2012（差 9 年，明显有一个是错的元数据），
    #   靠"首作者 + 年份"永远合不上，而它们显然是同一篇。
    #   注意门槛必须高（≥0.5 且两边都 ≥2 位作者）：标题+作者都撞车
    #   且不是同一篇的概率很低，但一旦错并很难拆。
    author_overlap_merge: float = 0.5

    # ---- STEP 5：排序与筛选 -------------------------------------------------
    keep_top: int = 60                   # 排序后保留多少篇进入后续阶段
    min_score: float = 0.0               # 低于此分数直接丢（0 = 不丢）
    # 评分权重（STEP 5 要求考虑：topic / abstract / citation / recency / venue）
    w_topic: float = 0.45                # 标题里的概念覆盖
    w_abstract: float = 0.25             # 摘要里的概念覆盖
    w_citation: float = 0.15             # 被引（取对数，避免被高被引垄断）
    w_recency: float = 0.10              # 时间新鲜度
    w_venue: float = 0.05                # venue 是否有名（预印本给低分）
    recency_half_life_years: float = 4.0  # 新鲜度半衰期
    diversify_by: str = "primary_topic"  # 多样性约束的维度；留空 = 不做

    # ---- STEP 6：引用网络扩展 -----------------------------------------------
    expand_citations: bool = True
    expand_top_n: int = 8                # 对排序最高的 N 篇做扩展
    expand_refs_per_paper: int = 25      # 每篇取多少条参考文献
    expand_cites_per_paper: int = 25     # 每篇取多少条被引
    expansion_rounds: int = 1            # 扩展轮数（V1 默认 1 轮，避免候选爆炸）
    expansion_keep: int = 60             # 扩展后再筛一次，保留多少篇

    # ---- STEP 7：全文获取 ---------------------------------------------------
    fetch_pdf: bool = True
    fetch_top_n: int = 12                # 只给排序最靠前的 N 篇抓 PDF（的成本控制）
    max_pdf_mb: float = 30.0             # 超过这个大小直接放弃（防呆）

    # ---- STEP 8：结构解析 ---------------------------------------------------
    parse_structure: bool = True
    grobid_url: str = "http://localhost:8070"
    parse_top_n: int = 12                # 只解析已抓到 PDF 的前 N 篇
    keep_tei: bool = True                # 是否保留原始 TEI XML（证据留存）

    # ---- PDF 坐标（图纸定位 / 高亮回链）----
    # GROBID 的坐标**不是默认输出**，必须显式请求 teiCoordinates。
    # 代价：TEI 体积变大（请求 p/head 时尤其明显），解析时间略增。
    tei_coordinates: bool = True
    # 请求哪些元素的坐标：
    #   ("figure","table","formula")              体积小，够做图表高亮（默认）
    #   + ("p","head","title","ref")              可做**句子级/段落级**回链，
    #                                             但要接受 TEI 明显变大
    # ★ 默认**带上段落**：下游 PaperOrganization 的 spans 表需要
    #   "句子→PDF 位置"的定位链，没有段落坐标就做不了。
    #   代价是 TEI 变大（段落数多），可按需删掉 "p"/"head"。
    coordinate_elements: tuple = ("figure", "table", "formula", "p", "head")

    # ---- 限流与容错 ---------------------------------------------------------
    sleep_between_calls: float = 0.5     # 跨数据源的礼貌间隔
    net_retries: int = 2
    on_source_error: str = "skip"        # skip | fail —— 单源失败时是跳过还是终止
    verbose: bool = True

    # ------------------------------------------------------------------
    def to_dict(self) -> dict:
        """转成可 JSON 序列化的 dict（写进 run.json 便于复现）。"""
        return asdict(self)

    def out_path(self, *parts: str) -> str:
        """在产物目录下拼一个路径，并确保父目录存在。"""
        p = os.path.join(self.out_dir, *parts)
        d = os.path.dirname(p)
        if d:
            os.makedirs(d, exist_ok=True)
        return p


def describe(cfg: PipelineConfig) -> str:
    """把配置渲染成人可读的几行，供运行开始时打印。

    只列"会显著影响结果"的项，不是把 dataclass 全倒出来 —— 那没人看。
    """
    lines = [
        "研究问题   : {}".format(cfg.research_question or "（未填写）"),
        "数据源     : {}".format(", ".join(
            [n for n, on in (("arXiv", cfg.use_arxiv), ("OpenAlex", cfg.use_openalex),
                             ("S2", cfg.use_semantic_scholar)) if on])),
        "每源条数   : {} / 检索式".format(cfg.per_source_limit),
        "年份范围   : {}".format(
            "{}-{}".format(cfg.year_from or "不限", cfg.year_to or "至今")
            if (cfg.year_from or cfg.year_to) else "不限"),
        "保留       : 排序后 {} 篇 → 扩展后 {} 篇".format(cfg.keep_top, cfg.expansion_keep),
        "全文获取   : {}（前 {} 篇）".format("开" if cfg.fetch_pdf else "关", cfg.fetch_top_n),
        "结构解析   : {}（前 {} 篇，GROBID {}）".format(
            "开" if cfg.parse_structure else "关", cfg.parse_top_n, cfg.grobid_url),
        "产物目录   : {}".format(cfg.out_dir),
    ]
    return "\n".join(lines)
