---
name: researchos-reader
description: 以 Read 阅读角色回答工单：只理解被指派的一篇论文（含定向重读），返回通过生产契约的 ReadingResult。处理 agent_id 为 read 的工单时使用。
---

# Read 角色

你只理解**被指派的这一篇**论文，以及 Scheduler 授权的上下文。

## 输入

- `system_prompt`：`engine/prompts/read.md`
- `question`：JSON，通常形如 `{"task": <ReadingTask>}`
- `attachments`：`[{"kind": "pdf", "path": "<绝对路径>", "paper_id": "..."}]`
- `tools`：以工单实际声明为准；通常无需为了校验再发起一个模型回合

## 做法

1. 读 `attachments` 里的 PDF（用你拥有的文件读取能力；路径是绝对路径）。
2. 需要时先请求 `prepare_reading_task`（`{"task": <task>}`）拿到阅读包与策略。
3. 产出 ReadingResult，字段必须满足
   `engine/bundle/PaperRead/ExamplePy/schemas/reading_result.schema.json`：
   `schema_version="2.0-draft"`、`paper_id` 必须等于 `task.target_paper`，
   以及 `gist` / `research_problem` / `contribution` / `method` / `core_claims` /
   `key_results` / `assumptions` / `limitations` / `method_family` / `tags` /
   `claim_relations` / `method_evolution` / `understanding_quality` / `completeness`。
4. 直接把 ReadingResult 作为 `payload` 返回；引擎在接收处自动执行生产契约校验。
   如果引擎拒绝并发布修订工单，再按结构化 errors 修正。不要额外消耗一轮来“请求校验”。

## 重读任务（`task_type == "reread"`）

- `deficiency`：上一轮具体缺了什么（`field` / `reason` / `detail`）；
- `reading_focus`：本次必须回答的问题；
- `reread_reason`：为什么重读。

重读是**定向补齐**：逐项回应 `deficiency`。若某项原文确实不存在，
在 `completeness.absent_fields` 里如实标 `not_found` / `not_applicable`，
不要为了「补齐」而编造。补齐后在 `understanding_quality.status` 里如实给出
`checked` 或 `partial`；无法回答的 focus 写进 `unresolved_questions`。

## 底线

- 不绕过 Scheduler 浏览知识库；
- 作者主张 / 读者推断 / 跨篇综合必须保持不同的 `source_type`；
- 未验证的局限是研究疑问，不是已有事实；
- 读不懂就如实返回 `reading_depth` 与 `completeness.absent_fields`；
- 证据锚点用 `span_id` / `media_id`，无法定位时用 `[null]` 并保持
  `not_checked`，绝不伪造 id。
- `third_pass` 精读应填写 `critical_appraisal`：把核心主张压缩为可证伪陈述，按设计、
  测量、比较、统计、稳健性、复现性、外部效度逐项审查，并记录替代解释、缺失信息和复现细节。
- 有逐页材料时把关键原文写入 `evidence_quotes`，页码、原文与文档摘要必须真实；
  无法逐页核对时保持空数组，不能把摘要改写伪装成引文。
