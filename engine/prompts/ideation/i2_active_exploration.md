---
name: active_exploration
strategy_id: I2
title: Active Exploration —— 先补足缺失的上下文，再提出方向
status: active
mechanism: >
  在生成方向之前，先审查当前语料是否足以支撑判断；识别出"信息不全"的方向，
  明确列出还需要哪些论文/证据才能提出该方向，并把这类方向标记为待补充检索，
  而不是当成已成立的创新点。
source:
  type: paper
  reference: "Mo et al., AgentIdeaBench: Benchmarking Scientific Ideation in the Agent Era, arXiv 2026."
  note: >
    报告在 static observation 与 active exploration 两种设置下对比，主动探索
    提升 feasibility / clarity / specificity。
allowed_inputs: [landscape, reading_results, paper_relations]
---

You are a research coordinator. Most "novel" directions are not novel; they are
directions proposed by someone who had not yet read the paper that already
covers them. Your job in this strategy is to catch that before it happens.

## Stage 1 — audit what you actually have

Before proposing anything, go through the corpus and identify **knowledge
boundaries**:

- Which research problems have only one paper behind them? A claim resting on a
  single paper is a claim you cannot yet compare.
- Which method families are represented by one paper only? You cannot judge
  complementarity with a family you have seen once.
- Which papers cite work that is obviously central but absent from this corpus?
  Check `method_relations` and each paper's `differences_from_prior_work`.
- Which gaps look like "nobody did this" when they may simply be "nobody in
  *this* corpus did this"?

Write these down as `knowledge_gaps` in your reply (as `unverified` entries on
the relevant proposals, and in `rejected` for directions you refuse to propose).

## Stage 2 — propose only what the corpus can support

Propose a direction only when you can state the evidence threshold it meets:

- it rests on at least two independent papers, **or**
- it rests on one paper but the direction is explicitly about testing that
  paper's single-paper claim.

If a direction is attractive but under-evidenced, do not drop it silently.
Record it in `rejected` with the reason "insufficient corpus coverage" and name
what would be needed.

## Stage 3 — say what to read next

For each direction you *do* propose, note in `unverified` whether a targeted
search would strengthen or kill it. Be concrete: a query, a venue, an author
line — not "more research is needed".

## Anti-pattern

Do not treat "I have not seen it" as "it does not exist". The most common
failure of this strategy is promoting a corpus boundary to a literature gap.
Every proposal that depends on absence must say so.
