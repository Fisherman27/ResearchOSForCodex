# ToolsPy —— 论文检索工具代码库

> 把 `PaperSearch` 里已完成的工作（多源检索 / PDF 获取 / GROBID 结构化解析）
> 组织成**可被 Agent 稳定调用**的工具库。
> 元件可精细调整，流水线可一条命令跑完，错误反馈足以让 Agent 自行定位问题。

**位置**：`PaperSearch/ToolsPy/` ｜ **依赖**：Python 3.8+ 标准库，无需 pip 安装
**底层实现**：复用 `PaperSearch/ExamplePy`（数据源）与 `ProcessPy`（编排），**未重写**

---

## 1. 五分钟上手

```python
import sys
sys.path.insert(0, "PaperSearch/ToolsPy")  # 让 papersearch 包可导入

import papersearch as tools                # 统一入口（推荐）
from papersearch import registry, result

# ① 任何工具报错前，先自检环境
print(result.pretty(tools.doctor()))

# ② 看有哪些工具（含一句话说明与使用时机）
registry.list_tools()

# ③ 只读某个工具的文档（而不是整份 API.md）
loc = registry.find_symbol("E10")["data"]
print(loc["line_start"], loc["line_end"], loc["read_hint"])

# ④ 检索
r = tools.search_all_sources("counterfactual regret minimization", max_per_source=10)
if result.is_usable(r):
    print(len(r["data"]["papers"]), "篇")

# ⑤ 一条命令跑完整流程
r = tools.full_pipeline("deep cfr", out_dir="/tmp/kb", fetch_top=5)
print(result.summarize_result(r))
```

命令行也可用：

```bash
cd PaperSearch/ToolsPy
PY="${PYTHON:-python}"
$PY cli.py doctor
$PY cli.py list
$PY cli.py doc E41                 # 打印 E41 的文档（含 API.md 行号）
$PY cli.py search "deep cfr" --top 10
$PY cli.py run "deep cfr" --out /tmp/kb --fetch-top 3
```

---

## 2. 文档导航

| 文件 | 作用 | 什么时候看 |
|---|---|---|
| **本文件（README.md）** | 总览：有哪些工具、怎么组织、设计约定 | 第一次接触 |
| [`API.md`](API.md) | **详细 API 文档**（自动生成，含参数/返回/状态/示例/错误码） | 要用某个工具时（**别整份读**，用 `find_symbol` 定位） |
| [`COORDINATES.md`](../ExamplePy/COORDINATES.md) | PDF 坐标提取的实现原理与数据位置 | 要处理图纸/高亮回链时 |
| [`docs/errors.md`](docs/errors.md) | 错误处理机制详解 | 要理解/扩展错误码时 |

**怎么查一个工具**（省 context 的正确姿势）：

```python
from papersearch import registry
registry.list_tools()                  # 先看全貌
registry.find_symbol("E41")            # 拿到行号区间
registry.read_symbol("E41")            # 或直接取该段 markdown
```

---

## 3. 工具总览

### 元件工具（原子能力，19 个）

| 编号 | 工具 | 一句话 |
|---|---|---|
| `E00` | `doctor` | 环境自检：依赖/凭证/GROBID/各源 |
| `E01` | `list_sources` | 数据源与能力矩阵 |
| `E10` | `search_arxiv` | arXiv 检索 |
| `E11` | `search_openalex` | OpenAlex 检索（主索引） |
| `E12` | `search_s2` | Semantic Scholar 检索（需 key） |
| `E13` | `search_all_sources` | 多源检索 + 去重 |
| `E20` | `normalize_ids` | 标识符归一化 + 生成 paper_id |
| `E21` | `resolve_doi` | DOI → Crossref + Unpaywall |
| `E22` | `match_title` | 标题 → Crossref 候选（补 DOI） |
| `E30` | `find_pdf_urls` | 列出候选 PDF 地址 |
| `E31` | `download_pdf` | 下载 + **完整性校验** |
| `E32` | `resolve_oa` | Unpaywall：DOI → OA 位置 |
| `E40` | `grobid_available` | GROBID 服务是否可用 |
| `E41` | `parse_pdf` | **PDF → 结构化元件**（核心） |
| `E42` | `extract_text` | 纯文本兜底 |
| `E50` | `get_references` | 参考文献 |
| `E51` | `get_citations` | 被引 |
| `E52` | `get_citation_edges` | 统一引用边表 |
| `E53` | `get_works_by_ids` | **按 ID 批量取元数据**（外部引用反查） |

### 自动化工具流（流水线，3 个）

| 编号 | 流水线 | 做什么 | 典型耗时 |
|---|---|---|---|
| `P10` | `search_pipeline` | 检索 → 去重 → 排序 | 数秒 |
| `P20` | `acquire_pipeline` | 检索 → 排序 → 抓 PDF → 解析 | 1~2 分钟 |
| `P30` | `full_pipeline` | 完整流程 + 引用扩展 + **三层落盘** | 1~5 分钟 |

---

## 4. 设计约定（Agent 需要知道的）

### 4.1 统一返回信封

所有工具返回同一个结构，**不抛异常、不返回裸 None**：

