"""Scaffolding shared by the module CLIs (`bridge`, `acceptance`).

Both are invoked as scripts under `engine/researchos/modules/`, and both used to
derive the engine root with their own copy of the same `dirname` chain. Two
copies of a path derivation is the same hazard as two copies of a filename: move
one file and only one of them breaks.
"""
from __future__ import annotations

import json
import os
from typing import Any


def engine_root() -> str:
    """The `engine/` directory, from this module's location."""
    return os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))


def emit(value: Any, *, code: int = 0) -> int:
    """Print one JSON envelope and return the process exit code."""
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))
    return code
