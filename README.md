# ResearchOS｜在 Codex 中运行的多智能体科研工作流

首次从 GitHub 安装、配置 Codex 插件或了解完整产物结构，请先阅读
[GUIDE.md](GUIDE.md)。

一套**Lead 协调 + 多个固定真实子智能体**的科研工作流：Codex 主会话就是 Lead，
Research 负责检索，Organization 拥有知识库，Scheduler 决定读什么并分配 Reader 组，
Read 只读被指派的论文，固定 Finalizer 会话完成八个终段。没有重复的 Director 子智能体。

```text
Codex（宿主 / Lead）          思考：按角色唤醒固定子智能体
engine/researchos             执行：工具调用、契约校验、状态机、关卡、审计链
```

引擎不调用任何模型，也不需要 API Key：Codex 自己就是智能体。

## 目录

```text
AGENTS.md                     项目规则（真实委派、边界、不可绕过的门槛）
.codex/config.toml            子智能体并发配置
.codex/agents/*.toml          research / organization / scheduler / read / finalizer
.agents/skills/*/SKILL.md     编排流程 + 每个角色的操作手册
config/
  handoff.json                默认：交接模式 + 真实 bundle 后端
  demo.json                   离线演示：bundle 后端 + inputs/demo 语料
  default.json / offline.json 进程内规则模式（无外部回合，用于测试/消融）
docs/
  architecture.md             两层分工、拓扑、状态机、重读、ideation
  contracts.md                工单/回复/阅读结果/审阅/终段的完整契约
  quickstart.md               在 Codex 里的运行步骤与常见情况
engine/
  researchos/                 运行时、端口、Agent、工作流、配置
  prompts/                    每个 Agent 的 system prompt（含 ideation 策略）
  bundle/                     四个真实工具库（PaperSearch / Organization / Scheduler / Read）
inputs/demo/                  离线演示语料（虚构占位 PDF）
tools/handoff.py              工单驱动：init / orders / order / reply / resume / status
tools/sessions.py             逻辑会话 ↔ Codex 真实 child id 的持久化映射
tools/codex.py                Codex 首选入口：next / bind / submit 自动组合上述操作
tools/check.py                确定性一致性检查（产物与状态是否自洽）
scripts/workbench.py          Codex 插件式本地工作台启动器
workbench/                    只读 HTTP API 与动态前端（默认 8 秒轮询）
tests/                        引擎、恢复、会话生命周期与 Codex 适配测试
```

## 最快验证

```sh
python tools/codex.py --run-id demo-001 --config config/demo.json init \
  --question "评估演示语料能支持哪些结论" \
  --search-options "$(cat inputs/demo/search_options.json)"
python tools/codex.py --run-id demo-001 --config config/demo.json next
```

按 `dispatch.action` 创建或续接子智能体，然后提交回复，循环到终态：

```sh
python tools/codex.py --run-id demo-001 --config config/demo.json \
  submit --order <order-id> --file <reply.json>
python tools/check.py --run-id demo-001 --config config/demo.json
```

`config/demo.json` 不联网；语料是虚构占位，结论必须标为演示。
真实运行去掉 `--config config/demo.json` 即可（会走真实检索，需要网络与服务）。

打开 Codex 工作台：

```sh
python scripts/workbench.py --workspace .
```

工作台只读展示运行状态和产物，不替代 Lead，也不会在后台调用模型。插件清单位于
`.codex-plugin/plugin.json`。WuTong / Vibe Research 中适合 ResearchOS 的分层阅读、证据审核
与工作台交互能力已经融合进对应的 Prompt、工具和前端；项目没有引入第二套研究状态机。

## 通过 GitHub 移植

仓库只需要保存源码、Prompt、Skills、相对路径配置和离线演示输入。研究运行、知识库、
检索下载、Session 映射、日志、数据库、缓存与本机凭据均由 `.gitignore` 排除，不应提交。

克隆到另一台电脑后，从本目录启动 Codex 或在本目录执行命令即可；配置路径会根据当前
项目根解析，不依赖原电脑的用户名或安装位置。真实检索所需凭据使用环境变量提供；如果
需要 GROBID，请在新电脑单独启动服务，不要把本机服务状态或路径写入仓库。

提交前可检查实际会进入 Git 的文件：

```sh
git status --short --ignored -- System/ExampleForCodex
```

## 设计要点

- **委派即工具**：子智能体是父 Agent 的一个普通工具；父 Agent 拿不到子智能体的证据池。
- **Lead 与科研角色分离**：Lead 只推进和交互；科研判断由固定角色会话完成，越权工具调用
  返回 `E2_TOOL_UNKNOWN`，不是靠提示词约束。
- **两层持久化**：run、checkpoint 与逻辑身份可跨进程恢复；真实 Codex child 上下文仅在
  当前主聊天仍可访问时续接，跨聊天替换会留下审计记录。
- **暂停不是失败**：`awaiting_external_agent` 表示有工单待答，状态可随时恢复。
- **关卡不可绕过**：阅读队列耗尽 ≠ 完成；`audit=pass` 才能落地 writing。
- **决策与装配分离**：Scheduler 做判断，`next_reading_tasks` 只报告事实与装配任务。
- **重读定向且可审计**：缺口、预算、停滞检测与结果对比都落盘。

详见 [架构](docs/architecture.md)、[契约](docs/contracts.md)、[运行](docs/quickstart.md)。
