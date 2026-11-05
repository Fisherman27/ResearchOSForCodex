"""策略加载器（add2）。

职责：把 `reading_strategy_id` 解析成**恰好一段**发送给被测模型的策略正文。

硬规则：
  1. 只加载一个策略；不组合、不混用、不自动切换。
  2. 未指定 ID → baseline。
  3. ID 不存在 → 报错；**不得静默降级**到另一个策略。
  4. status 为 excluded / future 的策略拒绝加载（S9 / S10）。
  5. 只返回 frontmatter 之后的正文；frontmatter 是给人看的，**不得**进入模型上下文。
  6. 返回正文的 SHA256，作为实验可复现记录的 `strategy_prompt_sha256`。
"""

import hashlib
import os
import re

from . import frontmatter, result as R
from .errors import make_error

# 首轮实验可运行的策略
CORE_STRATEGY_IDS = ["S0", "S1", "S2", "S3", "S4", "S5", "S6", "S7", "S8"]

# 明确不参与首轮运行的策略及其状态
NON_RUNNABLE_STATUS = {"excluded", "future"}

DEFAULT_STRATEGY_ID = "S0"


def _prompts_dir(prompts_dir=None) -> str:
    if prompts_dir:
        return os.path.abspath(prompts_dir)
    # 默认：<repo>/prompts，相对本包位置向上两级
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.abspath(os.path.join(here, "..", "..", "prompts"))


def list_strategies(prompts_dir=None) -> dict:
    """列出所有策略文件及其元数据（供实验执行器与文档使用）。

    返回信封；data 为按 strategy_id 排序的条目列表。
    """
    pdir = _prompts_dir(prompts_dir)
    if not os.path.isdir(pdir):
        return R.fail(make_error("E_NOT_FOUND", "策略目录不存在：{}".format(pdir)))

    entries, warnings = [], []
    for fname in sorted(os.listdir(pdir)):
        if not fname.endswith(".md"):
            continue
        path = os.path.join(pdir, fname)
        try:
            fm, body, _ = frontmatter.load_file(path)
        except Exception as exc:  # 读取失败不应中断整个列举
            warnings.append("{}: 读取失败 {}".format(fname, exc))
            continue

        sid = fm.get("strategy_id") or fm.get("name")
        if not sid:
            warnings.append("{}: 缺少 strategy_id/name，已跳过".format(fname))
            continue
        # 带 role 的文件是公共契约（common_read / common_qa），不是可选策略，
        # 不得出现在实验策略清单里，否则会被误当实验臂。
        if fm.get("role"):
            continue
        entries.append({
            "strategy_id": sid,
            "name": fm.get("name"),
            "title": fm.get("title", ""),
            "status": fm.get("status", "unknown"),
            "model_visible": bool(fm.get("model_visible", fm.get("status") not in NON_RUNNABLE_STATUS)),
            "file": path,
            "body_sha256": hashlib.sha256(body.strip().encode("utf-8")).hexdigest(),
            "runnable": fm.get("status") not in NON_RUNNABLE_STATUS,
        })

    entries.sort(key=lambda e: (len(e["strategy_id"]), e["strategy_id"]))
    diag = {"prompts_dir": pdir, "count": len(entries)}
    if not entries:
        return R.empty(data=entries, warnings=warnings, diagnostics=diag)
    return R.ok(data=entries, warnings=warnings, diagnostics=diag)


def resolve_strategy_path(strategy_id: str, prompts_dir=None) -> str:
    """把 strategy_id 解析为策略文件绝对路径。找不到返回 None。"""
    if not strategy_id:
        return None
    pdir = _prompts_dir(prompts_dir)
    if not os.path.isdir(pdir):
        return None

    target = str(strategy_id).strip()
    # 目标侧也做同样的归一化：去掉 `.md` 与排序前缀，
    # 这样 `baseline` / `baseline.md` / `s0_baseline` / `S0` 都能解析。
    target_stem = target[:-3] if target.lower().endswith(".md") else target
    target_forms = {target.lower(), target_stem.lower()}
    stripped_target = _strip_order_prefix(target_stem)
    if stripped_target:
        target_forms.add(stripped_target.lower())
    target_forms = {f for f in target_forms if f}   # 空串绝不能成为匹配键

    for fname in sorted(os.listdir(pdir)):
        if not fname.endswith(".md"):
            continue
        path = os.path.join(pdir, fname)
        stem = os.path.splitext(fname)[0]

        # 文件名允许带数字序号前缀（如 s4_mechanism_reconstruction.md）。
        # 剥掉 `s4_` / `4_` 之后再比对，这样带不带序号都能解析。
        candidates = {fname.lower(), stem.lower()}
        stripped = _strip_order_prefix(stem)
        if stripped:
            candidates.add(stripped.lower())

        try:
            fm, _, _ = frontmatter.load_file(path)
        except Exception:
            continue

        # 以内容为准：strategy_id / name 是最权威的标识
        # （必须是**非空**值才参与比对，否则缺字段的文件会匹配任何查询）
        for key in ("strategy_id", "name"):
            value = str(fm.get(key, "") or "").strip().lower()
            if value and value in target_forms:
                return path
        if candidates & target_forms:
            return path
    return None


_ORDER_PREFIX_RE = re.compile(r"^(?:s)?\d{1,2}[_\-]+", re.IGNORECASE)


def _strip_order_prefix(stem: str) -> str:
    """剥掉 `s4_` / `4_` / `4-` 这类排序前缀，返回其余部分（无前缀时返回 ''）。"""
    stripped = _ORDER_PREFIX_RE.sub("", stem)
    return stripped if stripped != stem else ""


