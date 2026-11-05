# ProcessPy —— 论文检索流程（V1）

> 把 ExamplePy 里那批**单接口示例**编排成的 **8 步流水线**，
> 产出定义的 **Structured Literature Corpus**（三层结构）。
> 这是检索阶段的成品，后续「论文理解」「研究方向发现」直接吃它的产物。

**基准日期**：2026-09-30 ｜ **运行时**：conda 环境 `researchos`（Python 3.12.14）

---

## 0. 这是什么 / 不是什么

| | 说明 |
|---|---|
| **是** | 流程编排层：查询理解 → 多源检索 → 实体统一 → 排序 → 引用扩展 → 全文 → 结构解析 → 三层落盘 |
| **不是** | 数据源实现（那在 ExamplePy）；也不是论文理解（那是下一阶段） |
| **依赖** | Python 3.8+ 标准库；底层能力全部来自 `../ExamplePy` |
| **验证** | 已用真实检索跑通（见）；`audit0930` 9 项契约测试全通过（b） |
| **状态语义** | `run()` 返回 `status`：`done`（全绿）/ `partial`（有源不可用或可降级阶段失败）/ `failed`（必需阶段失败或语料库为空）。**不再无条件报成功** |

---

## 1. 快速开始

```bash
cd PaperSearch/ProcessPy
PY="${PYTHON:-python}"

# ① 先看看会生成哪些检索式（不联网，秒回）—— 建议每次都先过一眼
$PY cli.py query "Deep CFR 在多人扑克中的收敛性与加速方法"

# ② 环境自检（接入是否正常、各源可用性、GROBID 状态）
$PY cli.py check

# ③ 跑完整流程
$PY cli.py run "Deep CFR 在多人扑克中的收敛性与加速方法"

# ④ 看结果
$PY cli.py show out/corpus.jsonl
cat out/report.md
```

**最常用的一次调用就是 `run "研究问题"`**，其余参数都有合理默认值。

### 常用参数

```bash
# 小规模试跑（省时间）
$PY cli.py run "..." --per-source 20 --top 40 --fetch-top 6

# 只做检索+排序，不抓全文/不解析（最快）
$PY cli.py run "..." --no-pdf --no-parse

# 中断后接着跑（跳过已完成的步骤）
$PY cli.py run "..." --resume

# 限定年份 / 关闭引用扩展 / 手工指定检索式
$PY cli.py run "..." --year-from 2020
$PY cli.py run "..." --no-expand
$PY cli.py run "..." --query 'all:"deep cfr"' --query 'all:"mccfr"'
```

> ⚠️ **STEP 8 结构解析需要 GROBID 服务**。没启动也能跑，会自动降级为纯文本
> （`sections` 留空、明确标记 `parsed_by="pypdf"`，不假装有结构）。
> 启动方式：`cd ../ExamplePy && ./grobid-serve.sh start`（约 20 秒）

---

## 2. 文件清单

| 文件 | 职责 | 关键入口 |
|---|---|---|
| [`_bootstrap.py`](_bootstrap.py) | 定位并接入 ExamplePy（唯一对外依赖入口） | `ensure_importable()` |
| [`config.py`](config.py) | 全部可调参数（分组 + 每个默认值的理由） | `PipelineConfig` |
| [`model.py`](model.py) | 统一 Paper 模型（落地）+ ID 归一化 | `Paper` `make_paper_id` |
| [`store.py`](store.py) | 三层落盘 + 断点 + 报告 | `CorpusStore` |
| [`query.py`](query.py) | STEP 1-2 查询理解与检索式生成 | `build_queries` |
| [`sources.py`](sources.py) | STEP 3 多源检索 / STEP 4 校正 / STEP 6 扩展 | `search_all` `expand_citations` |
| [`dedup.py`](dedup.py) | STEP 4 实体统一（含预印本/正式版处理） | `resolve` |
| [`rank.py`](rank.py) | STEP 5 可解释排序与多样性 | `rank_papers` |
| [`fulltext.py`](fulltext.py) | STEP 7 全文获取 / STEP 8 结构解析 | `acquire_pdf` `parse_structure` |
| [`pipeline.py`](pipeline.py) | 8 步编排（断点、容错、报告） | `run(cfg)` |
| [`cli.py`](cli.py) | 命令行 | `run` `query` `check` `show` |
| [`acceptance/`](acceptance/) | **V1 验收**（对照 test.md） | `run_acceptance.py` `make_report.py` `edge_tests.py` |

---

## 3. 八步流水线

