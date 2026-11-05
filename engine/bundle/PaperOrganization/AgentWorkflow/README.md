# PaperOrganization Agent Workflow

该 Workflow 负责三件事：导入 PaperSearch 产物、调用语义索引 Agent 提出可审查的
论文关系、在每次导入或阅读结果写回后更新图版本与可视化。

模型执行器是一个可注入 callable：接收消息数组，返回 RelationProposal 数组。
没有执行器时只维护原始引用图，不会把引用边伪装成已经确认的语义关系。

```python
from organization_workflow import OrganizationWorkflow

workflow = OrganizationWorkflow("/path/to/kb")
result = workflow.initialize("/path/to/PaperSearch/output", agent_executor=my_agent)
result = workflow.apply_reading_results([reading_result], agent_executor=my_agent)
```
