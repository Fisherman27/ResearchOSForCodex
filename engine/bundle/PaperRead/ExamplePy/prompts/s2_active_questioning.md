---
name: active_questioning
strategy_id: S2
title: Active Questioning —— 主动提问驱动阅读
status: active
mechanism_hypothesis: >
  以尚未解决、又影响核心理解的疑问驱动再次检查 PDF，比等量的通用摘要更有针对性。
source:
  type: llm_reasoning_transfer   # 迁移设想，不等于已证明"自问自答式论文精读"有效
  reference: >
    Press et al., Measuring and Narrowing the Compositionality Gap in Language Models,
    Findings of EMNLP 2023 (Self-Ask)；Lee et al., QASA, ICML 2023（科学论文深层问答）
  urls:
    - https://aclanthology.org/2023.findings-emnlp.378/
    - https://proceedings.mlr.press/v202/lee23n.html
allowed_inputs: [target_pdf]
requires_context: false
requires_tools: false
first_round: yes
temporary_artifacts:
  - 少量高优先级理解缺口清单（仅用于策略执行，不修改公共 ReadingResult）
stop_condition: 无重要问题残留，或回答预算耗尽
failure_modes:
  - 元认知不足导致假问题
  - 自我强化既有错误
  - 对自己虚构的问题编造"原文答案"
  - 额外 token 开销显著
---

Use question-driven reading. After an initial understanding, identify up
to five high-priority questions whose answers are necessary for explaining
the central method, argument or results. For each question, inspect the
supplied paper again, answer from its content when possible, and update
your understanding. Keep unresolved questions explicitly marked. Stop when
no important question remains or your answer budget is exhausted. Return
the common JSON reading report.
