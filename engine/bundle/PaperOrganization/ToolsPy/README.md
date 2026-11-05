# ToolsPy —— PaperOrganization 工具代码库

> 把「论文信息的组织与存储」封装成**可被智能体/工作流直接调用**的工具库。

**版本**：1.0.0 ｜ **Python**：3.8+（仅标准库；`locate` 读 PDF 时需要 pypdf）

---

## 1. 快速开始

```bash
cd PaperOrganization/ToolsPy

# 看有哪些工具
python cli.py elements

# 导入 PaperSearch 产物 → 知识库
python cli.py import --search-out ../../PaperSearch/ProcessPy/out --kb ./kb

# 查看统计 / 符合性检查
python cli.py stats --kb ./kb
python cli.py check --kb ./kb
```

作为库使用：

```python
import sys; sys.path.insert(0, "PaperOrganization/ToolsPy")
from paperorganization.pipelines import run_import_pipeline, store_reading_results_file
from paperorganization.paths import resolve_kb
from paperorganization import query

cfg = resolve_kb("PaperOrganization/ToolsPy/kb").data
run_import_pipeline("PaperSearch/ProcessPy/out", kb="PaperOrganization/ToolsPy/kb")
print(query.stats(cfg).data)
```

---

## 2. 工具索引

> 下表由 `python docs/apidoc.py index` 自动生成，链接直达 `API.md` 对应行。

