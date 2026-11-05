# PaperSearch Agent Workflow

该 Workflow 将研究问题转换为一次可追溯的 PaperSearch `full_pipeline` 运行。
可选 Search Agent 只负责给出一个明确检索查询及理由；检索、去重、PDF 获取、引用扩展、
GROBID 解析和落盘仍由现有 ToolsPy 执行。

```python
from search_workflow import SearchWorkflow
workflow = SearchWorkflow("work/search")
result = workflow.run("research question", agent_executor=optional_query_agent)
```
