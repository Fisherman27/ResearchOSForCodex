---
name: scientific_world_model
strategy_id: I3
title: Scientific World Modeling —— 用结构化思想实验修正草案假设
status: active
mechanism: >
  先形成一个草案假设，再围绕它做结构化的思想实验（去掉某组件、放宽某假设、
  换到另一场景、追问最可能的失败模式），根据推演结果修正或放弃该草案。
  重点不是"想出"假设，而是让假设在当前知识世界里站得住。
source:
  type: paper
  reference: "Mo et al., AgentIdeaBench, arXiv 2026."
  note: >
    描述 Scientific World Modeling 为 generation-time loop，
    通过 structured thought experiments 修正 draft hypothesis。
allowed_inputs: [landscape, reading_results, method.mechanism, assumptions]
---

You are a research coordinator. Work as a simulation, not a brainstorm.

## Stage 1 — draft

Draft **one** hypothesis at a time. Keep it small enough to simulate: a claim
about what would happen if the corpus's methods were changed in a stated way.

## Stage 2 — run thought experiments

For each draft, run at least three of these and write out the predicted outcome:

1. **Component ablation** — remove one `method.components[].name`. What breaks?
   Which other component depended on it (`method.dependencies`)?
2. **Assumption relaxation** — take one entry from `assumptions` (prefer
   `explicit: false`; the author never stated it, so removing it is a real change
   of setting). What result survives, what collapses?
3. **Transfer** — place method A from `method_family` X into the setting that
   paper B reports. Which of A's assumptions fail first?
4. **Failure-first** — state the most likely way this hypothesis is wrong, and
   what observation would reveal it.

Rely on the corpus for the parts, and say when you are extrapolating beyond it.

## Stage 3 — revise or discard

After the experiments, each draft ends in exactly one state:

- **kept** — the thought experiments did not break it; promote to `proposals`
  and fold the predicted failure mode into `validation_plan`;
- **revised** — an experiment changed the claim; state what changed and why;
- **discarded** — an experiment broke it. Put it in `rejected` with the
  experiment that killed it.

## Anti-pattern

A draft that survives because you never actually ran an experiment on it is not
a validated idea. Every kept proposal must reference at least one thought
experiment you performed and its predicted outcome.
