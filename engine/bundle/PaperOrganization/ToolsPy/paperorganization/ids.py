"""ID 生成规则（元件：ids）。

规定：
    claims.claim_id             c_<safe_id>_<n>
    assumptions.assumption_id   a_<safe_id>_<n>
    claim_relations.rel_id      r_<sha1前16位>（内容哈希，幂等）
    media.media_id              fig|tab|form_<safe_id>_<n>
    span_map.span_id            span_<safe_id>_<n>

为什么集中在这里：
    ID 规则是跨模块契约，散落各处会漂移（之前就出现过 claim_id 与 rel_id 命名不一致）。
"""
from __future__ import annotations

import hashlib

from .schema import safe_id

# 媒体类型 → ID 前缀映射
MEDIA_PREFIX = {"figure": "fig", "table": "tab", "formula": "form"}


def claim_id(paper_id: str, n: int) -> str:
    """c_<safe_id>_<n>，n 从 1 起。"""
    return "c_{}_{}".format(safe_id(paper_id), n)


def assumption_id(paper_id: str, n: int) -> str:
    """a_<safe_id>_<n>，n 从 1 起。"""
    return "a_{}_{}".format(safe_id(paper_id), n)


def relation_id(src_claim: str, dst_claim: str, kind: str, polarity: str) -> str:
    """r_<sha1前16位>：内容哈希，保证同一条边重跑不重复。"""
    raw = "{}|{}|{}|{}".format(src_claim, dst_claim, kind, polarity)
    return "r_" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def media_id(paper_id: str, kind: str, n: int) -> str:
    """<prefix>_<safe_id>_<n>，n 从 0 起（与 PaperSearch 的 figure_objects 索引一致）。"""
    prefix = MEDIA_PREFIX.get(kind, "obj")
    return "{}_{}_{}".format(prefix, safe_id(paper_id), n)


def span_id(paper_id: str, n: int) -> str:
    """span_<safe_id>_<n>，n 从 1 起。"""
    return "span_{}_{}".format(safe_id(paper_id), n)


# ---------------------------------------------------------------------------
# 论点引用（方案A）：让 PaperRead 不必知道 claim_id 也能指认「哪一篇的第几个主张」
# ---------------------------------------------------------------------------
# 语法：<paper_id>#<n>          n 从 1 起，与 PaperRead 输出里 core_claims 的顺序一致
#   例：arxiv:2609.11923#2      表示该论文第 2 个核心主张
#       doi:10.1609/aaai.v26i1.8241#1
#
# 也接受已是 claim_id 的写法（宽松）：c_arxiv_2609.11923_2 原样通过。
#
# 为什么需要它：
#   claim_id 由落库方生成（c_<safe_id>_<n>），PaperRead 在读论文 B 时
#   不可能预先知道论文 A 的主张 ID。用「论文ID + 序号」表达意图，
#   落库时再解析成真实 claim_id —— PaperRead 只需知道自己读到的序号。

REF_SEP = "#"


def make_ref(paper_id: str, n: int) -> str:
    """构造引用字符串：<paper_id>#<n>。"""
    return "{}{}{}".format(paper_id, REF_SEP, n)


def is_claim_id(s: str) -> bool:
    """判断是否已是 claim_id 形态（c_..._<n>）。"""
    return isinstance(s, str) and s.startswith("c_") and s.count("_") >= 2


def is_ref(s: str) -> bool:
    """判断是否是「论文ID#序号」形态。

    注意：paper_id 本身可能含 ':' 和 '/'，但**不含 '#'**，
    所以按最后一个 '#' 切分是安全的。
    """
    if not isinstance(s, str) or REF_SEP not in s:
        return False
    tail = s.rsplit(REF_SEP, 1)[1]
    return tail.isdigit() and int(tail) >= 1


def parse_ref(s: str):
    """解析引用字符串，返回 (paper_id, n)；不是引用形态则返回 (None, None)。

    支持：
        "arxiv:2609.11923#2"       → ("arxiv:2609.11923", 2)
        "c_arxiv_2609.11923_2"     → (None, None)   ← 已是 claim_id，原样处理
    """
    if not is_ref(s):
        return None, None
    paper_id, tail = s.rsplit(REF_SEP, 1)
    n = int(tail)
    return (paper_id or None), (n if n >= 1 else None)


def resolve_claim_ref(ref: str, own_paper_id: str, conn) -> str:
    """把引用字符串解析成真实 claim_id。

    参数：
        ref            引用字符串（<paper_id>#<n> 或已是 claim_id）
        own_paper_id   当前正在落库的论文 ID（用于支持「本论文#n」简写与省略论文ID）
        conn           数据库连接（查已落库论文的 claims）

    返回解析后的 claim_id；无法解析时返回空串（调用方据此报错）。

    支持的写法：
        "arxiv:2609.11923#2"   跨论文：显式写论文 ID
        "#2"                   本论文：省略论文 ID，指当前落库的论文
        "c_arxiv_2609.11923_2" 已是 claim_id：原样返回
    """
    if not ref:
        return ""
    # 已是 claim_id：仍需校验存在性（防孤儿边）
    if is_claim_id(ref):
        hit = conn.execute("SELECT 1 FROM claims WHERE claim_id=?", (ref,)).fetchone()
        return ref if hit else ""
    # 省略论文 ID 的简写 "#2" → 指本论文
    if ref.startswith(REF_SEP):
        ref = "{}{}".format(own_paper_id, ref)
    paper_id, n = parse_ref(ref)
    if not paper_id or not n:
        return ""
    target = claim_id(paper_id, n)
    # 校验目标是否真实存在（防悬空）
    hit = conn.execute("SELECT 1 FROM claims WHERE claim_id=?", (target,)).fetchone()
    return target if hit else ""

