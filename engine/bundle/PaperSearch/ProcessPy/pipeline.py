#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pipeline.py ——的 8 步流水线编排

     STEP 1  研究问题理解      query.extract_concepts
     STEP 2  检索式生成        query.build_queries
     STEP 3  多源检索          sources.search_all
     STEP 4  实体统一          dedup.resolve + sources.enrich_crossref/resolve_oa
     STEP 5  初步排序与筛选     rank.rank_papers
     STEP 6  引用网络扩展       sources.expand_citations → 再去重再排序
     STEP 7  全文获取          fulltext.acquire_pdf
     STEP 8  结构解析          fulltext.parse_structure
            ↓
     Structured Literature Corpus（store.py 的三层落盘）

编排层的三条硬规则
------------------
1. **每步结束立刻落盘**（papers + state）。中断后 `--resume` 能从下一步接着跑，
   而不是从头再来 —— 这是 arxiv_harvest 那条教训的直接复用。

2. **单步失败不等于整体失败**。可降级的步骤（S2 无 key、GROBID 没起、
   某源限流）都记为 skipped/partial 并继续；只有"检索到 0 篇"才终止
   （后面所有步骤都没有输入了）。

3. **报告里必须写清"哪一步没做成"**。检索结果最容易骗人的地方就是
   "看起来有 50 篇"但实际只有一个源在起作用、或者全部没有全文。
   summarize + report.md 专门暴露这些。

函数索引
─────────────────────────────────────────────────────────────────────────────
  [上下文] RunContext                     一次运行的状态容器
  [主入口] run(cfg)                       按 8 步执行，返回 run 结果 dict
  [单步]   step_understand / step_search / step_dedup / step_rank_initial
           step_expand / step_rank_expanded / step_fulltext / step_structure
  [报告]   _finish(ctx)                   落盘 corpus/graph/report/run
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import os
import sys
import time
from typing import Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import PipelineConfig, describe as describe_cfg   # noqa: E402
from model import Paper                                        # noqa: E402
from store import CorpusStore, summarize                       # noqa: E402
import query as query_mod                                      # noqa: E402
import sources                                                 # noqa: E402
import dedup                                                   # noqa: E402
import rank                                                    # noqa: E402
import fulltext                                                # noqa: E402


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


class RunContext:
    """一次运行的状态容器（避免给每个 step 传一长串参数）。

    字段
    ----
    cfg       : PipelineConfig
    store     : CorpusStore
    papers    : 当前论文列表（各步就地修改/替换）
    plan      : STEP 1-2 的查询规划
    stats     : 各步的统计（写进 run.json 与 report.md）
    timeline  : [{"step","status","at","sec","info"}]
    grobid_ok : STEP 8 用的服务可用性（只探活一次）
    """

    def __init__(self, cfg: PipelineConfig, store: CorpusStore):
        self.cfg = cfg
        self.store = store
        self.papers: List[Paper] = []
        self.plan: dict = {}
        self.stats: dict = {}
        self.timeline: List[dict] = []
        self.grobid_ok: Optional[bool] = None

    def say(self, msg: str) -> None:
        """同时打屏与写 run.log。"""
        self.store.log(msg)
        if self.cfg.verbose:
            print(msg, flush=True)

    def done(self, step: str, status: str, sec: float, **info) -> None:
        self.timeline.append({"step": step, "status": status, "at": _now(),
                              "sec": round(sec, 2), "info": info})
        self.store.mark_step(step, status, sec=round(sec, 2), **info)


def _timed(ctx: RunContext, step: str, fn, *args, **kw):
    """跑一步并计时；异常转成 failed 记录，**不向上抛**。

    为什么吞异常：单步失败（比如某个源超时）不应该让整轮检索白跑。
    失败信息会进 timeline / stats，报告里能看到。
    """
    t0 = time.time()
    ctx.say("── {} …".format(step))
    try:
        info = fn(ctx, *args, **kw) or {}
        ctx.done(step, info.pop("status", "done"), time.time() - t0, **info)
        return True
    except Exception as exc:
        ctx.done(step, "failed", time.time() - t0, error="{}: {}".format(
            type(exc).__name__, str(exc)[:200]))
        ctx.say("   ✗ {} 失败：{}".format(step, str(exc)[:160]))
        return False


