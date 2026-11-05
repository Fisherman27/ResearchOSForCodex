---
name: researchos-recovery
description: 恢复或验收一次中断的 ResearchOS 运行：从磁盘状态重建进度，只补跑缺失阶段，并核对真实委派证据。运行中断、换会话或需要验收时使用。
---

# 恢复与验收

## 恢复

1. 读 `runs/<id>/state.json` 与 `runs/<id>-literature/state.json`。
2. 跑 `python tools/check.py --run-id <id>`，确认状态与产物的一致性问题。
3. 跑 `python tools/codex.py --run-id <id> next`：
   - 有 jobs：按返回的 `dispatch.action` 继续；
   - 无工单但状态不是终态：读 `events.jsonl` 最后若干条，
     以及 `handoff/outbox` 是否残留未回答工单；
   - 状态 `awaiting_external_agent` 且无工单：属于外部前提缺失，报告缺口。
4. **不要把「文件存在」当作阶段通过**。终段以
   `state.final_artifacts[stage]` 与 `artifacts/<stage>.json` 同时存在为准。
5. `next` 已给出登记的真实 Codex child id，应优先续接。
   只有宿主明确报告该 id 在当前聊天不可访问时，才运行 `replace --reason ...` 后派生新实例；
   如实说明这是逻辑会话恢复，不是原模型上下文复活。

## 验收

检查并汇报：

| 检查 | 合格标准 |
|---|---|
| 委派 | `events.jsonl` 有 `handoff_required`；`agents/<id>/trace.jsonl` 存在 |
| 阶段 | 8 个终段齐全，`audit.verdict == "pass"` |
| 关卡 | 文献闭环经过 `relation_review`，且 `review_notes` 非空 |
| 契约 | `tools/check.py` 通过 |
| 恢复 | 新会话能仅凭磁盘状态继续 |

仅凭产物文件无法确认「真实委派」时，明确说明这一点。
