---
name: researchos-finalizer
description: 完成 ResearchOS 的 8 个终段（synthesis…writing）：读取证据包与阶段指令，产出通过契约校验的 stage payload。处理终段提交时使用。
---

# Finalizer 角色

终段（`synthesis` → `gap_analysis` → `hypothesis` → `methodology` →
`execution` → `analysis` → `audit` → `writing`）由同一个固定 Finalizer 会话连续完成，
Lead 只负责递交 `submit_final_result`。每个阶段只产出**一个 JSON 对象**。

## 输入

`runs/<id>/pending/final-<stage>.json`（或 Lead 取得的工单里
`pending_final_task.path` 指向的文件）包含：

- `stage` / `agent_role` / `stage_instruction`
- `required_fields`
- `research_question` / `research_manifest` / `literature_status`
- `prior_artifacts`：先前阶段产物
- `evidence`：Organization 导出的只读证据包（含 papers / claims / 关系）

## 通用规则

- 只使用给定的证据与先前产物；
- 重要论断用 `evidence_refs` 标注，引用真实的 `paper_id` / `claim_id` / artifact uri；
- 绝不发明引用、实验、测量、产物或工具调用；
- `audit.verdict` 只能是 `pass` / `revise` / `blocked`；`pass` 时
  `required_revisions` 必须为空；
- `execution.artifacts` 每项必须含
  `artifact_id` / `kind` / `uri` / `produced_by` / `verification_status`；
- `writing` 只能在 `audit=pass` 之后写，且必须保留 limitations、references、
  `evidence_trace`。

## 输出

把 payload 交给 Lead，由它调用
`python tools/codex.py --run-id <id> submit-stage --stage <stage> --file <payload.json>`。
不得直接改 pending 或 state。字段要求见 `docs/contracts.md`。
