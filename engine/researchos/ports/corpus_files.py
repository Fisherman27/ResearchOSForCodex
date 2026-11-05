"""The one place that knows what a search-output directory contains.

Two producers write these directories and several consumers read them, so the
names are a contract, not an implementation detail:

    PaperSearch (current)   corpus.jsonl    graph.jsonl
    PaperSearch (older)     papers.jsonl    citation_edges.jsonl   ← still on disk
    knowledge base reads    corpus.jsonl    graph.jsonl

Keeping the aliases here — instead of repeating them in every consumer — is what
stops the two schemes from drifting apart again. A consumer that reads a search
directory must go through `normalise()`.
"""
from __future__ import annotations

import os

#: Canonical names the knowledge base reads.
CORPUS = "corpus.jsonl"
GRAPH = "graph.jsonl"

#: Accepted spellings, in preference order per role.
CORPUS_ALIASES = ("papers.jsonl",)
GRAPH_ALIASES = ("citation_edges.jsonl",)

#: Directories moved into the run so it stays portable.
DATA_DIRS = ("pdfs", "structures")


def _alias(directory: str, source: str, target: str) -> None:
    """Give `source` the name `target` expects, without ever raising."""
    import shutil
    src = os.path.join(directory, source)
    dst = os.path.join(directory, target)
    if not os.path.isfile(src) or os.path.isfile(dst):
        return
    try:
        shutil.copy2(src, dst)
    except OSError:
        # A read-only corpus directory is a legitimate input. Failing to write
        # the alias must leave the caller with its own "corpus not found"
        # message rather than an unrelated PermissionError from here.
        pass


def normalise(directory: str) -> str:
    """Make `directory` readable under the canonical names, and return it."""
    if not directory or not os.path.isdir(directory):
        return directory
    for source in CORPUS_ALIASES:
        _alias(directory, source, CORPUS)
    for source in GRAPH_ALIASES:
        _alias(directory, source, GRAPH)
    return directory


def corpus_path(directory: str) -> str:
    return os.path.join(directory, CORPUS)


def graph_path(directory: str) -> str:
    return os.path.join(directory, GRAPH)
