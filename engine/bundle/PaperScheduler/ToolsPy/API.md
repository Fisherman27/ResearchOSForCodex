# PaperScheduler API

> 自动生成，请勿手工编辑。运行 `python docs/gen_api_md.py` 更新。

所有工具返回 `ToolsPyRules.md` 规定的统一信封。

### [E10] plan_reading

`plan_reading(snapshot: 'dict', *, strategy: 'str' = 'chronological', batch_size: 'int' = 1, completed: 'Optional[List[str]]' = None, require_material: 'bool' = True) -> 'dict'`

从一个 Organization 快照生成有解释的下一批阅读候选。

类型：`element`。

### [E20] select_context

`select_context(target_paper: 'str', knowledge_candidates: 'List[dict]', *, strategy: 'str' = 'none', budget_tokens: 'int' = 4000) -> 'dict'`

从 Organization 提供的已读知识候选中选择上下文。

类型：`element`。

### [E30] build_reading_task

`build_reading_task(target: 'dict', plan: 'dict', context: 'dict', *, task_id: 'str', reading_strategy_id: 'str' = 'S0', reading_focus: 'Optional[List[str]]' = None) -> 'dict'`

把一次规划决定和上下文组装成稳定 ReadingTask。

类型：`element`。

### [E31] filter_context_for_question

`filter_context_for_question(knowledge_items: 'List[dict]', question: 'str', *, budget_tokens: 'int' = 1000) -> 'dict'`

在同一任务的已授权知识池内处理 Reader 的增量上下文请求。

类型：`element`。

### [P10] next_reading_tasks

`next_reading_tasks(kb: 'str', *, planner_strategy: 'str' = 'chronological', context_strategy: 'str' = 'none', batch_size: 'int' = 1, context_budget_tokens: 'int' = 4000, reading_strategy_id: 'str' = 'S0', completed: 'Optional[List[str]]' = None, task_prefix: 'str' = 'read') -> 'dict'`

从 Organization 一致快照生成下一批 ReadingTask。

类型：`pipeline`。

