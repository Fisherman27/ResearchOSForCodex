# ResearchOS for Codex：安装与使用指南

本文介绍如何从 GitHub 获取 ResearchOS、把它装配到 Codex、启动一次研究、查看实时工作台、
恢复中断任务，以及最终会得到哪些可审计产物。

> ResearchOS 不通过 OpenAI API 另外调用模型。当前 Codex 对话充当 Lead，Research、
> Organization、Scheduler、Read 和 Finalizer 是 Codex 创建和管理的真实子智能体；
> Python 引擎只负责状态、工具、契约、证据和恢复。

## 1. 系统组成

| 角色 | 主要职责 | Session 规则 |
|---|---|---|
| Lead | 与研究者交互、启动与推进流程、检查状态、汇报成果 | 当前 Codex 主对话 |
| Research | 发散关键词、数据库检索、自主联网补充、去重，并从尚未收录的引用继续扩展语料 | 每个研究任务固定 |
| Organization | 管理知识库、写回阅读结果、关系审阅、导出证据 | 每个研究任务固定 |
| Scheduler | 判断读什么、是否重读、如何划分 Reader 组，以及何时结束文献循环 | 每个研究任务固定 |
| Read | 深读指定论文，返回带证据边界的 ReadingResult | 由 Scheduler 按论文关联性分组，可有多个 |
| Finalizer | 连续完成八个终段并接受契约校验 | 每个研究任务固定 |

完整流程：

```text
用户课题
  → Research：关键词发散、数据库检索、Agent 联网搜索、引用扩展、语料去重
  → Organization：初始化知识库
  → Scheduler：选择论文、划分 Reader 组
  → Read：阅读或定向重读
  → Organization：关系审阅
  → Scheduler：提出研究方向
  → Finalizer：八个终段
  → audit=pass
  → 最终报告与可视化产物
```

## 2. 环境要求

- Git；
- Codex 桌面端或带插件功能的 Codex CLI；
- 本地 Python 3；
- 真实论文检索需要网络；
- GROBID 是可选的 PDF 结构化解析服务，没有 GROBID 时系统可使用已有文本或其他可用解析路径。

不需要给 ResearchOS 配置模型 API Key。外部文献服务如果需要凭据，应通过环境变量提供，
不要把密钥写入仓库、配置文件或运行日志。

## 3. 从 GitHub 克隆

将下面的 `<GITHUB_REPOSITORY_URL>` 替换为实际仓库地址：

```sh
git clone <GITHUB_REPOSITORY_URL> ResearchOS
cd ResearchOS/System/ExampleForCodex
```

如果发布时 `ExampleForCodex` 本身就是仓库根目录，则使用：

```sh
git clone <GITHUB_REPOSITORY_URL> ResearchOS-for-Codex
cd ResearchOS-for-Codex
```

后文把这个目录记为 `$ROS`。它必须同时包含：

```text
AGENTS.md
.codex/config.toml
.codex/agents/
.agents/skills/
tools/codex.py
engine/researchos/
```

可以用以下命令快速确认：

```sh
test -f AGENTS.md \
  && test -f tools/codex.py \
  && test -d engine/researchos \
  && echo "ResearchOS project OK"
```

## 4. 推荐方式：作为 Codex 项目直接使用

这是最稳妥的方式，也适合开发和修改 ResearchOS。

1. 打开 Codex 桌面端；
2. 在左侧“项目”区域点击 `＋` 或 `Open project / 打开项目`；
3. 选择 `$ROS`，不要只选择它的上一级目录；
4. 如果 Codex 询问是否信任该目录，检查来源无误后选择信任；
5. 在该项目下新建对话。

必须直接打开 `$ROS` 的原因是：

- `AGENTS.md` 定义整个系统的协作规则；
- `.codex/agents/*.toml` 定义五类科研智能体；
- `.agents/skills/` 定义 Lead、恢复流程和角色操作手册；
- 相对路径配置会根据当前 ResearchOS 项目根解析。

只把上层大仓库作为项目打开时，Codex 不一定会自动发现嵌套在 `$ROS` 中的 Agent 配置。

## 5. 可选方式：安装成个人 Codex 插件

插件安装后，可以在 Codex 的插件列表中看到 ResearchOS，并通过 `@ResearchOS` 触发入口
Skill。正式执行研究时，仍建议打开 `$ROS` 项目，以确保角色配置完整加载。

### 5.1 安装位置

macOS/Linux 推荐：

