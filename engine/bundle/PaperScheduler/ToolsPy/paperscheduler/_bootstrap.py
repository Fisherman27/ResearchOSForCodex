"""接入 PaperOrganization 的唯一内部适配层。"""
from __future__ import annotations

import os
import sys


def project_root() -> str:
    cur = os.path.abspath(os.path.dirname(__file__))
    for _ in range(8):
        if os.path.isdir(os.path.join(cur, "PaperOrganization", "ToolsPy")):
            return cur
        cur = os.path.dirname(cur)
    raise RuntimeError("找不到 ResearchOS 根目录及 PaperOrganization/ToolsPy")


def load_organization():
    tools_dir = os.path.join(project_root(), "PaperOrganization", "ToolsPy")
    if tools_dir not in sys.path:
        sys.path.insert(0, tools_dir)
    from paperorganization import paths, query
    return paths, query
