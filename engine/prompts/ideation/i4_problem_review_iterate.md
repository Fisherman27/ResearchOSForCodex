---
name: problem_review_iterate
strategy_id: I4
title: Problem → Review → Iterate —— 先提研究问题，再经独立评审迭代
status: active
mechanism: >
  把"提方向"拆成两步：先生成候选研究问题，再以独立评审的视角按维度审查
  （新颖性、清晰性、可行性、有效性），依据评审意见迭代修改。
  评审者必须与生成者分离，不能自己生成自己拍板。
source:
  type: paper
  reference: "Baek et al., ResearchAgent: Iterative Research Idea Generation over Scientific Literature with LLMs, NAACL 2025."
allowed_inputs: [landscape, reading_results, paper_relations]
---

You are a research coordinator running two distinct roles. Keep them apart in
your output; never let the author's optimism leak into the review.

## Role A — problem identification

Generate a set of **candidate research problems**, not solutions. A research
problem is a question the field cannot currently answer, phrased so that an
answer would be recognisable.

For each: which papers make this question live, and what would count as an
answer. Put these in `opportunities`.

## Role B — independent review

Now switch roles. Review each candidate as a sceptical referee, scoring only
what you can defend from the corpus:

| dimension | question |
|---|---|
| novelty | is this already answered by a paper in the corpus? |
| clarity | could two researchers disagree about what is being asked? |
| feasibility | is there a concrete first experiment or proof attempt? |
| validity | does the evidence actually bear on this question? |

**Do not score novelty as high merely because you did not find a counterexample
in this corpus.** Record that as `unverified`.

## Role C — iterate

For each candidate that scored poorly on any dimension, either:

- **revise** it to address the specific critique, and say what changed; or
- **drop** it into `rejected` with the critique that killed it.

A candidate that was not revised and not dropped is a candidate you failed to
review. Every proposal in the final output must carry the critique it survived.

## Anti-pattern

Self-evaluation is the known failure mode of this pattern: a single pass that
both proposes and approves produces confident, unexamined ideas. If your review
section contains no actual criticism, you did not perform a review.
