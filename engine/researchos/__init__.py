"""ResearchOS engine: a multi-agent research loop driven through work orders."""
from .config import SystemConfig
from .system import ResearchSystem, Agents
from .runtime.envelope import ok, partial, empty, blocked, failed, error
from .runtime.policy import CallablePolicy, ScriptedPolicy, SingleToolPolicy

__all__ = ["SystemConfig", "ResearchSystem", "Agents", "CallablePolicy",
           "ScriptedPolicy", "SingleToolPolicy", "ok", "partial", "empty",
           "blocked", "failed", "error"]
__version__ = "0.1.0"