```text
插件源：      ~/.codex/plugins/researchos/
Marketplace：~/.agents/plugins/marketplace.json
安装缓存：    ~/.codex/plugins/cache/researchos-local/researchos/<version>/
```

Windows 对应位置：

```text
%USERPROFILE%\.codex\plugins\researchos\
%USERPROFILE%\.agents\plugins\marketplace.json
```

### 5.2 清理旧版 ResearchOS Skills

先检查旧版全局 Skills：

```sh
find ~/.codex/skills -maxdepth 1 -type d -name 'researchos-*'
```

如果存在，建议先移动到备份目录，而不是直接删除：

```sh
mkdir -p ~/.codex/skills-backup/researchos
for path in ~/.codex/skills/researchos-*; do
  [ -d "$path" ] && mv "$path" ~/.codex/skills-backup/researchos/
done
```

新版插件已经包含所需入口 Skill；角色 Skills 由项目中的 `.agents/skills/` 提供。保留旧版
全局 Skills 容易造成同名 Skill 重复或加载到旧提示词。

### 5.3 复制插件

从 `$ROS` 的上一级执行，或把 `$ROS` 替换成绝对路径：

```sh
mkdir -p ~/.codex/plugins
cp -R "$ROS" ~/.codex/plugins/researchos
```

不要复制 `runs/`、`engine/runs/`、`work/`、`engine/work/`、缓存或本机密钥。仓库自带的
`.gitignore` 已经排除了这些内容。

### 5.4 注册个人 Marketplace

创建 `~/.agents/plugins/marketplace.json`。如果该文件已经包含其他插件，请把下面的插件
对象合并到原来的 `plugins` 数组，不要覆盖其他条目。

```json
{
  "name": "researchos-local",
  "interface": {
    "displayName": "ResearchOS Local"
  },
  "plugins": [
    {
      "name": "researchos",
      "source": {
        "source": "local",
        "path": "./.codex/plugins/researchos"
      },
      "policy": {
        "installation": "AVAILABLE",
        "authentication": "ON_INSTALL"
      },
      "category": "Productivity"
    }
  ]
}
```

然后安装：

```sh
codex plugin marketplace list
codex plugin add researchos@researchos-local
codex plugin list
```

成功时应看到：

```text
pluginId: researchos@researchos-local
installed: true
enabled: true
```

根据 OpenAI 官方插件说明，本地 Marketplace 或插件更新后应完整退出并重启桌面应用，
首次安装后在新对话中测试：

- <https://developers.openai.com/plugins/build/plugins>
- <https://developers.openai.com/plugins/deploy/connect-chatgpt>

## 6. 在 Codex 中确认安装

重启 Codex 后：

1. 打开 `$ROS` 项目；
2. 新建一个对话；
3. 在输入框输入 `/plugins`；
4. 选择 `ResearchOS Local`；
5. 确认 `ResearchOS` 显示为 `Installed / 已安装`。

也可以在输入框输入 `@`，查看是否出现 `ResearchOS`。如果插件没有出现：

```sh
codex plugin marketplace list
codex plugin list --available
codex plugin list
```

如果项目级 Agent 没有生效，首先确认当前 Codex 项目就是 `$ROS`，而不是父目录或插件缓存
目录。

## 7. 第一次运行：建议先做离线演示

离线演示使用两篇虚构占位论文，不联网，适合检查工单、Session 和状态机。

在 `$ROS` 项目的 Codex 对话里输入：

```text
使用 ResearchOS 的离线演示语料，研究“演示语料能够支持哪些结论”。
请启动完整流程，并在每个真实角色 Session 建立后继续推进。
```

也可以手动初始化：

```sh
python tools/codex.py --run-id demo-001 --config config/demo.json init \
  --question "评估演示语料能支持哪些结论" \
  --search-options "$(cat inputs/demo/search_options.json)"
```

然后查看下一批工作：

```sh
python tools/codex.py --run-id demo-001 --config config/demo.json next
```

正常情况下会返回 JSON，并包含：

- `status=awaiting_external_agent`；
- `jobs[]`；
- `order_id`；
- `agent_id`；
- `dispatch.action=spawn` 或 `followup`；
- `logical_session_id`；
- 工单文件路径。

在正常 Codex 使用中，这些底层命令由 Lead 操作，用户不需要逐张提交工单。

## 8. 启动真实研究

在 `$ROS` 项目中新建对话，输入研究主题、已知关键词、范围、期望规模和输出要求。例如：

