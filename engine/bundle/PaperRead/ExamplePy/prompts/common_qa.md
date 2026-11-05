---
name: common_qa
role: common_qa_contract
model_visible: true
sent_to_model: true
description: >
  精读结束、ReadingResult 已落盘之后，针对同一篇论文追加问答的统一 Prompt。
  `{{QUESTION}}` 由程序替换为题目；
  绝不替换为 Gold Answer。每道题从同一精读结束检查点独立分叉。
---

Now answer the following question about the SAME paper you just read.
Answer from the supplied paper and its contents only. Be direct, accurate
and clear. If the paper does not support an answer, respond that it is
not answerable from this paper. Do not call tools or use external
literature. Cite a page, equation, figure or table if you can locate it
reliably; otherwise do not invent a location.

Return valid JSON:
{
 "answer": string,
 "answerable_from_paper": boolean,
 "support_locator": string_or_null
}

Question: {{QUESTION}}
