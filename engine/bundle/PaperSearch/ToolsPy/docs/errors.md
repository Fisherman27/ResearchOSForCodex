# ToolsPy 错误处理机制

> 目标：**Agent 拿到错误后能自己判断下一步做什么，不用来问人。**

---

## 1. 两个核心结构

### 1.1 返回信封（`result.py`）

所有工具返回同一个结构，**从不抛异常、从不返回裸 None**：

```jsonc
{
  "ok":       true,
  "status":   "partial",        // ok | partial | empty | blocked | failed
  "data":     { ... },
  "error":    null,             // 失败时是下面的错误字典
  "warnings": [ {...} ],        // 非致命问题
  "diagnostics": { "elapsed_sec": 2.1, "steps": [...], "n_results": 12 },
  "meta":     { "tool": "E10.search_arxiv", "params": {...}, "at": "..." }
}
```

### 1.2 错误字典（`errors.py`）

```jsonc
{
  "code":      "NET_TIMEOUT",
  "message":   "请求超时（第 2 次尝试）",
  "severity":  "degradable",       // input | degradable | fatal
  "retryable": true,
  "hint":      "请求超时，可重试。若反复超时，调大 timeout 或换源。",
  "stage":     "fetch_page",       // 多步工具：是第几步挂的
  "component": "E10",              // 哪个工具 → find_symbol 可跳文档
  "context":   { "url": "...", "attempt": 2 },
  "cause":     { "type": "TimeoutError", "message": "...", "traceback": "..." },
  "at":        "2026-10-08T22:30:00"
}
```

---

## 2. status 的语义边界

**这是本库最重要的约定**，也是自检重点守护的对象。

| status | ok | 含义 | Agent 该做什么 |
|---|---|---|---|
| `ok` | ✅ | 全部成功 | 直接用 |
| `partial` | ✅ | 拿到**部分**结果且有明确原因 | **结果可用**；看 `diagnostics.steps` 决定是否补救 |
| `empty` | ✅ | 成功执行但结果为空 | **空集不是错误**；放宽条件或接受 |
| `blocked` | ❌ | 缺前提（无 key / 服务没起） | 按 `hint` 补前提；**重试无用** |
| `failed` | ❌ | 执行出错 | 看 `code` + `retryable` 决定重试/上报 |

### 为什么 `empty` 要单独一类

传统代码里 `[]` / `None` 既表示"没有"也表示"失败"，下游分不清
**"这个领域真的没论文"** 和 **"我的请求挂了"**。

实测踩过的坑：HTTP 200 + HTML 错误页被当成"成功的空集"，
整个流程报 `done` —— **失败伪装成功**是最难发现的一类 bug。

区分之后：
```python
r = tools.search_all_sources("某个很偏的题目")
if r["status"] == "empty":
    # 请求是成功的，只是没结果 → 放宽查询词，或接受
elif r["status"] == "failed":
    # 请求本身有问题 → 看 error.code
```

### 为什么 `partial` 的 ok 是 `True`

多源检索里单源失败是**常态**（限流、缺凭证）。如果判为失败，
调用方会丢弃已经拿到的 50 篇 —— 那才是真正的损失。

```python
r = tools.search_all_sources("deep cfr")
# 返回 partial：arXiv 拿到 50 篇，OpenAlex 限流了
if result.is_usable(r):        # True
    papers = r["data"]["papers"]        # 50 篇照常用
    print(r["diagnostics"]["source_status"])   # {'arxiv': 'ok', 'openalex': 'failed'}
```

---

## 3. Agent 的决策树

拿到返回值后按这个顺序判断：

```
┌─ r["status"] 是什么？
│
├─ ok ────────────→ 直接用 r["data"]
│
├─ partial ───────→ 结果可用（r["ok"] == True）
│                   看 r["diagnostics"]["failed_steps"] 缺了什么
│                   能接受就继续；不能接受再补救
│
├─ empty ─────────→ 请求成功但没结果
│                   看 r["warnings"][0]["message"] 了解为什么空
│                   判断该放宽条件还是接受
│
├─ blocked ───────→ 缺前提，重试无用
│                   r["error"]["hint"] 里有可执行动作
│                   例：'在 Keys.txt 里补上 S2_API_KEY'
│                        'cd PaperSearch/ExamplePy && ./grobid-serve.sh start'
│
└─ failed ────────→ 看 r["error"]：
                    ├─ retryable=True  → 可重试（但要等，见 hint）
                    ├─ severity=input  → 我传错了参数，改参数
                    ├─ severity=degradable → 可降级继续（跳过这一步）
                    ├─ severity=fatal  → 做不成，需人工介入
                    └─ code=INTERNAL_* → **是本库的 bug**，
                                         把 error.cause + context 反馈
```

---

## 4. 错误码表（按域分组）

命名规范：`<域>_<情形>`。域让 Agent 能先分类再决策。

| 域 | 含义 | 通用策略 |
|---|---|---|
| `INPUT_` | 调用方参数问题 | 改参数，重试无用 |
| `AUTH_` | 凭证问题 | 补 key，重试无用 |
| `NET_` | 网络/传输 | 通常可重试 |
| `RATE_` | 限流 | 可重试但**必须等待** |
| `NOTFOUND_` | 资源不存在 | 重试无用，换路径 |
| `PARSE_` | 解析失败 | 通常不可重试 |
| `SERVICE_` | 外部服务 | 服务恢复后可重试 |
| `DATA_` | 数据质量 | 可降级继续 |
| `INTERNAL_` | 本库 bug | 上报 |

完整 28 个码及各自的 `retryable` / `hint` 见 [`API.md`](API.md) 的「四、错误码表」。

### 几个容易混淆的码