| 步 | 做什么 | 实现 | 关键设计 |
|---|---|---|---|
| STEP 1 | 研究问题 → 概念 / 同义词 / 缩写 | `query.extract_concepts` | 内置本领域术语表（CFR↔counterfactual regret minimization 等），**V1 不接 LLM** |
| STEP 2 | 生成**多条**检索式 + 各源方言 | `query.build_queries` | 三层策略：短语精确 → 概念组合 → 单概念宽搜，入口宁宽勿窄 |
| STEP 3 | arXiv / OpenAlex / S2 三源检索 | `sources.search_all` | 单源失败不影响其它源，逐源记录错误与"是否参与" |
| STEP 4 | 实体统一 + 元数据校正 | `dedup.resolve` + `enrich_crossref` + `resolve_oa` | 强标识符优先（DOI→arXiv），标题兜底；**顺序**：先去重再校正再查 OA |
| STEP 5 | 六维可解释排序 | `rank.rank_papers` | topic/abstract/citation/recency/venue + 多样性；**每篇分数可拆解** |
| STEP 6 | 引用网络扩展 | `sources.expand_citations` | 只对高排名种子、限额、限轮数；扩展后**重新去重与排序** |
| STEP 7 | 全文获取 | `fulltext.acquire_pdf` | 优先级 arXiv → OpenAlex OA → Unpaywall；只抓前 N 篇（成本控制） |
| STEP 8 | PDF → 结构 | `fulltext.parse_structure` | GROBID 主路径，`consolidateCitations=0`（实测 220s vs 1.6s）；不可用时降级并**如实标记** |

### 三层产物

| 层 | 文件 | 回答什么 |
|---|---|---|
| **Layer 1 Paper Store** | `out/corpus.jsonl` | 有哪些论文？ |
| **Layer 2 Literature Graph** | `out/graph.jsonl` | 论文之间什么关系？ |
| **Layer 3 Document Structure** | `out/structures/*.json` + `*.tei.xml` | 论文内部信息在哪？ |

---

## 4. 产物格式

### 4.1 目录

```
out/
├── run.json        配置 + 统计 + 时间线（**复现一次检索只需要这个文件**）
├── state.json      断点（支持 --resume）
├── corpus.jsonl    Layer 1，每行一篇 Paper
├── graph.jsonl     Layer 2，每行一条引用边
├── structures/     Layer 3
│   ├── <id>.json      sections / references / 图表公式
│   └── <id>.tei.xml   原始 TEI（证据留存）
├── pdfs/           下载到的 PDF
├── report.md       人类可读报告
└── run.log         运行日志
```

### 4.2 Paper 字段（对应）

```jsonc
{
  "paper_id": "doi:10.1609/aaai.v33i01.33012157",   // 确定性主键，见下
  "identity":      { "doi", "arxiv_id", "openalex_id", "s2_id", "openreview_id", "core_id" },
  "metadata":      { "title", "authors", "affiliations", "year", "preprint_year",
                     "venue", "categories", "keywords", "topics", "citation_count",
                     "is_retracted", ... },
  "abstract":      "...",
  "full_text":     { "pdf_path", "pdf_url", "pdf_source", "oa_status", "version", "license" },
  "citation_graph":{ "references": [...], "cited_by": [...], "source": "openalex+grobid" },
  "document_structure": { "parsed_by", "sections": [...], "figures", "tables",
                          "equations", "references_parsed", "tei_path" },
  "provenance":    { "found_by": [{"source","kind","query","at"}], "history": [...] },
  "score":         { "total", "parts": {...}, "why", "rank" }
}
```

**`paper_id` 生成规则**（确定性、人类可读，同一篇在任何源下都相同）：

```
doi:10.1038/nature12373     有 DOI
arxiv:1603.01121            有 arXiv ID（去掉版本号）
openalex:W2741809807        只有 OpenAlex ID
title:<sha1前16位>          都没有，用标题哈希
```

### 4.3 引用边格式

```jsonc
{ "from": "doi:...",      // 一定是语料库内的论文
  "to": "arxiv:1809.03057", // 目标在语料库内时才有值（图遍历用这个）
  "to_raw": "W2151123669",  // 源里的原始 ID（总是在）
  "internal": true,         // 目标是否落在语料库内
  "rel": "cites",           // cites（出边）| cited_by（入边）
  "source": "openalex+grobid" }
```

> ★ `to` / `to_raw` / `internal` 三个字段的分工必须分清：
> `to_raw` 用于"这篇引用了哪些外部文献"（完整性），`to` 用于图遍历，`internal` 让统计一眼看出连通度。

---

## 5. 设计决策与踩坑记录

