"""Ideation strategy layer: pluggable mechanisms for proposing research directions.

Mirrors ``PaperRead/ExamplePy/tools/paperread/strategy_loader.py`` deliberately:

* one strategy file = one mechanism, never combined;
* a missing/invalid strategy is an error, never a silent fallback;
* the strategy body is the *only* text sent to the model, and its SHA256 is
  returned so an experiment can prove which prompt produced a proposal;
* frontmatter is metadata for humans and must not enter the model context.

The whole point of this layer is comparability: "does a structured opportunity
discovery mechanism beat 'ask for 5 ideas'?" is only answerable if both arms
run through the same loader, the same output contract and the same filter.
"""
from __future__ import annotations

import hashlib
import os
import re
from typing import Dict, List, Optional

from ..runtime.envelope import empty, error, failed, ok


#: Strategies that are implemented and runnable today.
CORE_STRATEGY_IDS = ("I0", "I1")

#: Statuses that exist as design notes but must not be loaded as an arm.
NON_RUNNABLE_STATUS = ("excluded", "future", "draft")

DEFAULT_STRATEGY_ID = "I0"


class StrategyError(ValueError):
    """Raised when a strategy id cannot be resolved unambiguously."""


def _prompts_dir(directory: Optional[str] = None) -> str:
    if directory:
        return os.path.abspath(directory)
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.abspath(os.path.join(here, "..", "..", "prompts",
                                        "ideation"))


_FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)


def _parse_frontmatter(text: str) -> Dict[str, object]:
    """Minimal YAML-ish frontmatter reader.

    Only the flat ``key: value`` and ``key: >`` folded-block forms used by the
    strategy files are supported.  Keeping this local (rather than depending on
    PyYAML) matches the constraint that the runtime has no third-party deps.
    """
    match = _FRONTMATTER.match(text)
    if not match:
        return {}, text
    raw, body = match.group(1), match.group(2)
    meta: Dict[str, object] = {}
    lines = raw.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        index += 1
        if not line.strip() or line.strip().startswith("#"):
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key, value = key.strip(), value.strip()
        if value in (">", "|", ">-", "|-"):
            block: List[str] = []
            while index < len(lines):
                nxt = lines[index]
                if nxt.strip() and not nxt.startswith((" ", "\t")):
                    break
                block.append(nxt.strip())
                index += 1
            meta[key] = " ".join(part for part in block if part)
            continue
        meta[key] = value.strip("'\"")
    return meta, body


def list_strategies(directory: Optional[str] = None) -> dict:
    """Every ideation strategy on disk, with its runnable status."""
    root = _prompts_dir(directory)
    if not os.path.isdir(root):
        return failed(error("IDEATION_DIR_MISSING",
                            "策略目录不存在：{}".format(root),
                            hint="创建 prompts/ideation/ 或显式传入目录"))
    entries, warnings = [], []
    for name in sorted(os.listdir(root)):
        if not name.endswith(".md"):
            continue
        path = os.path.join(root, name)
        try:
            with open(path, encoding="utf-8") as handle:
                meta, body = _parse_frontmatter(handle.read())
        except OSError as exc:
            warnings.append("{}: 读取失败 {}".format(name, exc))
            continue
        if meta.get("role"):
            # Shared contracts (e.g. the common proposal contract) are not arms.
            continue
        sid = str(meta.get("strategy_id") or meta.get("name") or "").strip()
        if not sid:
            warnings.append("{}: 缺少 strategy_id，已跳过".format(name))
            continue
        entries.append({
            "strategy_id": sid,
            "name": meta.get("name"),
            "title": meta.get("title", ""),
            "status": meta.get("status", "unknown"),
            "mechanism": meta.get("mechanism", ""),
            "file": path,
            "body_sha256": hashlib.sha256(
                body.strip().encode("utf-8")).hexdigest(),
            "runnable": meta.get("status") not in NON_RUNNABLE_STATUS,
        })
    entries.sort(key=lambda item: (len(item["strategy_id"]),
                                   item["strategy_id"]))
    if not entries:
        return empty(entries, warnings=warnings,
                     diagnostics={"prompts_dir": root})
    return ok(entries, warnings=warnings,
              diagnostics={"prompts_dir": root, "count": len(entries)})