# =============================================================================
# STEP 1-2：查询理解与检索式生成
# =============================================================================

def step_understand(ctx: RunContext) -> dict:
    """STEP 1-2：研究问题 → 概念表 + 多源检索式。"""
    if not ctx.cfg.research_question and not ctx.cfg.queries:
        raise ValueError("既没有 research_question 也没有 queries，无法开始检索")
    ctx.plan = query_mod.build_queries(ctx.cfg.research_question, ctx.cfg)
    ctx.stats["query_plan"] = {
        "question": ctx.plan["question"],
        "sub_questions": ctx.plan["sub_questions"],
        "core_concepts": ctx.plan["core_concepts"],
        "n_concepts": len(ctx.plan["concepts"]),
        "queries": ctx.plan["queries"],
    }
    ctx.say("   核心概念（{}）：{}".format(
        len(ctx.plan["core_concepts"]), ", ".join(ctx.plan["core_concepts"][:6])))
    ctx.say("   检索式：arXiv {} 条 / OpenAlex {} 条 / S2 {} 条".format(
        len(ctx.plan["queries"]["arxiv"]), len(ctx.plan["queries"]["openalex"]),
        len(ctx.plan["queries"]["s2"])))
    # ★ 把 plan 一起写进断点：resume 跳过本步时才能恢复查询计划
    #   （只存 papers 不存 plan，续跑会拿着空 queries 去检索）
    ctx.store.mark_step("STEP1-2_understand", "done", plan=ctx.plan,
                        concepts=len(ctx.plan["core_concepts"]))
    return {"concepts": len(ctx.plan["core_concepts"]), "plan": ctx.plan}


# =============================================================================
# STEP 3：多源检索
# =============================================================================

def step_search(ctx: RunContext) -> dict:
    """STEP 3：三源检索，汇总候选。"""
    papers, stats = sources.search_all(ctx.plan, ctx.cfg)
    ctx.papers = papers
    ctx.stats["search"] = stats
    raw = sum(int(s.get("raw") or 0) for s in stats)
    for s in stats:
        note = s.get("note") or ""
        err = (s.get("errors") or [])
        ctx.say("   {:<18} 原始 {:<5} {}{}".format(
            s.get("source"), s.get("raw", 0),
            ("⚠ " + note) if note else "",
            (" | 错误 {} 条：{}".format(len(err), err[0][:70]) if err else "")))
    if raw == 0:
        raise RuntimeError("所有数据源都没有返回结果（检查网络 / Keys.txt / 检索式）")
    # ★ 源统计必须随**返回值**进断点：resume 跳过 STEP3 后 ctx.stats["search"]
    #   为空，最终状态会从 partial 变成 done —— 把"缺 S2 凭证"这个事实丢了。
    #   ⚠️ 不能在函数里直接调 mark_step：_timed 随后会用 ctx.done() 再次
    #      mark_step，而 mark_step 是**整体覆盖**（不是合并），先写的会被抹掉。
    #      走返回值才能和 _timed 的写入合到一起。
    return {"raw_total": raw, "sources": len(stats),
            "search_stats": stats, "status": "done"}


# =============================================================================
# STEP 4：实体统一 + 元数据校正
# =============================================================================