### 5.1 三条编排规则

1. **每步结束立刻落盘**（papers + state）—— 中断后 `--resume` 从下一步接着跑；
2. **单步失败不等于整体失败** —— 可降级的（S2 无 key、GROBID 没起、某源限流）
   记为 skipped/partial 并继续，只有"检索到 0 篇"才终止；
3. **报告必须写清"哪一步没做成"** —— 否则"看起来有 50 篇"其实只有一个源在起作用。

### 5.2 实测踩到的坑（全部已修）

| # | 现象 | 根因 | 修法 |
|---|---|---|---|
| 1 | 词序不同的同一标题相似度只有 **0.786**，低于阈值 → 不合并 | 只用 `SequenceMatcher`（序列匹配，不抗重排） | 取 `max(序列相似度, 词序无关相似度)`；实测该例变 1.000 |
| 2 | 中文残句与 `deep`、`learning` 这类泛词进了检索式 | 概念抽取没做过滤 | 剔除 CJK 概念与泛词单字（`GENERIC_SINGLE`） |
| 3 | 扩展进来的论文**全都没有摘要、没有引用边**（摘要 39/60、引用图 23/60） | 扩展查询的 `select` 只列了 5 个字段，漏了 `abstract_inverted_index` 和 `referenced_works` | 定义 `EXPAND_SELECT` 列全字段（同一请求，几乎零成本）→ **摘要 56、引用图 45** |
| 4 | **1802 条引用边只有 1 条能 join 上语料库（0%）** | 边存的是 OpenAlex 原始 ID（`W2006791053`），而主键是 `doi:...` | 新增 `build_id_index()` 做多形态 ID 映射 → **内部边 174 条、45 篇参与** |
| 5 | 预印本/正式版被当成两篇（如 Variance Reduction 2018/2019） | `require_year_match` 一刀切 | 加预印本容差：标题≥0.97 + 首作者吻合时允许 ±2 年 |
| 6 | 改了容差规则但**合并数一点没变** | 分块键是 `anchor|year`，不同年的配对根本不进同一个桶，规则**执行不到** | 分块键去掉年份（年份判断移到比较阶段） |
| 7 | 同一篇论文作者顺序不同、年份差 9 年（元数据有错）仍然合不上 | 只看首作者 | 加作者**集合** Jaccard 规则（≥0.5 且两边 ≥2 位作者） |
| 8 | 加了作者集合规则后仍不合并 | 首作者不一致时提前 `continue`，规则被短路 | 重排判断顺序，首作者不匹配不再直接跳过 |
| 9 | 合并后年份停在预印本年（2018），DOI 却是正式版（2019） | 合并没处理年份语义 | 取较晚者为 `year`，较早者存 `metadata.preprint_year` |
| 10 | `cli.py query` 直接崩：`AttributeError: 'Namespace' object has no attribute 'resume'` | `query`/`check`/`show` 子命令没有 `run` 的那些参数，但共用 `build_config` 直接取属性 | 改用 `getattr(a, name, 默认)`，默认值取自 `PipelineConfig` 本身 |

> 坑 3/4/6/8 有个共同点：**改了代码但效果为零**，而且不报错。
> 每次都是靠"对比修复前后的统计数字"才发现的 —— 这也是为什么
> 报告里要专门列出"有摘要/有引用图/内部边"这些**质量指标**，
> 光看"检索到多少篇"根本发现不了这些问题。

### 5.2b 第二轮审核（`audit0930/REVIEW.md`）修复记录

对照 `PaperSearch/test.md` 的验收契约，9 项离线契约测试**已全部通过**（修复前 9 项全部失败）。

