# PaperScheduler Agent Workflow

该 Workflow 读取 Organization 的一致快照，生成阅读计划和 `ReadingTask`。
确定性策略可直接运行；`agent` 策略通过可注入执行器选择目标论文，但仍由工具层
校验候选范围并构建任务。
