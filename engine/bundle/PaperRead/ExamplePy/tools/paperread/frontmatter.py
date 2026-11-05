"""极简 YAML frontmatter 解析（无第三方依赖）。

支持策略文件 frontmatter 实际用到的 YAML 子集：
  - `key: value` 标量（字符串 / 布尔 / 数字，自动去引号）
  - 嵌套映射（缩进子块）
  - 列表（`- item`，含 `- key: value` 形式的映射项）
  - 块标量：`>`（折叠）、`|`（保留换行）

**刻意不支持的**：锚点、别名、多文档、流式集合（`[...]` / `{...}`）、复杂引号转义。
解析不了的行**如实记录到 `__unparsed__`**，而不是猜测或静默丢弃——
错误的 frontmatter 必须能被发现。

为什么不用 PyYAML：本包承诺**零第三方依赖**（实验服务器上少一份环境风险）。
"""

import re

_FRONTMATTER_RE = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.DOTALL)
# YAML 要求冒号后跟空白或行尾；因此 `https://...` 不会被误认成映射键。
_KEY_RE = re.compile(r"^([A-Za-z0-9_\-]+):(?:[ \t]+(.*))?$")
_BLOCK_SCALARS = (">", "|", ">-", "|-", ">+", "|+")


class _Line:
    """预处理后的行：保留原始内容与缩进。

    `content` 保留**原始**文本（块标量正文需要它，其中的 `#` 是内容）；
    `effective` 是去掉行尾注释后的文本，用于键匹配与标量取值。
    """

    __slots__ = ("indent", "content", "effective", "blank", "comment_only")

    def __init__(self, raw):
        stripped = raw.lstrip(" ")
        self.indent = len(raw) - len(stripped)
        self.content = stripped.rstrip()
        self.blank = not self.content
        self.effective = _strip_comment(self.content)
        self.comment_only = (not self.blank) and (not self.effective.strip())


def split_frontmatter(text: str):
    """把文本切成 (frontmatter_text, body)。没有 frontmatter 时返回 (None, text)。"""
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return None, text
    return m.group(1), text[m.end():]


def _strip_quotes(value: str) -> str:
    v = value.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
        return v[1:-1]
    return v


def _strip_comment(text: str) -> str:
    """去掉行尾 YAML 注释：`#` 前须是行首或空白，且不在引号内。

    注意 `#` 紧跟在非空白字符后（如 `red#fff`、URL 的 `#fragment`）不是注释起始。
    块标量正文不经过本函数，其中的 `#` 会作为内容保留。
    """
    quote = None
    for idx, ch in enumerate(text):
        if quote:
            if ch == quote:
                quote = None
            continue
        if ch in ("'", '"'):
            quote = ch
            continue
        if ch == "#" and (idx == 0 or text[idx - 1] in (" ", "\t")):
            return text[:idx].rstrip()
    return text.rstrip()


def _coerce_scalar(value: str):
    """把标量字符串转成 list / bool / int / float / None / str。

    支持流式序列 `[a, b]`；流式映射 `{...}` 不支持，会按字符串保留。
    """
    v = _strip_quotes(value)
    low = v.lower()
    if low in ("true", "yes"):
        return True
    if low in ("false", "no"):
        return False
    if low in ("null", "~", ""):
        return None

    # 流式序列：[a, b, c]
    if len(v) >= 2 and v[0] == "[" and v[-1] == "]":
        inner = v[1:-1].strip()
        if not inner:
            return []
        return [_coerce_scalar(part) for part in inner.split(",")]

    if re.fullmatch(r"-?\d+", v):
        return int(v)
    if re.fullmatch(r"-?\d+\.\d+", v):
        return float(v)
    return v


def _fold(lines):
    """YAML 折叠块标量（>）：相邻非空行以空格连接，空行分段。"""
    parts, buf = [], []
    for line in lines:
        if line.strip():
            buf.append(line.strip())
        elif buf:
            parts.append(" ".join(buf))
            buf = []
    if buf:
        parts.append(" ".join(buf))
    return "\n".join(parts).strip()


def _next_content(lines, i):
    """返回 i 之后第一个非空、非纯注释行的下标；没有则返回 len(lines)。"""
    n = len(lines)
    while i < n and (lines[i].blank or lines[i].comment_only):
        i += 1
    return i


def _consume_block_scalar(lines, i, indicator, parent_indent):
    """收集块标量内容，返回 (text, 下一个下标)。

    块标量内容必须比父键缩进更深；遇到缩进 <= parent_indent 的行即结束。
    若不这样限制，块标量会吞掉后面的兄弟键（如 `urls:` / `note:`）。
    """
    n = len(lines)
    block = []
    while i < n:
        line = lines[i]
        if line.blank:
            block.append("")
            i += 1
            continue
        if line.indent > parent_indent:
            block.append(line.content)
            i += 1
            continue
        break
    if indicator.startswith(">"):
        return _fold(block), i
    return "\n".join(block).strip(), i