| # | 问题 | 根因 | 修法 |
|---|---|---|---|
| P1-1 | **失败被汇总为成功**：空检索也返回 `ok=True` | `run()` 里 `ok` 是**写死的 True**，完全没看 `_timed` 的返回值 | 引入 `status`（done/partial/failed）+ `source_status`；必需阶段失败或语料库为空 ⇒ `failed`，源不可用 ⇒ `partial` |
| P1-2 | **resume 丢失查询计划**：续跑后 STEP3 拿到空 queries，检索为空且不报错 | 跳过 STEP1-2 时只加载 `papers`，`ctx.plan` 仍是空字典 | 把 plan 写进断点并在 resume 时恢复；同时把跳过语义改成真正的**连续前缀**（原来逐步独立判断，可能跳过输入已失效的下游步骤） |
| P1-3 | **S2 引用扩展未实现**（test.md STEP4 要求两源） | `expand_citations` 只调 OpenAlex | 补上 S2 的 references/citations |
| P2-4 | 扩展忽略源开关；HTTP 失败当成空结果；`expansion_rounds` 未生效 | 只捕获异常不检查响应状态；无条件实例化 OpenAlex | 遵守 `use_*`；每响应做 `_ok()` 状态判定并记进 `errors`；轮数非 1 时明确报错而非静默忽略 |
| P2-5 | 引用图未按解析后端点去重；`cited_by` 方向约定不统一 | 去重键用 `to_raw`；`cited_by` 存成 `(P→X, rel=cited_by)` | 统一为 **citing→cited 的 `cites` 边**；按 `(from, to, rel)` 去重；未解析目标给稳定 `unresolved:` 节点 |
| P2-6 | **合并后丢失宿主旧 ID**；合并理由被改写 | 别名在 `host.paper_id` 改写**之后**才收集；理由在 merge **之后**才判定（merge 会把 ID 并到一起） | 修改前快照全部原始 ID 与合并依据 |
| P2-7 | PDF 地址按文件名猜、Unpaywall 回退不完整 | URL 必须以 `.pdf` 结尾或含 `/pdf/`；"已有 pdf_url"就跳过 Unpaywall | **照单全收、按响应验证**（`%PDF-` 魔数）；下载全灭后以 `only_missing=False` 再查一次 Unpaywall |

**修复过程中自己引入又被抓出的两个 bug**（说明"修完必须复跑真实流程"）：

| 现象 | 根因 |
|---|---|
| 引用边从 1910 掉到 **220** | 未解析目标的 `to` 是空串，`(from,to,rel)` 去重键把同一篇的几十条外部引用**塌缩成一条**。改用 `unresolved:` 节点 ID 参与去重 |
| 所有 PDF 下载失败且**没有兜底** | Unpaywall 回退里写错变量名（`tried` vs `tries`）抛 `NameError`，整条回退路径静默失效 |

> 另一个隐蔽点：`store.mark_step()` 是**整体覆盖**而非合并，所以在 step 函数里直接
> `mark_step` 保存的字段，会被随后 `_timed` 的 `ctx.done()` 抹掉。要随**返回值**传递。

### 5.3 刻意的取舍

| 取舍 | 理由 |
|---|---|
| 去重阈值偏高（0.92），宁可漏合并 | 漏合并只是少一条信息（后面能靠 DOI 补）；**错合并把两篇的证据混在一起，之后谁也拆不开** |
| V1 排序**不用模型**，纯规则可解释 |要求检索阶段不做语义判断；规则可复现、零依赖；目标是"别漏、别太离谱"而非精准排序 |
| pypdf 兜底时 `sections` 保持为空 |明确要求保留结构；给不了就如实标记，**不把纯文本伪装成章节** |
| 引用扩展只取高排名种子 + 限额 |的例子说明扩散是指数的，不加约束会瞬间淹没候选池 |
| Layer 3 保留原始 TEI |「尽可能保留原始证据」 |

---

## 6. 实测结果（2026-09-30）

**研究问题**：`Deep CFR 在多人扑克中的收敛性与加速方法`
**参数**：`--per-source 20 --top 40 --expand-top 4 --fetch-top 6 --parse-top 6`

| 阶段 | 结果 |
|---|---|
| STEP 1-2 | 5 个核心概念；arXiv 11 / OpenAlex 7 / S2 5 条检索式 |
| STEP 3 | arXiv 126 条 + OpenAlex 140 条（**S2 未配置 key，自动跳过**） |
| STEP 4 | 去重 **266 → 108**（合并 46 组：强标识符 146 处 / 标题 12 处） |
| STEP 5 | 111→40 保留；Top1 = *RainbowCFR*（0.595） |
| STEP 6 | 4 个种子 → 新增 110 条候选 → 去重后保留 60 篇 |
| STEP 7 | 6 篇目标，拿到 **4 个 PDF**（全部来自 arXiv） |
| STEP 8 | **4 篇全部 GROBID 解析成功**：21/23/20/11 个章节，130/78/62/30 段落 |

**最终语料库**：

```
60 篇论文 | 有摘要 56 | 有 PDF 4 | 已结构解析 4 | 有引用图 45
引用边 1910 条，其中两端都在语料库内 176 条
```

**引用图枢纽（自动浮现的经典工作）**：

| 被引次数 | 论文 |
|---|---|
| 22 | Monte Carlo Sampling for Regret Minimization in Extensive-Form Games（MCCFR） |
| 18 | DeepStack: Expert-level AI in heads-up no-limit poker |
| 15 | Heads-up limit hold'em poker is solved |
| 11 | Human-level control through deep reinforcement learning |
| 11 | Variance Reduction in MCCFR |