def load_strategy(strategy_id=None, prompts_dir=None, allow_non_runnable=False) -> dict:
    """加载一个策略，返回可直接拼进消息的正文。

    参数：
        strategy_id        : 'S4' / 'baseline' / 's4_mechanism_reconstruction' ...（序号前缀可选）
                             None 或空 → DEFAULT_STRATEGY_ID
        prompts_dir        : 策略目录；默认 <repo>/prompts
        allow_non_runnable : True 时允许加载 excluded/future 策略的正文
                             （**仅用于检查这类文件的内容，绝不可用于实验运行**）

    返回信封：
        data = {
          strategy_id, name, status, path,
          body,              # 送入模型的正文（已 strip）
          body_sha256,       # 实际发送内容的哈希
          frontmatter,       # 仅供人来阅读与记录，**不得**送入模型
          is_default,        # 是否因未指定而回落到 baseline
        }
    """
    sid = (strategy_id or "").strip() or DEFAULT_STRATEGY_ID
    is_default = not (strategy_id or "").strip()

    path = resolve_strategy_path(sid, prompts_dir)
    if not path:
        pdir = _prompts_dir(prompts_dir)
        return R.fail(make_error(
            "E_STRATEGY_UNKNOWN",
            "未找到策略 '{}'（目录：{}）".format(sid, pdir),
            details=[{"requested": sid, "prompts_dir": pdir}],
        ))

    try:
        fm, body, _ = frontmatter.load_file(path)
    except Exception as exc:
        return R.fail(make_error("E_INTERNAL", "读取策略文件失败：{}".format(exc)))

    if not fm:
        return R.fail(make_error(
            "E_STRATEGY_FRONTMATTER",
            "策略文件缺少 YAML frontmatter：{}".format(path)))

    # 公共契约（common_read / common_qa）不是可选策略，不得被当作策略加载。
    # 它必须经 load_common_contract 装载，否则"公共层固定"的边界会被打破。
    if fm.get("role"):
        return R.fail(make_error(
            "E_STRATEGY_UNKNOWN",
            "'{}' 是公共契约（role={}），不是可选策略".format(sid, fm.get("role")),
            details=[{"requested": sid, "role": fm.get("role"),
                      "hint": "请用 load_common_contract() 装载公共契约"}]))

    resolved_id = str(fm.get("strategy_id") or fm.get("name"))
    status = fm.get("status", "active")

    if status in NON_RUNNABLE_STATUS and not allow_non_runnable:
        return R.blocked(make_error(
            "E_STRATEGY_DISABLED",
            "策略 {} (status={}) 不得在本轮运行".format(resolved_id, status),
            details=[{"strategy_id": resolved_id, "status": status,
                      "reason": ("S9 无法实现真正闭卷隔离" if status == "excluded"
                                 else "S10 依赖尚未实现的 Scheduler 上下文")}],
        ))

    body = (body or "").strip()
    if not body:
        return R.fail(make_error(
            "E_STRATEGY_EMPTY",
            "策略 {} 的正文为空（frontmatter 之后没有内容）".format(resolved_id)))

    return R.ok(
        data={
            "strategy_id": resolved_id,
            "name": fm.get("name"),
            "status": status,
            "path": path,
            "body": body,
            "body_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
            "frontmatter": fm,
            "is_default": is_default,
        },
        warnings=(["未指定 reading_strategy_id，已按契约回落到 {}".format(DEFAULT_STRATEGY_ID)]
                  if is_default else []),
        diagnostics={"prompts_dir": _prompts_dir(prompts_dir)},
    )


def load_common_contract(name: str = "common_read", prompts_dir=None) -> dict:
    """加载公共任务契约（common_read / common_qa）的正文。

    公共契约与策略正文分开加载，确保"策略是可插拔变量、公共层固定"这一边界
    在代码层面也成立。
    """
    path = resolve_strategy_path(name, prompts_dir)
    if not path:
        return R.fail(make_error("E_NOT_FOUND", "未找到公共契约 '{}'".format(name)))
    fm, body, _ = frontmatter.load_file(path)
    body = (body or "").strip()
    if not body:
        return R.fail(make_error("E_STRATEGY_EMPTY", "公共契约 '{}' 正文为空".format(name)))
    return R.ok(data={
        "name": name,
        "path": path,
        "body": body,
        "body_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
        "frontmatter": fm,
    })


def build_read_messages(strategy_id=None, prompts_dir=None) -> dict:
    """按组装**纯消息数组**（不含 PDF 本体）。

    返回 messages 只包含：
        [{"role": "system", "content": common_read 正文},
         {"role": "user",   "content": 唯一选中策略正文}]

    调用方再把 PDF / 逐页图像作为 user 消息追加。**这里刻意不接收 PDF 路径**，
    以保证隔离规则（不挂载其他策略、不读历史结果）在装配阶段就成立。
    """
    common = load_common_contract("common_read", prompts_dir)
    if not common["ok"]:
        return common
    strat = load_strategy(strategy_id, prompts_dir)
    if not strat["ok"]:
        return strat

    messages = [
        {"role": "system", "content": common["data"]["body"]},
        {"role": "user", "content": strat["data"]["body"]},
    ]
    return R.ok(
        data={
            "messages": messages,
            "reading_strategy_id": strat["data"]["strategy_id"],
            "common_prompt_sha256": common["data"]["body_sha256"],
            "strategy_prompt_sha256": strat["data"]["body_sha256"],
        },
        warnings=common["warnings"] + strat["warnings"],
        diagnostics={"message_count": len(messages)},
        meta={"note": "PDF/图像由调用方追加；本函数不接触任何论文文件"},
    )
