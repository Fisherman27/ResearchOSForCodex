# 架构：谁在思考，谁在执行

## 两层分工

```text
宿主（Codex）          思考：每个模块一个真实子智能体，读工单、做判断
engine/researchos     执行：工具调用、契约校验、状态机、关卡、审计链
```

引擎**从不调用模型**。当某个 Agent 配置为交接角色时，它的 think-act
回合会被写成磁盘**工单**；Lead 读取工单，唤醒该角色的固定子智能体给出
`{"tool_calls": ...}` 或 `{"payload": ...}`，引擎再执行、校验并推进。

## 智能体拓扑

```text
                      Codex 主会话 Lead
                             │  推进 / 唤醒 / 用户交互
        ┌────────────────────┼─────────────────────┐
        ▼                    ▼                     ▼
     Research          Literature             Organization
        │                 │  ask_* 三个下属        │
        ▼                 │                       │
   research.query     ┌───┴────┬──────────┐        │
   （子智能体）         ▼        ▼          ▼        │
                 Organization Scheduler   Read ─────┘
                                  │         │
                                  ▼         ▼
                          scheduler.planner read.paper

  Literature 之后：同一个 Finalizer 会话连续完成 8 个阶段
  synthesis → gap_analysis → hypothesis → methodology
            → execution → analysis → audit → writing
```

每个方框都是一个 `Agent`：独立 id、独立 system prompt（`engine/prompts/*.md`）、
独立工具授权、独立 turn 预算、独立 `runs/<id>/agents/<id>/trace.jsonl`。

## 工具边界是结构性的

- Lead 不直接读取论文或替代专业角色做科研判断；
- 越权调用由 `ToolRegistry` 拒绝，返回 `E2_TOOL_UNKNOWN`，不是靠提示词约束。

## 状态机（与既有实现逐字一致）

文献闭环：

```text
created → planning → tasks_ready → reading → writing_back → replanning → ...
                          │
                          └─ 无候选 → 论点关系审阅关卡 → completed / blocked_material
```

顶层：

```text
research → literature → finalization(synthesis..writing) → completed
```

不可绕过的门槛：

| 机制 | 位置 |
|---|---|
| `batch_size=1`，写回后才重规划 | `workflow/literature.py` |
| 阅读队列耗尽 ≠ 完成，必须过关系审阅 | `_begin_relation_review` |
| 空 `relations` 合法但必须有 `review_notes` | `ports/bundle.py` + organization |
| 未读论文缺 PDF → `blocked_material` | `agents/scheduler.py` |
| `audit=pass` 才允许 writing 落地 | `workflow/director.py`（历史文件名，现由 Lead 推进） |
| `audit=revise` 回 methodology，`audit=blocked` → blocked_research | 同上 |
| 8 个终段字段契约 | `workflow/finalization.py` |
| 重读预算、单篇上限、停滞检测 | `agents/scheduler.py` + `ports/sufficiency.py` |

## 重读

Scheduler 在规划新论文之前，先检查已读论文的阅读反馈是否足够：

- `understanding_quality.status` 为 `partial` / `needs_revision` → 可重读；
- `completeness.absent_fields[].reason` 为 `not_extracted` / `not_checked` /
  `unknown` → 可重读；
- `not_found` / `not_applicable` → **不可重读**（原文没有，重读无用）。

重读任务带 `task_type="reread"`、`deficiency`、`reading_focus`、`reread_index`。
四条防失控规则：`reread_budget`、`max_rereads_per_paper`、停滞检测、
上限在 supervisor 侧强制（外部 Agent 漏传参数也绕不过）。

## 研究方向（ideation）

阅读闭环结束后、`completed` 之前运行一次，策略见
`engine/prompts/ideation/`（默认 I1）。

- `landscape` 由 `build_landscape()` 确定性投影，是所有策略的共同输入；
- 提案必须可追溯到 `paper_id` / `claim_id`，否则整份被拒；
- 新颖性永远在 `unverified` 里（闭世界语料无法证明）；
- ideation 失败**不会**让文献阶段失败——阅读已经成功了。

## 产物布局

```text
runs/<run_id>/
├── state.json                 顶层状态机
├── events.jsonl               所有 actor 的事件
├── artifacts/<stage>.json     8 个终段产物
├── agents/<agent_id>/trace.jsonl   每个 Agent 的 turn + tool_call
├── pending/                   待外部完成的终段包、阅读包
├── results/<task_id>.json     已落盘的阅读结果（跨暂停复用）
└── handoff/
    ├── outbox/                待宿主回答的工单
    ├── inbox/                 宿主的回复
    ├── done/                  两侧留档（可回放）
    ├── sessions/              每个 Agent 的对话快照
    └── answers.json           已完成委派的答案账本（幂等）

runs/<run_id>-literature/      文献闭环的独立状态机
```

## 两个 Codex 专有适配点

1. **工单 → 固定子智能体**：`.codex/agents/*.toml` 定义专业角色；
   `tools/sessions.py` 保存 ResearchOS 逻辑会话与 Codex child id 的映射。Scheduler 的
   `reader_groups` 决定 Reader 会话，而不是一篇论文机械创建一个新会话；
   `reading_depths` 决定每篇概要、粗读或精读，并由 ReadingResult 契约强制达到相应覆盖；
   角色文件只写行为指令，权限与 MCP 仍继承父会话。
2. **持久化边界**：交接模式下每条命令都是新进程，所以默认使用 `bundle` 后端
   （知识库在 SQLite / 磁盘上）。`memory` 后端只是测试替身，进程结束即丢状态。
   检索是唯一需要外网的步骤；`options.papers` 表示「语料已给定」，
   引擎直接把它落成 `corpus.jsonl`（离线演示、回放、已完成检索的复用）。磁盘状态能
   跨聊天恢复，但 Codex 模型上下文仅在旧 child id 仍属于可访问主聊天时保持。
