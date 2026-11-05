# PDF 坐标提取（teiCoordinates）

> 用途：知道每个图、表、公式、段落**在 PDF 的哪一页、哪个位置**。
> 这是后续做「高亮回链」「句子级证据定位」的基础。

---

## 1. 怎么实现的

### 1.1 关键前提：坐标不是默认输出

GROBID 返回的 TEI **默认不含坐标**。必须通过**可重复的多值参数**显式请求要坐标的元素：

```
POST /api/processFulltextDocument
Content-Disposition: form-data; name="teiCoordinates"      ← 重复多次

figure
table
formula
p
head
```

`grobid.py` 里对应：

```python
g.process_fulltext(pdf_path, tei_coordinates=["figure", "table", "formula", "p", "head"])
```

⚠️ 这里踩过一个坑：早期实现传的是字符串 `"true"`。GROBID 的参数语义是**元素名**，
收 `"true"` 等于什么都没请求到 —— 函数说明写着"附带全部坐标"，
实际一个都没有。

### 1.2 坐标的原始格式

请求成功后，TEI 元素上会多出 `coords` 属性：

```xml
<figure xml:id="fig_0"
        coords="2,319.50,452.12,238.50,9.03;2,319.50,463.47,238.51,8.64;...">
```

格式来自 GROBID 源码 `BoundingBox.toString()`：

```
页码,x,y,宽,高
```

多个框用 `;` 分隔。

**坐标系要点**（写错就会画到反方向）：

| 项 | 值 |
|---|---|
| 原点 | **页面左上角** |
| x 方向 | 向右为正 |
| y 方向 | **向下为正** |
| 单位 | PDF 点（1/72 英寸） |

注意这与 PDF 原生坐标系（原点左下、y 向上）**相反**。若要往 PDF 上画框，
需要转换：`pdf_y = page_height - grobid_y2`。

### 1.3 关键认知：一个元素有多个框

这是最容易做错的地方。GROBID 是**按行**给框的 —— 一个 `<figure>` 若含 16 行文字，
就有 **16 个小框**，每个只覆盖那一行：

```
fig_0 的 16 个原始框：
  页2 x=319.50 y=452.12 w=238.50 h=9.03   ← 第 1 行
  页2 x=319.50 y=463.47 w=238.51 h=8.64   ← 第 2 行
  ...
```

如果直接取第一个框，会得到一个 **238×9 的扁条** —— 完全不是图的真实区域。

所以必须**合并成包围盒**：同一页上取 `min(x)`、`min(y)`、`max(x+w)`、`max(y+h)`。

合并后：

```
fig_0 bbox = (319.50, 452.12) - (558.01, 625.53)   即 238×173
```

### 1.4 跨页元素

有些段落会跨页（实测一篇里有 5 个）。**跨页元素没有"单一真实矩形"** ——
硬合并成一个框会得到一个横跨两页、中间大片空白的假框。

处理方式：**每页各给一个框**，同时给一个整体 bbox 供参考。

```
p（无 xml:id）页1-2：
  第1页 (320,684)-(558,704)   [2 行框]
  第2页 (54,57)-(292,111)     [5 行框]
```

### 1.5 代码位置

| 函数 | 文件 | 作用 |
|---|---|---|
| `normalize_coordinates()` | `ExamplePy/grobid.py` | 把 `True`/字符串/列表规整成元素名列表 |
| `parse_coords_string()` | `ExamplePy/grobid.py` | 解析 `coords` 字符串 → 框列表 |
| `merge_boxes()` | `ExamplePy/grobid.py` | 多行框 → 每页包围盒 + 总体 bbox |
| `parse_tei_coordinates()` | `ExamplePy/grobid.py` | 抽取所有带坐标的元素（含所属章节、文本） |
| `attach_coordinates()` | `ExamplePy/grobid.py` | 按 `xml:id` 把坐标贴回图表公式对象 |
| `boxes_to_dict_list()` | `ExamplePy/grobid.py` | 框列表 → 紧凑字符串（与 TEI 格式一致） |
| `DEFAULT_COORD_ELEMENTS` | `ProcessPy/fulltext.py` | 默认请求的元素（图表公式） |
| `coordinate_elements` | `ProcessPy/config.py` | 可配置请求哪些元素 |

---

## 2. 坐标数据放在哪里

### 2.1 在 Paper 记录里（`papers.jsonl`）

每篇论文的 `document_structure` 字段新增：

```jsonc
"document_structure": {
  "parsed_by": "grobid",

  // 图/表/公式对象，每个都带 coords
  "figure_objects": [
    {
      "id": "fig_0",
      "label": "1",
      "text": "Fig. 1: Illustration of the signal model in one pulse.",
      "section_index": 3,

      "coords": {                    // ← 合并后的包围盒
        "page_first": 2, "page_last": 2,
        "x": 319.5, "y": 452.12, "x2": 558.01, "y2": 625.53,
        "width": 238.51, "height": 173.41
      },
      "coords_boxes_per_page": [     // ← 跨页时每页一个
        {"page": 2, "x": 319.5, "y": 452.12, "x2": 558.01, "y2": 625.53,
         "width": 238.51, "height": 173.41, "n_raw_boxes": 16}
      ],
      "coords_page_first": 2,
      "coords_page_last": 2,
      "coords_raw_boxes": [          // ← GROBID 原始行框（保留证据）
        {"page": 2, "x": 319.5, "y": 452.12, "width": 238.5, "height": 9.03},
        ...
      ],
      "coords_n_raw_boxes": 16,
      "coords_source": "tei:figure@coords"
    }
  ],
  "table_objects": [ ... ],
  "formula_objects": [ ... ],

  // 段落/标题级坐标（数量大，单独放）
  "coordinates": {
    "count": 14,
    "by_element": {"formula": 10, "figure": 4},
    "coordinate_space": "pdf_points_origin_top_left",
    "requested_elements": ["figure", "table", "formula", "p", "head"],
    "note": "",
    "elements": [
      {"xml_id": "", "element": "p", "page_first": 1, "page_last": 2,
       "bbox": {"x": 320, "y": 684, "x2": 558, "y2": 704, ...},
       "boxes_per_page": [...], "raw_boxes": [...],
       "text": "Counterfactual regret minimization (CFR) is ...",
       "section_index": 2}
    ]
  }
}
```