```text
使用 ResearchOS 完整研究“AI 系统是否具备类人的认知能力”。

初始关键词：
- artificial intelligence cognition
- cognitive architecture
- reasoning
- memory
- metacognition
- embodied cognition

请让 Research 先合理发散关键词，并根据已收录论文中尚未收录的引用继续扩展语料。
如果宿主允许联网，Research 还会用少量高区分度查询自主搜索最新论文，以 arXiv、DOI/
出版社、会议或作者主页等一手页面核验候选，再作为补充通道与数据库结果合并。联网候选会
记录发现查询和来源，并按 DOI、arXiv ID、规范化标题/年份去重；联网失败不阻断原检索。
Scheduler 根据论文关联性决定 Reader Session 分组。持续推进到最终成果；只有在缺少必要
材料、需要我作研究边界选择或出现不可恢复问题时再询问我。
```

如果已安装插件，也可以先在输入框输入 `@ResearchOS`，再发送上述任务。

建议明确告诉 Lead：

- 研究问题和边界；
- 初始关键词，而不是完整检索式；
- 语言、时间范围、学科范围；
- 期望收集或深读的论文数量；
- 是否必须读取 PDF；
- 是否允许使用预印本；
- 需要何种最终报告或实验设计。

## 9. 运行时会看到什么

### 9.1 Codex 对话

Lead 会返回宏观进度，例如：

- 已创建的 run ID；
- Research 正在检索或语料已经落盘；
- Scheduler 选择了哪些论文；
- 哪些 Reader 组正在工作；
- 完成了多少阅读；
- 是否进入关系审阅；
- 当前终段；
- 是否需要用户补充材料或选择研究边界。

`awaiting_external_agent` 和 `awaiting_stage_agent` 通常表示正在等待真实智能体结果，不等于
运行失败。

### 9.2 固定角色 Session

首次调用某个角色时，Lead 会创建真实子智能体并记录宿主 Session ID。以后再次需要该角色
时，Lead 会续接原 Session。Read 由 Scheduler 分组，因此可能同时出现多个 Reader Session。

Session 映射会持久化到：

```text
engine/runs/<run-id>/sessions/registry.json
```

如果切换 Codex 对话后旧子智能体无法再附着，系统可以从磁盘 checkpoint 恢复，但会记录
Session 替换，而不是伪装成原会话仍然存在。

### 9.3 ResearchOS 工作台

在对话中输入：

```text
打开 ResearchOS 工作台
```

或者手动启动：

```sh
python scripts/workbench.py --workspace .
```

命令会返回类似：

```text
http://127.0.0.1:8767
```

点击链接即可查看。工作台是只读界面，默认约每 8 秒刷新，展示：

- 顶部明确显示当前读取的 ResearchOS 工作目录完整路径；
- 点击“切换目录”可从自动发现的相邻项目中选择，也可以直接输入另一个项目根目录；
- 切换后会清除旧选择，并重新读取新目录中的运行列表、状态和产物；
- 当前运行状态和状态新鲜度；
- 收录语料数、已读论文数、调度轮次；
- 当前阶段和下一步；
- Agent 与持久 Session 状态；
- 已读论文标题；
- Scheduler 决策，以及专门的“文献综述”“研究缺口分析”窗口；
- 关系审阅数量；
- 研究方向；
- 用“哪个 Agent 完成了什么工作”表述的最近动态；
- 醒目的“打开论文与论点关系图”按钮，以及其他成果文件。

如果 `8767` 端口上已经运行着兼容的 ResearchOS 工作台，再用另一个 `--workspace`
启动时，启动器会让现有工作台切换到新目录，不需要手动关闭旧服务。工作台只接受有效的
ResearchOS 项目根目录或确实包含运行记录的目录，避免误选普通文件夹。

如果 Codex 没有自动打开页面，复制命令输出的 `http://127.0.0.1:8767` 到浏览器即可。
若该端口不可用，显式换一个端口：

```sh
python scripts/workbench.py --workspace . --port 8768
```

然后打开 `http://127.0.0.1:8768`。页面打开后先核对顶部“当前读取目录”；不正确时点击
“切换目录”选择项目根目录。不要把 `engine/`、单个 `runs/` 或某次 run 目录当项目根目录。

如果磁盘状态超过两分钟没有更新，工作台会显示“状态待确认”，而不会假装后台仍在持续
运行。

## 10. 运行目录与数据结构

默认 `config/handoff.json` 会把运行写入：

```text
engine/runs/<run-id>/
engine/runs/<run-id>-literature/
```