```jsonc
{ "ok": true, "status": "partial", "data": {...}, "error": null,
  "warnings": [...], "diagnostics": {...}, "meta": {...} }
```

### 4.2 status 的语义边界（最重要的约定）

| status | ok | 含义 | 该做什么 |
|---|---|---|---|
| `ok` | ✅ | 全部成功 | 直接用 |
| `partial` | ✅ | 拿到部分结果且有明确原因 | **结果可用**，看 diagnostics |
| `empty` | ✅ | 成功但结果为空 | **空集不是错误** |
| `blocked` | ❌ | 缺前提（无 key/服务没起） | 按 hint 补前提，**重试无用** |
| `failed` | ❌ | 执行出错 | 看 code + retryable |

**为什么 `empty` 单独一类**：传统代码里"0 命中"和"请求失败"都表现为空列表，
下游分不清"这领域真没论文"和"我的请求挂了"。实测踩过：HTTP 200 + HTML 错误页
被当成"成功的空集"，整个流程报 done —— 失败伪装成功最难发现。

**为什么 `partial` 的 ok 是 True**：多源检索里单源失败是常态，
若判失败会让调用方丢弃已拿到的结果 —— 那才是真损失。

### 4.3 错误反馈四件套

每个错误都带：`code`（稳定字符串码）｜ `retryable`（该不该重试）｜
`hint`（**可执行**的建议）｜ `cause`（原始异常 + traceback）。

```python
r = tools.parse_pdf("/nonexistent.pdf")
r["error"]["code"]      # 'INPUT_NOT_FOUND'
r["error"]["retryable"] # False  → 别重试
r["error"]["hint"]      # '传入的本地路径不存在。确认路径是否拼错、文件是否已下载。'
r["error"]["stage"]     # 多步工具里是第几步挂的
r["error"]["component"] # 'E41'  → 用 find_symbol('E41') 跳文档
```

**设计意图**：Agent 拿到错误后**不需要问人**就能判断下一步 ——
是改参数（input）、重试（retryable）、补前提（blocked）、还是上报（INTERNAL_）。

---

## 5. 文件清单

| 文件 | 职责 |
|---|---|
| [`papersearch/__init__.py`](papersearch/__init__.py) | **统一入口**：把元件与流水线聚到一起，`import papersearch` |
| [`papersearch/elements.py`](papersearch/elements.py) | 元件工具（18 个原子能力） |
| [`papersearch/pipelines.py`](papersearch/pipelines.py) | 自动化工具流（P10/P20/P30） |
| [`papersearch/registry.py`](papersearch/registry.py) | 工具注册表 + **API.md 符号索引器**（可复用） |
| [`papersearch/errors.py`](papersearch/errors.py) | 错误码表（28 个码，含 retryable/hint/severity） |
| [`papersearch/result.py`](papersearch/result.py) | 统一返回信封（5 种状态 + 异常映射） |
| [`papersearch/_bootstrap.py`](papersearch/_bootstrap.py) | 复用 PaperSearch 实现的接入层（**无需配置**） |
| [`docs/gen_api_md.py`](docs/gen_api_md.py) | 从代码生成 `API.md`（保证文档与实现同步） |
| [`cli.py`](cli.py) | 命令行（doctor/list/doc/search/run） |
| [`selfcheck.py`](selfcheck.py) | 自检：契约测试（离线） |
| [`API.md`](API.md) | 详细 API 文档（自动生成） |
| [`docs/errors.md`](docs/errors.md) | 错误处理机制详解 |
| [`COORDINATES.md`](../ExamplePy/COORDINATES.md) | 坐标提取说明 |

---

## 6. 依赖与接入

ToolsPy **不重新实现**任何数据源调用 —— 复用 `PaperSearch/ExamplePy`（数据源）
与 `ProcessPy`（编排），那些都经过了两轮审核 + 32 项边界测试。

**默认零配置**：ToolsPy 就在 `PaperSearch/` 内，`_bootstrap` 自动找到同级目录。
只有把 ToolsPy 单独拷出去用时才需要设 `TOOLSPY_HOME=/path/to/PaperSearch`。

```python
from papersearch import _bootstrap
_bootstrap.describe()      # 接入状态
_bootstrap.check_keys()    # 凭证有没有（**只返回布尔，不含值**）
```

---

## 7. 典型工作流

### 场景 A：调研一个题目（几秒）

```python
r = tools.search_pipeline("counterfactual regret minimization", keep_top=20)
for p in r["data"]["papers"][:5]:
    print(p["score"]["total"], p["metadata"]["title"][:60])
    print("   为什么排这儿:", p["score"]["why"])
```

### 场景 B：拿到全文与结构元件

```python
r = tools.acquire_pipeline("deep cfr", fetch_top=3, out_dir="/tmp/kb")
for it in r["data"]["parsed"]:
    st = it["stats"]
    print(it["paper_id"], "章节", st["n_sections"], "span", st["n_spans"],
          "图", st["n_figures"], "引文连接率", st["citation_link_rate"])
```

### 场景 C：产出可交给下游的语料库