### 2.2 在验收产物里

```
acceptance/<run_id>/
└── documents/
    ├── <paper_id>.json        ← 完整结构文档，含上述全部坐标字段
    │                             顶层有三个便于直接用的键：
    │                               figures_objects_with_coords
    │                               tables_objects_with_coords
    │                               formulas_objects_with_coords
    │                               coordinates.text_elements（段落/标题坐标）
    ├── <paper_id>.tei.xml     ← 原始 TEI，coords 属性就在这里（可回溯）
    └── manifest.jsonl         ← 精简索引，含 coords_count / coords_by_element
```

### 2.3 取用示例

```python
import json

doc = json.load(open("documents/doi_10.7939_r3m61bp9b.json", encoding="utf-8"))

# 第一个图的坐标
fig = doc["figures_objects_with_coords"][0]
b = fig["coords"]
print(f"{fig['id']} 在第 {b['page_first']} 页，"
      f"区域 ({b['x']}, {b['y']}) - ({b['x2']}, {b['y2']})")

# 某一段落的坐标 + 文本
for el in doc["coordinates"]["text_elements"]:
    if el["element"] == "p":
        print(el["page_first"], el["bbox"], el["text"][:60])

# 跨页元素：按页取框
for el in doc["coordinates"]["text_elements"]:
    if el["page_last"] > el["page_first"]:
        for pg in el["boxes_per_page"]:
            print(f"  第{pg['page']}页 {pg['x']},{pg['y']}-{pg['x2']},{pg['y2']}")
```

### 2.4 在 PDF 上画框时注意坐标转换

```python
h = page_height            # 如 792（Letter）
# GROBID(左上原点) → PDF(左下原点)
pdf_x1, pdf_y1 = b["x"],  h - b["y2"]
pdf_x2, pdf_y2 = b["x2"], h - b["y"]
```

---

## 3. 实测验证

样本：`doi_10.7939_r3m61bp9b.pdf`（页面 612×792 pt）

| 项 | 结果 |
|---|---|
| 图表公式坐标 | **14/14 全部拿到**（figure 4 + formula 10） |
| 原始行框数 | 最多 16 个/元素（已合并为 1 个包围盒） |
| 跨页元素 | 5 个，均正确给出 per-page 框 |
| 段落级坐标 | 打开 `p`/`head` 后 58 段落 + 12 标题全部有坐标 |

**坐标正确性的独立验证**（不依赖图形工具）：
用坐标区域从 PDF 里**反向提取文字**（pypdf 提取），与 GROBID 的 TEI 文本比对：

```
formula  p2  重叠率=100%  ✅
    PDF : π σ ( h ) = ∏ i ∈ N ∪{ c } π σ i ( h )
    TEI : π σ (h) = i∈N ∪{c} π σ i (h)
...
结论：14/14 个元素的坐标区域内能取到对应文字（100%）
```

两条独立路径（GROBID 的 TEI 文本 / pypdf 的 PDF 文字）在坐标区域内对上了，
说明坐标确实指向正确的区域。

---

## 4. 已知限制

| # | 限制 | 说明 |
|---|---|---|
| 1 | **GROBID 只识别到图注时，框只有一行** | 实测 `fig_2` 只有 1 个行框（宽高比 20.9），因为 GROBID 没识别出图的主体区域，只认到了图注那一行。**这是 GROBID 本身的局限，不是解析错误** —— 代码如实保留原始框，可据此判断可信度（`coords_n_raw_boxes == 1` 时需谨慎） |
| 2 | 表格坐标常为空 | 部分样本的 `<table>` 没有任何 `coords`（GROBID 没给）。此时 `coords` 为 `null` 并带 `coords_missing_reason` |
| 3 | 无 `xml:id` 的对象贴不上坐标 | `attach_coordinates()` **按 xml:id 精确匹配**，不按顺序臆测 —— 顺序错配会把 A 图的坐标安到 B 图上，下游极难发现 |
| 4 | 请求 `p`/`head` 会让 TEI 明显变大 | 默认只请求图表公式；需要段落级回链时在 `config.coordinate_elements` 里加 |
| 5 | 坐标不含旋转/倾斜信息 | GROBID 给的是轴对齐包围盒，倾斜的图会被框大 |

---

## 5. 开启方式

**默认已开启**（请求图表公式坐标）。要改：

```python
from config import PipelineConfig
cfg = PipelineConfig()
cfg.tei_coordinates = True                                    # 关就设 False
cfg.coordinate_elements = ("figure", "table", "formula", "p", "head", "ref")
```

**不开启时不会假装成功**：`parse_tei_coordinates()` 返回 `count=0` 并在
`note` 里写明「TEI 中没有任何 coords 属性 —— 解析时未传 tei_coordinates」。
