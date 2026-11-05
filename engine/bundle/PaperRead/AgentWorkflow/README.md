# PaperRead Agent Workflow

该 Workflow 把 Scheduler 的 `ReadingTask`、唯一阅读策略和目标论文材料组装成模型消息，
并对返回的 `ReadingResult` 做生产契约校验。它不直接修改 Organization；系统编排层在
验证通过后统一写回。

执行器若支持 `run(messages=..., tools=..., attachments=...)`，Workflow 会把目标 PDF
放入 `attachments`，并注入受控的 `request_context` 工具。补充信息仍由 Scheduler 从
当前任务绑定的 Organization 快照中选择，Reader 不能绕过 Scheduler 直接浏览知识库。