def step_dedup(ctx: RunContext) -> dict:
    """STEP 4：去重合并 → 校正 → OA 解析。

    顺序是有讲究的：
      1. 先 dedup —— 合并后标识符才齐全（arXiv 记录合并到含 DOI 的记录上），
         后续 Crossref/Unpaywall 才能少查很多次；
      2. 再 Crossref —— 补 DOI/venue/年份；
      3. 最后 Unpaywall —— 用刚补上的 DOI 去找 OA 全文。
         顺序颠倒的话，那些"本来能通过合并拿到 DOI"的论文会白查一遍。
    """
    papers, rep = dedup.resolve(ctx.papers, ctx.cfg)
    ctx.papers = papers
    ctx.stats["dedup"] = rep
    ctx.say("   去重：{} → {}（合并 {} 组，强标识符 {} 处 / 标题 {} 处）".format(
        rep["before"], rep["after"], rep["merged_groups"],
        rep["strong_links"], rep["weak_links"]))
    # 合并会重算 paper_id，把引用边里的旧 ID 重定向掉
    if rep.get("alias"):
        n = dedup.apply_alias(ctx.papers, rep["alias"])
        if n:
            ctx.say("   重定向引用边 {} 条（因合并导致 ID 变化）".format(n))

    cr = sources.enrich_crossref(ctx.papers, ctx.cfg)
    ctx.stats["crossref"] = cr
    ctx.say("   Crossref：查 {} 篇，采纳 {} 篇".format(cr["checked"], cr["adopted"]))

    # ★ Crossref 可能补上 DOI，而主键优先级是 doi > arxiv > openalex。
    #   不重算就会出现"有 DOI、主键却还是 openalex:W..."的记录
    #   （抽查发现的真实缺陷）。这里补一次重算 + 别名重定向。
    rk = dedup.rekey(ctx.papers)
    if rk:
        ctx.stats["rekey_after_crossref"] = {"changed": len(rk), "alias": rk}
        ctx.say("   主键重算：{} 篇升级为更强标识符（如 openalex→doi）".format(len(rk)))

    oa = sources.resolve_oa(ctx.papers, ctx.cfg)
    ctx.stats["unpaywall"] = oa
    ctx.say("   Unpaywall：查 {} 篇，新拿到 PDF 直链 {} 条".format(
        oa["checked"], oa["found_pdf"]))
    rk2 = dedup.rekey(ctx.papers)      # 同上：Unpaywall 也可能补标识符
    if rk2:
        ctx.say("   主键重算：{} 篇升级".format(len(rk2)))
    return {"before": rep["before"], "after": rep["after"]}


# =============================================================================
# STEP 5 / 6：排序与引用扩展
# =============================================================================

def step_rank_initial(ctx: RunContext) -> dict:
    """STEP 5：按 6 个维度打分排序，缩小候选集。"""
    concepts = ctx.plan.get("core_concepts") or ctx.plan.get("concepts") or []
    kept, rep = rank.rank_papers(ctx.papers, concepts, ctx.cfg, stage="initial")
    ctx.papers = kept
    ctx.stats["rank_initial"] = rep
    ctx.say("   排序筛选：{} 篇 → {} 篇（丢弃 撤稿{} 年份{} 低分{} 超限{}）".format(
        rep["in"], rep["out"], rep["dropped"]["retracted"], rep["dropped"]["year"],
        rep["dropped"]["low_score"], rep["dropped"]["over_limit"]))
    if kept:
        top = kept[0]
        ctx.say("   Top1 分数 {:.3f}：{}".format(
            top.score.get("total", 0), (top.metadata.get("title") or "")[:60]))
    return {"kept": rep["out"]}


def step_expand(ctx: RunContext) -> dict:
    """STEP 6：对高排名论文做引用网络扩展，然后合并、去重、再排序。

    为什么扩展后要**再来一遍去重和排序**：
      扩展拿回来的论文和已有候选大量重叠（参考文献往往就是候选中那几篇），
      而且新引进来的一批相关性参差不齐。不重排的话，扩展等于往候选池里
      灌了一批没排序的东西，反而劣化结果。
    """
    if not ctx.cfg.expand_citations:
        return {"skipped": True, "status": "skipped"}
    new, st = sources.expand_citations(ctx.papers, ctx.cfg)
    ctx.stats["expansion"] = st
    ctx.say("   扩展：种子 {} 篇 → 新增候选 {}（参考文献 {} / 被引 {}）".format(
        st["seeds"], len(new), st["refs"], st["cites"]))
    if not new:
        return {"new": 0}

    merged = ctx.papers + new
    papers, rep = dedup.resolve(merged, ctx.cfg)
    if rep.get("alias"):
        dedup.apply_alias(papers, rep["alias"])
    # ★ 扩展候选自带 DOI，需重算主键（理由见 dedup.rekey 的说明）
    rk = dedup.rekey(papers)
    if rk:
        ctx.say("   主键重算：{} 篇升级（扩展候选）".format(len(rk)))
    concepts = ctx.plan.get("core_concepts") or []
    kept, rrep = rank.rank_papers(papers, concepts, ctx.cfg, stage="expanded")
    ctx.papers = kept
    ctx.stats["rank_expanded"] = rrep
    ctx.say("   扩展后：{} 篇候选 → 去重 {} 篇 → 保留 {} 篇".format(
        len(merged), rep["after"], rrep["out"]))
    return {"new": len(new), "kept": rrep["out"]}


