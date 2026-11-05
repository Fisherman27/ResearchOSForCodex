---
name: intra_paper_contrast
strategy_id: S6
title: Intra-Paper Contrast —— 论文内部结构性对比
status: active
mechanism_hypothesis: >
  对比可以暴露论文方法相较于基线的真正机制差异与设计增量。
source:
  type: cognitive_theory_transfer
  reference: "Gentner, D. (1983). Structure-Mapping: A Theoretical Framework for Analogy. Cognitive Science 7(2)."
  doi: 10.1016/S0364-0213(83)80009-3
  url: https://doi.org/10.1016/S0364-0213(83)80009-3
  note: 关注关系结构而不仅是对象表面相似；当前策略是该理论在论文阅读上的迁移。
allowed_inputs: [target_pdf]
requires_context: false
requires_tools: false
first_round: "yes, 记录适用性"
temporary_artifacts:
  - 对比对象与共同维度的对齐表（问题/前提/机制/优化目标/资源/实验结果）
stop_condition: 可比对象已对齐分析，或确认论文内对比材料不足
failure_modes:
  - 论文没有足够详细的对照机制时，凭预训练知识伪造对比
  - 把 Related Work 中的一句描述等同于已经阅读了被引论文
applicability_note: >
  纯 PDF 下仅限论文内部对比；论文若缺乏详细可比对象，允许标记"不足以完成结构对比"，
  该样本对 S6 记为失效并单独切片报告。
---

Use intra-paper comparison ONLY. Compare the proposed method with
baselines, variants, alternative components or conditions explicitly
described in this same paper. Align their objectives, assumptions,
mechanisms and observed outcomes; explain the important relational
differences. Never claim you read any cited paper that was not supplied.
If the paper lacks detailed comparable cases, say so. Return the common
JSON report.
