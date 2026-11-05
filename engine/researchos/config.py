"""Configuration for the ResearchOS multi-agent system.

Fields fall into two groups:

* orchestration knobs (batch size, strategies, re-read caps, ideation strategy);
* deployment knobs (``port_backend``, ``mode``, ``handoff_agents``, ``policy``,
  ``max_agent_turns``) that decide who supplies each agent's thinking.

Paths stay relative to the product root so the directory remains copyable.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, fields
from typing import Any, Dict, List


@dataclass(frozen=True)
class SystemConfig:
    kb_dir: str
    runs_dir: str
    visualization_dir: str
    search_output_dir: str = ""
    planner_strategy: str = "dependency_based"
    context_strategy: str = "structured"
    reading_strategy_id: str = "S4"
    batch_size: int = 1
    context_budget_tokens: int = 4000
    max_cycles: int = 100
    research_goal: str = ""
    require_organization_agent: bool = False
    require_scheduler_agent: bool = False
    # -- deployment knobs ------------------------------------------------
    max_agent_turns: int = 8
    policy: Dict[str, str] = field(default_factory=dict)
    port_backend: str = "memory"
    # "inline"  = rules (or a policy_factory) drive agents in-process
    # "handoff" = every agent turn is published as a work order for a host agent
    mode: str = "inline"
    #: Which agents get an external LLM turn in handoff mode.  Default is the
    #: whole thinking layer; pass a subset (e.g. ["director"]) to let the rest
    #: run on rules, which is useful for isolating one agent in an experiment.
    handoff_agents: List[str] = field(default_factory=list)
    #: How many re-reads may be scheduled in one batch, and how many times a
    #: single paper may be re-read before its gaps are accepted as terminal.
    reread_budget: int = 1
    max_rereads_per_paper: int = 2
    #: Where per-run data lives.
    #:
    #: ``"shared"`` (default) keeps the configured ``kb_dir`` / ``visualization_dir``
    #: / ``search_output_dir`` exactly as written — every run reads and writes the
    #: same knowledge base.
    #:
    #: ``"per_run"`` nests the run's own ``kb/``, ``viz/`` and ``artifacts/`` inside
    #: ``<runs_dir>/<run_id>/``, so one task is one self-contained directory that
    #: can be archived or deleted as a unit. ``search_output_dir`` stays the
    #: configured input root, because a caller may point it at an existing corpus.
    layout: str = "shared"
    #: Which pluggable ideation mechanism to use (see prompts/ideation/).
    ideation_strategy: str = "I1"
    #: How many proposals survive the deterministic filter.
    max_proposals: int = 5

    @classmethod
    def from_file(cls, path: str) -> "SystemConfig":
        path = os.path.abspath(path)
        with open(path, encoding="utf-8") as handle:
            raw = json.load(handle)
        known = {item.name for item in fields(cls)}
        unknown = set(raw) - known
        if unknown:
            raise ValueError("unknown config keys: {}".format(sorted(unknown)))
        base = root_of(path)
        if not raw.get("search_output_dir"):
            raw["search_output_dir"] = os.path.join(
                os.path.dirname(raw["kb_dir"]), "search")
        for key in ("kb_dir", "runs_dir", "visualization_dir", "search_output_dir"):
            value = raw[key]
            raw[key] = value if os.path.isabs(value) else os.path.join(base, value)
        raw.setdefault("policy", {})
        config = cls(**raw)
        config.validate()
        return config

    def validate(self) -> None:
        if self.layout not in ("shared", "per_run"):
            raise ValueError("layout must be 'shared' or 'per_run'")
        if self.batch_size < 1:
            raise ValueError("batch_size must be positive")
        if self.context_budget_tokens < 0:
            raise ValueError("context_budget_tokens must be non-negative")
        if self.max_cycles < 1:
            raise ValueError("max_cycles must be positive")
        if self.max_agent_turns < 1:
            raise ValueError("max_agent_turns must be positive")
        if self.reread_budget < 0:
            raise ValueError("reread_budget must be non-negative")
        if self.max_rereads_per_paper < 0:
            raise ValueError("max_rereads_per_paper must be non-negative")
        if not str(self.ideation_strategy or "").strip():
            raise ValueError("ideation_strategy must not be empty")
        if self.max_proposals < 1:
            raise ValueError("max_proposals must be positive")
        if self.port_backend not in ("memory", "bundle"):
            raise ValueError("port_backend must be memory or bundle")
        if self.mode not in ("inline", "handoff"):
            raise ValueError("mode must be inline or handoff")

    def policy_for(self, agent: str) -> str:
        if self.mode == "handoff":
            return "handoff"
        return self.policy.get(agent) or self.policy.get("*") or "rule"


def root_of(path: str) -> str:
    """Locate the engine root for a config file.

    Two layouts are supported, because a config does not have to live inside the
    engine directory:

    * **inline** -- the config sits under the engine root (the shipped
      ``engine/config/*.json`` case), found by walking up;
    * **external** -- the config lives with a run's outputs (e.g.
      ``System/Test3-1/config.json``). Walking up from there never finds
      ``cli.py``, so fall back to the engine that is *executing this code*,
      and to ``$DSH_RESEARCHOS_DIR`` when the package was moved.
    """
    current = os.path.dirname(os.path.abspath(path))
    for _ in range(8):
        if (os.path.isfile(os.path.join(current, "cli.py"))
                and os.path.isdir(os.path.join(current, "researchos"))):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    # The config is external to the engine: use the engine we are running from.
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if (os.path.isfile(os.path.join(here, "cli.py"))
            and os.path.isdir(os.path.join(here, "researchos"))):
        return here
    override = os.environ.get("DSH_RESEARCHOS_DIR", "").strip()
    if override:
        candidate = os.path.abspath(override)
        for probe in (candidate, os.path.join(candidate, "engine")):
            if (os.path.isfile(os.path.join(probe, "cli.py"))
                    and os.path.isdir(os.path.join(probe, "researchos"))):
                return probe
    raise ValueError("cannot locate the ResearchOS root from {}".format(path))