# =============================================================================
# STEP 7 / 8：全文与结构
# =============================================================================

def step_fulltext(ctx: RunContext) -> dict:
    """STEP 7：对排序最高的 N 篇抓 PDF（的成本控制）。"""
    if not ctx.cfg.fetch_pdf:
        return {"skipped": True, "status": "skipped"}
    targets = ctx.papers[:max(0, ctx.cfg.fetch_top_n)]
    ok = skipped = 0
    fails = []
    for p in targets:
        r = fulltext.acquire_pdf(p, ctx.store, ctx.cfg)
        if r["ok"]:
            ok += 1
            ctx.say("   ✓ {} [{}] {} KB".format(
                p.paper_id[:44], r["source"], int(r["bytes"] / 1024)))
        else:
            skipped += 1
            fails.append("{}: {}".format(p.paper_id[:30], r["error"][:60]))
            ctx.say("   ✗ {} {}".format(p.paper_id[:44], r["error"][:70]))
    ctx.stats["fulltext"] = {"targets": len(targets), "ok": ok, "failed": skipped,
                             "samples": fails[:5]}
    return {"targets": len(targets), "ok": ok, "failed": skipped}


def step_structure(ctx: RunContext) -> dict:
    """STEP 8：对已拿到 PDF 的前 N 篇做结构解析。

    GROBID 只探活一次（理由见 fulltext.grobid_available）：
    不可用时自动降级为纯文本，并把"这一批没有章节结构"写进统计，
    让报告能如实反映。
    """
    if not ctx.cfg.parse_structure:
        return {"skipped": True, "status": "skipped"}
    with_pdf = [p for p in ctx.papers if ctx.store.has_pdf(p)]
    targets = with_pdf[:max(0, ctx.cfg.parse_top_n)]
    if not targets:
        ctx.stats["structure"] = {"targets": 0, "note": "没有已下载的 PDF 可解析"}
        ctx.say("   跳过：没有本地 PDF")
        return {"targets": 0, "status": "skipped"}

    ok, ok_grobid, ok_fallback, fails = 0, 0, 0, []
    if ctx.grobid_ok is None:
        alive, msg = fulltext.grobid_available(ctx.cfg)
        ctx.grobid_ok = alive
        ctx.say("   GROBID {} | {}".format("可用" if alive else "不可用", msg))
        if not alive:
            ctx.say("   → 降级为纯文本兜底（无章节结构）；启动服务："
                    "./grobid-serve.sh start")
    for p in targets:
        r = fulltext.parse_structure(p, ctx.store, ctx.cfg, grobid_ok=bool(ctx.grobid_ok))
        if r.get("ok"):
            ok += 1
            if r.get("parsed_by") == "grobid":
                ok_grobid += 1
                ctx.say("   ✓ {} 章节 {} / 段落 {} / 图表公式 {}/{}/{}".format(
                    p.paper_id[:40], r.get("sections"), r.get("paragraphs"),
                    r.get("figures"), r.get("tables"), r.get("equations")))
            else:
                ok_fallback += 1
                ctx.say("   ~ {} 纯文本兜底 {} 字符（无结构）".format(
                    p.paper_id[:40], r.get("chars")))
        else:
            fails.append("{}: {}".format(p.paper_id[:30], str(r.get("error"))[:70]))
            ctx.say("   ✗ {} {}".format(p.paper_id[:40], str(r.get("error"))[:70]))
    ctx.stats["structure"] = {"targets": len(targets), "ok": ok,
                              "by_grobid": ok_grobid, "by_fallback": ok_fallback,
                              "failed": len(fails), "samples": fails[:5]}
    return {"targets": len(targets), "ok": ok, "grobid": ok_grobid,
            "fallback": ok_fallback}


