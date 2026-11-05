---
name: scheduler_context
strategy_id: S10
title: Scheduler-Assisted Contextual Reading —— 基于论文索引图与历史精读知识的阅读
status: future
mechanism_hypothesis: >
  已经理解的前置论文知识与索引图能否改善下一篇论文的深入理解。
  这是阅读策略与上层知识供给共同作用的机制，必须与单篇 PDF 策略分开研究。
source:
  type: system_level_proposal      # AutoResearch 自身提出的系统级候选策略
  reference: >
    借鉴知识依赖、先修知识支架、类比理解以及检索增强的通用思想；
    不是已有文献中被直接证实的完整 PaperRead 机制。
allowed_inputs: [target_pdf, reading_task, reading_context, organization_query, request_context]
requires_context: true
requires_tools: true
first_round: "future / NOT_RUN"
model_visible: false
---

<!--
本文件不是首轮可加载的策略 Prompt。frontmatter 之后的正文描述的是**未来**概念流程，
若被显式选中，loader 必须按 status: future 拒绝加载（见 tools/strategy_loader.py）。

## 预期输入（未来）

目标 PDF、Scheduler 提供的 `ReadingTask`、Organization 中相关论文的结构化阅读结果、
论文间有证据支持的语义关系、知识快照与上下文预算。

## 策略内容（未来，概念流程）

1. 识别目标论文在论文索引图中的位置：直接先修、方法来源、对立/改进/扩展关系（图中边包含可靠性与来源）；
2. 获取 Scheduler 筛选的少量关键前置知识：问题定义、方法机制、必要理论、假设和已知局限；
3. 阅读当前论文时明确区分**目标论文自身的内容**与**已读论文中提供的前置知识**；
4. 判断它继承了什么、改动了什么、提出什么新的假设与结论；
5. 遇到真实理解缺口时，向 Scheduler 请求进一步上下文（工具契约未来才定义）；
6. 把新确认的知识和关系候选返回 Organization，形成 Scheduler 动态重规划的依据。

## 与另外两模块的边界

Organization 负责维护语义索引和原始出处；Scheduler 负责阅读顺序及上下文选择，
且冷启动时只有粗粒度论文图，随着 Read 产出不断修订剩余阅读顺序；
PaperRead 负责依赖给定上下文理解当前论文，**不直接自行管理全局论文图**。

## 实验因果歧义

S10 多了知识与 token，若与单篇 PDF 基线直接比较，不能认定提升全部来自认知策略。
未来应设计 `no_context`、`random_context`、`matched_budget_summary`、`structured_context`、
`graph_guided_context` 等匹配上下文对照，固定知识快照 / 阅读顺序分别研究。

## 当前状态

`future / NOT_RUN`。不在首轮隔离实验，**不得假装已实现 Scheduler 工具**；
在报告中标注 `not_evaluated`。
-->