```python
r = tools.full_pipeline("deep cfr", out_dir="/tmp/kb",
                        expand_top=4, fetch_top=8, parse_top=8)
print(result.summarize_result(r))
# 产物：papers.jsonl / citation_edges.jsonl / structures/ / pdfs/ / run.json
```

`papers.jsonl` 每行含 `document_structure`：
`spans`（句子级 + 坐标）、`figure_objects`/`table_objects`/`formula_objects`（含包围盒）、
`citation_links`（正文引用 ↔ 文献条目）、`sections`（章节树）。

### 场景 D：只要某篇论文的图在哪一页

```python
r = tools.parse_pdf("paper.pdf", elements=["figure", "table", "formula"])
for f in r["data"]["figure_objects"]:
    c = f.get("coords")
    if c:
        print(f["id"], "第", c["page_first"], "页",
              f"({c['x']},{c['y']})-({c['x2']},{c['y2']})",
              "｜原始行框", f["coords_n_raw_boxes"])
```

---

## 8. 已知限制

| # | 限制 | 说明 |
|---|---|---|
| 1 | 无 S2_API_KEY 时该源不可用 | 返回 `blocked`（不是 failed）。影响：缺 citation context |
| 2 | 闭源论文拿不到 PDF | 返回 `empty`（不是 failed）—— 这是客观边界，不是缺陷 |
| 3 | GROBID 未启动时降级为纯文本 | `parsed_by="pypdf"`，`sections`/`spans` 为空，**不假装有结构** |
| 4 | 句子级 span 的坐标是**段落级** | `coords_level="paragraph"` 如实标注；句子级框需要逐 token 坐标（GROBID 不提供） |
| 5 | 表格坐标常为空 | GROBID 有时不给 table 的 coords，`coords=null` 并带 `coords_missing_reason` |
| 6 | `coords_n_raw_boxes == 1` 时坐标可疑 | GROBID 可能只识别到图注那一行 —— 如实保留原始框供判断 |
| 7 | 检索覆盖受查询词限制 | 实测：2007 年 CFR 奠基作标题里没有 "counterfactual"，相关度排序进不了 top-10 |

---

## 8b. Agent 模拟调研发现的缺陷（2026-10-08）

用 Agent 视角跑了一遍真实调研（"神经网络 CFR 在多人博弈中的进展"），
**只用文档 + 公开接口**，发现并修复了 3 个问题：

| # | 现象（Agent 会怎么误解） | 根因 | 修复 |
|---|---|---|---|
| 1 | 诊断摘要显示 `search n=4` 而 `rank n=24`，像是"检索只拿到 4 篇、排序却冒出 24 篇" | `_Steps.add` 用 `len(data)` 算条目数；而 `search_all_sources` 的 data 是 **dict**，`len(dict)` 得到的是**键的个数**（4），不是论文数（24） | 优先用 `diagnostics.n_results`；dict 场景按 `papers/works/edges/urls` 等结果键取长度；取不到就给 `None`（**不猜**） |
| 2 | 同一目录解析多篇 PDF，`tei_path` 全是 `unknown.tei.xml` —— 后一篇**覆盖**前一篇，`papers.jsonl` 里两篇指向同一文件 | `parse_pdf` 内部用 `new_paper({"doi":""}, "")`，paper_id 为空 → `safe_name` 得到 `unknown` | 新增 `paper_id` 参数（流水线传真实主键）；不传时用「PDF 文件名 + 路径哈希」生成唯一 ID |
| 3 | 传无效 ID（`"???"`）被报成 `NET_BAD_RESPONSE`（**retryable=True**）—— 让 Agent 去重试一个永远不可能成功的请求 | 无法识别的写法被原样丢给 API，服务端返回非预期结构 | 本地判定为 `INPUT_INVALID`（retry=False）并给出格式示例；混合输入时正确返回 `partial` 并列出无效项 |

### 同时补上的能力缺口：`E53 get_works_by_ids`

调研时最常问的一句是「**这些被共同引用的论文究竟是什么？**」——
引用边里绝大多数目标是语料库外的，只存了 OpenAlex ID（`to_raw: "W32403112"`）。
原有的 E50/E51 是"从某篇出发找它的引用/被引"，**不是**"给定 ID 反查是什么"，
语义不同，硬用会得到错误结果。

补上 E53 后，一次调用就能看出领域基石：

```python
r = tools.get_works_by_ids(["W2103315867", "W2106975967", ...])
# → [2007] Regret Minimization in Games with Incomplete Information (被引 494)
#   [2009] Monte Carlo Sampling for Regret Minimization in Extensive Games (被引 187)
#   [2015] Fictitious Self-Play in Extensive-Form Games (被引 151)
```

## 9. 维护

```bash
cd PaperSearch/ToolsPy
PY="${PYTHON:-python}"

$PY selfcheck.py            # 自检（离线契约测试）
$PY docs/gen_api_md.py           # 改代码后重新生成 API.md
$PY docs/gen_api_md.py --check   # 检查文档是否与代码同步
```

**改了工具签名/状态语义后必须重跑 `docs/gen_api_md.py`** ——
否则 API.md 会与实现脱节，而 Agent 完全依赖那份文档。