主运行目录保存总流程和终段，`-literature` 目录保存阅读循环、Scheduler 决策、关系审阅和
研究方向。工作台会把两者合并成一个运行视图。

典型结构：

```text
engine/runs/<run-id>/
├── state.json                    # 主运行状态
├── events.jsonl                  # 追加式事件日志
├── sessions/registry.json        # 逻辑角色与 Codex Session 映射
├── handoff/
│   ├── outbox/                   # 待回答工单
│   ├── inbox/                    # 智能体回复
│   └── done/                     # 已消费工单
├── kb/
│   ├── index.sqlite              # 知识库索引
│   └── raw/                      # 论文、PDF 和结构化材料
├── artifacts/                    # 八个终段 JSON
├── results/                      # 其他运行结果
└── viz/                          # HTML/JSON 可视化

engine/runs/<run-id>-literature/
├── state.json                    # 阅读循环状态
├── events.jsonl
├── results/                      # ReadingResult 等结果
└── ideation/                     # Scheduler 研究方向
```

这些目录都已被 `.gitignore` 排除，不应提交到 GitHub。

## 11. 最终会得到什么

### 11.1 检索和语料产物

- 扩展后的查询计划；
- 来源和检索轨迹；
- 去重后的论文语料；
- PDF 或全文可用性信息；
- 引用关系和引用扩展记录；
- 可复查的检索运行目录。

### 11.2 阅读与知识库产物

- 每篇已读论文的 ReadingResult；
- 研究问题、方法、核心论点、关键结果和局限；
- 证据引用与未验证项；
- 定向重读记录；
- 论文之间的支持、冲突、补充或边界关系；
- Organization 知识库和关系审阅记录。

### 11.3 Scheduler 产物

- 一份可直接阅读的自然语言文献综述正文：按主题与方法演进综合全部主要研究路线，说明机制、
  假设、证据条件、共识、真实分歧、局限和未知，而不是逐篇摘要列表；
- 每轮阅读决策；
- Reader 分组依据；
- 停止、继续或重读的理由；
- 文献闭环后的研究机会；
- 可检验的研究方向、机制假设和验证计划；
- 被拒绝的研究方向及其理由。

### 11.4 八个终段产物

位于：

```text
engine/runs/<run-id>/artifacts/
```

| 文件 | 内容 |
|---|---|
| `synthesis.json` | 共识、分歧、未知问题与证据综合 |
| `gap_analysis.json` | 有证据边界的研究缺口 |
| `hypothesis.json` | 可证伪假设与证伪条件 |
| `methodology.json` | 研究设计、输入、步骤、评估与风险 |
| `execution.json` | 实际执行动作、真实产物和失败记录 |
| `analysis.json` | 结果、不确定性、稳健性和证据引用 |
| `audit.json` | 独立审核，结论为 `pass`、`revise` 或 `blocked` |
| `writing.json` | 最终标题、摘要、发现、局限、参考文献与证据追踪 |

只有 `audit.verdict=pass` 后，`writing.json` 才能作为最终成果落盘。如果审核要求修订，系统
会回到相应阶段，不会伪造完成状态。

### 11.5 可视化产物

通常位于：

```text
engine/runs/<run-id>/viz/
```

可能包含：

- 自包含 HTML 论文索引图；
- 对应的 JSON 图数据；
- 论文节点、引用边和阅读状态；
- 知识关系或其他导出文件。

新版关系图遵守以下可视化规则：

- 时间从上到下由早到晚，中心主轴贯穿全部年份；越新的论文分布越靠下且树冠逐渐展开；
- 左侧垂直进度条用于放大后沿时间轴上下移动；滚轮仍负责缩放；
- 论点节点与所属论文保持可见关系，选择论点时必须同步高亮对应论文；
- “时间演示”按年份依次显示论文、论点和已出现节点之间的关系；
- 论文关系、原始引用、论点关系和候选关系使用不同颜色，候选不得伪装成已确认事实；
- HTML 必须自包含，JSON 数据仍单独导出，便于复现和重新渲染。

可视化是否反映最终结论，取决于它的生成时间。工作台会提供文件链接，但研究者仍应查看
时间戳和说明，避免把“语料索引图”误认为“研究结论图”。

## 12. 如何继续或恢复中断运行

在同一个 `$ROS` 项目里输入：

```text
使用 ResearchOS 检查并恢复最近一次未完成的运行。先读取磁盘状态和待处理工单，不要重新
开始；继续推进到终态。
```

