---
name: evidence_audit
strategy_id: S5
title: Claim-Evidence Audit —— 主张—证据审查
status: active
mechanism_hypothesis: >
  由"接受作者主张"改为"检查主张得到何种证据支持"，减少过度概括与不准确的解释。
source:
  type: practice_plus_llm_transfer
  reference: >
    科学同行评审中的 claim-evidence 审查；K-Dense-AI Scientific Agent Skills 的 Peer Review /
    Scientific Critical Thinking；Dhuliawala et al., Chain-of-Verification, Findings of ACL 2024
  urls:
    - https://github.com/K-Dense-AI/scientific-agent-skills
    - https://aclanthology.org/2024.findings-acl.212/
  note: CoVe 在它自己的事实任务上做独立检验；论文阅读场景需要另证。
allowed_inputs: [target_pdf]
requires_context: false
requires_tools: false
first_round: yes
temporary_artifacts:
  - 主张-证据核对表（定理/证明、实验表格、消融、定性案例、作者推测）
stop_condition: 最重要的若干主张均已审查，或达到输出预算
failure_modes:
  - 单模型"自我验证"重复自己的错误
  - 因引用了真实图表就自动判定结论成立
  - 图表读取质量取决于模型视觉输入
  - 用当前 PDF 以外的论文补证
---

Use a claim-evidence audit. Identify the most important claims in the
paper. For each, examine what proof, experiment, table, figure or argument
in the supplied paper actually supports it and under which conditions.
Flag overgeneralization and unsupported causal interpretations; do not
assume a cited result proves a broader statement. Produce the common JSON
report using carefully bounded claims and available evidence locators.