> 这些不是人工挑的，是 Layer 2 图算出来的入度 —— 说明引用图已经可用于
> "经典工作识别 / 研究分支发现"（的后续任务）。

---

## 7. 已知限制

| # | 限制 | 影响 | 后续 |
|---|---|---|---|
| 1 | **S2 未配置 API key**（申请中） | 缺 citation context / intents（的引用上下文）—— S2 的独有能力。**代码已支持 S2 前后向扩展**（audit0930 P1-3 已修），配上 key 即启用 | 配 `Keys.txt` 的 `S2_API_KEY` |
| 2 | arXiv 独有的论文没有引用边（本轮 12 篇） | arXiv API 不提供参考文献；这些论文的图是空的 | 可按标题反查 OpenAlex 补（V1.1） |
| 3 | 全文获取率偏低（6 目标得 4） | 闭源论文本来就拿不到；arXiv 命中率很高 | 加 CORE 兜底（V2） |
| 4 | 只对前 N 篇抓全文/解析 | 这是的**刻意设计**（成本控制），不是缺陷 | 需要更多就调 `--fetch-top` |
| 5 | 排序是规则评分，无语义理解 | 可能漏掉"用词不同但同义"的论文 | 查询扩展可覆盖一部分；更深的需要下一阶段的语义模块 |
| 6 | `noRecordsMatch` / OAI 相关能力未用 | V1 只用 Export API，未接 OAI-PMH | 大批量采集时改用 `arxiv_harvest.py` |
| 7 | 实体合并仍有漏网 | 见坑 5-8，已覆盖主要场景，但不保证 100% | 合并决策都记在 `run.json` 里可复核 |

---

## 8. 与下一阶段的接口

后续「论文理解」「研究方向发现」应直接消费本目录产物：

| 后续任务 | 用哪一层 | 取什么字段 |
|---|---|---|
| 单篇论文理解 | Layer 3 `structures/*.json` | `sections[].paragraph_list`、`figures/tables/equations` |
| 领域脉络分析 | Layer 2 `graph.jsonl` | 入度（经典工作）、出度（继承关系）、时间序 |
| 引用语境分析 | `corpus.jsonl` | `citation_graph` + （待 S2 key）`contexts` / `intents` |
| 研究方向发现 | Layer 1 + 2 + 3 | `score.why`（排序理由）、`metadata.limitations`（待理解阶段补） |
| 复现一次检索 | `run.json` | 完整配置 + 统计 + 时间线 |

---

## 8b. V1 验收（对照 `PaperSearch/test.md`）

`acceptance/` 按 test.md 固定规格执行完整验收，产出要求的 ①–⑪ 类产物。

```bash
cd PaperSearch/ExamplePy && ./grobid-serve.sh start          # STEP7 需要
cd PaperSearch/ProcessPy
PY="${PYTHON:-python}"

$PY acceptance/run_acceptance.py                              # 完整验收（联网，约 16 分钟）
$PY acceptance/make_report.py <run_dir>                       # 生成验收报告
$PY -m unittest discover -s acceptance -p 'edge_tests.py'     # 32 项离线边界测试 E1–E9
$PY -m unittest discover -s audit0930  -p 'test_contracts.py' # 9 项契约测试
```

**最近一次验收**（`acceptance/20261008-165716/`，运行 968 秒）：

| 项 | 实测 | test.md 阈值 | 结论 |
|---|---|---|---|
| 种子查询 | 8 组 × 3 源（+ 奠基通道） | 8 组 | ✅ |
| 原始命中 | 145 | ≤240 | ✅ |
| 分页验证 | 两页各 5 条、无跨页重复 | 两页 10 条 | ✅ |
| 去重 | 135 → 78（合并 29 组） | — | ✅ |
| 筛选台账 | 纳入 51 / 待定 6 / 排除 21 | 三类 + D 标签 | ✅ |
| 最终集合 | **30 篇** D1=30 D2=8 D3=2 D4=7 | 15–30，每方向 ≥2 | ✅ |
| 有效 PDF | **5 篇**（均带 SHA256） | ≥5 | ✅ |
| GROBID 结构 | **5 篇**，bibr 连接率 100% | ≥5 | ✅ |
| 引用图 | 641 边 / 24 内部 / 464 unresolved 节点 | 非空真实图 | ✅ |
| 复跑幂等 | 论文 30→30、边 641→641、重复 0 | 不新增重复 | ✅ |
| 方向覆盖 | D3 仅 2 篇 | ≥2 | ⚠️ 临界 |
| 标题锚点 | 2/4 未召回（诊断证实文献存在） | 调查并报告 | ❌ 缺陷 |
| S2 数据源 | 未配置 key | 必需来源 | ⛔ 受阻 |

