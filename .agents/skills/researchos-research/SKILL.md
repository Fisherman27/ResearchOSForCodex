---
name: researchos-research
description: 以 Research 检索角色回答工单：把研究问题变成一次可追溯的检索运行，返回落盘目录与查询计划。处理 agent_id 为 research 的工单时使用。
---

# Research 角色

## 输入

- `system_prompt`：`engine/prompts/research.md`
- `messages`：历史；最后一条可能是 `tool` 结果
- `tools`：`plan_query`、`run_search_pipeline`（以工单实际声明为准）
- `question`：JSON，含 `research_question`、`run_id`、`query`、`options`

## 做法

1. 如果还没有精确查询，先请求 `plan_query`，参数 `{"research_question": "..."}`。
2. 宿主提供联网搜索能力时，以规划出的 2–4 条高区分度查询自主搜索论文。只收录能核对
   题名且有 arXiv、DOI/出版社、会议或作者主页等一手落地页的候选；联网不可用时正常降级。
3. 拿到查询后请求 `run_search_pipeline`，把联网候选放入 `options.web_candidates`：

```json
{"tool_calls": [{"name": "run_search_pipeline", "arguments": {
  "research_question": "<原问题>",
  "run_id": "<工单 metadata.run_id 或 question.run_id>",
  "query": "<查询>",
  "reason": "<为什么这样检索>",
  "options": {"web_candidates": [{
    "title": "<已核对题名>", "source_url": "<一手落地页>",
    "doi": "<可选>", "arxiv_id": "<可选>", "year": 2026,
    "authors": ["..."], "abstract": "<可选>",
    "search_query": "<发现它的查询>", "source_name": "arXiv"
  }]}
}}]}
```

4. 如果最后一条 `messages` 是 `tool`，说明流水线已经跑完：
   返回 `{"payload": <该工具结果 data>}`，保留 `search_output` 与 `query_plan`。

## 约束

- 不判断论文质量，不做文献综述；
- 检索结果为空是 `empty`，不是 `failed`；
- 不要发明论文或声称覆盖范围；
- 不把搜索引擎片段当成论文证据；无法确认一手落地页的网页候选不得提交；
- 查询规划返回空查询时必须失败，不要退回含糊默认查询。
