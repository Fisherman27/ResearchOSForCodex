---
name: closed_book_recall
strategy_id: S9
title: Closed-Book Recall and Repair —— 真正闭卷回忆与纠错
status: excluded
mechanism_hypothesis: >
  读完后禁止访问原文、回忆核心内容，然后再次开放原文纠错；研究主动回忆的学习效应。
source:
  type: human_learning_transfer
  reference: "Roediger, H. L. & Karpicke, J. D. (2006). Test-Enhanced Learning. Psychological Science 17(3)."
  doi: 10.1111/j.1467-9280.2006.01693.x
  url: https://doi.org/10.1111/j.1467-9280.2006.01693.x
allowed_inputs: []
requires_context: false
requires_tools: false
first_round: excluded
model_visible: false
---

<!--
本文件不是可加载的策略 Prompt。frontmatter 之后的正文不会作为可用策略正文被发送；
若被显式选中，loader 必须按 status: excluded 拒绝加载（见 tools/strategy_loader.py）。

## 当前不可行原因

当论文 PDF 已经进入同一模型上下文时，Prompt 声称"请忘记 PDF"不可能构成真正的闭卷隔离；
即使转到新的模型调用，模型的预训练知识依然存在。**不得用假闭卷冒充 S9。**

## 未来可选方案

独立上下文 / 多进程分阶段读取并重建，会改变执行架构；需要在另一个专门实验中验证。

## 实验状态

`excluded`。不可放入本轮"单一 PDF、单策略"比较。

在报告中必须标注 `not_evaluated`，不得声称已运行。
-->