**判定：FAIL** —— 依 test.md「已有实现缺陷时总结应为 FAIL，不能用 BLOCKED 遮盖」。
缺陷：标题锚点覆盖不足（8 组查询在每源 10 条上限内无法召回 2007/2009 两篇奠基作）。
受阻：S2 缺凭证。详见报告。

### 未修复的实现缺口（来自 32 项离线边界测试）

高严重度 5 项：

| 编号 | 问题 | 影响 |
|---|---|---|
| G1 | `dedup.strong_keys()` 不归一化标识符 | `https://doi.org/…` 与裸写、`1809.03057v3` 与 `1809.03057` 强匹配失败 → 漏合并 |
| G5 | 冲突强标识符不阻止合并 | 两条各带不同 arXiv ID 的记录可能仅凭标题相似被合并 |
| G7 | HTTP 200 + HTML 错误页被当成功 | 失败伪装成功，`run()` 报 `status=done` |
| G9 | 单条查询异常丢掉同源前面已得结果 | 违反 test.md E5「已有结果保留」 |
| ~~G14~~ | ~~`save_papers([])` 的 -1 被当论文数~~ | **已修复**（`_finish()` 改用 `len(ctx.papers)`）|

已修复：**G13**（resume 配置漂移检测：筛选配置变化时自动失效受影响步骤）、**G14**。

## 8c. 第二轮验收修复（2026-10-08）

对照b 那次验收暴露的问题修复。全部改动都有离线测试或产物验证。

| # | 问题 | 根因 | 修法 |
|---|---|---|---|
| 1 | **主键规则被违反**：4 篇有 DOI 却仍用 `openalex:W...` 作主键 | DOI 是 `enrich_crossref`/Unpaywall/引用扩展**之后**才补上的，补完没人重算 `paper_id`。后果：主键取决于"DOI 是哪一步补的"这种偶然因素，**同一篇在不同运行里可能得到不同主键** | 新增 `dedup.rekey()`：按当前 identity 重算主键并生成别名表；在 Crossref 后、Unpaywall 后、**扩展合并后**各调用一次。修复后主键分布 doi=22 / arxiv=7 / openalex=1（那 1 篇确实无 DOI） |
| 2 | GROBID 一次探活失败 ⇒ **整批放弃解析**、规模验收直接不达标 | `step7` 只探活一次，瞬时抖动被放大成整段产物缺失 | 探活重试 3 次 × 2 秒间隔 |
| 3 | `raw_responses/` 是空的（③ 必需产物缺失） | `_archive()` 只写内存、从未落盘 | 按「查询×源×通道」分片落盘，含 `record_sha256` 可校验未被篡改 |
| 4 | 存档可能泄漏凭证 | OpenAlex 客户端把 `api_key`/`mailto` 拼进 URL | 新增 `redact_url()` 逐参数脱敏；OpenAlex/S2 改存端点模板并**如实标注**（不假装是真实 URL）。已实测：全目录搜索真实 key 与邮箱均不出现 |
| 5 |人工抽查完全缺失（PASS 必要条件） | 未曾实现 | 新增 `acceptance/spot_check.py`：8 篇论文 / 5 条边 / 3 篇文档 / 3 例合并，**每项都给出磁盘上可打开的路径 + 实际读出的值**，并主动全量排查（重复标题、悬空边、方向错误、年份越界、引文连接率） |
| 6 | 复跑幂等检查把 596 条未解析边误判成"重复键" | 检查用了 `(from,to,rel)`，而未解析边的 `to` 是空串；`save_graph` 实际用的是 `unresolved` 节点 ID | 检查键与实现对齐。实测 641/641 全唯一 |

**当前判定：FAIL**（依 test.md「有实现缺陷时不得用 BLOCKED 遮盖」）
- 缺陷：标题锚点覆盖不足 —— 2007/2009 两篇 CFR 奠基作在 OpenAlex 确实存在（相似度 1.0），但 8 组种子查询在每源 10 条上限内无法召回。根因：2007 那篇标题里没有 "counterfactual"（这名字是后来普及的），相关度排序里被新论文埋在 10 名之外。
- 受阻：S2 未配置 `S2_API_KEY`。

### 尚未修复的高严重度缺口（来自 32 项离线边界测试）

