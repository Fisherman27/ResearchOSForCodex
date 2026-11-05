---
name: researchos
description: 打开 ResearchOS Codex 工作台，或启动、继续、恢复一个多智能体科研运行。
---

# ResearchOS for Codex

项目根目录记为 `$ROS`。主会话是 Lead，科研判断委派给固定角色 Session。

优先把当前 Codex 工作区中同时包含 `AGENTS.md`、`tools/codex.py` 和
`engine/researchos/` 的目录作为 `$ROS`。如果当前工作区不是 ResearchOS 项目，先明确
提示用户在 Codex 中打开 ResearchOS 项目目录；不要把运行产物写进其他项目，也不要假设
某台电脑上的固定绝对路径。

## 打开工作台

运行：

```sh
python "$ROS/scripts/workbench.py" --workspace "$ROS"
```

这是只读的本地工作台；它展示项目中的真实磁盘状态，不调用模型、不修改运行。
在 Codex 内可打开命令返回的本地 URL。若端口被其他工作区占用，改用 `--port`。

## 执行研究

遵循 `$ROS/AGENTS.md` 和 `$ROS/.agents/skills/researchos-workflow/SKILL.md`。使用
`python "$ROS/tools/codex.py" --run-id <id> ...`，不要手改状态文件。
