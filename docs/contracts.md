# 产物与协议契约

编码 UTF-8。本文件描述**本工程约定**，不是 OpenAI 或 Codex 标准。

## 1. 工单（`runs/<id>/handoff/outbox/<order_id>.json`）

```json
{
  "order_id": "0003-scheduler",
  "seq": 3,
  "agent_id": "scheduler",
  "role": "scheduler",
  "system_prompt": "……该 Agent 的完整系统提示词……",
  "messages": [{"role": "user", "content": "..."}],
  "question": "……本轮要回答的问题（纯 JSON 或自然语言）……",
  "tools": [{"name": "...", "description": "...", "parameters": {...}}],
  "attachments": [{"kind": "pdf", "path": "/abs/path.pdf", "paper_id": "..."}],
  "metadata": {"tool": "...", "run_id": "...", "strategy_id": "..."},
  "reply_contract": {"shapes": {...}, "rules": [...]}
}
```

宿主**拿到的就是全部**：不需要读知识库，也不需要自己执行工具。

## 2. 回复（`runs/<id>/handoff/inbox/<order_id>.json`）

二选一：

```json
{"tool_calls": [{"name": "next_reading_tasks", "arguments": {"batch_size": 1}}]}
```

```json
{"payload": {"selected": ["arxiv:2401.00001"], "reasons": {...}, "note": "..."}}
```

规则：

- 只能调用工单 `tools` 中声明的工具，参数必须符合其 JSON Schema；
- 一次可以请求多个工具调用，结果在下一张工单里返回；
- 给出 `payload` 即结束该 Agent 本次任务，之后不能再请求工具；
- 不要臆造工具结果；看不到的数据就说看不到。

## 3. 阅读任务与阅读结果

`ReadingTask` 关键字段：`task_id`、`target_paper`、`target_material.pdf_path`、
`reading_context.knowledge_items`、`reading_strategy_id`、
`requested_reading_depth`（`summary` / `skim` / `deep`）、
`task_type`（`first_read` / `reread`）、`reading_focus`、`deficiency`、
`knowledge_snapshot`、`planner_reason`。

`ReadingResult` 必须通过
`engine/bundle/PaperRead/ExamplePy/schemas/reading_result.schema.json`
（`schema_version = "2.0-draft"`）与 production 语义校验：

- `paper_id` 必须等于 `ReadingTask.target_paper`；
- 作者主张 / 读者推断 / 跨篇综合分属不同 `source_type`；
- 空字段必须在 `completeness.absent_fields` 解释原因；
- 新阅读必须带 `screening`；初筛排除只影响后续预算，不删除论文；
- `deep` 必须达到 `third_pass`，并提交可证伪主张、七维质量判断、替代解释、
  适用边界、缺失信息和复现细节；
- `evidence_quotes` 区分页码逐字引文与解释，并保留 accepted/pending/rejected 审核状态；
- 证据锚点用 `span_id` / `media_id`，无法定位时 `[null]` 且保持
  `not_checked`，不得伪造。

## 4. 关系审阅

```json
{"relations": [{"src_claim": "...", "dst_claim": "...",
                "kind": "support|contradict|extend|use",
                "polarity": "positive|negative|neutral",
                "evidence": ["span:..."], "confidence": 0.6}],
 "review_notes": "比较了什么；为空时说明为何没有可辩护的边",
 "generated_by": "organization_claim_relation_review"}
```

- `relations: []` 合法，但必须有非空 `review_notes`；
- 不得创建新 claims、论文级关系、引用或阅读结果；
- 仅凭发表年份不得推断逻辑关系。

## 5. 研究方向（ideation）

```json
{
  "opportunities": [{"opportunity_id": "O1", "kind": "contradiction",
                     "grounds": [{"paper_id": "...", "what": "..."}],
                     "why_not_solved": "..."}],
  "proposals": [{"proposal_id": "R1", "title": "...", "research_question": "...",
                 "gap": "...", "from_opportunity": "O1",
                 "core_hypothesis": "...", "proposed_mechanism": "...",
                 "why_it_might_work": "...", "validation_plan": ["..."],
                 "evidence_refs": ["arxiv:..."]}],
  "rejected": [{"idea": "...", "reason": "..."}]
}
```

- 必填：`proposal_id`、`title`、`research_question`、`gap`、`from_opportunity`、
  `core_hypothesis`、`proposed_mechanism`、`validation_plan`、`evidence_refs`；
- `from_opportunity` 必须指向本次返回的某个 `opportunity_id`；
- `evidence_refs` 必须来自 `corpus.known_paper_ids` / `known_claim_ids`，
  全部无法锚定 → 整份提案被拒；
- 系统强制把「新颖性未验证」写进 `unverified`。

## 6. 8 个终段

| stage | 必填字段 | 说明 |
|---|---|---|
| `synthesis` | summary, consensus, disagreements, unknowns, evidence_refs | 只综合已有证据 |
| `gap_analysis` | gaps, selected_gap, evidence_refs | 不得把语料缺失写成普遍缺失 |
| `hypothesis` | hypotheses, selected_hypothesis, falsification_conditions | 不得写成已有发现 |
| `methodology` | design, inputs, procedure, evaluation, risks | 不得声称已执行 |
| `execution` | actions, artifacts, failures | artifacts 每项需 `artifact_id/kind/uri/produced_by/verification_status` |
| `analysis` | results, uncertainties, robustness, evidence_refs | 不得改动执行记录 |
| `audit` | verdict, checks, required_revisions | verdict ∈ pass/revise/blocked；pass 时 revisions 必须为空 |
| `writing` | title, abstract, findings, limitations, references, evidence_trace | 只能在 audit=pass 后落地 |

## 7. 运行状态

`runs/<id>/state.json` 关键字段：`status`、`phase`、`current_stage`、
`research_question`、`research_output`、`research_manifest`、
`literature_status`、`literature_initialized`、`final_artifacts`、
`artifact_paths`、`pending_final_task`、`revision_count`、`final_artifact`、
`last_error`。

状态值：`created`、`research_running`、`literature_initializing`、
`awaiting_external_agent`、`awaiting_stage_agent`、`final_synthesis`、
`final_revision`、`completed`、`blocked_research`、`failed`。

**`awaiting_external_agent` 是暂停，不是失败。**