| 编号 | 问题 | 影响 |
|---|---|---|
| G1 | `dedup.strong_keys()` 不归一化标识符 | `https://doi.org/…` 与裸写、`1809.03057v3` 与 `1809.03057` 强匹配失败 → 漏合并 |
| G5 | 冲突强标识符不阻止合并 | 各带不同 arXiv ID 的两条记录可能仅凭标题相似被合并 |
| G7 | HTTP 200 + HTML 错误页被当成功 | **失败伪装成功**，`run()` 报 `status=done` |
| G9 | 单条查询异常丢掉同源前面已得结果 | 违反 test.md E5「已有结果保留」 |

## 8d. 缺口修复与 PDF 坐标（2026-10-08）

### 已修复的缺口（子代理 32 项边界测试找出的 G1–G14）

| 编号 | 问题 | 修法 | 验证 |
|---|---|---|---|
| **G1** | `strong_keys()` 用**原始字符串**做键 → `https://doi.org/10.1609/AAAI...` 与裸写、`1809.03057v3` 与 `1809.03057` 都匹配不上，强标识符那一轮完全失效 | 改用 `normalize_doi()` / `normalize_arxiv()`；OpenAlex 等取短 ID 并小写 | 边界测试 E1 翻转后通过 |
| **G2** | 合并后 `identity["doi"]` 仍是宿主原来的 URL 写法 | `Paper.merge()` 结束时调 `_normalize_identity()` 写回规范形态 | E1 |
| **G3** | arXiv 版本号被彻底丢弃（只在 comment 自由文本里可能残留） | adapter 把剥离前的版本存进 `metadata.arxiv_version`；identity/主键仍刻意去版本 | E1 |
| **G5** | 两条各带**不同** arXiv ID 的记录，仅凭标题相似就被合并（一个 ID 被静默丢弃） | 新增 `conflicting_strong()`：同类强标识符值不同即判为不同论文；冲突配对记进 `report.blocked_by_conflict` | E2 |
| **G6** | 非法 DOI（如 `not-a-doi`）让 `paper_id` 塌缩成 `"doi:"`，多篇主键相同 | `make_paper_id()` 逐级回退（DOI→arXiv→OpenAlex→S2→标题哈希），归一化后为空就换下一类 | E3 |
| **G7** | **HTTP 200 + HTML 错误页被当成功** → `run()` 报 `status=done` | 新增 `response_ok()`：除状态码外检查 ① body 里的 error ② 结果列表字段存在且为 list；检索与扩展两阶段统一用它 | E4 |
| **G9** | 单条查询抛异常会**丢掉同源前面已得结果** | 三个源的循环体加 per-query `try/except`，坏查询记进 `errors`、好查询结果保留，并置 `partial` 标记 | E4 |
| **G14** | `save_papers([])` 的 `-1` 被当论文数 | `_finish()` 改用 `len(ctx.papers)` | E9 |

### 顺带修的两个隐蔽问题

| 问题 | 根因 |
|---|---|
| `build_id_index()` 漏掉大小写与 URL 形态 | OpenAlex ID 大小写不敏感，记录里存 `w_b`、引用目标写 `W_B`，只按原样查会漏 → 边解析失败。现在多形态 + 大小写不敏感查找 |
| `response_ok()` 过度校验，把**合法空集**当错误 | `get_references()` 返回 `{"works": []}`，而校验只认 `results/entries/papers/data` → "这篇没有参考文献"被错报成"接口失败"。补齐键名清单并明确"空 list 也是成功" |

**结果：32/32 边界测试全部转为验证正确行为并通过。**

### PDF 坐标提取（新增能力）

知道每个图、表、公式、段落在 PDF 的**哪一页、哪个位置**。

```python
cfg.tei_coordinates = True                                   # 默认开
cfg.coordinate_elements = ("figure","table","formula")        # 默认只取图表公式
# 需要段落级回链时：+ ("p","head","ref")
```

**实现要点**（完整说明见 [`../ExamplePy/COORDINATES.md`](../ExamplePy/COORDINATES.md)）：

1. GROBID 的坐标**不是默认输出**，必须用可重复参数 `teiCoordinates=<元素名>` 显式请求；
2. 原始格式是 `页码,x,y,宽,高`，多个框用 `;` 分隔；
3. **一个元素有多个框**（GROBID 按行给）—— 一个 16 行的 `<figure>` 有 16 个小框，
   直接取第一个会得到 238×9 的扁条，必须**合并成包围盒**；
