# PaperScheduler ToolsPy

该工具库把 `` 中的 `ReadingPlanner`、`ContextSelector` 和
`ReadingTask` 组装落实为可调用代码。它只做规划与上下文选择；知识真相由
PaperOrganization 管理，论文理解由 PaperRead 完成。

```python
import sys
sys.path.insert(0, "PaperScheduler/ToolsPy")
import paperscheduler

result = paperscheduler.next_reading_tasks(
    "PaperOrganization/ToolsPy/kb",
    planner_strategy="dependency_based",
    context_strategy="structured",
    batch_size=1,
    reading_strategy_id="S4",
)
```

原子能力：`plan_reading`、`select_context`、`build_reading_task`、
`filter_context_for_question`。流水线：`next_reading_tasks`。

所有函数返回 `ToolsPyRules.md` 规定的普通字典信封。`partial` 仍然
`ok=True`；空候选使用 `empty`，不会伪装成失败。
