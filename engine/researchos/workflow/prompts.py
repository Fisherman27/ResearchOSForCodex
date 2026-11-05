"""Prompt loading.

Prompts live in ``prompts/*.md`` and are loaded by name.  No prompt text is
inlined in Python except the short per-stage instructions that the pipeline also
kept in code; that exception is documented in ``workflow/finalization.py`` so
the duplication is visible rather than accidental.
"""
from __future__ import annotations

import os
from typing import Dict


class PromptLibrary:
    def __init__(self, root: str):
        self.root = os.path.abspath(root)
        self._cache: Dict[str, str] = {}

    def path(self, name: str) -> str:
        return os.path.join(self.root, name if name.endswith(".md") else name + ".md")

    def get(self, name: str) -> str:
        key = name if name.endswith(".md") else name + ".md"
        if key not in self._cache:
            path = os.path.join(self.root, key)
            if not os.path.isfile(path):
                raise FileNotFoundError("prompt not found: {}".format(path))
            with open(path, encoding="utf-8") as handle:
                self._cache[key] = handle.read()
        return self._cache[key]

    def names(self) -> list:
        if not os.path.isdir(self.root):
            return []
        return sorted(name for name in os.listdir(self.root) if name.endswith(".md"))
