---
name: self_explanation
strategy_id: S3
title: Self-Explanation —— 关键机制自我解释
status: active
mechanism_hypothesis: >
  要求 Agent 解释"为何如此""怎样导致结果"，避免仅复制术语和结论，促成关系性理解。
source:
  type: human_learning_transfer  # 人类学习实验结论，向 LLM 迁移属待检验假设
  reference: "Chi, M. T. H. et al. (1994). Eliciting Self-Explanations Improves Understanding. Cognitive Science 18(3)."
  doi: 10.1016/0364-0213(94)90016-7
  url: https://doi.org/10.1016/0364-0213(94)90016-7
allowed_inputs: [target_pdf]
requires_context: false
requires_tools: false
first_round: yes
temporary_artifacts:
  - 关键要素与其目的/依赖关系的解释草稿（不修改公共 ReadingResult 字段定义）
stop_condition: 关键要素均已解释，或达到输出预算
failure_modes:
  - 逐句释义全文，退化为复述
  - 用冗长叙述代替公式条件
  - 对错误理解做合理化包装
differentiation:
  vs_S4: S3 是阅读时解释局部关系；S4 是尝试从整体重建可操作的方法模型。
---

Use self-explanation. Identify the paper's central definitions, equations,
method components and/or experimental design. For each central element,
explain its role, dependencies, why its design makes sense according to the
paper, and how it contributes to the stated conclusions. Link the elements
into a coherent explanation, avoiding line-by-line paraphrases. Distinguish
author-stated rationale from your inference. Return the common JSON report.