def _parse_block(lines, i, indent, unparsed):
    """解析缩进 >= indent 的块。返回 (值, 下一个下标)。

    块的形态由第一个内容行的写法决定：`- ` 开头是列表，否则是映射。
    """
    i = _next_content(lines, i)
    n = len(lines)
    if i >= n or lines[i].indent < indent:
        return None, i

    text = lines[i].effective
    if text.startswith("- ") or text == "-":
        return _parse_list(lines, i, indent, unparsed)
    return _parse_map(lines, i, indent, unparsed)


def _parse_list(lines, i, indent, unparsed):
    """解析 `- item` 列表，支持标量项与 `- key: value` 映射项。"""
    items = []
    n = len(lines)
    while i < n:
        line = lines[i]
        if line.blank or line.comment_only:
            i += 1
            continue
        if line.indent < indent:
            break
        text = line.effective
        if line.indent > indent:
            # 上一轮已消费子块；走到这里说明缩进异常，如实记录
            unparsed.append(line.content)
            i += 1
            continue
        if not (text.startswith("- ") or text == "-"):
            break

        rest = text[2:].strip() if text.startswith("- ") else ""
        i += 1

        if not rest:
            # 列表项带嵌套块
            j = _next_content(lines, i)
            if j < n and lines[j].indent > indent:
                value, i = _parse_block(lines, i, lines[j].indent, unparsed)
                items.append(value)
            else:
                items.append(None)
            continue

        m = _KEY_RE.match(rest)
        if m:
            # `- key: value` —— 映射项；后续兄弟键缩进更深
            item = {}
            key = m.group(1)
            inline = (m.group(2) or "").strip()
            if inline in _BLOCK_SCALARS:
                value, i = _consume_block_scalar(lines, i, inline, line.indent)
                item[key] = value
            elif inline:
                item[key] = _coerce_scalar(inline)
            else:
                j = _next_content(lines, i)
                if j < n and lines[j].indent > line.indent:
                    value, i = _parse_block(lines, i, lines[j].indent, unparsed)
                    item[key] = value
                else:
                    item[key] = None
            # 吸收同一映射项的后续键（缩进 > indent 且不是新的 `- `）
            j = _next_content(lines, i)
            if (j < n and lines[j].indent > indent
                    and not lines[j].effective.startswith("- ")):
                more, i = _parse_map(lines, i, lines[j].indent, unparsed)
                if isinstance(more, dict):
                    item.update(more)
            items.append(item)
        else:
            items.append(_coerce_scalar(rest))

    return items, i


def _parse_map(lines, i, indent, unparsed):
    """解析缩进 == indent 的映射。"""
    result = {}
    n = len(lines)
    while i < n:
        line = lines[i]
        if line.blank or line.comment_only:
            i += 1
            continue
        if line.indent < indent:
            break
        if line.indent > indent:
            # 上一轮已消费子块；走到这里说明缩进异常，如实记录
            unparsed.append(line.content)
            i += 1
            continue

        m = _KEY_RE.match(line.effective)
        if not m:
            unparsed.append(line.content)
            i += 1
            continue

        key = m.group(1)
        inline = (m.group(2) or "").strip()
        i += 1

        if inline in _BLOCK_SCALARS:
            value, i = _consume_block_scalar(lines, i, inline, indent)
            result[key] = value
            continue

        if inline:
            result[key] = _coerce_scalar(inline)
            continue

        # `key:` 后跟子块？
        j = _next_content(lines, i)
        if j < n and lines[j].indent > indent:
            value, i = _parse_block(lines, i, lines[j].indent, unparsed)
            result[key] = value
        else:
            result[key] = None

    return result, i


def parse(text: str) -> dict:
    """解析 frontmatter 文本为 dict。解析不了的行收进 __unparsed__。"""
    lines = [_Line(raw) for raw in text.splitlines()]
    unparsed = []
    result, _ = _parse_map(lines, 0, 0, unparsed)
    if not isinstance(result, dict):
        result = {}
    if unparsed:
        result["__unparsed__"] = unparsed
    return result


def load_file(path: str):
    """读文件并返回 (frontmatter_dict, body, raw_text)。无 frontmatter 时 dict 为 {}。"""
    with open(path, encoding="utf-8") as f:
        text = f.read()
    fm_text, body = split_frontmatter(text)
    fm = parse(fm_text) if fm_text is not None else {}
    return fm, body, text


def dump(data: dict, indent: int = 0) -> str:
    """把 dict 写回 frontmatter 文本（仅测试与工具用，不追求 YAML 完整性）。"""
    pad = " " * indent
    out = []
    for k, v in data.items():
        if isinstance(v, dict):
            out.append("{}{}:".format(pad, k))
            out.append(dump(v, indent + 2))
        elif isinstance(v, list):
            out.append("{}{}:".format(pad, k))
            for item in v:
                if isinstance(item, dict):
                    inner = dump(item, 0)
                    parts = inner.split("\n")
                    out.append("{}  - {}".format(pad, parts[0]))
                    for extra in parts[1:]:
                        out.append("{}    {}".format(pad, extra))
                else:
                    out.append("{}  - {}".format(pad, item))
        else:
            out.append("{}{}: {}".format(pad, k, v))
    return "\n".join(x for x in out if x != "")
