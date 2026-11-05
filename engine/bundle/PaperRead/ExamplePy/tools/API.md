# tools/API.md —— PaperRead 工具层 API 参考

> 给 Agent 按需查，**不必整份阅读**。所有工具只依赖标准库。
> 返回信封语义见 `README.md`。

```python
import sys
sys.path.insert(0, "PaperRead/ExamplePy/tools")
from paperread import (...)
```

---

## 目录

- [A. 策略装载](#a-策略装载)
- [B. 公共契约](#b-公共契约)
- [C. 消息组装](#c-消息组装)
- [D. 结果校验](#d-结果校验)
- [E. 格式适配](#e-格式适配)
- [F. 底层校验器](#f-底层校验器)
- [G. 错误码](#g-错误码)

---

## A. 策略装载

### `load_strategy(strategy_id=None, prompts_dir=None, allow_non_runnable=False)`

把 `reading_strategy_id` 解析成**恰好一段**送入模型的策略正文。

| 参数 | 类型 | 说明 |
|---|---|---|
| `strategy_id` | `str \| None` | `"S4"` / `"mechanism_reconstruction"` / `"s4_mechanism_reconstruction"`；`None`/空 → 回落 `S0` |
| `prompts_dir` | `str \| None` | 策略目录覆盖；默认 `<repo>/prompts` |
| `allow_non_runnable` | `bool` | 允许读取 `excluded`/`future` 策略正文。**仅用于检查文件内容，绝不可用于实验运行** |

**成功返回** `data`：

```python
{
  "strategy_id": "S4",
  "name": "mechanism_reconstruction",
  "status": "active",                 # active | excluded | future
  "path": ".../prompts/s4_mechanism_reconstruction.md",
  "body": "Use mechanism reconstruction. ...",   # ← 送入模型的内容（已 strip）
  "body_sha256": "<64 位十六进制>",     # ← 实验记录用的 strategy_prompt_sha256
  "frontmatter": {...},               # ← 仅供人阅读，**不得送入模型**
  "is_default": False,                # 是否因未指定而回落
}
```

**失败情况**：

| 错误码 | status | 触发条件 |
|---|---|---|
| `E_STRATEGY_UNKNOWN` | `failed` | ID 不存在；或把公共契约当策略加载 |
| `E_STRATEGY_DISABLED` | `blocked` | `status` 为 `excluded` / `future` |
| `E_STRATEGY_FRONTMATTER` | `failed` | 缺少 YAML frontmatter |
| `E_STRATEGY_EMPTY` | `failed` | frontmatter 之后无正文 |

> ⚠️ **未知策略不会静默回落到 baseline**。这是刻意的：静默降级会让实验报告与实际
> 运行不一致（报告写 S6、实际跑 S0），且极难事后发现。

### `resolve_strategy_path(strategy_id, prompts_dir=None)`

返回策略文件绝对路径，找不到返回 `None`。匹配顺序：`strategy_id` → `name` → 文件名（含/不含 `.md`）。

### `list_strategies(prompts_dir=None)`

列出所有**可选策略**（公共契约会被排除）。

`data` 为列表，每项含 `strategy_id` / `name` / `title` / `status` / `model_visible` / `file` / `body_sha256` / `runnable`。

---

## B. 公共契约

### `load_common_contract(name="common_read", prompts_dir=None)`

装载公共任务契约（`common_read` / `common_qa`）正文。

公共契约与策略分开装载，确保「**公共层固定、策略是可插拔变量**」这一边界在代码层成立。
`data`：`{name, path, body, body_sha256, frontmatter}`。

> 公共契约**不能**经 `load_strategy` 装载（返回 `E_STRATEGY_UNKNOWN`），
> 否则会被误当实验臂、污染策略清单。

---

## C. 消息组装

### `build_read_messages(strategy_id=None, prompts_dir=None)`

按 ``组装**纯消息数组**。

```python
env = build_read_messages("S4")
env["data"]["messages"]
# [ {"role": "system", "content": <common_read 正文>},
#   {"role": "user",   "content": <S4 策略正文>} ]
```

**本函数刻意不接收 PDF 路径**——这保证"只挂载当前策略、不读历史结果、不访问其他策略文件"
的隔离规则在装配阶段就成立。PDF / 逐页图像由调用方追加为后续 user 消息。

`data`：

| 字段 | 说明 |
|---|---|
| `messages` | `[{role, content}, ...]`，固定 2 条 |
| `reading_strategy_id` | 实际选中的策略（可能是回落的 `S0`） |
| `common_prompt_sha256` | 公共契约正文哈希（各组应相同） |
| `strategy_prompt_sha256` | 策略正文哈希（各组应不同） |

**多题分叉**：同一篇论文的多道问题，应从**同一精读结束检查点**分叉，
每个问题新开分支，**禁止**把前一题的模型回答追加给下一题。

---

## D. 结果校验

### `validate_reading_result(data, mode="experiment", strict_anchor=None, schema_dir=None)`

两层校验：**结构层**（JSON Schema）+ **契约层**（语义规则）。

| 参数 | 说明 |
|---|---|
| `data` | `dict` 或 JSON 字符串 |
| `mode` | `"experiment"`（首轮隔离实验，`exp-1.0`）或 `"production"`（生产，`2.0-draft`） |
| `strict_anchor` | `evidence` 是否必须是 `span_id`/`media_id` 形状。`None` → 按 mode 自动（production=True） |
| `schema_dir` | Schema 目录覆盖 |

**成功** `data`：`{errors: [], warnings: [...], schema: <path>}`。
有 warning 时 `status="partial"`（**结果仍可用**）。

**失败** `data`：`{errors: [...], warnings: [...], schema: ...}`，
`error.code="E_VALIDATION"`，`error.details` 为逐条错误：

```json
{"pointer": "/core_claims/0/evidence",
 "message": "evidence 锚点形状非法（应为 span_id 或 media_id）",
 "actual": ["page 4, theorem 2"]}
```

**所有返回**都带 `diagnostics.scientific_validity = "not_assessed"`。

#### production 模式的契约规则

| 规则 | 违反后果 |
|---|---|
| `core_claims`/`key_results`/`assumptions`/`claim_relations` 必须**显式含** `evidence` 字段 | error |
| `verification_status="supported"` 但无可用锚点 | error（：填了字段≠自动 supported） |
| `evidence` 锚点形状必须是 `span_id`/`media_id` | error |
| `explicit=false`（读者推断的隐含假设）标为 `paper_assertion` | error |
| `reading_depth=unread/first_pass/second_pass` 却产出 `assumptions` 或 `unacknowledged` 局限 | error（对齐 Organization `store.py`） |
| 枚举取值非法 | error |
| 空字段未在 `completeness.absent_fields` 说明原因 | warning |
| `method_evolution` 标为 `paper_assertion` | warning（跨篇演化通常含读者综合） |

#### experiment 模式的契约规则

| 规则 | 违反后果 |
|---|---|
| `paper_evidence_locator` 出现 `span_id`/`media_id` 形状 | error（本轮无 Organization，锚点是伪造的） |
| `derived_inference` 却给了原文定位 | warning |
| `uncertain` 却给了定位 | warning |
| `contribution`/`key_results`/`assumptions`/`limitations` 为空 | warning（提示区分"确实未发现"与"尚未核查"） |
| `method.mechanism` 为空 | warning |

### `validate_qa_answer(data, schema_dir=None)`

校验阅读后问答返回：`{answer, answerable_from_paper, support_locator}`。

---

## E. 格式适配

### `experiment_to_production(exp, generated_by=None, reading_task_id=None, context_snapshot=None, reading_depth=None, generated_at="")`

把隔离实验中间格式（`exp-1.0`）映射为生产格式（`2.0-draft`）。
**确定性映射**：同一输入必得同一输出。

| 映射 | 规则 |
|---|---|
| `paper_evidence_locator` | → `soft_locator`；`evidence` 置 `[None]` |
| `source_type` | `author_claim`→`paper_assertion`；`derived_inference`→`reader_inference`；`uncertain`→`reader_inference` + `verification_status=inconclusive` |
| `verification_status` | 一律 `not_checked`（**软定位不得升级为 supported**） |
| `assumptions`（字符串数组） | → `explicit=false`、`source_type=reader_inference`（**不得伪造作者明示**） |
| `method.mechanism`（字符串） | → `mechanism.notes`，其余结构子字段置空并记 `not_extracted` |
| `reading_depth` | 未给出时推断：有 `assumptions` → `third_pass`，否则 `second_pass` |
| `generated_at` | **不自动取当前时间**（保证可复现）；未提供则告警 |

**适配产物仍需再跑一次 `validate_reading_result(..., mode="production")`。**

---

## F. 底层校验器

### `schema_check.load_schema(path) -> (schema, error)`

读取 Schema 文件。第二个返回值为 `None` 表示成功。

### `schema_check.validate(instance, schema) -> (errors, warnings)`

JSON Schema 子集校验。

**支持的**关键字：`type`（含联合类型如 `["string","null"]`）、`required`、
`properties`、`additionalProperties`、`items`、`enum`、`const`、`minimum`、`maximum`、
`minLength`、`oneOf`、`$ref`（`#/$defs/*`、`#/definitions/*`）。

**不支持**的关键字（见 `schema_check.UNSUPPORTED_KEYWORDS`，如 `pattern`、`allOf`、
`anyOf`、`not`、`uniqueItems`、`minItems`）：出现时**记为 warning**，不会静默当成通过。

> 注意：`bool` 不被当作 `integer`（Python 中 `bool` 是 `int` 子类，已特殊处理）。

### `frontmatter.split_frontmatter(text) -> (fm_text, body)`

切分 frontmatter 与正文。无 frontmatter 时返回 `(None, text)`。

### `frontmatter.parse(text) -> dict`

极简 YAML 子集解析：标量、块列表、折叠块标量（`>`）、保留块（`|`）。

**刻意的行为**：无法解析的行**不静默丢弃**，收进 `__unparsed__` 列表——
错误的 frontmatter 应当被发现，而不是被吞掉。

`frontmatter.load_file(path) -> (fm_dict, body, raw_text)`。

### `contract._is_span_or_media(anchor) -> bool`

判断锚点形状是否为 `span_*` 或图表类（`fig*`/`tab*`/`formula*`/`eq*`）。

---

## G. 错误码

见 `paperread/errors.py` 的 `ERRORS`。每个错误含 `code` / `category` / `meaning` /
`message` / `action` / `details`——`action` 给出**可执行的修正建议**，便于 Agent 自定位。

| 错误码 | category | 含义 |
|---|---|---|
| `E_ARG` | argument | 参数缺失或类型错误 |
| `E_NOT_FOUND` | path | 文件/目录不存在 |
| `E_NOT_DIR` | path | 期望目录但拿到文件 |
| `E_STRATEGY_UNKNOWN` | strategy | 策略不存在（**不得静默降级**） |
| `E_STRATEGY_DISABLED` | strategy | S9/S10 等不可运行策略被选中 |
| `E_STRATEGY_FRONTMATTER` | strategy | 缺少合法 frontmatter |
| `E_STRATEGY_EMPTY` | strategy | 策略正文为空 |
| `E_SCHEMA_NOT_FOUND` | schema | Schema 文件不存在 |
| `E_SCHEMA_INVALID` | schema | Schema 文件本身不合法（**工具缺陷**，不是模型问题） |
| `E_VALIDATION` | validation | 结果不满足 Schema / 契约 |
| `E_JSON_PARSE` | validation | 模型输出不是合法 JSON |
| `E_CONTRACT` | contract | 违反数据契约 |
| `E_INTERNAL` | internal | 工具内部异常（**工具缺陷**） |