# =============================================================================
# 收尾
# =============================================================================

def _finish(ctx: RunContext) -> dict:
    """落盘三层产物 + 统计 + 报告。

    ⚠️ 论文数取 `len(ctx.papers)`，**不能**取 `save_papers()` 的返回值：
       save_papers 在"输入为空但磁盘已有非空文件"时返回 -1（拒绝覆盖的保护），
       第一版直接把 -1 当成论文数，于是合法的空结果被报成
       `status=done, papers=-1`，而磁盘上还留着上一轮的非空语料库 ——
       统计、状态、产物三者自相矛盾（离线边界测试 G14）。
       "有多少篇论文"是关于**内存状态**的事实，就该从内存读。
    """
    n = len(ctx.papers)
    written = ctx.store.save_papers(ctx.papers)
    if written == -1:
        ctx.store.add_note(
            "save_papers 拒绝用空输入覆盖已有非空文件（保护生效）；"
            "本次论文数为 0（内存状态）")
    n_edges = ctx.store.save_graph(ctx.papers)
    if n_edges == -1:
        ctx.store.add_note("save_graph 拒绝用空输入覆盖已有非空图文件（保护生效）")
        n_edges = 0
    edges = ctx.store.load_graph()
    st = summarize(ctx.papers, edges)
    st["edges"] = n_edges
    ctx.stats["corpus"] = st
    report = ctx.store.write_report(ctx.papers, st)
    ctx.store.save_run(ctx.cfg, ctx.stats, ctx.timeline)
    ctx.stats["report_path"] = report
    return {"papers": n, "edges": n_edges}


# 步骤依赖：后面的步骤都依赖前面全部步骤
STEP_ORDER = ("STEP1-2_understand", "STEP3_search", "STEP4_dedup",
              "STEP5_rank_initial", "STEP6_expand", "STEP7_fulltext",
              "STEP8_structure")

# 哪些步骤失败算"整轮没做成"（必需阶段）。
# STEP6/7/8 可降级：没有扩展/PDF/GROBID 仍能交出可用语料库；
# 但前四步任何一步失败，产物就没有意义。
REQUIRED_STEPS = ("STEP1-2_understand", "STEP3_search", "STEP4_dedup",
                  "STEP5_rank_initial")


def scope_snapshot(cfg) -> dict:
    """把"决定检索范围"的配置抽出来，用于 resume 时检测漂移。

    ★ 为什么需要它（离线边界测试 G13）：
      resume 会跳过已完成的步骤。如果这次调用换了 `--year-from`，
      而做过滤的 STEP5 被当作"已完成"跳过，新过滤**根本不会施加** ——
      结果就是"任务范围与过滤配置不一致"，而 run.json 里记的却是新配置
      （记录与实际内容矛盾）。这类问题不会报错，只会静默给出错误范围的数据。

    分两组返回，因为失效的起点不同：
      "search" 改变 ⇒ 检索结果本身就不同 ⇒ 从 STEP1-2 起全部失效
      "filter" 改变 ⇒ 检索结果可复用，只是筛选规则变了 ⇒ 从 STEP5 起失效
    """
    search = {
        "research_question": cfg.research_question or "",
        "queries": sorted(cfg.queries or []),
        "use_arxiv": bool(cfg.use_arxiv),
        "use_openalex": bool(cfg.use_openalex),
        "use_semantic_scholar": bool(cfg.use_semantic_scholar),
        "per_source_limit": int(cfg.per_source_limit),
        "categories": sorted(cfg.categories or []),
    }
    filt = {
        "year_from": int(cfg.year_from or 0),
        "year_to": int(cfg.year_to or 0),
        "keep_top": int(cfg.keep_top),
        "min_score": float(cfg.min_score),
        "expand_citations": bool(cfg.expand_citations),
        "expansion_keep": int(cfg.expansion_keep),
    }
    return {"search": search, "filter": filt}


