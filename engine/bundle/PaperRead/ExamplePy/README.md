# PaperRead/ExamplePy —— 科研论文精读 Skill（可被 Agent 加载的能力包）

> 本目录是 **PaperRead 的交付产物**：一套「**任务协议固定、阅读策略可插拔**」的科研论文精读 Skill。

**定位**：这是**面向 Agent 的可复用能力包**，不是给最终用户手动操作的界面，也不是一套重造的 Python LLM Agent Runtime。

---

## 0. 一句话总纲

> **`SKILL.md` 规定 What 与 Contract；`prompts/` 研究 How；`schemas/` 规定 Result；`tools/` 提供 Capability；`evals/` 衡量 Effectiveness。**

论文理解由 **Agent/LLM** 完成；Python 只做确定性辅助（装载、校验、适配）。
**不在公共层写死三遍阅读、主动提问、机制重建、反事实分析**——它们是待独立研究与消融的**策略变量**。

---

## 1. 目录结构

```text
PaperRead/ExamplePy/
├── SKILL.md                              # ★ 稳定任务契约（What/Input/Strategy/Result/Tools/Constraints）
├── prompts/                              # ★ 阅读策略 = 实验的主要自变量
│   ├── common_read.md                    # 公共阅读任务契约（各组逐字一致，SYSTEM 消息）
│   ├── common_qa.md                      # 公共问答契约（阅读后追加，{{QUESTION}} 占位）
│   ├── s0_baseline.md                    # S0  直接阅读（对照组）
│   ├── s1_progressive_reading.md         # S1  分层渐进阅读
│   ├── s2_active_questioning.md          # S2  主动提问
│   ├── s3_self_explanation.md            # S3  关键机制自我解释
│   ├── s4_mechanism_reconstruction.md    # S4  方法/理论/实验机制重建
│   ├── s5_evidence_audit.md              # S5  主张—证据审查
│   ├── s6_intra_paper_contrast.md        # S6  论文内部结构性对比
│   ├── s7_counterfactual_testing.md      # S7  反事实机制压力测试
│   ├── s8_multi_perspective.md           # S8  单 Agent 多视角质询
│   ├── s9_closed_book_recall.md          # S9  excluded —— 不可运行
│   └── s10_scheduler_context.md          # S10 future   —— 不可运行
├── schemas/                              # ★ 结果契约
│   ├── reading_result.schema.json        #    生产契约（2.0-draft，与 Organization 对齐）
│   ├── reading_result_experiment.schema.json  # 首轮隔离实验最小中间格式（exp-1.0）
│   └── qa_answer.schema.json             #    阅读后问答返回格式
├── tools/                                # ★ 任务装载与契约校验（纯标准库）
│   ├── cli.py                            #    命令行入口（给人用）
│   ├── selfcheck.py                      #    一键离线自检（必须全绿）
│   ├── README.md / API.md                #    文档
│   └── paperread/                        #    Python 包（唯一导入名，防遮蔽）
├── tests/                                # ★ 契约测试（96 项）
│   ├── test_contract.py
│   └── fixtures/                         #    正例 fixture（实验格式 / 生产格式）
├── evals/                                # 评估适配与实验记录（见 evals/README.md）
└── README.md                             # 本文件
```

---

## 2. 快速开始

```bash
cd PaperRead/ExamplePy

# ① 一键自检（离线，不接触网络/PDF/模型）
python tools/selfcheck.py              # 期望：74 PASS / 0 FAIL / 0 WARN

# ② 看有哪些策略可用（S9/S10 会被标为 NOT-RUNNABLE）
python tools/cli.py strategies

# ③ 看某个策略真正送进模型的内容（frontmatter 不会出现在这里）
python tools/cli.py show S4

# ④ 看组装好的纯消息数组（公共契约 + 唯一策略，不含 PDF）
python tools/cli.py messages S4

# ⑤ 校验一份模型输出
python tools/cli.py validate out.json --mode experiment
python tools/cli.py validate out.json --mode production

# ⑥ 中间格式 → 生产格式（确定性映射，不编造缺失字段）
python tools/cli.py adapt out.json --out prod.json --generated-at 2026-10-13T00:00:00Z
```

运行环境：**Python 3.10+，仅标准库**。本目录的 `tools/` 故意不依赖 `jsonschema`，
避免在共享实验服务器上引入环境风险（自带 Schema 子集校验器，见 `tools/API.md`）。

### `prompts/` 的命名规则

策略文件名统一为 **`s<序号>_<策略英文名>.md`**，与 ``的清单一致：

```text
common_read.md / common_qa.md   → 公共契约（所有实验组逐字一致，先加载）
s0_baseline.md                  → S0 直接阅读（对照组）
s4_mechanism_reconstruction.md  → S4 方法机制重建
s8_multi_perspective.md         → S8 单 Agent 多视角质询
...
```

约定三点：

1. **序号 = `` 的 S 编号**（`s4_*` 里必须是 S4），有测试守护；
2. **序号之后是策略的完整英文名**，且与 frontmatter 的 `name` 逐字一致，避免两处漂移；
3. **序号只影响排序与人工核对，不参与匹配**。`strategy_loader` 会自动剥掉 `s4_` 前缀，
   因此下列写法都解析到同一文件：

```python
load_strategy("S4")                            # 最常用：用编号
load_strategy("s4")                            # 小写序号
load_strategy("mechanism_reconstruction")      # 用 frontmatter 的 name（可省序号）
load_strategy("s4_mechanism_reconstruction")   # 完整文件名
load_strategy(None)                            # 不指定 → 回落 S0
```