def load_strategy(strategy_id: str, directory: Optional[str] = None) -> dict:
    """Resolve one strategy id to its body, hash and metadata.

    Deliberately strict: an unknown id is a failure, and a non-runnable status
    is refused.  Falling back to a default here would silently turn a comparison
    experiment into two runs of the same arm.
    """
    root = _prompts_dir(directory)
    if not os.path.isdir(root):
        return failed(error("IDEATION_DIR_MISSING",
                            "策略目录不存在：{}".format(root)))
    identifier = str(strategy_id or "").strip()
    if not identifier:
        return failed(error("IDEATION_STRATEGY_MISSING",
                            "必须显式指定 ideation 策略 id",
                            hint="可用策略见 list_strategies()"))
    listing = list_strategies(root)
    if not listing["ok"]:
        return listing
    normalised = identifier.lower().removesuffix(".md")
    match = None
    for entry in listing["data"]:
        # Accept every stable handle, matching PaperRead's loader: the strategy
        # id (``I3``), the frontmatter name (``scientific_world_model``) and the
        # file stem (``i3_world_model``).  Experiments record prompts by id, but
        # humans refer to them by name.
        stem = os.path.splitext(os.path.basename(entry["file"]))[0]
        forms = {str(entry["strategy_id"]).lower(),
                 str(entry.get("name") or "").lower(),
                 stem.lower()}
        stripped = stem.lower()
        for prefix in (str(entry["strategy_id"]).lower() + "_",):
            if stripped.startswith(prefix):
                forms.add(stripped[len(prefix):])
        forms.discard("")
        if normalised in forms:
            match = entry
            break
    if match is None:
        available = [entry["strategy_id"] for entry in listing["data"]]
        return failed(error(
            "IDEATION_STRATEGY_UNKNOWN",
            "未知的 ideation 策略：{}".format(strategy_id),
            hint="可用：{}".format(", ".join(available) or "无")))
    if not match["runnable"]:
        return failed(error(
            "IDEATION_STRATEGY_NOT_RUNNABLE",
            "策略 {} 的状态是 {}，不能作为实验臂".format(
                match["strategy_id"], match["status"]),
            hint="只能加载 status 为 active 的策略"))
    with open(match["file"], encoding="utf-8") as handle:
        meta, body = _parse_frontmatter(handle.read())
    return ok({"strategy_id": match["strategy_id"],
               "name": match["name"],
               "title": match["title"],
               "status": match["status"],
               "mechanism": meta.get("mechanism", ""),
               "body": body.strip(),
               "body_sha256": match["body_sha256"],
               "file": match["file"]})


def common_contract(directory: Optional[str] = None) -> str:
    """The shared output contract every strategy is wrapped in.

    Kept separate from the strategy body so that an experiment varies only the
    *mechanism*, never the required fields.  A missing contract is an error: an
    ideation run without a fixed output shape is not comparable to anything.
    """
    root = _prompts_dir(directory)
    path = os.path.join(root, "common_proposal.md")
    if not os.path.isfile(path):
        raise StrategyError("ideation 公共契约缺失：{}".format(path))
    with open(path, encoding="utf-8") as handle:
        meta, body = _parse_frontmatter(handle.read())
    return body.strip()


def build_ideation_messages(strategy_id: str, payload: dict, *,
                            directory: Optional[str] = None) -> dict:
    """Assemble the exact messages for one ideation turn.

    Returns the messages plus the strategy hash, so a proposal can always be
    traced back to the prompt that produced it.
    """
    import json
    strategy = load_strategy(strategy_id, directory)
    if not strategy["ok"]:
        return strategy
    try:
        contract = common_contract(directory)
    except StrategyError as exc:
        return failed(error("IDEATION_CONTRACT_MISSING", str(exc)))
    messages = [
        {"role": "system", "content": contract},
        {"role": "user", "content": strategy["data"]["body"]},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]
    return ok({"messages": messages,
               "strategy_id": strategy["data"]["strategy_id"],
               "strategy_prompt_sha256": strategy["data"]["body_sha256"],
               "common_contract_sha256": hashlib.sha256(
                   contract.encode("utf-8")).hexdigest()})
