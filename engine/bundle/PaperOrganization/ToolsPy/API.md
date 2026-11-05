# ToolsPy API 文档

> PaperOrganization 工具库的完整接口说明。
> 配套：[`README.md`](README.md) 是索引，本文件是详细文档。
> 定位工具：`python docs/apidoc.py find <name>` 可直接打印对应条目。

**约定**：
- 所有工具函数返回 `ToolResult`（见 [`errors`](#errors--统一结果与错误机制)），**不抛裸异常**。
- 路径参数 `cfg` 是 `KBConfig`（见 [`paths`](#paths--路径与配置)），由 `resolve_kb()` 得到。

**目录**

| 层 | 条目 |
|---|---|
| 基础 | [errors](#errors--统一结果与错误机制)、[paths](#paths--路径与配置) |
| 元件 | [schema](#schema--建表与连接)、[ids](#ids--id-生成规则)、[importers](#importers--数据转换元件)、[writers](#writers--写库元件)、[validators](#validators--校验元件)、[store](#store--阅读结果落库元件)、[query](#query--查询元件)、[exporters](#exporters--导出元件)、[locate](#locate--全文定位元件)、[conformance](#conformance--符合性检查元件) |
| 流水线 | [pipelines](#pipelines--流水线自动化工具流) |

---

## errors — 统一结果与错误机制

模块：`paperorganization.errors`

工具库的基础层。定义所有函数统一的返回类型与错误结构。

### ToolResult — 统一返回类型

所有工具函数的返回值。

| 属性 | 类型 | 说明 |
|---|---|---|
| `ok` | bool | 是否成功 |
| `data` | Any | 成功时的产物（dict / list / 值） |
| `error` | ToolError \| None | 失败时的结构化错误 |
| `warnings` | List[str] | 非致命问题（降级/跳过/可疑），必须显式记录 |
| `meta` | dict | 元信息（耗时 `ms`、统计等） |

方法：
- `bool(r)` / `if r:` → 等价于 `r.ok`
- `r["key"]` / `r.get("key")` → 直接取 `data` 的字段（`data` 是 dict 时）
- `r.to_dict()` → 转纯 dict（可 JSON 序列化）
- `r.summary()` → 一行摘要
- `r.raise_if_failed()` → 需要异常语义时才用

示例：
```python
r = query.get_paper(cfg, "arxiv:2609.11923")
if r:
    print(r.data["title"])
else:
    print(r.error.code, r.error.hint)
```

### ToolError — 结构化错误

| 字段 | 说明 |
|---|---|
| `code` | 机器可读错误码（`ErrorCode` 常量） |
| `message` | 人类可读说明 |
| `location` | 出错位置，如 `validators.validate_reading_result` |
| `hint` | 修复建议（Agent 据此自我调整） |
| `detail` | 内部 bug 时的堆栈 |
| `context` | 相关数据快照 |

### ErrorCode — 错误码常量

| 前缀 | 常量 | 含义 |
|---|---|---|
| 输入 | `MISSING_FIELD` / `INVALID_ENUM` / `INVALID_TYPE` / `INVALID_VALUE` / `MISSING_ARG` / `INCONSISTENT` | 调用方可自行修正 |
| 状态 | `NOT_FOUND` / `ALREADY_EXISTS` / `EMPTY` / `NOT_INITIALIZED` / `DEPENDENCY_MISSING` | 前置条件问题 |
| 格式 | `FORMAT_MISMATCH` / `PARSE_ERROR` / `SCHEMA_MISMATCH` | 数据格式问题 |
| IO | `FILE_NOT_FOUND` / `READ_ERROR` / `WRITE_ERROR` / `DB_ERROR` | 读写问题 |
| 内部 | `INTERNAL` / `UNEXPECTED` | 工具 bug，需上报 |
| 部分 | `PARTIAL` | 部分成功 |

### ok / empty / fail / blocked / partial — 构造结果

```python
ok(data=None, warnings=None, meta=None) -> ToolResult
empty(data=None, warnings=None, meta=None) -> ToolResult
fail(code, message, location="", hint="", detail="", context=None, warnings=None) -> ToolResult
blocked(code, message, location="", hint="", context=None) -> ToolResult
partial(data=None, warnings=None, message="部分成功", location="", hint="") -> ToolResult
fail_from_exception(exc, location="", hint="", code=UNEXPECTED, ...) -> ToolResult
```

状态语义遵循根目录 `ToolsPyRules.md`：`ok/partial/empty` 的 `ok=True`；
`blocked/failed` 的 `ok=False`。`partial` 的结果可用，边界写入 `warnings`；
`empty` 表示成功执行但结果为空。

### Timer — 计时器

```python
with Timer() as t:
    ...
    t.elapsed_ms()      # 块内可取当前耗时
print(t.ms)             # 退出后可用
```

---

## paths — 路径与配置

模块：`paperorganization.paths`

### resolve_kb — 解析知识库路径

```python
resolve_kb(kb: str | None = None) -> ToolResult   # data: KBConfig
```

| 参数 | 说明 |
|---|---|
| `kb` | `None`→默认 `ToolsPy/kb`；绝对路径→直接用；相对路径→**相对当前工作目录** |

```python
from paperorganization.paths import resolve_kb
cfg = resolve_kb("PaperOrganization/ToolsPy/kb").data   # 相对 CWD
cfg2 = resolve_kb().data                                 # 默认位置
```

### KBConfig — 知识库配置

| 属性 | 说明 |
|---|---|
| `kb_dir` | 知识库根目录 |
| `db_path` | `kb_dir/index.sqlite` |
| `raw_dir` | `kb_dir/raw/papers` |
| `pdf_dir` | `kb_dir/raw/pdfs` |
| `exports_dir` | `kb_dir/exports` |
| `ensure_dirs()` | 创建所需目录（幂等） |

### assert_ready — 前置条件检查

```python
assert_ready(cfg, need_tables=True) -> ToolResult
# data={"tables":[...], "counts":{...}, "config":{...}}
```

未初始化时 `ok=False`，`code=E_STATE_NOT_INITIALIZED`，`hint` 给出修复命令。

### find_project_root — 定位项目根

```python
find_project_root(start=None) -> str | None   # 向上找含 PaperOrganization/ 的目录
```

---

## schema — 建表与连接

模块：`paperorganization.schema`

### ensure_schema — 建表

```python
ensure_schema(cfg) -> ToolResult    # data={"tables":[...]}
```
幂等。8 张表：`papers` / `paper_relations` / `citations` / `claims` / `claim_relations` / `assumptions` / `media` / `span_map`。

### connect — 打开连接

```python
connect(cfg) -> sqlite3.Connection    # row_factory=Row，调用方负责 close
```

### clear_tables — 清空表

```python
clear_tables(cfg, tables=None) -> ToolResult   # 默认清空全部（幂等重建用）
```

### safe_id — paper_id 转安全片段

```python
safe_id("doi:10.1/a") -> "doi_10.1_a"    # ':' '/' 换成 '_'
```

---

## ids — ID 生成规则

模块：`paperorganization.ids`

| 函数 | 格式 | 示例 |
|---|---|---|
| `claim_id(paper_id, n)` | `c_<safe_id>_<n>` | `c_arxiv_2609.11923_1` |
| `assumption_id(paper_id, n)` | `a_<safe_id>_<n>` | `a_arxiv_2609.11923_1` |
| `relation_id(src, dst, kind, polarity)` | `r_<sha1前16位>` | `r_3f2a...`（内容哈希，幂等） |
| `media_id(paper_id, kind, n)` | `fig\|tab\|form_<safe_id>_<n>` | `fig_arxiv_2609.11923_0` |
| `span_id(paper_id, n)` | `span_<safe_id>_<n>` | `span_arxiv_2609.11923_1` |

**为什么集中在 ids.py**：ID 是跨模块契约，散落会漂移。

---

## importers — 数据转换元件

模块：`paperorganization.importers` ｜ **纯函数，不做 IO**

### build_spans — 段落 → span 记录

```python
build_spans(paper: dict) -> List[dict]
# [{"span_id","section","char_start","char_end","text"}, ...]
```

数据来源：`paper["document_structure"]["sections"][]["paragraph_list"]`。

**`char_start`/`char_end` 语义**（必须说清，否则下游误用）：
> 以「本文所有段落按顺序拼接后的字符串」为基准的字符偏移（0-based，半开区间）。
> **不是** PDF 字节偏移，**也不是** TEI 偏移。同章节内相对顺序准确。

### build_media — 图表公式 → media 记录

```python
build_media(paper: dict) -> List[dict]
# [{"media_id","kind","label","caption","section_index","coords",
#   "bboxes_per_page","coords_raw_boxes","coords_n_raw_boxes",
#   "coords_source","coords_missing_reason","raw_tei"}, ...]
```

数据来源：`paper["document_structure"]` 的 `figure_objects` / `table_objects` / `formula_objects`。

### paper_to_raw — Paper 记录 → raw JSON

```python
paper_to_raw(paper: dict) -> ToolResult   # data:的四段结构
```

返回 A 段元数据（**`source_ids` 在 `metadata` 内部**）/ B 段 spans / B2 段 media / C 段 understanding（空，待 PaperRead 填）。

错误：缺 `paper_id` → `E_INPUT_MISSING_FIELD`。

---

## writers — 写库元件

模块：`paperorganization.writers` ｜ 只负责写，不做转换

| 函数 | 说明 |
|---|---|
| `write_paper(conn, paper)` | 写 `papers` 表一行 |
| `write_spans(conn, spans)` | 批量写 `span_map`（记录需含 `paper_id`） |
| `write_media(conn, media)` | 批量写 `media`（含坐标 `page_first`/`page_last`/`bbox`） |
| `write_citations(conn, edges)` | 批量写 `citations`（见下） |
| `write_raw_json(cfg, paper_id, raw)` | 写 `raw/papers/<safe_id>.json` |
| `load_raw_json(cfg, paper_id)` | 读 raw JSON；不存在 → `E_STATE_NOT_FOUND` |

### write_citations — 引用边的三个字段

```python
write_citations(conn, edges: List[dict]) -> ToolResult
# edges: [{"from","to","to_raw","internal","source"}, ...]
```

★ 字段分工（对齐 PaperSearch ProcessPy INDEX）：

| 字段 | 含义 | 用途 |
|---|---|---|
| `to_raw` | 源里的原始 ID（总是在） | 「引了哪些外部文献」的完整性 |
| `to_paper` | 解析后的目标：库内=`to`，库外=`to_raw` | 图遍历 |
| `internal` | 目标是否在语料库内 | 一眼看出连通度 |

---

## validators — 校验元件

模块：`paperorganization.validators` ｜ **纯函数**

### validate_reading_result — 校验阅读结果

```python
validate_reading_result(result: dict) -> ToolResult
```

校验规则：
1. `paper_id` / `reading_depth` / `generated_by` 必填
2. 枚举合法（`evidence_type` / `polarity` / `kind` / `reading_depth`）
3. claim / key_result / assumption / claim_relation 必须**有 `evidence` 字段**
4. `reading_depth` 与产出**一致**（`second_pass` 不许带 `assumptions`/`unacknowledged`）

失败时 `code=E_INPUT_INVALID_VALUE`，`context["errors"]` 是逐项错误列表：
```json
[{"code":"E_INPUT_MISSING_FIELD","location":"result.core_claims[0].evidence",
  "message":"缺少 evidence 字段","hint":"每条 claim 必须带 evidence..."}]
```

### check_evidence_resolvable — 证据是否存在

```python
check_evidence_resolvable(conn, evidence) -> ToolResult
# data={"ids":[...],"resolved":[...],"dangling":[...]}
```

### parse_evidence — 归一化证据字段

```python
parse_evidence(None) -> []
parse_evidence("span_1") -> ["span_1"]
parse_evidence('["a","b"]') -> ["a","b"]
```

### 枚举常量

`EVIDENCE_TYPES` = theoretical/empirical/ablation/cited
`POLARITIES` = positive/negative/neutral
`REL_KINDS` = support/contradict/extend/use
`READING_DEPTHS` = unread/first_pass/second_pass/third_pass

---

## store — 阅读结果落库元件

模块：`paperorganization.store`

### store_reading_result — 落一篇阅读结果

```python
store_reading_result(result: dict, cfg, strict_evidence=True) -> ToolResult
# data={"paper_id","reading_depth","claims","assumptions","relations","removed"}
```

流程：① 结构校验（不过即拒收）→ ② **校验论文已存在**（否则 `E_STATE_NOT_FOUND`）→
③ **校验 evidence 可解析** + **关系边端点可解析**（`strict_evidence=True` 时拒收）→
④ 写 SQLite（事务，全有或全无；**替换语义**：先删该论文旧 claims/assumptions 及孤儿边，再写）
→ ⑤ 写 raw JSON 的 `understanding` 段。

**四条硬性保证**（都是实测问题的修复）：

| 保证 | 说明 |
|---|---|
| 论文必须已存在 | `paper_id` 不在 `papers` 表 → 拒收，不产生孤儿数据 |
| evidence 必须可解析 | 指向不存在的 span/media → 拒收（`strict_evidence=False` 可放宽为 warning） |
| 替换语义 | 本次结果覆盖该论文全部 claims/assumptions，**多余旧记录会被删除**，不残留幽灵数据 |
| **关系边一并落库** | `result["claim_relations"]` 会写入 `claim_relations` 表（曾经只校验不写入，导致边被静默丢弃） |

**跨论文引用（方案A）**：边端点支持三种写法，落库时解析成真实 `claim_id`：

| 写法 | 含义 |
|---|---|
| `"#2"` | 本论文第 2 个主张（序号对应 `core_claims` 顺序，1-based） |
| `"arxiv:1901.07621#2"` | 指定论文的第 2 个主张（**对端须已落库**） |
| `"c_arxiv_1901.07621_2"` | 已是 claim_id，原样使用（仍校验存在性） |

```python
from paperorganization import store

# 读第一篇：记录它
store.store_reading_result({
    "paper_id": "arxiv:2609.11923", "reading_depth": "third_pass",
    "generated_by": "paperread-v1",
    "core_claims": [{"text": "编译消除动态开销", "evidence": [], "evidence_type": "empirical"}],
    "limitations": {"acknowledged": [], "unacknowledged": []},
}, cfg)

# 读第二篇：记录它，并声明「它引用了第一篇的第 1 个主张」
r = store.store_reading_result({
    "paper_id": "arxiv:2408.14778", "reading_depth": "third_pass",
    "generated_by": "paperread-v1",
    "core_claims": [{"text": "GPU 并行加速", "evidence": [], "evidence_type": "empirical"}],
    "limitations": {"acknowledged": [], "unacknowledged": []},
    "claim_relations": [{
        "src_claim": "#1",                       # 本论文第 1 个主张
        "dst_claim": "arxiv:2609.11923#1",       # 对端论文第 1 个主张
        "kind": "extend", "polarity": "positive",
        "evidence": None, "confidence": 0.8,
    }],
}, cfg)
print(r.data["relations"])   # 1 —— 边已落库
print(r.data["removed"])     # 被替换掉的旧记录数
```

### store_claim_relations — 落论点间关系

```python
store_claim_relations(paper_id, relations: list, cfg, generated_by="") -> ToolResult
# relations: [{"src_claim","dst_claim","kind","polarity","evidence","confidence"}]
# data={"written": n}
```

端点同样支持方案A 的三种写法（`#2` / `<paper_id>#2` / `claim_id`），
其中 `#2` 的归属论文由 `paper_id` 参数决定。`rel_id` 为内容哈希，重跑不重复。

⚠ **优先用 `store_reading_result`**：在 `result["claim_relations"]` 里声明边即可，
不需要分两步调用。本函数用于**补充**（读多篇之后追加边）的场景。

```python
# 单独的端点无法解析时拒收（错误码 E_STATE_NOT_FOUND，hint 说明正确写法）
store.store_claim_relations("arxiv:2408.14778", [{
    "src_claim": "#1", "dst_claim": "arxiv:2609.11923#1",
    "kind": "use", "polarity": "neutral",
    "evidence": None, "confidence": 0.5,
}], cfg)
```

---

## query — 查询元件

模块：`paperorganization.query` ｜ Agent 消费知识库的主要入口

| 函数 | 返回 data | 说明 |
|---|---|---|
| `list_papers(cfg, limit=None)` | 论文列表 | paper_id/title/year/reading_depth |
| `get_paper(cfg, paper_id)` | 论文详情 + understanding | 含 raw JSON 的 understanding |
| `get_claims(cfg, paper_id)` | 论点列表 | |
| `get_assumptions(cfg, paper_id)` | 假设列表 | explicit 区分明说/隐含 |
| `get_claim_relations(cfg, paper_id=None)` | 关系边 | 给 paper_id 则取相关边 |
| `get_media(cfg, paper_id, kind=None)` | 图表公式摘要 | kind ∈ figure/table/formula |
| `get_media_detail(cfg, media_id)` | 单个 media 完整信息 | **含 `locate_hint` 可信度提示** |
| `find_paper_by_media(cfg, media_id)` | 反查论文 | 定位链用 |
| `get_spans(cfg, paper_id, limit=None)` | 正文段落 | 注意上下文预算 |
| `get_span(cfg, span_id)` | 单个段落原文 | 决定细读时用 |
| `stats(cfg)` | 统计 + 质量指标 | 库健康度 |

### stats 的质量指标

```python
r = query.stats(cfg)
r.data["counts"]    # 各表行数
r.data["quality"]   # papers_read / media_with_coords / internal_citations / claims_with_evidence
```

**空结果不是错误**：返回 `ok=True` + warning `"xxx：查询到 0 条"`，区分「没有」和「出错」。

---

## exporters — 导出元件

模块：`paperorganization.exporters`

### export_tsv — 导出 TSV

```python
export_tsv(cfg, specs=None) -> ToolResult
# data={"files": {"papers.tsv": 15, ...}, "dir": ...}
```

默认导出：`papers` / `media` / `citations` / `claims` / `assumptions` / `claim_relations` / `spans`，**行排序**（git diff 友好）。制表符/换行会被替换，不破坏结构。

---

## locate — 全文定位元件

模块：`paperorganization.locate` ｜ 需要 pypdf

### get_locate_info — 取定位信息

```python
get_locate_info(cfg, media_id) -> ToolResult
# data={"media_id","paper_id","pdf_path","page","bbox","bbox_pdf","kind","caption",...}
```

Agent 拿到 `page` + `bbox_pdf` 即可打开 PDF 对应页并高亮区域。

无坐标 → `E_STATE_EMPTY`（hint 给出原因）；PDF 不在 → `E_IO_FILE_NOT_FOUND`。

### verify_locate — 验证单个 media 定位

```python
verify_locate(cfg, media_id) -> ToolResult
# data={"media_id","kind","method","passed","evidence"}
```

★ **按类型区分验证方法**（否则会得出错误结论）：

| kind | 方法 | 原因 |
|---|---|---|
| figure / table | bbox 区域反提文字 ↔ caption 比对（归一化空白） | caption 是普通文本 |
| formula | 几何合理性 | PDF 用符号字体，提取字符与 Unicode 数学符号编码不同，**文字比对不适用** |

### verify_all — 批量验证

```python
verify_all(cfg) -> ToolResult
# data={"by_kind":{...}, "pass_rate":{...}, "details":[...]}
```

### 坐标工具

- `parse_bbox("x,y,x2,y2")` → `[float]`
- `bbox_to_pdf_rect(bbox, page_height)` → PDF 坐标系矩形（左上原点 → 左下原点）
- `geom_ok(bbox)` → 几何合理性
- `caption_keyword(caption)` → 归一化关键词

---

## conformance — 符合性检查元件

模块：`paperorganization.conformance`

### check_conformance — 对照  检查

```python
check_conformance(cfg) -> ToolResult
# data={"summary":{"pass","fail","warn"}, "checks":[...], "failures":[...], "warnings":[...]}
```

五个维度：

| 维度 | 检查内容 |
|---|---|
| A 目录结构 | `kb/raw/papers`、`kb/exports`、`index.sqlite` |
| B raw JSON | 顶层 A/B/B2/C 字段 + `source_ids` 必须在 `metadata` 内 |
| C SQLite | 8 张表存在 + 列名符合|
| D 数据质量 | evidence 可解析、枚举合法、ID 规则、坐标覆盖、`reading_depth` 一致性 |
| E 导出层 | `exports/*.tsv` 存在 + 行排序 |

有 FAIL 时 `ok=False`（`code=E_FORMAT_SCHEMA_MISMATCH`），`data.failures` 列出问题。

---

## pipelines — 流水线（自动化工具流）

模块：`paperorganization.pipelines`

### init_kb — 初始化知识库

```python
init_kb(cfg) -> ToolResult   # 建目录 + 建表，幂等
```

### import_search_output — 导入 PaperSearch 产物

```python
import_search_output(search_out, cfg, copy_pdf=False, clear_first=True) -> ToolResult
# data={"papers","spans","media","citations","internal_citations","pdfs","exports"}
```

流程：读 `corpus.jsonl` → 每篇转 raw JSON + 写 papers（含 `abstract`）/span_map/media →
读 `graph.jsonl` 写 citations（保留 `to`/`to_raw`/`internal`）→ 可选复制 PDF → 导出 TSV。

⚠ **`clear_first=True`（默认）会清空全部表，包括已有的阅读结果**（claims/assumptions/claim_relations）。
若库内已有阅读结果，返回的 `warnings` 里会明确告警；需要保留时传 `clear_first=False`。

```python
from paperorganization import pipelines
r = pipelines.import_search_output(
    "PaperSearch/ProcessPy/out", cfg, copy_pdf=True)
print(r.data)        # {'papers': 15, 'spans': 282, 'media': 91, ...}
print(r.warnings)    # 如清空了阅读结果，这里会说明
```

### store_reading_results — 批量落库

```python
store_reading_results(results: list, cfg) -> ToolResult
# data={"ok_count","fail_count","ok":[...],"fail":[{"paper_id","error"}]}
```

逐篇落库，**失败不阻断其他**。有失败时返回 `partial`（`ok=True`，`data` 保留成功/失败边界，详情写入 `warnings`）。

### store_reading_results_file — 从文件批量落库

```python
store_reading_results_file(path, cfg) -> ToolResult
# 文件内容为数组，或 {"results": [...]}
```

### rebuild_exports — 重新导出

```python
rebuild_exports(cfg) -> ToolResult
```

### run_import_pipeline — 一键导入

```python
run_import_pipeline(search_out, kb=None, copy_pdf=True) -> ToolResult
# data={"config":{...}, "import":{...}}
```

```python
from paperorganization.pipelines import run_import_pipeline
run_import_pipeline("PaperSearch/ProcessPy/out",
                    kb="PaperOrganization/ToolsPy/kb")
```

---

## Scheduler-facing snapshot API

模块：`paperorganization.query`

### get_reading_state — 阅读状态

返回论文阅读深度、是否完成和知识是否可用；任务运行状态仍由 Scheduler 管理。

### list_reading_candidates — 阅读候选

按语料范围和已读状态列出稳定排序的候选论文。

### get_paper_overview — L0/L1 概览

返回标题、摘要、时间、gist、贡献和方法族，不暴露 SQLite 私有访问。

### get_paper_understanding — L1/L2 理解

按 `aspects` 读取完整 raw understanding 中的指定方面。

### get_graph_snapshot — 一致图快照

一次读取论文、引用、论文关系、论点和论点关系，并根据规范化内容生成稳定
`snapshot_id`。论文节点同时带 `pdf_available` 和可用时的 `pdf_path`。

### get_related_knowledge — 相关已读知识

返回与目标相邻且已经实际读过的论文概览与 claims。该函数只检索候选，最终选择由
Scheduler 的 ContextSelector 完成。

---

## Semantic index and visualization

模块：`paperorganization.semantic` / `paperorganization.visualization`

### validate_relation_proposal — 校验关系提议

机械校验端点、关系类型、状态、置信度与验证状态，不把格式通过解释为科学正确。

### store_relation_proposals — 保存关系提议

事务保存候选关系及修订号，不修改原始 `citations`。

### list_relation_proposals — 查询候选/精选关系

可按 `candidate/accepted/rejected/superseded/needs_review` 状态筛选。

### create_graph_version — 图版本

记录 source snapshot、触发事件、父版本与说明，供重放和调度绑定。

### export_graph_visualization — 导出 3D 时间图

生成 `paper_index_graph.json` 和自包含 Canvas 3D HTML。只读知识库，不反写关系；
视觉语义遵循 `PaperOrganization/viz_rules.md`。