<!-- API-INDEX -->
| 工具 | API 文档位置 |
|---|---|
| `ToolResult` | [`API.md#L27-L54`](API.md#L27-L54) |
| `ToolError` | [`API.md#L55-L65`](API.md#L55-L65) |
| `ErrorCode` | [`API.md#L66-L76`](API.md#L66-L76) |
| `ok / empty / fail / blocked / partial` | [`API.md#L77-L91`](API.md#L77-L91) |
| `Timer` | [`API.md#L92-L102`](API.md#L92-L102) |
| `resolve_kb` | [`API.md#L107-L122`](API.md#L107-L122) |
| `KBConfig` | [`API.md#L123-L133`](API.md#L123-L133) |
| `assert_ready` | [`API.md#L134-L142`](API.md#L134-L142) |
| `find_project_root` | [`API.md#L143-L150`](API.md#L143-L150) |
| `ensure_schema` | [`API.md#L155-L161`](API.md#L155-L161) |
| `connect` | [`API.md#L162-L167`](API.md#L162-L167) |
| `clear_tables` | [`API.md#L168-L173`](API.md#L168-L173) |
| `safe_id` | [`API.md#L174-L181`](API.md#L174-L181) |
| `build_spans` | [`API.md#L202-L214`](API.md#L202-L214) |
| `build_media` | [`API.md#L215-L225`](API.md#L215-L225) |
| `paper_to_raw` | [`API.md#L226-L237`](API.md#L226-L237) |
| `write_citations` | [`API.md#L251-L267`](API.md#L251-L267) |
| `validate_reading_result` | [`API.md#L272-L289`](API.md#L272-L289) |
| `check_evidence_resolvable` | [`API.md#L290-L296`](API.md#L290-L296) |
| `parse_evidence` | [`API.md#L297-L304`](API.md#L297-L304) |
| `枚举常量` | [`API.md#L305-L313`](API.md#L305-L313) |
| `store_reading_result` | [`API.md#L318-L374`](API.md#L318-L374) |
| `store_claim_relations` | [`API.md#L375-L399`](API.md#L375-L399) |
| `stats 的质量指标` | [`API.md#L418-L429`](API.md#L418-L429) |
| `export_tsv` | [`API.md#L434-L444`](API.md#L434-L444) |
| `get_locate_info` | [`API.md#L449-L459`](API.md#L449-L459) |
| `verify_locate` | [`API.md#L460-L473`](API.md#L460-L473) |
| `verify_all` | [`API.md#L474-L480`](API.md#L474-L480) |
| `坐标工具` | [`API.md#L481-L489`](API.md#L481-L489) |
| `check_conformance` | [`API.md#L494-L514`](API.md#L494-L514) |
| `init_kb` | [`API.md#L519-L524`](API.md#L519-L524) |
| `import_search_output` | [`API.md#L525-L545`](API.md#L525-L545) |
| `store_reading_results` | [`API.md#L546-L554`](API.md#L546-L554) |
| `store_reading_results_file` | [`API.md#L555-L561`](API.md#L555-L561) |
| `rebuild_exports` | [`API.md#L562-L567`](API.md#L562-L567) |
| `run_import_pipeline` | [`API.md#L568-L582`](API.md#L568-L582) |
| `get_reading_state` | [`API.md#L587-L590`](API.md#L587-L590) |
| `list_reading_candidates` | [`API.md#L591-L594`](API.md#L591-L594) |
| `get_paper_overview` | [`API.md#L595-L598`](API.md#L595-L598) |
| `get_paper_understanding` | [`API.md#L599-L602`](API.md#L599-L602) |
| `get_graph_snapshot` | [`API.md#L603-L607`](API.md#L603-L607) |
| `get_related_knowledge` | [`API.md#L608-L614`](API.md#L608-L614) |
| `validate_relation_proposal` | [`API.md#L619-L622`](API.md#L619-L622) |
| `store_relation_proposals` | [`API.md#L623-L626`](API.md#L623-L626) |
| `list_relation_proposals` | [`API.md#L627-L630`](API.md#L627-L630) |
| `create_graph_version` | [`API.md#L631-L634`](API.md#L631-L634) |
| `export_graph_visualization` | [`API.md#L635-L638`](API.md#L635-L638) |
<!-- /API-INDEX -->

**完整索引**（用 `python docs/apidoc.py list` 查看全部条目与行号）：

| 工具 | API 文档位置 |
|---|---|
| `命令行（cli.py）` | [`API.md`](API.md) |
| `元件层总览` | [`API.md`](API.md) |
| `流水线层总览` | [`API.md`](API.md) |

---

## 3. 分层设计

### 元件层（element）—— 可精细调整

| 模块 | 职责 |
|---|---|
| [`paperorganization/errors.py`](paperorganization/errors.py) | 统一结果/错误机制（`ToolResult` / `ToolError` / `ErrorCode`） |
| [`paperorganization/paths.py`](paperorganization/paths.py) | 路径与配置（`resolve_kb` / `assert_ready` / `KBConfig`） |
| [`paperorganization/schema.py`](paperorganization/schema.py) | 建表与连接（`ensure_schema` / `connect` / `safe_id`） |
| [`paperorganization/ids.py`](paperorganization/ids.py) | ID 规则（`claim_id` / `media_id` / `span_id` …） |
| [`paperorganization/importers.py`](paperorganization/importers.py) | 数据转换**纯函数**（`build_spans` / `build_media` / `paper_to_raw`） |
| [`paperorganization/writers.py`](paperorganization/writers.py) | 写库（`write_paper` / `write_spans` / `write_media` / `write_citations`） |
| [`paperorganization/validators.py`](paperorganization/validators.py) | 校验**纯函数**（`validate_reading_result` …） |
| [`paperorganization/store.py`](paperorganization/store.py) | 阅读结果落库（`store_reading_result` / `store_claim_relations`） |
| [`paperorganization/query.py`](paperorganization/query.py) | 查询（`get_paper` / `get_claims` / `get_media_detail` / `stats` …） |
| [`paperorganization/exporters.py`](paperorganization/exporters.py) | 导出 TSV（`export_tsv`） |
| [`paperorganization/locate.py`](paperorganization/locate.py) | 全文定位（`get_locate_info` / `verify_locate`） |
| [`paperorganization/conformance.py`](paperorganization/conformance.py) | 符合性检查（`check_conformance`） |
| [`paperorganization/semantic.py`](paperorganization/semantic.py) | 语义关系候选、修订与图版本 |
| [`paperorganization/visualization.py`](paperorganization/visualization.py) | 按 `viz_rules.md` 导出 3D 时间图 |

### 流水线层（pipelines）—— 自动化工具流

| 函数 | 作用 |
|---|---|
| `init_kb` | 初始化知识库（建目录 + 建表） |
| `import_search_output` | PaperSearch 产物 → 知识库（完整导入） |
| `store_reading_results` | 批量落库阅读结果（列表） |
| `store_reading_results_file` | 从 JSON 文件批量落库 |
| `rebuild_exports` | 重新导出 TSV |
| `run_import_pipeline` | 一键：初始化 + 导入 + 导出 |

---

## 4. 数据安全保证（重要）

落库与自检有**三条硬性防护**，均为实测问题的修复（详见 `API.md` 的 store 段）：

| 防护 | 行为 |
|---|---|
| **论文必须已存在** | 落库不存在的 `paper_id` → 拒收 `E_STATE_NOT_FOUND`，不产生孤儿数据 |
| **evidence 必须可解析** | 指向不存在的 span/media → 拒收，不让幻觉证据入库 |
| **替换语义** | 本次结果覆盖该论文全部 claims/assumptions，多余旧记录连同孤儿边一并删除 |

另外：

- **`selfcheck.py` 用独立临时目录**，绝不触碰工作用的 `kb/`（曾经会 `rmtree` 工作库，跑一次自检就清空真实成果）。
- **`import_search_output` 清空前告警**：若已有阅读结果，`warnings` 会明确说明被清空的数量。

---

## 5. 统一返回与报错（重要）

**所有工具函数返回 `ToolResult`，不抛裸异常。** `to_dict()` 提供完整的
`ok/status/data/error/warnings/diagnostics/meta` 信封。

```python
from paperorganization import query
r = query.get_paper(cfg, "arxiv:2609.11923")

if r:                       # 或 r.ok
    print(r.data)
else:
    print(r.error.code)     # 机器可读错误码，如 E_INPUT_MISSING_FIELD
    print(r.error.message)  # 人类可读说明
    print(r.error.location) # 出错位置，如 validators.validate_reading_result
    print(r.error.hint)     # 修复建议（Agent 据此自我调整）
    print(r.error.detail)   # 内部 bug 时的堆栈
    print(r.error.context)  # 相关数据快照
```

**错误码前缀含义**：

| 前缀 | 含义 | Agent 该怎么做 |
|---|---|---|
| `E_INPUT_*` | 输入问题 | 可自行修正后重试 |
| `E_STATE_*` | 前置条件未满足 | 检查/初始化环境 |
| `E_FORMAT_*` | 数据格式不符 | 检查数据源 |
| `E_IO_*` | 读写失败 | 检查路径/权限 |
| `E_INTERNAL_*` | 工具自身 bug | 上报（`detail` 里有堆栈） |
| `E_PARTIAL` | 部分成功 | 看 `data` 区分成功/失败边界 |

**三条设计原则**：
1. **不静默**：跳过/降级写进 `warnings`，不假装成功。
2. **全有或全无**：落库校验不过就拒收，事务回滚，不落半截数据。
3. **区分「没有」和「出错」**：空结果 `ok=True` + warning；错误 `ok=False` + code。

---

## 6. 命令行

```bash
python cli.py init    --kb <kb>                                   # 初始化
python cli.py import  --search-out <out> --kb <kb> [--no-pdf]     # 导入
python cli.py read    --file <results.json> --kb <kb>             # 落库阅读结果
python cli.py export  --kb <kb>                                   # 重新导出
python cli.py stats   --kb <kb>                                   # 统计
python cli.py check   --kb <kb>                                   # 符合性检查
python cli.py locate  --kb <kb> [--media-id <id>]                 # 坐标定位验证
python cli.py papers  --kb <kb> [--limit N]                       # 列出论文
python cli.py elements                                            # 列出所有工具
```

退出码：`0`=成功，`1`=失败（结构化错误打印到 stderr）。

---

## 7. 文档

| 文件 | 内容 |
|---|---|
| [`API.md`](API.md) | 每个工具的详细说明（签名/参数/返回/示例/错误） |
| [`docs/apidoc.py`](docs/apidoc.py) | 文档定位工具：`list` 列条目行号、`find <name>` 跳转、`index` 更新本文件索引 |
| [`API.md`](API.md) | 表结构、字段与 ID 规则 |

```bash
python docs/apidoc.py list                # 列出 API.md 所有条目及行号
python docs/apidoc.py find store_reading_result   # 打印该函数的文档
python docs/apidoc.py index               # 刷新本文件的索引区
```

---

## 8. 目录结构

```
ToolsPy/
├── README.md                    本文件（索引）
├── API.md                       详细 API 文档
├── cli.py                       命令行入口
├── selfcheck.py                  自检
├── kb/                          知识库产物（raw/ + index.sqlite + exports/）
├── paperorganization/           工具代码（包，唯一导入名）
│   ├── __init__.py              统一入口
│   ├── errors.py                统一结果/错误
│   ├── paths.py                 路径配置
│   ├── schema.py                建表
│   ├── ids.py                   ID 规则
│   ├── importers.py             数据转换（元件）
│   ├── writers.py               写库（元件）
│   ├── validators.py            校验（元件）
│   ├── store.py                 落库（元件）
│   ├── query.py                 查询（元件）
│   ├── exporters.py             导出（元件）
│   ├── locate.py                定位（元件）
│   ├── conformance.py           符合性检查（元件）
│   ├── semantic.py              可演化语义索引（元件）
│   ├── visualization.py         3D 时间图导出（元件）
│   └── pipelines.py              流水线（自动化工具流）
└── docs/
    └── apidoc.py                文档定位工具（可复用到其他工具库）
```

## 9. Scheduler 与 Agent Workflow 接口

`query.get_graph_snapshot()` 提供带稳定 `snapshot_id` 的一次性图快照；
`get_reading_state()`、`list_reading_candidates()`、`get_paper_overview()`、
`get_paper_understanding()` 和 `get_related_knowledge()` 是 Scheduler 的公开读接口。
Scheduler 不需要也不应直接读取 SQLite。

`semantic.py` 将原始引用事实与 Agent 语义提议分开存储。可视化同时展示论文、论点、
原始引用、论文脉络和候选语义关系，并明确各自方向、来源和验证状态。
正式布局遵循 `../viz_rules.md`：时间早上晚下、中心时间主轴、放大后的纵向位置滑条、
论点与所属论文联动高亮，以及按年份逐步展开的可停止动态演示。
