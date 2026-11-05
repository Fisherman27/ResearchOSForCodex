---
name: researchos-workflow
description: 用 Codex Lead 驱动 ResearchOS：取得下一批工单，按固定 Session 派发真实子智能体，提交回复并推进到终态。
---

# ResearchOS Codex 编排

Lead 使用 `$ROS/tools/codex.py`；`$ROS` 是本项目根目录。先读 `$ROS/AGENTS.md`。

## 1. 取得工作

```sh
python tools/codex.py --run-id <id> init --question "<研究问题>"
python tools/codex.py --run-id <id> next
```

每个 job 都包含 `dispatch`：

- `action=spawn`：按 `dispatch.skill` 创建真实子智能体；成功后立即执行
  `bind --order <order_id> --host-session-id <child-id>`。
- `action=followup`：用 `followup_task` 唤醒 `dispatch.host_session_id`。
- `kind=final_stage`：交给固定 Finalizer；首次使用时执行
  `bind --role finalizer --host-session-id <child-id>`。

Read 的 `dispatch.subtask` 已从 `reader_group_id` 自动解析；没有分组时退化为 paper/task id。
不要自行重新计算 Session。

## 2. 派发

把 `job.order_path` 交给 `dispatch.skill` 对应角色，让它读取文件并按其中 system prompt、
messages、tools、question 和绝对附件路径作答；不要把整张工单复制进 Lead 上下文。
子智能体只返回一个 JSON：

```json
{"tool_calls": [{"name": "...", "arguments": {}}]}
```

或 `{"payload": {...}}`。

Organization 的 curator/relation reviewer 共用 Organization 会话；Scheduler 的阅读决策和
研究方向共用 Scheduler 会话。旧 run 若出现 director 工单，仅按兼容 Skill 处理一次。

## 3. 提交并继续

把子智能体 JSON 保存到文件后：

```sh
python tools/codex.py --run-id <id> submit --order <order_id> --file <reply.json>
python tools/codex.py --run-id <id> submit-stage --stage <stage> --file <payload.json>
```

命令会自动校验、推进并返回下一批 jobs。重复“派发 → 提交”，直到 `terminal=true`。
没有回复但引擎明确可继续时使用 `advance`。

## 4. 恢复与收项

登记的 child id 应先尝试 `followup_task`。只有宿主明确报告不可访问，才用
`tools/sessions.py replace --reason ...` 后创建替代 Agent；如实说明这是 checkpoint 恢复。

完成后运行 `python tools/check.py --run-id <id>`。向用户报告 run-id、状态、Scheduler 关键
判断/研究方向、最终成果、八阶段产物、可视化/导出链接、校验结果和未解决项。
