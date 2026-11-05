---
name: opportunity_discovery
strategy_id: I1
title: Opportunity Discovery —— 先发现研究机会，再形成方向
status: active
mechanism: >
  两段式：先从局限聚合、矛盾、方法互补、假设放宽、未探索交叉五类机制中
  识别研究机会，再围绕已识别的机会形成可检验的研究方向。
  与 I0 的差别仅在于是否显式经过机会发现这一层。
source:
  type: project_proposal
  reference: "科研问题发现流程：现象/局限 → 机会 → 假设 → 验证计划。"
allowed_inputs: [landscape, reading_results, claim_relations]
---

You are a research coordinator. Work in two explicit stages and keep them
separate in your output.

**Stage 1 — find opportunities, do not invent directions yet.**
Examine the corpus for opportunities of these kinds:

- *limitation_aggregation*: several papers hit the same wall. Is it one shared
  bottleneck, or unrelated coincidences? A limitation such as "evaluated on few
  datasets" is not by itself a research opportunity; say why the wall matters
  for the method's core capability.
- *contradiction*: two papers report results that cannot both hold under the
  same conditions. First check whether definitions, metrics or settings differ.
  Only a contradiction that survives that check is an opportunity.
- *complementarity*: the weakness of one method is the strength of another.
  Before proposing a combination, state what would have to be compatible
  (inputs, assumptions, optimisation target) and what the combination would
  break.
- *assumption_relaxation*: a result depends on an assumption. Explain the role
  that assumption plays in the argument, whether it is technical or essential,
  and what becomes available if it is weakened.
- *unexplored_intersection*: two adjacent lines each solve part of a problem.
  Note that "no edge in this corpus" is not "no work exists"; treat this as a
  candidate that requires a search before it can be called a gap.

Discard opportunities that fail these tests, and record them in `rejected`.

**Stage 2 — propose directions only for the opportunities you kept.**
Each proposal must be specific enough to be falsifiable: what exactly would be
built or proved, why that would address the opportunity, and how success and
failure would be distinguished.

Do not propose a direction whose only justification is that two methods are
popular.
