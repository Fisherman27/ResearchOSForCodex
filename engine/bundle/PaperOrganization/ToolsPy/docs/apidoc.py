#!/usr/bin/env python3
"""API 文档定位工具（可复用到其他工具库）。

问题：
    README.md 是索引（列出有哪些工具），API.md 是详细文档（每个工具一节）。
    用户/Agent 看到 README 里的条目后，需要跳到 API.md 的对应行去看细节。

本工具做的事：
    1. 扫描 API.md，找出每个「工具条目」的起始行号。
    2. 在 README.md 的索引表里，为每个条目自动补上 `API.md#L<行号>` 链接。
    3. 支持查询：给定工具名，直接打印它在 API.md 的行号区间。

设计约定（其他工具库也能照用）：
    - API.md 里每个工具用二级或三级标题：`## name` / `### name`
    - 标题格式建议：`### 函数名 — 一句话`
    - README.md 里索引条目用 `| 名称 | ... |` 表格行，或 `- 名称`

用法：
    python docs/apidoc.py index        # 重新生成 README 的索引链接
    python docs/apidoc.py find <name>  # 查某个工具在 API.md 的行号
    python docs/apidoc.py list         # 列出所有工具及行号
"""
from __future__ import annotations

import os
import re
import sys
from typing import Dict, List, Optional, Tuple

_HERE = os.path.dirname(os.path.abspath(__file__))
_TOOLSPY = os.path.dirname(_HERE)
API_MD = os.path.join(_TOOLSPY, "API.md")
README_MD = os.path.join(_TOOLSPY, "README.md")

# 标题行匹配：## / ### + 名称（名称取到 " — " 或 " - " 或空格前）
HEADING_RE = re.compile(r"^(#{2,4})\s+(.+?)\s*$")
# 工具名提取：去掉 " — 说明" / " - 说明" / "（说明）"
NAME_SPLIT_RE = re.compile(r"\s+(?:—|-|–)\s+|（|\(")


def parse_api(path: str = API_MD) -> List[Dict]:
    """扫描 API.md，返回 [{"name","line","level","title"}, ...]。"""
    entries = []
    if not os.path.isfile(path):
        return entries
    with open(path, encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            m = HEADING_RE.match(line.rstrip("\n"))
            if not m:
                continue
            level = len(m.group(1))
            title = m.group(2).strip()
            name = NAME_SPLIT_RE.split(title)[0].strip().strip("`")
            if not name:
                continue
            entries.append({"name": name, "line": lineno,
                            "level": level, "title": title})
    return entries


def find_entry(name: str, entries: Optional[List[Dict]] = None) -> List[Dict]:
    """按名称（或名称子串）查找条目。"""
    entries = entries if entries is not None else parse_api()
    exact = [e for e in entries if e["name"] == name]
    if exact:
        return exact
    return [e for e in entries if name.lower() in e["name"].lower()]


def line_range(entry: Dict, entries: List[Dict]) -> Tuple[int, int]:
    """算出一个条目的行号区间（到下一个同级或更高级标题前）。"""
    start = entry["line"]
    end = None
    for e in entries:
        if e["line"] > start and e["level"] <= entry["level"]:
            end = e["line"] - 1
            break
    if end is None:
        # 到文件末尾
        end = start
        if os.path.isfile(API_MD):
            with open(API_MD, encoding="utf-8") as f:
                end = sum(1 for _ in f)
    return start, end


def cmd_list():
    """列出 API.md 里所有条目及行号。"""
    entries = parse_api()
    if not entries:
        print("API.md 不存在或没有条目：{}".format(API_MD))
        return 1
    print("API.md 条目（{} 个）：\n".format(len(entries)))
    for e in entries:
        s, t = line_range(e, entries)
        print("  {:<34s} 行 {:>4d}-{:<4d}  {}".format(
            e["name"], s, t, "  " * (e["level"] - 2) + e["title"]))
    return 0


def cmd_find(name: str):
    """查某个工具在 API.md 的行号区间，并打印该区间内容。"""
    entries = parse_api()
    hits = find_entry(name, entries)
    if not hits:
        print("未找到匹配 '{}' 的条目。可用 'list' 查看全部。".format(name))
        return 1
    for e in hits:
        s, t = line_range(e, entries)
        print("{}".format(e["title"]))
        print("  → {}#L{}-L{}".format(API_MD, s, t))
        print()
        # 打印该区间内容
        with open(API_MD, encoding="utf-8") as f:
            lines = f.readlines()
        for i in range(s - 1, min(t, len(lines))):
            sys.stdout.write(lines[i])
        print("\n" + "-" * 60)
    return 0


def cmd_index():
    """在 README.md 的索引条目上补/更新 API.md 行号链接。

    做法：README 里若有 `<!-- API-INDEX -->` 与 `<!-- /API-INDEX -->`
    标记，则在这两个标记之间自动生成索引；否则只报告不动文件。
    """
    if not os.path.isfile(README_MD):
        print("README.md 不存在：{}".format(README_MD))
        return 1
    entries = parse_api()
    if not entries:
        print("API.md 没有条目，无法生成索引")
        return 1

    with open(README_MD, encoding="utf-8") as f:
        content = f.read()

    start_mark = "<!-- API-INDEX -->"
    end_mark = "<!-- /API-INDEX -->"
    if start_mark not in content or end_mark not in content:
        print("README.md 缺少索引标记 {} / {}".format(start_mark, end_mark))
        print("（不自动改动文件，避免破坏手工内容）")
        return 1

    # 生成索引表：只取 level==3 的条目（跳过大类 level==2）
    rows = ["| 工具 | API 文档位置 |", "|---|---|"]
    for e in entries:
        if e["level"] < 3:
            continue
        s, t = line_range(e, entries)
        rows.append("| `{}` | [`API.md#L{}-L{}`](API.md#L{}-L{}) |".format(
            e["name"], s, t, s, t))
    block = "\n".join(rows)

    new = re.sub(
        re.escape(start_mark) + r".*?" + re.escape(end_mark),
        start_mark + "\n" + block + "\n" + end_mark,
        content, flags=re.S)
    with open(README_MD, "w", encoding="utf-8") as f:
        f.write(new)
    print("已更新 README.md 索引（{} 条）".format(len(rows) - 2))
    return 0


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    cmd = sys.argv[1]
    if cmd == "list":
        return cmd_list()
    if cmd == "find":
        if len(sys.argv) < 3:
            print("用法: python docs/apidoc.py find <name>")
            return 1
        return cmd_find(sys.argv[2])
    if cmd == "index":
        return cmd_index()
    print("未知命令：{}（可用 list / find / index）".format(cmd))
    return 1


if __name__ == "__main__":
    sys.exit(main())
