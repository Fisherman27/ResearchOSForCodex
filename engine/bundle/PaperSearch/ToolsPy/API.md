# ToolsPy API 文档

> 本文档由 `gen_api_md.py` 从代码自动生成 —— **不要手工编辑**，
> 改代码后重跑该脚本。手写文档一定会和实现脱节，而 Agent 完全依赖它。

## 怎么用这份文档（给 Agent）

别一次读完 —— 它很长。正确姿势：

```python
from papersearch import registry

registry.list_tools()              # 1. 看有哪些工具（含一句话说明与使用时机）
registry.find_symbol("E10")        # 2. 拿到该条目在本文档的**行号区间**
#   → {'line_start': 123, 'line_end': 178, 'read_hint': "read(..., offset=123, ...)"}
registry.read_symbol("E10")        #    或直接取该段 markdown（更省事）
```

这样只读需要的那一段，不必把整份文档塞进上下文。

---

## 目录

- [`E00` doctor](#e00-doctor) — 见第 142 行
- [`E01` list_sources](#e01-list_sources) — 见第 164 行
- [`E10` search_arxiv](#e10-search_arxiv) — 见第 186 行
- [`E11` search_openalex](#e11-search_openalex) — 见第 216 行
- [`E12` search_s2](#e12-search_s2) — 见第 245 行
- [`E13` search_all_sources](#e13-search_all_sources) — 见第 272 行
- [`E14` search_crossref](#e14-search_crossref) — 见第 301 行
- [`E20` normalize_ids](#e20-normalize_ids) — 见第 322 行
- [`E21` resolve_doi](#e21-resolve_doi) — 见第 348 行
- [`E22` match_title](#e22-match_title) — 见第 374 行
- [`E30` find_pdf_urls](#e30-find_pdf_urls) — 见第 402 行
- [`E31` download_pdf](#e31-download_pdf) — 见第 428 行
- [`E32` resolve_oa](#e32-resolve_oa) — 见第 457 行
- [`E40` grobid_available](#e40-grobid_available) — 见第 484 行
- [`E41` parse_pdf](#e41-parse_pdf) — 见第 510 行
- [`E42` extract_text](#e42-extract_text) — 见第 539 行
- [`E50` get_references](#e50-get_references) — 见第 565 行
- [`E51` get_citations](#e51-get_citations) — 见第 592 行
- [`E52` get_citation_edges](#e52-get_citation_edges) — 见第 619 行
- [`E53` get_works_by_ids](#e53-get_works_by_ids) — 见第 646 行
- [`P10` search_pipeline](#p10-search_pipeline) — 见第 675 行
- [`P20` acquire_pipeline](#p20-acquire_pipeline) — 见第 705 行
- [`P30` full_pipeline](#p30-full_pipeline) — 见第 734 行

---

## 一、核心契约（读工具前必看）

### 1.1 所有工具都返回同一个信封

```jsonc
{
  "ok":       true,          // 调用是否成功（语义见下表）
  "status":   "partial",     // ok | partial | empty | blocked | failed
  "data":     {...},          // 主产物（成功时）
  "error":    null,           // 失败时是标准错误字典（含 code/retryable/hint/cause）
  "warnings": [...],          // 非致命问题（数据可疑、源降级……）
  "diagnostics": {...},       // 过程指标：耗时、各步状态、命中数、失败详情
  "meta":     {...}           // 工具名、参数快照、时间
}
```

### 1.2 status 的语义边界（**本库最重要的约定**）

| status | ok | 含义 | 调用方该做什么 |
|---|---|---|---|
| `ok` | ✅ | 全部成功，data 完整 | 直接用 |
| `partial` | ✅ | 拿到了**部分**结果且有明确原因 | **结果可用**，看 diagnostics 决定是否补救 |
| `empty` | ✅ | 成功执行但结果为空 | **空集不是错误**；放宽条件或接受 |
| `blocked` | ❌ | 缺前提（无 key/服务没起） | 按 error.hint 补前提，**重试无用** |
| `failed` | ❌ | 执行出错 | 看 error.code 与 retryable 决定重试/上报 |

> ⚠️ **为什么 `empty` 要单独一类**：
> "检索到 0 篇" 和 "检索失败" 在传统代码里都表现为空列表/None，
> 下游无法区分"这个领域真的没论文"和"我的请求挂了"。
> 实测踩过：HTTP 200 + HTML 错误页被当成"成功的空集"，整个流程报 done ——
> **失败伪装成功**是最难发现的一类 bug。

> ⚠️ **为什么 `partial` 的 ok 是 True**：
> 多源检索里单源失败是常态（限流/缺凭证）。若判为失败，
> 调用方会丢弃已经拿到的结果 —— 那才是真正的损失。

### 1.3 判断要不要继续往下走

```python
from papersearch import result
r = search_pipeline("deep cfr")
result.is_usable(r)   # True  → 结果可用（含 partial/empty），可以继续
result.is_ok(r)       # True  → 严格成功（仅 ok）
result.pretty(r)      # 一行摘要，适合打日志
```

### 1.4 出错时 Agent 该怎么定位

每个错误都带这几样（全部来自 `errors.py`）：

| 字段 | 怎么用 |
|---|---|
| `error.code` | 稳定的字符串码，可 `==` 判断、可写进分支逻辑 |
| `error.retryable` | **该不该重试** —— 重试有代价（耗时/限流），别盲目重试 |
| `error.hint` | 该怎么办，**可执行**的措辞（不是"检查网络"这种废话） |
| `error.severity` | `input`(改参数) / `degradable`(可降级) / `fatal`(做不成) |
| `error.stage` | 多步工具的步骤名 —— 告诉你是第几步挂的 |
| `error.component` | 出错的工具编号（如 `E41`）→ 用 `find_symbol` 跳到本文档对应节 |
| `error.context` | 相关输入快照 —— "我传了什么导致这样" |
| `error.cause` | 原始异常 + **traceback**（本项目自己的 bug 靠它定位） |

---

## 二、工具清单

| 编号 | 名称 | 类型 | 一句话说明 |
|---|---|---|---|
| `E00` | `doctor` | 元件 | 环境自检：依赖 / 凭证 / GROBID 服务 / 各源可达性 |
| `E01` | `list_sources` | 元件 | 列出数据源与能力矩阵 |
| `E10` | `search_arxiv` | 元件 | arXiv 检索（Export API） |
| `E11` | `search_openalex` | 元件 | OpenAlex 检索（主索引：被引数/参考文献/OA/摘要） |
| `E12` | `search_s2` | 元件 | Semantic Scholar 检索（**需 key**；独有 citation context） |
| `E13` | `search_all_sources` | 元件 | 多源检索 + 跨源去重 |
| `E14` | `search_crossref` | 元件 | Crossref DOI 出版物补召回（无需 key） |
| `E20` | `normalize_ids` | 元件 | 标识符归一化 + 生成确定性 paper_id |
| `E21` | `resolve_doi` | 元件 | DOI → Crossref + Unpaywall 记录 |
| `E22` | `match_title` | 元件 | 标题 → Crossref 候选（给缺 DOI 的论文补 DOI） |
| `E30` | `find_pdf_urls` | 元件 | 列出候选 PDF 地址（不下载，按优先级排序） |
| `E31` | `download_pdf` | 元件 | 下载 PDF 并校验完整性（头/尾/可打开） |
| `E32` | `resolve_oa` | 元件 | Unpaywall：DOI → OA 位置 |
| `E40` | `grobid_available` | 元件 | GROBID 服务是否可用（解析前先调） |
| `E41` | `parse_pdf` | 元件 | PDF → 结构化元件（章节/段落/span/图表/坐标/引文连接） |
| `E42` | `extract_text` | 元件 | 纯文本兜底（不依赖 GROBID，**无结构**） |
| `E50` | `get_references` | 元件 | 取某论文的参考文献（它引用了谁） |
| `E51` | `get_citations` | 元件 | 取某论文的被引（谁引用了它） |
| `E52` | `get_citation_edges` | 元件 | 把一批论文抽成统一引用边（citing→cited） |
| `E53` | `get_works_by_ids` | 元件 | **按 ID 批量取元数据**（把 OpenAlex/DOI/arXiv ID 变成完整记录） |
| `P10` | `search_pipeline` | 流水线 | 检索 → 去重 → 排序（不抓 PDF，快） |
| `P20` | `acquire_pipeline` | 流水线 | 检索 → 排序 → 抓 PDF → 解析结构 |
| `P30` | `full_pipeline` | 流水线 | 完整流程：检索→去重→排序→引用扩展→抓PDF→解析→三层落盘 |

### 常用组合（菜谱）

- **先看这个题目有哪些论文** → `search_pipeline(query, keep_top=20)`
  - 不抓 PDF，几秒返回
- **拿到论文全文与结构元件** → `acquire_pipeline(query, fetch_top=5)`
  - fetch_top 控制成本：抓 PDF 最慢
- **产出一份可交给下游的语料库** → `full_pipeline(query, out_dir=..., fetch_top=8)`
  - 三层落盘 + run.json 可复现
- **工具报错了，先查环境** → `doctor()`
  - 看 next_actions；多数问题是缺 key 或 GROBID 没起
- **只想解析本地一个 PDF** → `parse_pdf('/path/to.pdf')`
  - 先 grobid_available() 确认服务在
- **只要纯文本、不要结构** → `extract_text('/path/to.pdf')`
  - 不需要 GROBID
- **给一篇没 DOI 的论文补 DOI** → `match_title(title, author='smith')`
  - 保守：达不到门槛不给 accepted，别硬塞

---

## 三、工具详解

### 元件工具（原子能力）

### [E00] doctor

**模块**：`papersearch.elements` ｜ **签名**：`doctor() -> 'Dict[str, Any])`

**用途**：环境自检：依赖 / 凭证 / GROBID 服务 / 各源可达性

**什么时候用**：**任何工具报错前先跑它** —— 多数'工具不工作'是缺 key 或服务没起

**参数**：无

**返回**：`{dependencies, credentials, grobid, sources, next_actions}`

**状态语义**：ok | partial(有补救项) | failed(依赖不可用)

**详细说明**

```text
用途：**任何工具报错前先跑它** —— 大多数"工具不工作"其实是缺 key 或
服务没起，而不是代码问题。Agent 应先 doctor() 再决定策略。

返回 data = {
    "dependencies": {...},      # PaperSearch 接入状态
    "credentials":  {...},      # 各 key 有没有（**不含值**）
    "grobid":       {...},      # 服务地址/是否在线/版本
    "sources":      {...},      # 各源可用性下结论
    "next_actions": [...]       # 建议的补救动作（可执行）
}
```

---

### [E01] list_sources

**模块**：`papersearch.elements` ｜ **签名**：`list_sources() -> 'Dict[str, Any])`

**用途**：列出数据源与能力矩阵

**什么时候用**：不确定该用哪个源时

**参数**：无

**返回**：`{sources[], capability_matrix{}}`

**状态语义**：ok

**详细说明**

```text
用途：Agent 先看"哪个源能干什么"再决定用哪条路径，不用去读源码。

返回 data = {"sources": [{name, kinds, needs_key, rate_limit, notes}, ...],
             "capability_matrix": {能力: [源...]}}
```

---

### [E10] search_arxiv

**模块**：`papersearch.elements` ｜ **签名**：`search_arxiv(query: 'str', max_results: 'int' = 20, *, start: 'int' = 0, sort_by: 'str' = 'relevance', sort_order: 'str' = 'descending', year_from: 'int' = 0, year_to: 'int' = 0) -> 'Dict[str, Any])`

**用途**：arXiv 检索（Export API）

**什么时候用**：需要 arXiv 全文（PDF 直下最稳）或投递时间

**参数**

| 参数 | 说明 |
|---|---|
| `query` | arXiv 语法，支持 all:/ti:/abs:/au:/cat: 与布尔 |
| `max_results` | 上限 2000，默认 20 |
| `start` | 偏移（翻页） |
| `sort_by` | relevance|lastUpdatedDate|submittedDate |
| `year_from/year_to` | 本地过滤（API 不支持） |

**返回**：`[Paper]`

**状态语义**：ok | empty(0 命中) | failed

**详细说明**

```text
参数
----
query       : arXiv 检索式。支持字段前缀与布尔：
                all:"deep cfr"       全部字段
                ti:"..." / abs:"..." 标题 / 摘要
                au:smith             作者
                cat:cs.AI            分类
                "a" AND "b" / OR / ANDNOT
max_results : 本次最多取多少条（**官方上限 2000**）。默认 20。
              超过 200 会明显变慢；超过 2000 会被拒。
start       : 起始偏移（翻页用）。深度分页在 ~10000 处会 500。
sort_by     : relevance | lastUpdatedDate | submittedDate
year_from/to: 年份过滤（**本地过滤**，arXiv API 不支持按年份筛）。
              传了会多取一些再筛，diagnostics 里会说明真实请求量。

返回 data = [paper 字典]（字段见 API.md 的「Paper 结构」）
     status: ok / partial / empty / failed
注意：arXiv 不提供参考文献 —— 引用图要用 OpenAlex（E11/E50）。

示例
----
    >>> r = search_arxiv('all:"counterfactual regret minimization"', 10)
    >>> r["ok"], len(r["data"])
    (True, 10)
```

---

### [E11] search_openalex

**模块**：`papersearch.elements` ｜ **签名**：`search_openalex(query: 'str', max_results: 'int' = 25, *, page: 'int' = 1, sort: 'str' = '', year_from: 'int' = 0, year_to: 'int' = 0, per_page: 'int' = 0) -> 'Dict[str, Any])`

**用途**：OpenAlex 检索（主索引：被引数/参考文献/OA/摘要）

**什么时候用**：几乎总是 —— 信息最全的源

**参数**

| 参数 | 说明 |
|---|---|
| `query` | 自由文本，支持 AND/OR/NOT 与短语 |
| `max_results` | 默认 25，内部自动翻页 |
| `sort` | 如 publication_date:asc、cited_by_count:desc |
| `year_from/year_to` | 服务端过滤 |

**返回**：`[Paper]（含 citation_count/topics/abstract）`

**状态语义**：ok | empty | failed | failed(RATE_LIMITED)

**详细说明**

```text
参数
----
query      : 自由文本；支持 OpenAlex 语法：
               "deep cfr"（短语）  a AND b   a OR b   a NOT b
max_results: 最多返回多少条（内部按 per_page 分页取够）
per_page   : 每页条数（1~200）。0 = 自动（≤ per_page 上限）
sort       : 排序，如 "publication_date:asc"、"cited_by_count:desc"
year_from/to: 年份过滤（OpenAlex **支持**服务端过滤，会走 filter）

返回 data = [paper 字典]，含 citation_count / topics / OA 位置 / 摘要。
     ⚠️ 摘要来自 `abstract_inverted_index`，本函数已还原成文本。

为什么 OpenAlex 是主索引：它一份记录里同时有被引数、参考文献、
OA 位置、主题标签和摘要 —— 其他源都只覆盖一部分。

示例
----
    >>> r = search_openalex("counterfactual regret minimization", 10)
    >>> r["data"][0]["metadata"]["citation_count"]     # 被引数
```

---

### [E12] search_s2

**模块**：`papersearch.elements` ｜ **签名**：`search_s2(query: 'str', max_results: 'int' = 20, *, fields: 'str' = '') -> 'Dict[str, Any])`

**用途**：Semantic Scholar 检索（**需 key**；独有 citation context）

**什么时候用**：需要引用上下文（引用它的原句）时

**参数**

| 参数 | 说明 |
|---|---|
| `query` | 自由文本 |
| `max_results` | 上限 100 |

**返回**：`[Paper]`

**状态语义**：**blocked**(无 key) | ok | empty | failed

**详细说明**

```text
参数
----
query       : 自由文本
max_results : 最多条数（S2 单次上限 100）
fields      : 要哪些字段；留空用推荐集合（含 externalIds / abstract /
              citationCount / openAccessPdf）。**externalIds 是跨库对齐的桥梁**，
              别去掉。

无 key 时返回 **blocked**（不是 failed）—— 这是前提缺失，不是代码问题：
    error.hint 会告诉你把 S2_API_KEY 加到 Keys.txt。

本源的独有能力：**citation context**（引用它的原句），
需要用 citations(contexts=...) 单独取，见 API.md。

示例
----
    >>> r = search_s2("deep cfr", 5)
    >>> r["status"]        # 无 key 时
    'blocked'
```

---

### [E13] search_all_sources

**模块**：`papersearch.elements` ｜ **签名**：`search_all_sources(query: 'str', max_per_source: 'int' = 20, *, sources: 'Optional[List[str]]' = None, dedup: 'bool' = True, year_from: 'int' = 0, year_to: 'int' = 0) -> 'Dict[str, Any])`

**用途**：多源检索 + 跨源去重

**什么时候用**：常规入口：一次拿到多源合并结果

**参数**

| 参数 | 说明 |
|---|---|
| `query` |  |
| `max_per_source` | 每源条数 |
| `sources` | 限定源；None=全部可用 |
| `dedup` | 是否去重（默认 True） |

**返回**：`{papers, by_source, source_status, dedup}`

**状态语义**：ok | partial(某源失败但有结果) | blocked(全缺凭证) | empty

**详细说明**

```text
参数
----
query          : 检索式（会按各源语法转换）
max_per_source : **每个源**最多取多少条
sources        : 限定用哪些源；None = 全部可用源（自动跳过无凭证的）
dedup          : 是否跨源去重合并（按 DOI/arXiv/标题+作者+年）

返回 data = {
    "papers": [统一 Paper...],           # 已去重
    "by_source": {"arxiv": n, "openalex": n, ...},
    "source_status": {"arxiv": "ok"|"blocked"|"failed"|"empty", ...},
    "dedup": {"before": n, "after": n, "merged_groups": n, "alias": {...}}
}

★ 状态语义：只要**任一源**有结果就是 ok/partial；
  全部源都 blocked/failed 才是 failed。
  单源失败不会让整个调用失败 —— 这是多源检索的关键设计
  （限流和缺凭证是常态，丢掉已拿到的结果才是真损失）。

示例
----
    >>> r = search_all_sources('deep cfr', 10)
    >>> r["diagnostics"]["by_source"]
    {'arxiv': 10, 'openalex': 10, 'semantic_scholar': 0}
```

---

### [E14] search_crossref

**模块**：`papersearch.elements` ｜ **签名**：`search_crossref(query: 'str', max_results: 'int' = 20) -> 'Dict[str, Any])`

**用途**：Crossref DOI 出版物补召回（无需 key）

**什么时候用**：补充未进入预印本索引的正式出版物

**参数**

| 参数 | 说明 |
|---|---|
| `query` | 自由文本 |
| `max_results` | 上限 20 |

**返回**：`[Paper]（正式出版信息、参考 DOI、可用 PDF 链接）`

**状态语义**：ok | empty | failed

---

### [E20] normalize_ids

**模块**：`papersearch.elements` ｜ **签名**：`normalize_ids(identity: 'Optional[dict]' = None, *, doi: 'str' = '', arxiv_id: 'str' = '', openalex_id: 'str' = '', s2_id: 'str' = '', title: 'str' = '') -> 'Dict[str, Any])`

**用途**：标识符归一化 + 生成确定性 paper_id

**什么时候用**：跨源对齐、验证主键规则时

**参数**

| 参数 | 说明 |
|---|---|
| `identity 或 doi/arxiv_id/openalex_id/s2_id/title` |  |

**返回**：`{paper_id, identity, arxiv_version, id_kind}`

**状态语义**：ok | failed(无任何标识符)

**详细说明**

```text
用途：把各源五花八门的 ID 写法统一，得到**稳定主键**。
同一篇论文在任何源、任何写法下都应得到同一个 paper_id —— 这是去重的基础。

参数
----
identity   : 直接给 {"doi":..., "arxiv_id":...}（与下面几个参数二选一）
doi        : 支持 URL / 裸写 / 带前缀：https://doi.org/10.1/X、doi:10.1/X、10.1/X
arxiv_id   : 支持带版本：1809.03057v3 → 1809.03057（版本存进 arxiv_version）
title      : 兜底用（所有 ID 都没有时按标题哈希生成主键）

返回 data = {
    "paper_id":  "doi:10.1038/nature12373" | "arxiv:1809.03057" | "title:<sha1>",
    "identity":  {归一化后的六个字段},
    "arxiv_version": "v3" | "",
    "id_kind":   "doi" | "arxiv" | "openalex" | "s2" | "title"
}

示例
----
    >>> normalize_ids(doi="https://doi.org/10.1038/Nature12373")["data"]["paper_id"]
    'doi:10.1038/nature12373'
    >>> normalize_ids(arxiv_id="1809.03057v3")["data"]
    {'paper_id': 'arxiv:1809.03057', ..., 'arxiv_version': 'v3', ...}
```

---

### [E21] resolve_doi

**模块**：`papersearch.elements` ｜ **签名**：`resolve_doi(doi: 'str') -> 'Dict[str, Any])`

**用途**：DOI → Crossref + Unpaywall 记录

**什么时候用**：拿到 DOI 后补书目与 OA 位置

**参数**

| 参数 | 说明 |
|---|---|
| `doi` | 支持 URL/裸写 |

**返回**：`{crossref, unpaywall, merged}`

**状态语义**：ok | partial(某源无记录) | empty

**详细说明**

```text
用途：拿到一个 DOI 后补齐书目信息与 OA 位置。

返回 data = {
    "crossref": {...} | None,     # 正式出版信息（标题/作者/venue/license）
    "unpaywall": {...} | None,    # OA 位置（best_pdf / oa_status / license）
    "merged": {...}               # 合并后的统一字段（冲突时以 Crossref 为准）
}
```

---

### [E22] match_title

**模块**：`papersearch.elements` ｜ **签名**：`match_title(title: 'str', *, author: 'str' = '', rows: 'int' = 3, min_similarity: 'float' = 0.92) -> 'Dict[str, Any])`

**用途**：标题 → Crossref 候选（给缺 DOI 的论文补 DOI）

**什么时候用**：论文缺 DOI 且想补全时。**保守**：达不到门槛不给 accepted

**参数**

| 参数 | 说明 |
|---|---|
| `title` |  |
| `author` | 首作者姓氏（提高准确率） |
| `min_similarity` | 采纳门槛，默认 0.92（保守） |

**返回**：`{accepted, candidates, accepted_rule}`

**状态语义**：ok(有达标候选) | partial(有候选但都未达标) | empty

**详细说明**

```text
参数
----
title          : 论文标题
author         : 首作者姓氏（可选，能显著提高准确率）
rows           : 取多少候选
min_similarity : **采纳门槛**。默认 0.92（保守）——
                 Crossref 的匹配分不能单独作为合并依据，
                 必须自己再比标题相似度 + 年份 + 作者。

返回 data = {
    "accepted": {...} | None,      # 达到门槛、可直接采纳的那条
    "candidates": [{...}],         # 全部候选（含未达门槛的，供人工判断）
    "accepted_rule": "标题相似度≥0.92 且（年份一致或缺失）且（首作者一致或缺失）"
}

⚠️ 为什么保守：**错合并比漏合并危害大**（错合并后两篇的证据混在一起拆不开）。
   达不到门槛时 accepted=None，调用方应保持"无 DOI"状态而不是硬塞一个。
```

---

### [E30] find_pdf_urls

**模块**：`papersearch.elements` ｜ **签名**：`find_pdf_urls(paper: 'Optional[dict]' = None, *, doi: 'str' = '', arxiv_id: 'str' = '') -> 'Dict[str, Any])`

**用途**：列出候选 PDF 地址（不下载，按优先级排序）

**什么时候用**：下载失败时先看有哪些候选

**参数**

| 参数 | 说明 |
|---|---|
| `paper 或 doi/arxiv_id` |  |

**返回**：`{urls[], n}`

**状态语义**：ok | empty(闭源，正常情形)

**详细说明**

```text
用途：下载失败时先看有哪些候选，再决定换哪个源。

参数
----
paper    : 统一 Paper 字典（有 identity / full_text 时最准）
doi / arxiv_id : 没有 paper 时可直接给 ID（会先补 OA 位置）

返回 data = {"urls": [{url, source, priority}], "n": n}
    priority 越小越优先：arXiv(10) → OpenAlex/S2(20) → Unpaywall(30) → 其他(40)
```

---

### [E31] download_pdf

**模块**：`papersearch.elements` ｜ **签名**：`download_pdf(paper: 'Optional[dict]' = None, *, doi: 'str' = '', arxiv_id: 'str' = '', out_dir: 'str' = '', max_mb: 'float' = 30.0, verify: 'bool' = True) -> 'Dict[str, Any])`

**用途**：下载 PDF 并校验完整性（头/尾/可打开）

**什么时候用**：需要全文时。失败会依次试所有候选地址

**参数**

| 参数 | 说明 |
|---|---|
| `paper 或 doi/arxiv_id` |  |
| `out_dir` |  |
| `max_mb` | 默认 30 |
| `verify` | 默认 True，别关 |

**返回**：`{path, bytes, sha256, source, url, verified}`

**状态语义**：ok | empty(无 OA 地址) | failed

**详细说明**

```text
参数
----
paper     : 统一 Paper 字典（优先用它的 pdf_url / alt_locations）
out_dir   : 保存目录；留空则用系统临时目录（返回真实路径）
max_mb    : 大小上限（超过则放弃，防呆）
verify    : 是否做完整性校验（%PDF- 头 + %%EOF 尾 + pypdf 可打开）。
            **建议保持 True** —— 去掉后截断文件会被当成功。

返回 data = {"path", "bytes", "sha256", "source", "url", "verified"}
  status: ok / empty（无可用地址，闭源正常情形）/ failed

⚠️ 失败时会**依次尝试候选地址**（arXiv → OA → Unpaywall 回退），
   最终在 diagnostics.attempts 里给出每个地址的失败原因。
```

---

### [E32] resolve_oa

**模块**：`papersearch.elements` ｜ **签名**：`resolve_oa(doi: 'str', *, only_missing: 'bool' = True, current_pdf_url: 'str' = '') -> 'Dict[str, Any])`

**用途**：Unpaywall：DOI → OA 位置

**什么时候用**：想单独查某 DOI 有没有免费全文

**参数**

| 参数 | 说明 |
|---|---|
| `doi` |  |
| `only_missing` | 已有链接时跳过 |

**返回**：`{best_pdf, is_oa, oa_status, version, license, locations}`

**状态语义**：ok | empty(无 OA 版本，正常) | failed

**详细说明**

```text
参数
----
only_missing    : True 时若已有 pdf_url 就跳过（省配额）
current_pdf_url : 与 only_missing 配合 —— 已有的地址

返回 data = {"best_pdf", "oa_status", "version", "license", "locations": [...]}
  没有 OA 版本时 status="empty"（**正常情形**，不是错误）。
```

---

### [E40] grobid_available

**模块**：`papersearch.elements` ｜ **签名**：`grobid_available(url: 'str' = 'http://localhost:8070') -> 'Dict[str, Any])`

**用途**：GROBID 服务是否可用（解析前先调）

**什么时候用**：**解析前必调** —— 避免每篇白等 120s 超时

**参数**

| 参数 | 说明 |
|---|---|
| `url` | 默认 http://localhost:8070 |

**返回**：`{available, version, url, note}`

**状态语义**：ok | blocked(服务没起，hint 给启动命令)

**详细说明**

```text
为什么要单独一个元件：GROBID 请求超时是 120s，服务没起的话
每篇都会白等两分钟。一次探活（几秒）换掉 N 次超时。

返回 data = {"available", "version", "url", "note"}
  服务没起时 status="blocked"，error.hint 给出启动命令。
```

---

### [E41] parse_pdf

**模块**：`papersearch.elements` ｜ **签名**：`parse_pdf(pdf_path: 'str', *, url: 'str' = 'http://localhost:8070', elements: 'Optional[List[str]]' = None, consolidate_citations: 'int' = 0, keep_tei: 'bool' = True, out_dir: 'str' = '', paper_id: 'str' = '') -> 'Dict[str, Any])`

**用途**：PDF → 结构化元件（章节/段落/span/图表/坐标/引文连接）

**什么时候用**：**核心元件** —— 需要论文内部结构时

**参数**

| 参数 | 说明 |
|---|---|
| `pdf_path` |  |
| `elements` | 要坐标的元素，默认含 p/head |
| `consolidate_citations` | **保持 0**（设 1 会慢 140 倍） |
| `keep_tei` | 默认 True |

**返回**：`{parsed_by, sections, spans, figure_objects, table_objects, formula_objects, references, citation_links, coordinates, stats}`

**状态语义**：ok | partial(有降级/空字段) | empty(无内容) | failed

**详细说明**

```text
参数
----
pdf_path  : 本地 PDF 路径
paper_id  : 产物文件的命名依据。**建议传真实的 paper_id**（如 doi:10.xxx）——
            不传时自动用 PDF 文件名+路径哈希生成。
            它决定 `structures/<name>.json` 与 `.tei.xml` 的文件名：
            **不同论文必须给不同值**，否则产物会互相覆盖。
elements  : 要坐标的元素列表。默认 ["figure","table","formula","p","head"]
            —— 含 p/head 才能给句子级 span 配坐标。
            只要图表坐标可传 ["figure","table","formula"]（TEI 更小）。
consolidate_citations : 0（默认）。**别设 1** —— 那会让 GROBID 逐条参考文献
            查 Crossref，实测同一篇 1.6s → 220s（慢 140 倍）。
keep_tei  : 是否保留原始 TEI（证据留存，建议 True）
out_dir   : 产物目录；留空用临时目录

返回 data = {
    "parsed_by": "grobid" | "pypdf",
    "sections":       [{title, level, paragraphs, paragraph_list, text}],
    "spans":          [{span_id, section, char_start, char_end, text,
                        coords, coords_level, page_first, page_last}],
    "figure_objects": [{id, label, text, coords, coords_boxes_per_page, ...}],
    "table_objects":  [...], "formula_objects": [...],
    "references":     [{title, authors, year, doi}],     # 参考文献条目
    "citation_links": {links, unlinked, link_rate, ...}, # 正文↔文献连接
    "coordinates":    {count, by_element, elements},      # 段落级坐标
    "tei_path", "structure_path",
    "stats": {n_sections, n_paragraphs, n_spans, n_figures, n_citation_links}
}

★ 坐标精度如实标注：`coords_level="paragraph"` —— GROBID 只给段落级框，
  句子级框需要逐 token 坐标（它不提供）。硬估会给**假精度**。

★ 降级行为：GROBID 不可用时自动退回 pypdf 纯文本，
  `parsed_by="pypdf"` 且 **sections/spans 为空** —— 不假装有结构。

示例
----
    >>> r = parse_pdf("paper.pdf")
    >>> r["data"]["stats"]
    {'n_sections': 8, 'n_spans': 236, 'n_figures': 8, ...}
```

---

### [E42] extract_text

**模块**：`papersearch.elements` ｜ **签名**：`extract_text(pdf_path: 'str') -> 'Dict[str, Any])`

**用途**：纯文本兜底（不依赖 GROBID，**无结构**）

**什么时候用**：只要文本、不需要结构时

**参数**

| 参数 | 说明 |
|---|---|
| `pdf_path` |  |

**返回**：`{text, n_chars, n_pages, method}`

**状态语义**：ok | empty(扫描版抽不出字) | failed

**详细说明**

```text
用途：GROBID 不可用、或只想快速看内容时。
⚠️ 它**给不了结构**（无章节、无坐标、无图表）——
   不要用它替代 parse_pdf(E41)，除非你明确只需要文本。

返回 data = {"text", "n_chars", "n_pages", "method"}
  method: "pypdf" | "pdftotext" | ...
  抽不出文字（扫描版）时 status="empty" 并说明原因。
```

---

### [E50] get_references

**模块**：`papersearch.elements` ｜ **签名**：`get_references(work_id: 'str', *, limit: 'int' = 50) -> 'Dict[str, Any])`

**用途**：取某论文的参考文献（它引用了谁）

**什么时候用**：向后追溯方法渊源

**参数**

| 参数 | 说明 |
|---|---|
| `work_id` | OpenAlex ID |
| `limit` | 默认 50 |

**返回**：`{works, claimed_count, resolved_count, missing_ids}`

**状态语义**：ok | partial(有声明的引用但取不到元数据) | empty

**详细说明**

```text
参数
----
work_id : OpenAlex ID（W123... / 完整 URL 都行）
limit   : 最多返回多少条元数据（OpenAlex 的 referenced_works 可能上百条，
          本函数先取 ID 列表再批量换元数据，**超出 limit 的只计数不取详情**）

返回 data = {"works": [Paper...], "claimed_count": n, "resolved_count": n,
             "missing_ids": [...]}
    claimed_count：OpenAlex 声称引用了多少篇
    resolved_count：实际取到元数据的篇数
    ★ 两个数可能不等（有些被引文献 OpenAlex 没有元数据）——
      如实给出，不要用一个数字掩盖。
```

---

### [E51] get_citations

**模块**：`papersearch.elements` ｜ **签名**：`get_citations(work_id: 'str', *, limit: 'int' = 50) -> 'Dict[str, Any])`

**用途**：取某论文的被引（谁引用了它）

**什么时候用**：向前找后续工作、识别经典文献

**参数**

| 参数 | 说明 |
|---|---|
| `work_id` |  |
| `limit` | 默认 50 |

**返回**：`{works, total_count}`

**状态语义**：ok | empty(新论文常见) | failed

**详细说明**

```text
参数
----
work_id : OpenAlex ID
limit   : 最多多少条

返回 data = {"works": [Paper...], "total_count": n}
    total_count 是该论文被引总数（可能远大于 limit）。

提示：想知道"哪些引用是正面/负面"需要 citation context ——
      那是 Semantic Scholar 的独有能力，见 API.md 的 S2 小节。
```

---

### [E52] get_citation_edges

**模块**：`papersearch.elements` ｜ **签名**：`get_citation_edges(papers: 'List[dict]', *, include_unresolved: 'bool' = True) -> 'Dict[str, Any])`

**用途**：把一批论文抽成统一引用边（citing→cited）

**什么时候用**：要分析论文间关系时

**参数**

| 参数 | 说明 |
|---|---|
| `papers` | [Paper] |
| `include_unresolved` | 默认 True |

**返回**：`{edges, nodes, stats}`

**状态语义**：ok | empty(无引用关系)

**详细说明**

```text
参数
----
papers             : 统一 Paper 字典列表
include_unresolved : 是否包含"目标不在本批论文内"的边（默认含）

返回 data = {
    "edges":   [{from, to, rel:"cites", internal, to_raw, sources, ...}],
    "nodes":   [{node_id, kind}],       # 含 unresolved 外部节点
    "stats":   {n_edges, n_internal, n_unresolved, n_nodes}
}

★ 方向契约：**所有边都是 citing → cited，rel 恒为 "cites"**。
  论文自身的 `references` / `cited_by` 只是视角，落盘前统一方向 ——
  下游不需要知道"这条边方向是反的"。

★ 未解析目标：`to` 为空但给 `unresolved: <原始ID>` 稳定节点 ID，
  让外部引用在图里也能当节点用，而不是悬空值。
```

---

### [E53] get_works_by_ids

**模块**：`papersearch.elements` ｜ **签名**：`get_works_by_ids(ids: 'List[str]', *, batch_size: 'int' = 50) -> 'Dict[str, Any])`

**用途**：**按 ID 批量取元数据**（把 OpenAlex/DOI/arXiv ID 变成完整记录）

**什么时候用**：引用边里的外部 ID 想反查是什么论文时 —— 调研高频需求

**参数**

| 参数 | 说明 |
|---|---|
| `ids` | 混合列表，支持 W123 / DOI / arXiv ID |
| `batch_size` | 每次请求合并多少（默认 50） |

**返回**：`{works, requested, resolved, missing, missing_ids, id_map}`

**状态语义**：ok | partial(部分 ID 取不到) | empty | failed

**详细说明**

```text
★ 为什么需要它（Agent 模拟调研时发现的高频断点）：
  引用边里绝大多数目标是**语料库外**的论文，只存了 OpenAlex ID
  （`to_raw: "W32403112"`）。调研时最常问的一句话是
  **"这些被共同引用的论文究竟是什么？"** —— 没有这个元件，
  Agent 只能拿 ID 去猜，或者逐个手写 OpenAlex 查询（那就绕过工具库了）。

  E50/E51 是"从某篇出发找它的引用/被引"，**不是**"给定 ID 反查是什么"。
  语义不同，硬用会得到错误结果（把"这个 ID 的参考文献"当成"这个 ID 的信息"）。

参数
----
ids        : 混合列表，支持 OpenAlex ID（W123 / 完整 URL）、DOI（10.x/y）、
             arXiv ID（1809.03057）。**大小写不敏感。**
batch_size : 每次请求合并多少个 ID（OpenAlex 的 filter 支持 OR 批量；
             50 是个稳妥值，太大 URL 会超长）

返回 data = {
    "works": [Paper...],              # 取到的（顺序不保证与输入一致）
    "requested": n, "resolved": n, "missing": n,
    "missing_ids": [...],             # **取不到的 ID 照样列出来**，不静默丢
    "id_map": {"W123": "openalex:w123"}   # 输入 ID → paper_id
}

★ 状态语义：取到一部分 → `partial`（**结果可用**，missing 里能看到缺哪些）；
  一个都没取到 → `empty`。查询失败才是 `failed`。

示例
----
    >>> r = tools.get_works_by_ids(["W2103315867", "10.1609/aaai.v33i01.33012157"])
    >>> for w in r["data"]["works"]:
    ...     print(w["metadata"]["title"])
```

---

### 自动化工具流（流水线）

### [P10] search_pipeline

**模块**：`papersearch.pipelines` ｜ **签名**：`search_pipeline(query: 'str', *, max_per_source: 'int' = 20, keep_top: 'int' = 40, year_from: 'int' = 0, year_to: 'int' = 0, concepts: 'Optional[List[str]]' = None, sources: 'Optional[List[str]]' = None) -> 'Dict[str, Any])`

**用途**：检索 → 去重 → 排序（不抓 PDF，快）

**什么时候用**：只需文献列表、做调研时

**参数**

| 参数 | 说明 |
|---|---|
| `query` |  |
| `max_per_source` | 默认 20 |
| `keep_top` | 排序后保留，默认 40 |
| `year_from/year_to` |  |
| `concepts` | 留空自动抽取 |

**返回**：`{papers, by_source, source_status, dedup, ranking}`

**状态语义**：ok | partial | empty | blocked | failed

**详细说明**

```text
适用：想先看"这个题目有哪些论文"、或只做文献调研时。

参数
----
query          : 检索式（多词自由文本即可，会按各源语法转换）
max_per_source : 每源取多少条
keep_top       : 排序后保留多少篇（0 = 全留）
year_from/to   : 年份范围（0 = 不限）
concepts       : 用于打分的概念词；**留空则自动从 query 抽取**
sources        : 限定源；None = 全部可用

返回 data = {
    "papers": [Paper...]（已去重、已排序、含 score.why）,
    "by_source", "source_status", "dedup",
    "ranking": {n_in, n_out, dropped, top1}
}

示例
----
    >>> r = search_pipeline("counterfactual regret minimization", keep_top=20)
    >>> len(r["data"]["papers"])
    20
    >>> r["data"]["papers"][0]["score"]["why"]     # 为什么排第一
    '标题命中 counterfactual regret minimization; 被引 21'
```

---

### [P20] acquire_pipeline

**模块**：`papersearch.pipelines` ｜ **签名**：`acquire_pipeline(query: 'str', *, max_per_source: 'int' = 20, keep_top: 'int' = 20, fetch_top: 'int' = 5, parse: 'bool' = True, year_from: 'int' = 0, year_to: 'int' = 0, out_dir: 'str' = '', coordinate_elements: 'Optional[List[str]]' = None) -> 'Dict[str, Any])`

**用途**：检索 → 排序 → 抓 PDF → 解析结构

**什么时候用**：既要列表又要全文与元件时

**参数**

| 参数 | 说明 |
|---|---|
| `query` |  |
| `fetch_top` | 抓几篇（**成本关键**） |
| `parse` | 默认 True |
| `out_dir` |  |

**返回**：`{papers, fulltext, parsed, out_dir}`

**状态语义**：ok | partial | empty | failed

**详细说明**

```text
适用：除了找到论文，还要拿到全文与结构化元件（图/表/坐标/span）。

参数
----
fetch_top : 给排序最靠前的多少篇抓 PDF（**成本控制的关键参数**：
            抓 PDF 是最慢的一步，别一次抓几百篇）
parse     : 是否解析结构（需要 GROBID；不可用时自动降级为纯文本）
out_dir   : 产物目录（PDF/TEI 落在这里）；留空用临时目录
coordinate_elements : 要坐标的元素，默认含 p/head（才能给 span 配坐标）

返回 data = {
    "query", "papers": [...],
    "fulltext": [{paper_id, ok, path, source, bytes, sha256, error}],
    "parsed":   [{paper_id, ok, parsed_by, stats, structure_path}],
    "out_dir": "..."
}

★ 状态语义：PDF 拿不到（闭源）是 **empty 不是 failed** ——
  流水线会继续处理其他论文，最终 status=partial 并说明有几篇没拿到。
```

---

### [P30] full_pipeline

**模块**：`papersearch.pipelines` ｜ **签名**：`full_pipeline(query: 'str', *, out_dir: 'str' = '', max_per_source: 'int' = 20, keep_top: 'int' = 30, expand_top: 'int' = 4, fetch_top: 'int' = 8, parse_top: 'int' = 8, year_from: 'int' = 0, year_to: 'int' = 0, expand: 'bool' = True, fetch_pdf: 'bool' = True, parse: 'bool' = True) -> 'Dict[str, Any])`

**用途**：完整流程：检索→去重→排序→引用扩展→抓PDF→解析→三层落盘

**什么时候用**：一条命令产出可交给下游的语料库

**参数**

| 参数 | 说明 |
|---|---|
| `query` |  |
| `out_dir` | **建议显式指定** |
| `expand_top` | 扩展种子数 |
| `fetch_top` |  |
| `parse_top` |  |

**返回**：`{papers, corpus_path, graph_path, out_dir, stats}`

**状态语义**：ok | partial | empty | blocked | failed

**详细说明**

```text
等价于 `ProcessPy` 的 8 步流水线，但**返回值是信封**（不打印、不依赖 CLI），
并且把每一步的状态都放进 diagnostics —— Agent 能精确定位是哪步出了问题。

参数
----
out_dir    : 产物目录。留空则用 `<临时目录>/toolspy_full_<时间>`。
             **建议显式指定** —— 产物要留着给下游用。
expand_top : 对排序最靠前的多少篇做引用扩展（0 = 不扩展）
fetch_top  : 给多少篇抓 PDF
parse_top  : 解析多少篇（应为 fetch_top 的子集）
expand     : 是否做引用网络扩展

返回 data = {
    "papers": [Paper...]（含 document_structure）,
    "corpus_path", "graph_path", "report_path",
    "run_manifest": {...},
    "stats": {n_papers, n_edges, n_pdf, n_parsed, ...}
}

产物落盘（三层结构）：
    <out>/papers.jsonl       Layer 1 论文（含 spans / 图表对象 / 引文连接）
    <out>/citation_edges.jsonl Layer 2 引用边（统一 citing→cited）
    <out>/structures/        Layer 3 结构文档 + TEI
    <out>/pdfs/              原始 PDF
    <out>/run.json           参数快照 + 逐步状态（**复现只需这个文件**）

示例
----
    >>> r = full_pipeline("deep cfr", out_dir="/tmp/kb", fetch_top=3)
    >>> r["status"], r["data"]["stats"]["n_papers"]
    ('partial', 30)
```

---

## 四、错误码表

| 错误码 | 严重度 | 可重试 | 修法 |
|---|---|---|---|
| `AUTH_FORBIDDEN` | fatal | ❌ 否 | 服务端拒绝（403）。多为该接口需要更高权限或 token；换接口或补权限。 |
| `AUTH_INVALID_KEY` | fatal | ❌ 否 | key 无效（服务端返回 401）。核对 Keys.txt 里的值是否过期/写错。 |
| `AUTH_MISSING_KEY` | fatal | ❌ 否 | 缺少 API key。在 Keys.txt 里补上对应字段（见 error.context.key_name），或改用不依赖该源的路径。 |
| `DATA_CONFLICT` | degradable | ❌ 否 | 多源字段冲突（如两个不同的 arXiv ID）。已按保守策略处理，详见 error.context。 |
| `DATA_LOW_QUALITY` | degradable | ❌ 否 | 数据可能不可靠（如坐标只识别到图注、摘要缺失）。按需人工复核。 |
| `DATA_PARTIAL` | degradable | ❌ 否 | 只拿到了部分数据（如分页中断、某源失败）。**结果可用**，但检查 diagnostics 看缺了什么。 |
| `INPUT_INVALID` | input | ❌ 否 | 参数取值非法。对照 API.md 里该工具的「参数」小节。 |
| `INPUT_MISSING` | input | ❌ 否 | 必填参数为空。检查调用参数（见 error.context.missing）。 |
| `INPUT_NOT_FOUND` | input | ❌ 否 | 传入的本地路径不存在。确认路径是否拼错、文件是否已下载。 |
| `INPUT_UNSUPPORTED` | input | ❌ 否 | 该组合/取值本版本不支持。看 error.hint 里的替代做法。 |
| `INTERNAL_ERROR` | fatal | ❌ 否 | ToolsPy 内部异常 —— **这是 bug**。把 error.cause 里的 traceback 和 error.context 一起反馈，可直接定位到代码行。 |
| `INTERNAL_NOT_IMPLEMENTED` | fatal | ❌ 否 | 该能力尚未实现。看 API.md 里该工具的「状态」标注。 |
| `NET_BAD_RESPONSE` | degradable | ✅ 是 | HTTP 通了但内容不是预期格式（常见：网关以 200 返回 HTML 错误页）。可重试；持续出现说明该源或代理有问题。 |
| `NET_HTTP_ERROR` | degradable | ✅ 是 | 服务端返回错误状态码。5xx 可重试；4xx 通常重试无用（看 error.context.status）。 |
| `NET_TIMEOUT` | degradable | ✅ 是 | 请求超时，可重试。若反复超时，调大 timeout 或换源。 |
| `NET_TRUNCATED` | degradable | ✅ 是 | 响应不完整（传输中断）。可重试；大文件建议带断点续传。 |
| `NET_UNREACHABLE` | degradable | ✅ 是 | 网络不可达，可重试；持续失败则检查网络/代理。 |
| `NOTFOUND_DOI` | degradable | ❌ 否 | 该 DOI 在源里不存在。核对 DOI；或改用标题检索。 |
| `NOTFOUND_EMPTY_RESULT` | degradable | ❌ 否 | 检索命中 0 条（成功的空集，不是错误）。放宽查询词/年份，或换源。注意与 NET_BAD_RESPONSE 区分：后者是请求失败，这是请求成功但没结果。 |
| `NOTFOUND_PDF` | degradable | ❌ 否 | 没有可用的开放获取全文。**这不是 bug** —— 闭源论文拿不到。下游应把该论文标记为「无全文」并继续处理其他论文。 |
| `PARSE_EMPTY` | degradable | ❌ 否 | 解析成功但内容为空（0 字符/0 章节）。可能是扫描版 PDF（无文字层）或该文档本身没有正文。换一篇，或标记为需 OCR。 |
| `PARSE_NO_COORDS` | degradable | ❌ 否 | TEI 里没有坐标 —— 解析时**没请求** tei_coordinates。坐标不是默认输出，要在 params 里给 coordinate_elements。 |
| `PARSE_PDF_INVALID` | degradable | ✅ 是 | PDF 不完整或结构损坏（缺 %%EOF / 打不开 / 0 页）。可重试下载；若换了源仍失败，该 PDF 本身有问题。 |
| `PARSE_TEI_INVALID` | degradable | ❌ 否 | GROBID 返回的 TEI 不是合法 XML（常见于响应被截断）。重跑该篇解析。 |
| `RATE_LIMITED` | degradable | ✅ 是 | 被限流。**必须等待**再重试 —— 优先按 error.context.retry_after 秒等待；匿名调用建议配置 key（OpenAlex/S2 都有免费额度）。 |
| `RATE_QUOTA_EXHAUSTED` | fatal | ❌ 否 | 配额用尽（如 OpenAlex 每日 $1）。等次日重置，或改用其他源。 |
| `SERVICE_DEGRADED` | degradable | ❌ 否 | 服务可用但功能降级（如 GROBID 不可用时退回纯文本）。产物里会标明降级原因。 |
| `SERVICE_UNAVAILABLE` | fatal | ✅ 是 | 依赖的服务没起来。GROBID 启动：在 ExamplePy 下跑 ./grobid-serve.sh start（约 20 秒）。 |

---

## 五、Paper 结构（所有工具返回的论文对象）

```jsonc
{
  "paper_id": "doi:10.1609/aaai.v33i01.33012157",  // 确定性主键，见下
  "identity": { "doi", "arxiv_id", "openalex_id", "s2_id",
                "openreview_id", "core_id" },
  "metadata": { "title", "authors", "year", "preprint_year",
                "venue", "categories", "keywords", "topics",
                "citation_count", "is_retracted", "arxiv_version", ... },
  "abstract": "...",
  "full_text": { "pdf_path", "pdf_url", "pdf_source", "oa_status",
                 "version", "license", "pdf_bytes" },
  "citation_graph": { "references", "cited_by", "reference_count", "source" },
  "document_structure": {
      "parsed_by": "grobid",
      "sections":  [{title, level, paragraphs, paragraph_list, text}],
      "spans":     [{span_id, section, char_start, char_end, text,
                      coords, coords_level, page_first, page_last}],
      "figure_objects": [{id, label, text, coords, coords_boxes_per_page,
                           coords_raw_boxes, coords_n_raw_boxes}],
      "table_objects": [...], "formula_objects": [...],
      "citation_links": {links, unlinked, link_rate},
      "coordinates": {count, by_element, elements},
      "references_parsed": [{title, authors, year, doi}] },
  "provenance": { "found_by": [{source, kind, query, at}], "history": [...] },
  "score": { "total", "parts", "why", "rank" }
}
```

### paper_id 生成规则（确定性、可读）

```
doi:10.1038/nature12373      有 DOI（归一化后小写）
arxiv:1603.01121             有 arXiv ID（**去版本号**，v1/v2 是同一篇）
openalex:W2741809807         只有 OpenAlex ID
title:<sha1前16位>           都没有，用标题哈希兜底
```

> 非法标识符会被跳过，**逐级回退**（不会生成 `doi:` 这种空主体）。
> 版本号本身存在 `metadata.arxiv_version`，不丢。

### 坐标（document_structure）

| 项 | 说明 |
|---|---|
| 坐标系 | **原点页面左上角，y 向下**，单位 PDF 点（1/72 英寸） |
| `coords` | 合并后的**包围盒** `{x,y,x2,y2,width,height,page_first,page_last}` |
| `coords_boxes_per_page` | 跨页时每页一个框（跨页元素**没有**单一矩形） |
| `coords_raw_boxes` | GROBID 原始**行框**（证据留存） |
| `coords_n_raw_boxes` | 原始行框数；**==1 时坐标可疑**（GROBID 可能只认到图注） |
| `coords_level` (spans) | 恒为 `paragraph` —— GROBID 只给段落级框，句子级是**假精度** |

> 画到 PDF 上要转换：`pdf_y = page_height - grobid_y2`。

---

## 六、复现与诊断

- 流水线产物目录含 `run.json`：**参数快照 + 逐步状态** —— 复现一次运行只需要它。
- `result.summarize_result(env)` 给你一段人类可读的逐步摘要。
- 出错先跑 `doctor()`：多数"工具不工作"是缺 key 或 GROBID 没启动。

