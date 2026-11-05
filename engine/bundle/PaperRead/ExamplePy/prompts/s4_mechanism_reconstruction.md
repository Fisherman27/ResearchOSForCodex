---
name: mechanism_reconstruction
strategy_id: S4
title: Mechanism Reconstruction —— 方法、理论与实验机制重建
status: active
mechanism_hypothesis: >
  理解必须足以在不逐句抄录的情况下重构方法关系；重构与对照过程本身会暴露理解空缺。
source:
  type: project_proposal          # 具体 Agent Prompt 由本项目提出，非现成已验证论文算法
  reference: "方法学参考：Keshav 2007 第三遍阅读建议从作者视角虚拟重做论文；复现实践强调能否重建技术步骤。"
  url: https://doi.org/10.1145/1273445.1273458
allowed_inputs: [target_pdf]
requires_context: false
requires_tools: false
first_round: yes
temporary_artifacts:
  - 按论文类型重建的方法草图（算法/理论/实验/系统各一套重建维度）
stop_condition: 重建结果已与原文对应部分对照并修正偏差，或达到输出预算
failure_modes:
  - 把模型习惯性的算法模板写成作者真实实现
  - 成本较高
  - 综述或立场论文不适合算法重建标准
  - 被误读为要求产出可执行代码（不要求）
adaptation:
  algorithm_paper: 重建目标函数、关键变量、组件调用顺序、迭代规则
  theory_paper: 重建定义、定理前提、证明主线及关键转换
  experimental_paper: 重建自变量/对照、数据、指标、可支持的因果或统计结论
  system_paper: 重建模块、数据流、假设与性能瓶颈
---

Use mechanism reconstruction. Determine the paper type, then reconstruct
its core method without merely copying the original wording. For an
algorithm paper reconstruct objectives, inputs/outputs and key update
steps; for a theory paper reconstruct assumptions and proof structure;
for an experimental paper reconstruct hypothesis, design, measures and
comparisons. Check your reconstruction against the supplied paper and
correct discrepancies. Do not invent missing implementation details.
Return the common JSON report.