指定 run ID：

```text
继续 ResearchOS 运行 <run-id>。核对主运行、literature 子运行、Session registry 和工单，
只补跑缺失步骤。
```

手动查看：

```sh
python tools/codex.py --run-id <run-id> next
python tools/check.py --run-id <run-id>
```

恢复原则：

- 磁盘状态是事实来源；
- 已提交并通过契约的结果不重复生成；
- 优先续接已登记的角色 Session；
- 只有确认旧 Session 不可访问时才创建替代 Session；
- 不手改 `state.json` 伪造完成；
- 不绕过关系审阅、audit 或其他关卡。

## 13. 完成后的验收

运行：

```sh
python tools/check.py --run-id <run-id>
```

验收会检查：

- 状态文件是否可读且状态合法；
- 八个终段是否满足字段契约；
- 状态中的产物和磁盘文件是否一致；
- `writing` 是否确实在 `audit=pass` 后生成；
- 文献循环是否完成关系审阅；
- 待处理工单与回复是否平衡。

Lead 最终给用户的汇报应至少包含：

- run ID 和终态；
- 收集与深读的论文数量；
- Scheduler 的关键判断和研究方向；
- 核心研究结论及其证据边界；
- 八个终段产物路径；
- 可视化或导出文件链接；
- 一致性检查结果；
- 尚未解决的问题和材料缺口。

## 14. 更新插件

如果只是从 GitHub 更新项目源码：

```sh
cd "$ROS"
git pull
```

如果还安装了个人插件，需要把更新后的源码同步到插件源，然后重新安装或刷新缓存：

```sh
cp -R "$ROS" ~/.codex/plugins/researchos-new
```

确认新副本完整后，再替换旧插件源。不要直接覆盖一个正在运行的研究目录，也不要把旧
`runs/` 和 `work/` 复制进新版本。更新后完整退出并重启 Codex，在新对话中测试。

开发者更推荐直接把 Git clone 的 `$ROS` 作为 Codex 项目使用；这样修改 Prompt、Skills 或
引擎后不需要再编辑插件缓存。

## 15. 常见问题

### `/plugins` 里没有 ResearchOS

检查：

```sh
codex plugin marketplace list
codex plugin list --available
codex plugin list
```

确认 `~/.agents/plugins/marketplace.json` 是合法 JSON，`source.path` 指向真实存在的插件目录，
然后完整退出并重启 Codex。

### Codex 没有创建 Research、Scheduler 等角色

通常是因为当前打开的项目不是 `$ROS`。确认项目根能看到 `.codex/agents/`、
`.agents/skills/` 和 `AGENTS.md`，然后新建对话。

### 工作台没有更新

先确认后端状态文件是否在变化：

```sh
ls -lt engine/runs/<run-id>/state.json \
       engine/runs/<run-id>-literature/state.json
```

工作台只是读取磁盘。如果 Codex 对话还在工作但状态文件长期不更新，应检查当前工单、
引擎活动记录和后端错误，而不是只刷新浏览器。

如果页面打开的是另一个项目，点击顶部“切换目录”，或重新执行：

```sh
python scripts/workbench.py --workspace "$PWD"
```

启动器会让已经占用同一端口的兼容工作台切换到当前项目。

### 运行显示等待智能体

`awaiting_external_agent` 表示已经发布工单并在等待角色作答，通常不是错误。让 Lead 检查
outbox、inbox 和 Session registry，然后续接对应角色。

### PDF 无法解析

确认 PDF 实际存在且路径可访问。GROBID 可提升结构化解析质量，但不是模型“凭空读取本地
路径”的替代品；Agent 必须能访问文件内容或通过可用解析工具获得文本。

### 可以把运行目录提交到 GitHub 吗

不建议。运行目录可能很大，还可能包含 PDF、本机路径、Session ID 和检索缓存。项目的
`.gitignore` 已排除这些目录。需要共享一次研究时，应单独导出经过审核的成果、证据包和
必要的可视化文件。

## 16. 最简使用清单

```text
1. git clone
2. 直接把 ExampleForCodex 目录作为 Codex 项目打开
3. 信任项目并新建对话
4. 输入课题、初始关键词、范围和数量要求
5. 让 ResearchOS 持续推进
6. 输入“打开 ResearchOS 工作台”查看状态
7. 中断后按 run ID 恢复，不重新开始
8. 完成后运行 tools/check.py
9. 查看 artifacts/writing.json、Scheduler 研究方向和 viz/
```

