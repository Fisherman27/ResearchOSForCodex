# tools/ —— PaperRead 工具层

> PaperRead Skill 的**任务装载与契约校验**适配层。
> 只放必要的适配器：策略加载、消息组装、Schema 与契约校验。

---

## 1. 这个库做什么、不做什么

**做**（确定性辅助工作，适合 Python）：

| 能力 | 位置 |
|---|---|
| 按 `reading_strategy_id` 加载**唯一**策略 | `paperread/strategy_loader.py` |
| 把策略 frontmatter 与送入模型的正文**分离** | `paperread/frontmatter.py` |
| 组装纯消息数组（公共契约 + 单一策略） | `paperread/strategy_loader.build_read_messages` |
| 无第三方依赖的 JSON Schema 校验 | `paperread/schema_check.py` |
|  语义契约校验（evidence、枚举、深度一致性…） | `paperread/contract.py` |
| 隔离实验中间格式 → 生产格式的确定性迁移 | `paperread/adapter.py` |
| 统一返回信封与错误码 | `paperread/result.py` / `paperread/errors.py` |

**不做**（避免与其它模块职责重叠）：

- ❌ 阅读策略的认知实现 → 那是 `prompts/` 的职责，由 Agent 执行
- ❌ Organization 的存储、图查询、证据定位 → 用 `PaperOrganization/ToolsPy/`
- ❌ Scheduler 的排序与上下文选择 → 用 `PaperScheduler`
- ❌ PDF 下载/渲染、模型推理、Benchmark 评分 → 那是实验执行器的职责

---

## 2. 快速开始

```bash
cd PaperRead/ExamplePy

python tools/selfcheck.py                     # 一键自检（期望 74 PASS / 0 FAIL）
python tools/cli.py strategies                # 列出策略与可运行性
python tools/cli.py show S4                   # 打印送进模型的策略正文
python tools/cli.py messages S4               # 打印组装好的纯消息数组
python tools/cli.py validate out.json --mode experiment
python tools/cli.py adapt out.json --out prod.json
```

作为库使用：

```python
import sys; sys.path.insert(0, "PaperRead/ExamplePy/tools")
from paperread import build_read_messages, validate_reading_result, experiment_to_production

env = build_read_messages("S4")
msgs = env["data"]["messages"]           # [{"role":"system",...},{"role":"user",...}]
# 调用方再把 PDF / 逐页图像追加成 user 消息内容

env = validate_reading_result(model_output_dict, mode="experiment")
if not env["ok"]:
    print(env["error"]["details"])        # 带 JSON Pointer 的逐条定位
```

---

## 3. 设计取舍（为什么这样做）

### 3.1 零第三方依赖

`Schema` 校验在通用场景会用 `jsonschema`，但实验服务器上多一个依赖就多一份环境风险，
且 `jsonschema` 的报错对本项目的 Agent 不够可操作。因此：

- 自带一个 JSON Schema **子集**校验器，支持本项目实际用到的关键字；
- 错误以 **JSON Pointer** 定位并附 `expected` / `actual`，便于 Agent 自行修正；
- 遇到**不支持**的关键字（如 `pattern`、`allOf`）**记为 warning 而非静默忽略**，
  并有测试守护（`test_schemas_use_no_unsupported_keywords`）。

### 3.2 frontmatter 绝不进入模型上下文

策略文件的 frontmatter 记录**机制假设、文献来源、停止条件、失败模式**——这些是
给研究者看的**实验设计信息**，若混入 Prompt 就等于把策略的"设计意图"泄漏给被测模型，
对照组也会受到暗示。因此 loader 只返回 `---` 之后的正文，并有测试逐策略守护。

### 3.3 未知策略必须报错，不得静默降级

`load_strategy("S_TYPO")` 返回 `E_STRATEGY_UNKNOWN`，**不会**回落到 baseline。
静默降级会破坏实验可复现性：报告里写 S6、实际跑的是 S0，这种错误极难事后发现。

### 3.4 S9/S10 用 `status` 表达状态，loader 强制拒绝

- S9 `excluded`：PDF 已在上下文时无法实现真闭卷，**不得用假闭卷冒充**；
- S10 `future`：依赖尚未实现的 Scheduler 上下文，**不得假装已实现**。

两者默认拒绝加载，只有 `allow_non_runnable=True`（用于检查文件内容）才能读取正文。

### 3.5 软定位与强证据**在结构上分离**

这是本工具层最重要的一条设计。首轮隔离实验没有 Organization，拿不到 `span_id`/`media_id`，
只能用"页码 + 短引文"这类**软定位**。但软定位**不等于**强语义支持。

如果在迁移时把软定位塞进 `evidence`，就会制造出"看起来证据齐全"的假象——
下游看到 `evidence` 非空，会误以为有原文锚定。因此：

| 字段 | 含义 | 隔离实验阶段 |
|---|---|---|
| `evidence` | Organization 级锚点（`span_id` / `media_id`） | `[null]` |
| `soft_locator` | 页码 / 引文 / 公式号 / 图表号 | 软定位字符串 |
| `verification_status` | 证据支持程度 | 一律 `not_checked` |

有测试守护这一点（`test_adapter_keeps_soft_locator_out_of_evidence` 等）。

### 3.6 机械校验不等于科学正确性

所有校验返回都带 `diagnostics.scientific_validity = "not_assessed"`。
JSON 合法、字段齐全、`span_id` 形状正确，**都不能证明"理解更深"**。
这个常量是刻意保留的提示，防止下游把格式通过当成结论可用。

---

## 4. 统一返回信封

按 `../../../ToolsPyRules.md`实现，PaperRead 侧选**普通 dict**形态（与 PaperSearch 一致）：

```python
{
  "ok": bool, "status": "ok|partial|empty|blocked|failed",
  "data": Any, "error": dict|None,
  "warnings": [...], "diagnostics": {...}, "meta": {...},
}
```

| status | ok | 含义 |
|---|---|---|
| `ok` | ✅ | 全部成功 |
| `partial` | ✅ | 拿到结果但有非致命问题（**结果可用**，看 `warnings`） |
| `empty` | ✅ | 成功执行但结果为空（**空集不是错误**） |
| `blocked` | ❌ | 被政策/前置条件挡住，**未执行**（如 S9/S10） |
| `failed` | ❌ | 执行失败（如未知策略、Schema 非法） |

`blocked` 与 `failed` 的区别很重要：前者是"按设计不该跑"，后者是"调用出错了"。

---

## 5. 自检

```bash
python tools/selfcheck.py           # 结构检查 + 96 项 unittest
python tools/selfcheck.py --quick   # 只做结构检查
```

自检**不接触网络、不接触 PDF、不调用模型**，因此可在任何环境一键跑通。
