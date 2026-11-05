---
name: multi_perspective
strategy_id: S8
title: Multi-Perspective Critique —— 单 Agent 多视角质询
status: active
mechanism_hypothesis: >
  作者、审稿人和复现者视角关注的遗漏不同，多视角审查可暴露知识盲区。
source:
  type: llm_reasoning_transfer
  reference: "Du et al. (2024). Improving Factuality and Reasoning in Language Models through Multiagent Debate. ICML."
  url: https://proceedings.mlr.press/v235/du24e.html
  note: >
    本阶段是同一模型模拟多个角色，不等同原论文的独立模型多 Agent Debate；
    这是角色提示实验，而非真正的多 Agent 系统实验。
allowed_inputs: [target_pdf]
requires_context: false
requires_tools: false
first_round: yes
temporary_artifacts:
  - 三个视角各自的要点与冲突点清单
stop_condition: 观点开始重复，或冲突已用 PDF 内依据裁决
failure_modes:
  - 同一模型不同角色存在相关性偏差
  - 成本高
  - 多视角观点重复时未及时停止
perspectives:
  author: 最强的研究动机、核心机制及其逻辑
  reviewer: 针对假设、证据、泛化和结果的具体质疑
  replicator: 关键实现或推导所依赖的细节与可能缺失
---

Use several perspectives inside THIS SINGLE MODEL SESSION (do not create
other agents). From the author's perspective identify the strongest
motivation and reasoning; from a reviewer's perspective identify concrete
assumption, evidence and scope questions; from a replicator's perspective
identify indispensable technical details and missing specifications.
Resolve disagreements by checking the supplied paper, preserve remaining
uncertainty, and return one common JSON reading report. No external sources.