| 码 | 什么时候用 | 与谁区分 |
|---|---|---|
| `NOTFOUND_EMPTY_RESULT` | **成功**的空集 | vs `NET_BAD_RESPONSE`（请求失败） |
| `NET_BAD_RESPONSE` | HTTP 通了但内容是垃圾（HTML 错误页） | vs `NOTFOUND_EMPTY_RESULT`（真的没结果） |
| `NOTFOUND_PDF` | 论文闭源，拿不到全文 | vs `PARSE_PDF_INVALID`（拿到了但文件坏） |
| `SERVICE_DEGRADED` | 服务可用但功能降级 | vs `SERVICE_UNAVAILABLE`（服务没起） |
| `DATA_LOW_QUALITY` | 数据可疑（如坐标只识别到图注） | vs `PARSE_EMPTY`（完全没内容） |

---

## 5. 异常映射

工具内部统一用 `result.from_exception(exc, ...)` 兜底，所以
**异常永远不会以裸 traceback 形式泄漏给调用方**（裸 traceback 对 Agent 没用 ——
它无法据此分支，只能把原文转给人看）。

映射规则（`errors._EXC_MAP`，按类名逐级匹配）：

| 异常 | → 错误码 |
|---|---|
| `TimeoutError` / `socket.timeout` | `NET_TIMEOUT` |
| `urllib.error.URLError` / `ConnectionError` | `NET_UNREACHABLE` |
| `HTTPError` | `NET_HTTP_ERROR` |
| `json.JSONDecodeError` | `NET_BAD_RESPONSE` |
| `ParseError`（XML） | `PARSE_TEI_INVALID` |
| `FileNotFoundError` | `INPUT_NOT_FOUND` |
| `KeyError` | `INPUT_MISSING` |
| `ValueError` | `INPUT_INVALID` |
| 其他未见过的 | `INTERNAL_ERROR`（**不伪装成网络问题**） |

`INTERNAL_ERROR` 的含义是"**这大概率是本库的 bug**" —— 这个判断很重要：
把本库 bug 误报成"网络问题"会让 Agent 反复重试一个永远不会成功的东西。

---

## 6. 定位问题（Agent 与人都适用）

### 6.1 先跑 `doctor()`

大多数"工具不工作"是**环境问题**，不是代码问题：

```python
r = tools.doctor()
```
```jsonc
{
  "dependencies": {"ok": true, ...},
  "credentials":  {"s2": false, ...},          // 只返回布尔，**不含值**
  "grobid":       {"available": true, "version": "0.9.1"},
  "sources":      {"arxiv": {"usable": true}, "semantic_scholar": {"usable": false}},
  "next_actions": [                             // ← 可执行的补救动作
    {"action": "add_key", "key": "S2_API_KEY",
     "command": "编辑 PaperSearch/Keys.txt"}
  ]
}
```

### 6.2 用 `component` 跳到文档

```python
r = tools.parse_pdf("bad.pdf")
if not r["ok"]:
    comp = r["error"]["component"]        # 'E41'
    loc = registry.find_symbol(comp)      # → API.md 的行号区间
    print(registry.read_symbol(comp)["data"]["markdown"])   # 只读那一段
```

### 6.3 多步工具看 `diagnostics.steps`

```python
r = tools.full_pipeline("deep cfr", out_dir="/tmp/kb")
print(tools.summarize_result(r))
```
```
[partial] P30.full_pipeline 耗时 74.6s ｜ 5 步
  ~ search       partial  6 项
  ✓ expand       ok       35 项
  ✓ fetch_pdf    ok       2 项
  ✓ parse        ok       2 项
  ✓ persist      ok       2 项
⚠ 源 semantic_scholar 未成功，结果来自其余源
```

哪一步慢、哪一步空、哪一步失败，一眼可见。

### 6.4 复现

流水线产物目录里有 `run.json`：**参数快照 + 逐步状态**。复现一次运行只需要它。

---

## 7. 写新工具时的要求

1. **永远返回信封**，不要 `return None` 或抛异常给调用方；
2. **`except` 分支统一写** `return R.from_exception(exc, tool=..., component=..., stage=...)`；
3. **先校验参数，再检查前提** —— 先告诉调用方"你传错了"，
   再告诉他"环境缺东西"（前者他能立刻改；反过来会让他去补一个不需要的 key）；
4. **空结果用 `empty` 而不是 `failed`** —— 并在 `reason` 里写清为什么空；
5. **部分结果用 `partial`** —— 别因为一个可降级步骤失败就丢掉已有结果；
6. **错误要带 `context`** —— Agent 靠它判断"我传了什么导致这样"；
7. **`hint` 写成可执行的动作**，不要写"检查网络"这种废话；
8. 加了新错误码就更新 `errors.CODES`，然后重跑 `gen_api_md.py`。

---

## 8. 自检守护的契约

`selfcheck.py`（28 项，离线）专门守住上面这些约定中最容易被"顺手改坏"的部分：

| 测试 | 防的是什么 |
|---|---|
| `test_empty_is_not_failure` | 有人觉得"没结果就是失败"，把 empty 的 ok 改 False |
| `test_partial_is_usable` | 有人把 partial 判成失败，导致调用方丢弃已有结果 |
| `test_ok_with_empty_data_degrades_to_empty` | 空结果被当成有结果 |
| `test_all_codes_have_required_fields` | 新增错误码时漏了 hint/retryable |
| `test_unknown_exception_is_internal_error` | 本库 bug 被伪装成网络问题 |
| `test_traceback_is_preserved` | 丢掉 traceback，Agent 无法定位 |
| `test_empty_query_rejected` | 参数校验被放到前提检查之后 |
| `test_api_md_in_sync_with_code` | 文档与实现脱节（Agent 依赖文档） |

```bash
python selfcheck.py     # 28 项，全部离线
```