> `strategy_id`（frontmatter 里的 `S4`）始终是**最权威**的标识；文件名只是便利别名。
> S9/S10 排在 `s1_` 之后是字典序的正常结果，它们本轮不运行，不影响实验。

---

## 3. 设计要求的落地对应关系

| 设计要求 | 本目录的落点 |
|---|---|
| `SKILL.md` 只定义不随策略变化的部分 | `SKILL.md`（六项协议，**无任何具体阅读机制**） |
| `prompts/` 是主要可插拔变量 | `prompts/S0–S10`，每个文件带**机制假设 / 来源 / 停止条件 / 失败模式** frontmatter |
| `schemas/` 统一结果契约 | `schemas/*.json`，生产与实验两套，**不互相冒充** |
| `tools/` 优先复用、只放必要适配器 | 只做装载 + 校验 + 适配；**不**重复 Organization 的存储/查询/定位 |
| `evals/` 独立评估 | `evals/README.md` 说明评估契约与产物落点 |
| 策略不写入公共层 | 公共层由测试强制校验（`test_baseline_does_not_name_other_strategies`） |
| 未知策略不得静默降级 | `load_strategy` 返回 `E_STRATEGY_UNKNOWN`，有测试守护 |
| S9 不可假闭卷、S10 不可假装可用 | 两者 `status` 为 `excluded`/`future`，loader 默认**拒绝加载** |
| 证据不得伪造；软定位≠强支持 | 生产 Schema 有 `soft_locator` 字段与 `evidence` **严格分离**，有测试守护 |

---

## 4. 与其它模块的边界（不越界）

```text
PaperSearch  ──PDF / GROBID / 引用 / 媒体证据──▶  PaperOrganization
                                                        │  论文图 + 已读知识 + 阅读状态
                                                        ▼
                                                  PaperScheduler
                                          阅读顺序 + 前置知识组装 → ReadingTask
                                                        │
                                                        ▼
                                              ┌──────────────────────┐
                                              │   PaperRead Skill    │
                                              │ SKILL.md   固定契约   │
                                              │ prompts/   选定策略   │
                                              │ Agent      实际理解   │
                                              │ schemas/   统一输出   │
                                              └──────────────────────┘
                                                        │  ReadingResult
                                                        ▼
                                                  PaperOrganization（校验 / 版本化 / 落库）
```

| 模块 | 本目录**做** | 本目录**不做** |
|---|---|---|
| PaperRead | 任务契约、策略 Prompt、Schema、装载与校验适配 | — |
| PaperOrganization | 按它定义的权威 Schema 产出结果 | 不实现独立知识库、不操作它的私有存储目录 |
| PaperScheduler | 通过 `ReadingTask` 接收输入、记录 `reading_task_id`/`context_snapshot` | 不做全局排序、不自行检索上下文 |
| 实验执行器 | 提供可直接拼装的消息数组与哈希 | 不做 PDF 渲染、不做模型推理、不做数据下载 |

**新增字段写回需要两侧同步升级**：`schemas/reading_result.schema.json` 中的 `method.mechanism`、
`understanding_quality` 等属于  目标设计，**不意味着** Organization 现有 validator
已经支持。接入前按 `schema_version` 分流，**不得静默丢弃新字段**。

---

## 5. 两条运行路径

### 5.1 协同路径（目标工作流，需 Scheduler）

输入是 `ReadingTask`，含目标论文引用 + 前置知识条目 + 快照。
Reader 用前置知识辅助理解，产出生产格式 `ReadingResult`，交 Organization 落库。
证据锚点用 `span_id` / `media_id`（严格模式）。

### 5.2 隔离路径（首轮实验，单篇 PDF）

按 ``组装：

```text
SYSTEM: prompts/common_read.md 正文
USER:   prompts/<strategy_id>.md 正文  +  目标论文 PDF（逐页图像）
```

无 Scheduler、无历史结果、无工具、无检索。产出中间格式（`exp-1.0`），
证据用**软定位**（页码 + 引文/公式号/图表号），**不生成** `span_id` / `media_id`。
需要用生产格式时走 `adapt` 做确定性迁移，迁移后软定位进 `soft_locator`、
`evidence` 保持 `[null]`、`verification_status` 保持 `not_checked`。

---

## 6. 当前状态与验证

| 项 | 结果 |
|---|---|
| 离线自检 | **74 PASS / 0 FAIL / 0 WARN** |
| 契约测试 | **96 项全绿**（`tests/test_contract.py`） |
| 策略文件 | S0–S8 可加载；S9/S10 存在但**拒绝加载** |
| Schema | 3 份，均为合法 JSON，且不含校验器不支持的关键字 |
| 第三方依赖 | **无**（仅标准库） |

**本目录已完成**：Skill 骨架、策略 Prompt、Schema、工具适配、契约测试、文档。
**本目录未包含**（属实验执行器职责）：PDF 下载与渲染、模型推理、Benchmark 评测脚本、
`evals/` 下的具体评分实现。

> ⚠️ **自检全绿 ≠ 理解正确**。JSON 合法、字段齐全、`span_id` 形状正确，
> 都不构成"读懂了"的证明。科学正确性必须由独立评估回答。

---

## 7. 文档导航

| 文件 | 给谁看 | 内容 |
|---|---|---|
| `SKILL.md` | 被测 Agent / 调用方 | 任务契约：输入、输出、策略加载、底线规则 |
| `tools/README.md` | 集成方 | 工具库用途、快速开始、设计取舍 |
| `tools/API.md` | Agent（按需查） | 逐函数 API、信封语义、错误码、校验规则 |
| `evals/README.md` | 实验实施 Agent | 评估契约、产物落点、与执行器的分工 |
| `schemas/*.json` | 集成方 | ReadingResult 的结构契约 |
