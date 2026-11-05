"""Research agent: turns a question into a structured corpus.

Responsibilities:

* first expand the task into a bounded keyword and query plan;
* run the full pipeline;
* return the on-disk search output plus the query plan in diagnostics.

The agent boundary is what is new: the query planner is a subagent, and the
pipeline itself is a port call, so replacing PaperSearch later changes one
adapter and no agent.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

from .base import AgentKit, agent_tool
from ..runtime.agent import Agent
from ..runtime.envelope import error, failed, ok
from ..runtime.machine import RouterPolicy
from ..runtime.messages import AgentReply, ToolCall


#: Words that carry no retrieval signal in an academic query. Question framing,
#: articles and connectives make arXiv/OpenAlex rank badly, so a long research
#: question must be reduced to its concept terms before it is sent.
_QUERY_STOPWORDS = {
    "what", "which", "who", "whom", "whose", "when", "where", "why", "how",
    "does", "do", "did", "is", "are", "was", "were", "be", "been", "being",
    "can", "could", "should", "would", "will", "shall", "may", "might", "must",
    "the", "a", "an", "of", "in", "on", "at", "to", "for", "from", "by", "with",
    "and", "or", "but", "not", "no", "nor", "if", "then", "than", "that", "this",
    "these", "those", "it", "its", "they", "them", "their", "we", "our", "us",
    "tell", "tells", "say", "says", "said", "about", "into", "over", "under",
    "between", "among", "such", "there", "here", "have", "has", "had", "also",
    "some", "any", "all", "both", "each", "more", "most", "other", "own",
    "so", "as", "up", "out", "off", "again", "further", "once", "only", "very",
    # Interrogative / hedging verbs: these frame the QUESTION, they are not
    # concepts. Leaving them in produces variants like "frameworks disagree
    # whether systems think", which retrieves nothing useful.
    "disagree", "disagrees", "agree", "whether", "if", "think", "thinks",
    "mean", "means", "consider", "considered", "regard", "regarded", "claim",
    "claims", "argue", "argues", "suggest", "suggests", "imply", "implies",
    "possess", "possesses", "understand", "understands", "explain", "explains",
    "tell", "describe", "describes", "define", "defines", "distinguish",
    "relationship", "relation", "between", "compare", "compared", "differ",
    "differs", "difference", "differences", "role", "roles", "way", "ways",
    "exhibit", "exhibits", "improve", "improves", "improved", "enable",
    "enables", "cause", "causes", "affect", "affects", "influence",
    "influences", "shape", "shapes", "drive", "drives", "support", "supports",
    "require", "requires", "allow", "allows", "provide", "provides",
}


#: Clause separators that mark a genuine phrase boundary in the user's own
#: sentence. Splitting on these keeps a variant semantically whole; splitting on
#: a raw word offset ("keywords[2:4]") does not -- it produced fragments like
#: "sociology philosophy", which is not a thing anyone searches for.
_CLAUSE_SPLIT = re.compile(
    r"[,;:.!?()\[\]\u2014\u2013]|\b(?:and|or|but|whereas|while|versus|vs)\b",
    re.IGNORECASE)


def _concept_phrases(text: str) -> List[str]:
    """Split a question into clause-level concept phrases, stopwords removed.

    Preserves the user's own grouping, so each phrase is something the question
    actually asserts as a unit.
    """
    phrases = []
    for clause in _CLAUSE_SPLIT.split(str(text or "")):
        tokens = re.findall(r"[A-Za-z][A-Za-z0-9'\-]*", clause)
        kept = [tok for tok in tokens
                if tok.lower() not in _QUERY_STOPWORDS and len(tok) >= 2]
        if kept:
            phrases.append(kept)
    return phrases


def build_retrieval_query(research_question: str, *, max_terms: int = 12) -> str:
    """Reduce a research question to a keyword query.

    Deterministic on purpose: the retrieval backends rank badly on a full
    natural-language question (an early live run sent the whole sentence and got
    gravitational-wave papers back for a question about LLM cognition). This is
    the "PTC-shaped" part of the module -- pure string work, no judgement.

    Keeps order and de-duplicates, so the phrase still reads like the user's
    topic rather than a bag of words.
    """
    text = str(research_question or "")
    seen = set()
    kept = []
    for tokens in _concept_phrases(text):
        for token in tokens:
            lowered = token.lower()
            if lowered in seen:
                continue
            seen.add(lowered)
            kept.append(token)
            if len(kept) >= max_terms:
                break
        if len(kept) >= max_terms:
            break
    if not kept:
        # Everything was a stopword: fall back to the original so the run is
        # never blocked by the reducer itself.
        return text.strip()
    return " ".join(kept)


def build_keyword_plan(research_question: str) -> dict:
    """Deterministic fallback for a richer, auditable retrieval plan.

    A model-backed Query Planner may add genuine synonyms and neighbouring
    terminology. The rule policy cannot invent semantic equivalents safely --
    claiming synonyms it did not derive would make the audit trail lie -- so it
    stays honest about its limits while still producing *valid* variants:

    * the main query (whole reduced concept set) is what the pipeline runs;
    * each clause-level concept phrase becomes one narrower variant, because the
      user already grouped those terms together;
    * variants are deduplicated, bounded, and never a mid-phrase slice.

    `expanded_keywords` stays empty rather than echoing `core_keywords`: an
    "expansion" identical to the core set is not an expansion, and downstream
    auditors should see that no synonym work happened.
    """
    text = str(research_question or "")
    reduced = build_retrieval_query(text)
    keywords = reduced.split()
    phrases = _concept_phrases(text)

    variants = []
    if reduced:
        variants.append(reduced)
    # One variant per clause, longest first: a clause of 2+ terms is a real
    # narrower query ("cognitive psychology"), a 1-term clause is a bare keyword
    # the main query already covers.
    clause_variants = [" ".join(tokens) for tokens in phrases if len(tokens) >= 2]
    clause_variants.sort(key=lambda item: (-len(item.split()), item))
    variants.extend(clause_variants)

    bounded = []
    for item in variants:
        if item and item not in bounded:
            bounded.append(item)
        if len(bounded) >= 4:      # cap: query explosion is a real cost
            break
    if not bounded and reduced:
        bounded = [reduced]

    return {
        "query": reduced,
        "core_keywords": keywords,
        # Empty on purpose: the rule policy performs no synonym expansion, and
        # pretending otherwise would corrupt the audit record (see docstring).
        "expanded_keywords": [],
        "query_variants": bounded,
        "reason": ("提取核心概念并按其原文短语边界生成由精确到窄域的检索入口"
                   "（规则回退，不做语义扩展）"),
    }


def build_citation_expansion_plan(options: Optional[dict] = None) -> dict:
    """Describe the bounded citation-chasing pass used by PaperSearch."""
    options = dict(options or {})
    offline = bool(options.get("papers") or options.get("search_output"))
    seed_budget = max(0, int(options.get("expand_top", 4)))
    return {
        "enabled": not offline and seed_budget > 0,
        "seed_paper_budget": seed_budget,
        "seed_selection": "top-ranked retrieved papers",
        "candidate_scope": "references/citations not already in corpus",
        "deduplication": "DOI, arXiv id, then normalized title/author/year",
        "offline_replay": offline,
    }


def build_query_planner(kit: AgentKit, *, agent_id: str) -> Agent:
    def draft_query(research_question: str = ""):
        return ok(build_keyword_plan(research_question))

    registry = kit.registry.with_tools([
        ("draft_query", "把研究问题转换为一次检索查询",
         {"type": "object",
          "properties": {"research_question": {"type": "string"}},
          "required": ["research_question"], "additionalProperties": False},
         draft_query),
    ])
    return Agent(role="query_planner",
                 system_prompt=kit.prompts.get("research_query_planner.md"),
                 tools=registry,
                 policy=kit.resolve_policy(agent_id, _QueryRulePolicy()),
                 budget=kit.budget(turns=3),
                 id=agent_id)


class _QueryRulePolicy:
    name = "query_rule"

    def respond(self, request):
        initial = dict(request.metadata.get("initial") or {})
        last = request.messages[-1] if request.messages else None
        if last is not None and last.role == "tool":
            try:
                return AgentReply(payload=json.loads(last.content))
            except json.JSONDecodeError:
                return AgentReply(payload=None)
        return AgentReply(text="", tool_calls=[ToolCall("draft_query", {
            "research_question": initial.get("research_question", "")})])


def build_research_agent(kit: AgentKit, *, agent_id: str = "research") -> Agent:
    planner = build_query_planner(kit, agent_id=agent_id + ".query")

    def run_research(research_question: str = "", run_id: str = "",
                     query: str = "", reason: str = "",
                     options: dict = None,
                     query_variants: list = None):
        if not str(research_question or "").strip():
            return failed(error("RESEARCH_INPUT_MISSING", "research_question 不能为空"))
        if not query:
            context = kit.context(planner.id, parent_id=agent_id)
            planned = planner.one_shot(
                context, research_question,
                metadata={"initial": {"research_question": research_question}})
            # The planner returns a full envelope, so `query` sits under
            # `payload.data.query` -- not at the top level. Reading only the top
            # level silently falls back to the raw research question, which is
            # what sent a whole natural-language sentence to the retrieval
            # backends and returned unrelated papers.
            payload = planned.payload or {}
            for _ in range(3):
                if isinstance(payload, dict) and "query" in payload:
                    break
                nxt = None
                for key in ("data", "payload"):
                    inner = payload.get(key) if isinstance(payload, dict) else None
                    if isinstance(inner, dict):
                        nxt = inner
                        break
                if nxt is None:
                    break
                payload = nxt
            query = str(payload.get("query") or research_question).strip()
            reason = str(payload.get("reason") or "")
            keyword_plan = {
                "core_keywords": list(payload.get("core_keywords") or []),
                "expanded_keywords": list(payload.get("expanded_keywords") or []),
                "query_variants": list(payload.get("query_variants") or []),
            }
        else:
            keyword_plan = {"core_keywords": [], "expanded_keywords": [],
                            "query_variants": [query]}
        if not query:
            return failed(error("RESEARCH_QUERY_INVALID", "查询规划器返回空查询"))
        search_options = dict(options or {})
        # Hand the planner's variants to the port so they are actually searched,
        # not merely recorded. The port bounds how many run.
        chosen_variants = [str(item) for item in (query_variants or []) if item]
        if not chosen_variants:
            chosen_variants = list(keyword_plan.get("query_variants") or [])
        search_options["query_variants"] = chosen_variants
        result = kit.ports.research.run(research_question, run_id=run_id,
                                        query=query, reason=reason,
                                        options=search_options)
        if not result["ok"]:
            return result
        result.setdefault("diagnostics", {})["query_plan"] = {
            "research_question": research_question, "query": query,
            "reason": reason, **keyword_plan,
            "citation_expansion": build_citation_expansion_plan(search_options)}
        return result

    registry = kit.registry.with_tools([
        ("run_search_pipeline", "执行一次可追溯的检索流水线",
         {"type": "object",
          "properties": {"research_question": {"type": "string"},
                         "run_id": {"type": "string"},
                         "query": {"type": "string"},
                         "reason": {"type": "string"},
                         "options": {"type": "object"},
                         "query_variants": {"type": "array",
                                            "items": {"type": "string"}}},
          "required": ["research_question", "run_id"],
          "additionalProperties": False},
         run_research),
    ])
    kit.register_agent(planner, name="research.plan_query")
    registry.register(agent_tool(
        planner, name="plan_query",
        description="让查询子智能体把研究问题转成检索查询",
        kit=kit, parent_id=agent_id))
    return Agent(role="research",
                 system_prompt=kit.prompts.get("research.md"),
                 tools=registry,
                 policy=kit.resolve_policy(agent_id, RouterPolicy(_ResearchRulePolicy())),
                 budget=kit.budget(),
                 id=agent_id)


class _ResearchRulePolicy:
    name = "research_rule"

    def respond(self, request):
        initial = dict(request.metadata.get("initial") or {})
        last = request.messages[-1] if request.messages else None
        if last is not None and last.role == "tool":
            try:
                return AgentReply(payload=json.loads(last.content))
            except json.JSONDecodeError:
                return AgentReply(payload=None)
        return AgentReply(text="", tool_calls=[ToolCall("run_search_pipeline", {
            "research_question": initial.get("research_question", ""),
            "run_id": initial.get("run_id", ""),
            "query": initial.get("query", ""),
            "reason": initial.get("reason", ""),
            "options": initial.get("options") or {},
        })])