def _load_plan_from_state(store) -> dict:
    """resume 时从断点里恢复 STEP1-2 的产物（查询计划）。

    ★ 为什么必须单独恢复它（实测踩到的 P1 缺陷）：
      STEP1-2 完成后 ctx.plan 只存在内存里。resume 跳过 STEP1-2 时
      只加载了 papers，ctx.plan 仍是空字典 —— STEP3 拿到空 queries
      什么都检索不到，STEP5 也失去 concepts 无法打分。
      表现为"续跑成功但结果为空"，而且**不报错**。
    """
    steps = (store.state() or {}).get("steps", {})
    return (steps.get("STEP1-2_understand") or {}).get("plan") or {}


def run(cfg: PipelineConfig) -> dict:
    """按 8 步执行整条流水线。返回 {ok, steps, stats, out_dir, ...}。

    resume 语义：
      --resume 时会读已有 corpus.jsonl 作为起点，并跳过 state 里已 done 的步骤。
      注意**依赖链**：跳过的步骤必须保证它之后的步骤有输入 —— 所以这里的
      跳过判断是"从第一步开始连续已完成的才跳"，而不是逐步骤各自判断，
      否则会出现"STEP 5 跳过了但 STEP 3 没跑过、papers 是空的"这种错。
    """
    store = CorpusStore(cfg.out_dir, resume=cfg.resume)
    ctx = RunContext(cfg, store)
    step_failed: List[str] = []
    resume_broken = False
    scope_now = scope_snapshot(cfg)
    scope_checked = False

    if cfg.verbose:
        print("=" * 78)
        print("论文检索流程（V1）")
        print("=" * 78)
        print(describe_cfg(cfg))
        print("-" * 78)

    for name, fn in (("STEP1-2_understand", step_understand),
                     ("STEP3_search", step_search),
                     ("STEP4_dedup", step_dedup),
                     ("STEP5_rank_initial", step_rank_initial),
                     ("STEP6_expand", step_expand),
                     ("STEP7_fulltext", step_fulltext),
                     ("STEP8_structure", step_structure)):
        # ---- resume：只跳过"连续完成的前缀" ----
        # ⚠️ 必须是**前缀**语义，不能逐步独立判断：
        #    若 STEP3 失败而 STEP5 曾被标 done，独立判断会跳过 STEP5，
        #    可它的输入（papers）已经失效。所以一旦遇到未完成步骤，
        #    后面的都不再跳过（resume_broken 置位）。
        # ---- resume 的配置漂移检测（G13）----
        # 第一次进循环时比对一次；不一致就把受影响步骤的 done 标记清掉，
        # 让它们重新执行 —— 而不是默默用旧范围的数据配新配置。
        if cfg.resume and not scope_checked:
            scope_checked = True
            prev = (store.state() or {}).get("scope") or {}
            if prev:
                if prev.get("search") != scope_now["search"]:
                    n = store.invalidate_steps_from("STEP1-2_understand")
                    ctx.say("   ⚠️ 检测到**检索范围配置**与上次不同 → "
                            "已失效 {} 个步骤，将重新检索".format(n))
                    resume_broken = True
                elif prev.get("filter") != scope_now["filter"]:
                    n = store.invalidate_steps_from("STEP5_rank_initial")
                    ctx.say("   ⚠️ 检测到**筛选配置**与上次不同 → "
                            "已失效 {} 个步骤（保留检索结果，重新筛选）".format(n))
                    resume_broken = True
            store.set_scope(scope_now)

        skipped = False
        if cfg.resume and not resume_broken and store.step_done(name):
            if name == "STEP1-2_understand":
                try:
                    ctx.papers = store.load_papers()
                except Exception:
                    pass
                ctx.plan = _load_plan_from_state(store)
                # 恢复源可用性统计（否则 resume 后状态会误报 done）
                _steps = (store.state() or {}).get("steps", {})
                _ss = (_steps.get("STEP3_search") or {}).get("search_stats")
                if _ss:
                    ctx.stats["search"] = _ss
                if ctx.plan:
                    ctx.stats["query_plan"] = {
                        "restored_from_state": True,
                        "core_concepts": ctx.plan.get("core_concepts", []),
                        "queries": ctx.plan.get("queries", {}),
                    }
                    ctx.say("   已从断点恢复查询计划（{} 个核心概念）".format(
                        len(ctx.plan.get("core_concepts", []))))
                    skipped = True
                else:
                    ctx.say("   ⚠️ 断点里没有查询计划，重新执行本步")
                    resume_broken = True
            else:
                ctx.say("── {} 已完成，跳过（resume）".format(name))
                skipped = True
        if skipped:
            continue
        # 遇到未完成的步骤 → 后续不再享受"跳过"待遇
        if cfg.resume and not store.step_done(name):
            resume_broken = True
        # 每一步都先把当前 papers 落盘，保证中断不丢
        if not _timed(ctx, name, fn):
            step_failed.append(name)
        try:
            store.save_papers(ctx.papers)
        except Exception:
            pass

    fin = _finish(ctx)
    st = ctx.stats.get("corpus", {})

    # ---- 真实状态聚合 ----
    # ★ 第一版这里写死 `ok: True`，于是"检索到 0 篇"也报成功，
    #   下游 Agent 无法判断任务是否完成（实测 P1 缺陷）。
    #   现在按三件事判定：
    #     1) 必需阶段是否有失败（REQUIRED_STEPS）
    #     2) 语料库是否真的有内容（0 篇 = 没做成）
    #     3) 各源可用性（S2 没配 key 只影响"完整性"，不判失败但要如实标出）
    src_stats = ctx.stats.get("search") or []
    required_failed = [n for n in step_failed if n in REQUIRED_STEPS]
    optional_failed = [n for n in step_failed if n not in REQUIRED_STEPS]
    n_papers = int(fin["papers"] or 0)

    # 各源状态：ok / unavailable（缺凭证）/ failed
    source_status = {}
    for sd in src_stats:
        nm = sd.get("source", "?")
        if sd.get("available") is False:
            source_status[nm] = "unavailable"
        elif sd.get("errors"):
            source_status[nm] = "failed"
        else:
            source_status[nm] = "ok"

    if required_failed or n_papers == 0:
        status = "failed"
    elif optional_failed or any(v != "ok" for v in source_status.values()):
        status = "partial"
    else:
        status = "done"

    ok = (status != "failed")
    errs = []
    if required_failed:
        errs.append("必需阶段失败：{}".format(", ".join(required_failed)))
    if n_papers == 0:
        errs.append("语料库为空（0 篇）——检索没有产出")
    if optional_failed:
        errs.append("可降级阶段失败：{}".format(", ".join(optional_failed)))
    unavailable = [k for k, v in source_status.items() if v == "unavailable"]
    if unavailable:
        errs.append("数据源不可用（缺凭证）：{}".format(", ".join(unavailable)))

    result = {
        "ok": ok,
        "status": status,                  # done | partial | failed
        "error": "; ".join(errs),
        "errors": errs,
        "source_status": source_status,
        "steps_failed": step_failed,
        "out_dir": store.dir,
        "papers": fin["papers"], "edges": fin["edges"],
        "stats": ctx.stats, "timeline": ctx.timeline,
        "corpus_path": os.path.join(store.dir, "corpus.jsonl"),
        "graph_path": os.path.join(store.dir, "graph.jsonl"),
        "report_path": st.get("report_path") or ctx.stats.get("report_path", ""),
    }
    try:
        store.mark_step("FINAL", status, ok=ok, papers=n_papers)
    except Exception:
        pass
    if cfg.verbose:
        print("-" * 78)
        print("状态：{}（ok={}）{}".format(
            status, ok, " — " + result["error"] if result["error"] else ""))
        print("完成：{} 篇论文 / {} 条引用边".format(fin["papers"], fin["edges"]))
        print("  有摘要 {} | 有 PDF {} | 已结构解析 {} | 有引用图 {}".format(
            st.get("with_abstract"), st.get("with_pdf"),
            st.get("with_structure"), st.get("with_citation_graph")))
        print("  语料库 : {}".format(result["corpus_path"]))
        print("  引用图 : {}".format(result["graph_path"]))
        print("  报告   : {}".format(result["report_path"]))
    return result
