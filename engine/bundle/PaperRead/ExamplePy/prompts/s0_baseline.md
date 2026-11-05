---
name: baseline
strategy_id: S0
title: Direct Reading / Baseline —— 最小额外约束的直接阅读
status: active
mechanism_hypothesis: >
  不是新的认知机制，而是提示策略比较中的控制组。给定论文与统一输出目标，
  Agent 自主决定阅读方式；公共结构化输出本身已有引导作用，因此本组不是"无提示词"。
source:
  type: project_control       # 项目对照组，无外部文献来源
  reference: S0
allowed_inputs: [target_pdf]
requires_context: false
requires_tools: false
first_round: required
---

Read the supplied paper and complete the common JSON reading report.
Decide how to read it yourself. No additional reading procedure is imposed.