4. **跨页元素没有单一矩形** —— 每页各给一个框，另给整体 bbox；
5. 坐标系是**原点左上、y 向下、单位 PDF 点**（与 PDF 原生相反，画框要换算）。

**数据位置**：`document_structure.figure_objects[].coords`（含 `coords_boxes_per_page`、
`coords_raw_boxes`）、`document_structure.coordinates.elements`（段落/标题级）。

**实测**：`GPU-CFR` 等 4 篇共 118 个元素有坐标，图表 **36/36 全部拿到**；
用坐标区域从 PDF 反向提取文字与 TEI 比对，重叠率 100%（独立验证坐标指向正确区域）。

## 8e. 第二轮缺口修复 + 句子级 span（2026-10-08）

### G10 / G12 已修复

| 编号 | 问题 | 修法 | 验证 |
|---|---|---|---|
| **G10** | 只校验 `%PDF-` 魔数 → **截断的 PDF 被当成功**，写盘、计入 `with_pdf`、到 GROBID 才报错 | 新增 `fulltext.pdf_integrity()`：三层校验 ① `%PDF-` 头 ② `%%EOF` 结尾 ③ **pypdf 能打开且页数 ≥1** | E7 翻转后通过；6 个真实 PDF 全部通过（无误杀） |
| **G12** | `parse_structure()` 没调用 `parse_tei_citation_links()` → **引文连接只活在验收脚本里**，Layer 3 拿不到 | 接线到 `document_structure.citation_links`（含 `links`/`unlinked`/`object_links`/`link_rate`） | E8 翻转后通过；真实样本连接率 **100%** |

> G10 的取舍记录：第一版用"文件 < 1024 字节即判不完整"作判据，
> 结果**误杀了一个 584 字节的合法最小 PDF**。完整性要靠**结构**判断，不能靠体积。

### 新增：句子级 span（对接下游 `spans` 契约）

读了下游 `` 与 ``，其 `spans` 段要求
`span_id / section / char_start / char_end / text`，且 `claims` 表每条论点**必须**
指向 span 或 media_id（防幻觉锚点）。原先只给到"整段文本"，粒度太粗 ——
一个 200 字段落里可能混着多个论点，claim 指向整段等于没定位。

**实现**（`ExamplePy/grobid.py: build_spans()`）：

```
document_structure.spans = [
  {"span_id": "span_0001", "section": "Introduction", "section_index": 2,
   "paragraph_index": 1, "sentence_index": 0,
   "char_start": 0, "char_end": 85,
   "text": "Counterfactual Regret Minimization(CFR) has shown ...",
   "coords": {"x":74,"y":238,"x2":273,"y2":326,...},
   "coords_level": "paragraph",      ← ★ 如实标注精度
   "page_first": 1, "page_last": 1}
]
```

**两个关键取舍**：

1. **`coords_level: "paragraph"`** —— GROBID 只给**段落**级框，句子级框需要逐 token
   坐标（GROBID 不提供）。硬估会给出**假精度**，下游按假句子框高亮会指错位置。
   所以句子继承所属段落的框，并明确标注精度级别。
2. **分句用"看后一个字符"的统一判据**，不用缩写白名单。
   白名单永远列不全且会互相打架（实测：保护 `Sec.` 之后 `(Lanctot et al. 2009).`
   反而被从中间切开）。统一判据是 `[.!?] + 空白 + 大写/引号/括号开头` 才切：

   | 输入 | 结果 |
   |---|---|
   | `See Fig. 3 for details.` | 不在 `Fig.` 后切 ✅ |
   | `(Lanctot et al. 2009).` | 不切 ✅ |
   | `bounded. Sec. 3.1 shows` | 在 `bounded.` 后切 ✅ |
   | `Version 1.2.3 was released.` | 不在编号处切 ✅ |

**实测**（3 篇真实论文）：**1247 个 span，1240 个带段落坐标**，引文连接率 100%。

### 下游对接状态

`PaperOrganization/import_papersearch.py` 已能消费全部字段。其第 186 行原注释
"本轮导入暂不切句，留空结构"—— 现在 `spans` 已可填充。

## 9. 迁移说明

| 项 | 处理 |
|---|---|
| `_bootstrap.py` | **必须保留** —— 它是唯一的对外依赖入口；换机器只需设 `PROCESSPY_EXAMPLEPY` |
| `out/` | 运行产物，不迁移（已在 `.gitignore`） |
| `__pycache__/` | 不迁移 |
| 与 ExamplePy 的关系 | ProcessPy **只调用、不修改** ExamplePy；两边可独立升级 |
| Python | 3.8+（与 ExamplePy 一致，无第三方依赖） |
