# 在 Codex 中运行

## 1. 打开项目

把 `System/ExampleForCodex` 作为 Codex 的工作目录（项目根）。Codex 会读取：

| 文件 | 作用 |
|---|---|
| `AGENTS.md` | 项目规则：真实委派、暂停不是失败、不可绕过的边界 |
| `.codex/config.toml` | 子智能体并发上限 |
| `.codex/agents/*.toml` | `research` / `organization` / `scheduler` / `read` / `finalizer` 五类角色 |
| `.agents/skills/*/SKILL.md` | 编排流程 + 每个角色的操作手册 |

> Skills 的发现范围是**当前目录到仓库根**的祖先路径。如果你从 ResearchOS 根目录
> 启动，本项目内的 `.agents/skills/` 可能不会被自动发现——此时直接按绝对路径
> 读取 `SKILL.md`（`researchos-workflow` 里已经写明各角色该读哪个文件）。

## 2. 无需 API Key

Codex 自己就是智能体：工单里的 `system_prompt`、`messages`、`tools` 交给
子智能体（`spawn_agent`），子智能体只产出「请求什么工具」或「最终 payload」。
工具的**执行**在引擎进程内完成，不经过任何模型 API。

## 3. 一次离线演示（推荐先跑这个）

```sh
python tools/codex.py --run-id demo-001 --config config/demo.json init \
  --question "评估演示语料能支持哪些结论" \
  --search-options "$(cat inputs/demo/search_options.json)"
python tools/codex.py --run-id demo-001 --config config/demo.json next
# → 按 dispatch.action 派发；spawn 后用 bind 登记 child id
python tools/codex.py --run-id demo-001 --config config/demo.json \
  submit --order <order_id> --file <reply.json>
python tools/check.py --run-id demo-001 --config config/demo.json
```

`config/demo.json` 用 `bundle` 后端 + `inputs/demo` 语料，**不联网**。
语料是虚构的占位 PDF，只用于验证流程，结论必须标为演示。

## 4. 真实运行

```sh
python tools/codex.py --run-id <id> init --question "<研究问题>"
python tools/codex.py --run-id <id> next
```

此时 `config/handoff.json`（`bundle` 后端）会走真实检索（arXiv / OpenAlex /
Semantic Scholar / Crossref，GROBID 可选）。需要网络与相应服务；
凭证通过环境变量提供，**不要写进任何文件或日志**。

## 5. 宿主循环

```text
next → 按 dispatch.action 派发/续接 → submit → 自动推进并返回下一批 jobs
```

- `next` 自动解析逻辑 Session；`spawn` 后用 `codex.py bind --order ...` 保存 child id，
  `followup` 直接唤醒返回的 id；
- Read 按 Scheduler 的 `reader_groups` 复用会话；不同组可以并行；
- 同一 `agent_id` 同时最多一张工单；
- `submit` 会连续消费直到状态不再变化，然后直接返回下一批 jobs；
- 终态：`completed` / `blocked_research` / `failed`。

## 6. 换角色边界

`config/handoff.json` 的 `handoff_agents` 决定哪些角色需要外部回合：

```json
{"mode": "handoff", "handoff_agents": ["research", "scheduler", "read",
 "organization.curator", "organization.relation_reviewer"]}
```

配置中的角色走真实外部回合；确定性胶水、状态机与契约校验不消耗模型回合。

真实 child 上下文能否继续由 Codex 主聊天的生命周期决定。换聊天时磁盘 run 可以恢复，
但旧 child id 不保证可附着；确认不可访问后用 `tools/sessions.py ... replace` 留痕再重建。

## 7. 测试

```sh
python -m unittest discover -s tests -t . -q
```

覆盖：运行时契约、拓扑与工具隔离、循环保真、策略替换、交接协议、
真实 bundle 端口、重读规划、ideation。

## 8. 常见情况

| 现象 | 处理 |
|---|---|
| `status=awaiting_external_agent` 且无工单 | 外部前提缺失（PDF/检索凭证）；汇报缺口，不要伪造 |
| `pending_final_task` 存在 | 终段需要外部结果：回答 `submit_final_result` 工单 |
| `audit.verdict=revise` | 回到 methodology，重新提交该阶段 |
| `audit.verdict=blocked` | 停在 `blocked_research`，如实汇报 |
| 文献 `blocked_material` | 有未读论文缺 PDF，汇报缺哪篇 |
| ideation `failed` | 不影响文献阶段；议程缺失记为警告 |
