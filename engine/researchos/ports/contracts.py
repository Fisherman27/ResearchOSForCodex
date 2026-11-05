"""Module ports.

A *port* is the only thing a module agent may assume about the world outside
itself.  Each port corresponds to one of the four bundled modules in
``bundle/``:

    ResearchPort      <- PaperSearch   (AgentWorkflow/search_workflow.py)
    OrganizationPort  <- PaperOrganization (AgentWorkflow/organization_workflow.py)
    SchedulerPort     <- PaperScheduler   (AgentWorkflow/scheduler_workflow.py)
    ReadPort          <- PaperRead        (AgentWorkflow/read_workflow.py)

When the tool libraries are ported, an adapter implements these ports by
calling the real ToolsPy functions.  Nothing else in the engine changes: the
agents already talk to ports through tools, never to files or SQLite.

All port methods return the shared envelope (see ``runtime.envelope``).
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Protocol, runtime_checkable


@runtime_checkable
class ResearchPort(Protocol):
    """One traceable search run producing a structured corpus on disk."""

    def run(self, question: str, *, run_id: str, query: str,
            reason: str = "", options: Optional[Dict[str, Any]] = None) -> dict:
        ...


@runtime_checkable
class OrganizationPort(Protocol):
    """The sole owner of stored knowledge."""

    def import_search_output(self, search_output: str) -> dict:
        ...

    def snapshot(self) -> dict:
        ...

    def store_reading_results(self, results: Iterable[dict]) -> dict:
        ...

    def related_knowledge(self, paper_id: str) -> dict:
        ...

    def paper_overview(self, paper_id: str) -> dict:
        ...

    def paper_understanding(self, paper_id: str,
                            aspects: Optional[List[str]] = None) -> dict:
        ...

    def curation_package(self) -> dict:
        """Snapshot + overviews + existing relations, for the curator agent."""

    def store_relation_proposals(self, proposals: List[dict],
                                 source_snapshot: str) -> dict:
        ...

    def relation_review_package(self) -> dict:
        ...

    def apply_claim_relation_review(self, review: dict) -> dict:
        ...

    def export_evidence(self) -> dict:
        ...


@runtime_checkable
class SchedulerPort(Protocol):
    """Reading order and authorised context."""

    def plan(self, snapshot: dict, *, strategy: str, batch_size: int,
             completed: Optional[List[str]] = None) -> dict:
        ...

    def select_context(self, target_paper: str, knowledge: List[dict], *,
                       strategy: str, budget_tokens: int) -> dict:
        ...

    def filter_context_for_question(self, knowledge_items: List[dict],
                                    question: str, *,
                                    budget_tokens: int = 1000) -> dict:
        ...


@runtime_checkable
class ReadPort(Protocol):
    """Reading-task contract and production validation."""

    def prepare(self, task: dict, *, material: Optional[dict] = None) -> dict:
        ...

    def validate(self, result: dict, task: dict) -> dict:
        """Validate and return the *result itself* in ``data``.

        Contract: ``data`` is always the normalised ReadingResult, and the
        validation report travels in ``diagnostics``.  Returning the report as
        ``data`` (which the bundled library does) would make every caller
        responsible for remembering which shape it holds, and a write-back would
        silently store a report instead of a reading.
        """
        ...


class PortError(RuntimeError):
    """Raised by an adapter when the underlying library is unavailable."""
