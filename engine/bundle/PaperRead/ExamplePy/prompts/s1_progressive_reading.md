---
name: progressive_reading
strategy_id: S1
title: Progressive Reading —— 分层渐进阅读
status: active
mechanism_hypothesis: >
  从全局到细节的渐进组织，降低 Agent 在局部技术细节上失去研究主线的概率。
source:
  type: human_reading_method     # 人类研究者的阅读方法建议，非 LLM 上的性能保证
  reference: "Keshav, S. (2007). How to Read a Paper. ACM SIGCOMM CCR 37(3), 83-84."
  doi: 10.1145/1273445.1273458
  url: https://doi.org/10.1145/1273445.1273458
allowed_inputs: [target_pdf]
requires_context: false
requires_tools: false
first_round: yes
temporary_artifacts:
  - 三层理解的内部推进顺序（不要求逐层输出全文）
stop_condition: 三层均已覆盖，或达到输出预算
failure_modes:
  - 把全文转成三份冗长摘要，token 成本上升而信息未增加
  - 固定遍次对短论文无益
  - 分层退化为机械重复阅读，层间无信息增量
---

Use progressive reading. First establish the paper's overall research
question, contribution, structure and main claim. Next understand the
important methods, figures, results and theoretical statements. Finally
examine the technically central details, assumptions and limitations.
Integrate the three levels into one common JSON reading report. Do not
produce three redundant summaries.
