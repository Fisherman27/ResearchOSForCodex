"""Supervisors and gates: the loop, unchanged in behaviour.

Imports are deliberately lazy.  ``workflow`` modules import ``agents`` (which
imports ``workflow.prompts`` and ``workflow.state``), so eager re-exports here
would create a cycle.  Public names are resolved on first access instead.
"""
from typing import Any

_LAZY = {
    "build_director": ("researchos.workflow.director", "build_director"),
    "build_literature_supervisor": ("researchos.workflow.literature",
                                    "build_literature_supervisor"),
    "PromptLibrary": ("researchos.workflow.prompts", "PromptLibrary"),
    "RunStore": ("researchos.workflow.state", "RunStore"),
    "literature_initial_state": ("researchos.workflow.state",
                                 "literature_initial_state"),
    "STAGES": ("researchos.workflow.finalization", "STAGES"),
    "AGENT_ROLES": ("researchos.workflow.finalization", "AGENT_ROLES"),
    "REQUIRED": ("researchos.workflow.finalization", "REQUIRED"),
    "stage_package": ("researchos.workflow.finalization", "stage_package"),
    "validate_stage": ("researchos.workflow.finalization", "validate_stage"),
}

__all__ = sorted(_LAZY)


def __getattr__(name: str) -> Any:
    if name not in _LAZY:
        raise AttributeError("module 'researchos.workflow' has no attribute {!r}".format(name))
    import importlib
    module_name, attribute = _LAZY[name]
    return getattr(importlib.import_module(module_name), attribute)


def __dir__():
    return sorted(__all__)
