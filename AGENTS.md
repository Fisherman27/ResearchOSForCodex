# ResearchOS for Codex

本项目把科研判断交给 Codex 多智能体，把状态、数据、契约和关卡交给确定性引擎。
当前主会话是 Lead；新运行不派生 Director。

## 入口

- 首选控制器：`python tools/codex.py --run-id <id> <command>`
- 默认配置：`config/handoff.json`
- 运行目录：`runs/<run-id>/`；每次使用新 id，不覆盖旧运行。
- Python 使用已有 Conda 环境；不得自行新建环境或安装依赖。

## 不变量

1. Research、Organization、Scheduler、Read、Finalizer 的科研判断必须由真实子智能体完成；
   Lead 只与用户交互、派发、续接和推进。
2. `codex.py next` 返回 `dispatch.action`：`spawn` 才创建子智能体；`followup` 必须唤醒
   `host_session_id`。spawn 后立即用 `codex.py bind --order ...` 登记真实 child id。
3. Research、Organization、Scheduler、Finalizer 每个 run 各有一个固定会话；Read 按
   Scheduler 的 reader group 固定会话。不得逐工单新建同一角色。
4. 子智能体只返回一个 JSON：`{"tool_calls": [...]}` 或 `{"payload": {...}}`。
   工具执行、写库、校验和状态迁移只由引擎完成。
5. `awaiting_external_agent` 是等待工单，不是失败。跨聊天无法续接旧 child 时，必须登记
   replacement 原因；磁盘恢复不等于旧模型隐状态仍存在。
6. 禁止手改 `state.json`、events、trace 或知识库；不得跳过关系审阅、伪造引用或产物。
   阅读队列耗尽不等于研究完成；只有 `audit=pass` 后才能落地 writing。

详细循环读取 `.agents/skills/researchos-workflow/SKILL.md`；角色工单已经携带实际 prompt、
messages、tools 和 attachments，不要重复加载全部角色文档。
