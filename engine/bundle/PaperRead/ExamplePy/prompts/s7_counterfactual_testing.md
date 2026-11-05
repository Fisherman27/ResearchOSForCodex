---
name: counterfactual_testing
strategy_id: S7
title: Counterfactual Stress Testing —— 反事实机制压力测试
status: active
mechanism_hypothesis: >
  能否合理分析"若关键条件改变，方法和结论会怎样变化"，是区分机械复述与机制理解的一个信号。
source:
  type: project_proposal          # 完整阅读流程由本项目提出，无单篇论文直接验证其对科学 PDF 阅读的效果
  reference: "科学方法中的反事实、替换假设、控制变量与消融的通用直觉。"
allowed_inputs: [target_pdf]
requires_context: false
requires_tools: false
first_round: yes
temporary_artifacts:
  - 2-4 个紧贴核心机制的改动及其后果推断
stop_condition: 反事实问题均已分析或反查 PDF 后仍无法确定，或达到输出预算
failure_modes:
  - 把反事实推断写成作者实测结论
  - 高度依赖推断质量，可能产出貌似合理的虚构机制
  - 以"有创意"代替"论文理解正确"
grounding_rule: 推断须区分三类：论文已有的真实消融、从理论必然推出的推论、无法验证的猜想。
---

Use counterfactual stress testing. Identify two to four central design
choices, assumptions or components. Consider what would follow if each
were changed or removed. Ground your reasoning in the paper's equations,
theory and/or ablations. Separate actual reported experiments, logically
supported implications and speculative possibilities; never portray
speculation as an observed result. Use the exercise to refine the common
JSON reading report.
