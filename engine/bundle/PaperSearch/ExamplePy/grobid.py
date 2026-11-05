#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""grobid.py —— GROBID 与 PDF 结构解析

这不是"数据源"，而是**检索之后的结构化处理工具**：
    PDF → GROBID → TEI XML → 结构化论文（章节/段落/图表/公式/参考文献）

─────────────────────────────────────────────────────────────────────────────
为什么必须做结构解析
─────────────────────────────────────────────────────────────────────────────
错误做法：PDF → 一大段纯文本 → 喂给模型
正确做法：保留文档结构，后续分析才能定位"方法在第 3 节"、"局限在 5.2 段"

GROBID 能识别：
    title / authors / abstract
    section / paragraph（层级化的章节树）
    figure / table / formula（含标题与引用）
    reference（参考文献条目，可拆出 DOI/年份/作者）
    citation marker（正文里的 [12] 指向哪条参考文献）
    TEI XML 里还带部分元素的 PDF 坐标（可用于高亮回链）

─────────────────────────────────────────────────────────────────────────────
⚠️ 当前状态：**BLOCKED —— 本机无法部署 GROBID**
─────────────────────────────────────────────────────────────────────────────
实测（2026-09-28）本机环境：
    /usr/bin/java  → 空壳 stub："Unable to locate a Java Runtime"（**退出码却是 0**！）
    docker         → 未安装
    brew / conda   → 未安装
    pdftotext      → 未安装
    python pypdf / pdfminer / PyMuPDF / pdfplumber → 全部未安装
⇒ GROBID 需要 JRE 8+ 或 Docker，当前一个都没有。

**踩坑记录：macOS 的 /usr/bin/java 即使没装 JDK 也会以退出码 0 结束。**
所以判断"有没有 Java"必须解析输出文本，不能看退出码。
本文件的 java_available() 已按此实现。

─────────────────────────────────────────────────────────────────────────────
三条可选路线
─────────────────────────────────────────────────────────────────────────────
A) 本地部署 GROBID（推荐，一次装好长期可用）
     方式 1：装 Docker Desktop → docker run -t --rm -p 8070:8070 lfoppiano/grobid:0.8.1
     方式 2：装 OpenJDK 17 → 下载 GROBID release zip → ./gradlew run
     启动后服务在 http://localhost:8070，用本文件的方法调用
B) 远程 GROBID 服务（不想装环境时）
     把 GROBID_URL 指向别人的公网实例；注意隐私与稳定性
C) 降级：pypdf / pdftotext 只抽纯文本（**丢失结构**）
     由 conda 环境 researchos 提供：conda create -n researchos python=3.12 pypdf -c conda-forge -y
     （等价方案：pip install pypdf）
     代价：拿不到章节树、图表、参考文献结构，后续分析能力大打折扣

命令行用法：
    python3 grobid.py env                              # 只看本机环境
    python3 grobid.py isalive                          # GROBID 服务是否在线
    python3 grobid.py header paper.pdf                 # 只解析头部（快）
    python3 grobid.py fulltext paper.pdf --out out.tei.xml   # 全文 TEI
    python3 grobid.py fallback paper.pdf               # 纯文本兜底
    python3 grobid.py check

─────────────────────────────────────────────────────────────────────────────
函数索引（快速定位）
  ★ 本模块当前状态：本机无 JRE/Docker → GROBID 服务不可用；
    但**纯文本兜底已可用**（pypdf，由 conda 环境 researchos 提供）
  [环境]  find_sample_pdf() / java_available() / env_report()
          找样本 PDF / 检测 JRE（★ 必须解析输出，macOS 空壳 java 退出码为 0）/ 全量环境
  [服务]  Grobid.isalive() / version()
          健康检查与版本（发解析请求前务必先探活，否则白等超时）
  [解析]  Grobid.process_header(pdf_path, consolidate_header)
          只解头部（标题/作者/DOI/摘要），比全文快一个数量级
  [解析]  Grobid.process_fulltext(pdf_path, tei_coordinates, ...)
          完整解析 → TEI；teiCoordinates 支持元素名列表（多值参数）
  [解析]  Grobid.process_references(pdf_path, consolidate)
          只抽参考文献
  [解析]  parse_tei_header(xml)                 ★ DOI 限定在 teiHeader 内，作者查两处
  [解析]  parse_tei_fulltext(xml)               ★ 章节树 + paragraph_list 保留段落边界
  [解析]  parse_tei_references(xml)             biblStruct → 参考文献列表
  [兜底]  extract_text_fallback(pdf_path)
          pypdf / PyPDF2 / PyMuPDF / pdfminer / pdftotext 逐级降级（丢失结构）
  [自检]  check()  [CLI] main(argv)             env / isalive / header / fulltext / refs / fallback
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import re
import os
import shutil
import subprocess
import sys
import uuid
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import http_request, get_key, section, kv


def pdf_backend_note() -> str:
    """报告当前用哪个库做纯文本兜底，以及拿不到时该怎么办。

    pypdf 由 **conda 环境 `researchos`** 提供（Python 3.12 + pypdf 6.19.0）。
    曾经在目录内放过一份 vendor/ 副本作为可移植兜底，现已删除 ——
    副本会覆盖环境里装的版本，两处版本不一致时很难排查，得不偿失。

    如果换到一台没装这个环境的机器，纯文本兜底会失效，
    按下面的提示装一次即可（GEOBID 走的是另一条完全独立的路径）。
    """
    for mod in ("pypdf", "PyPDF2", "fitz", "pdfminer", "pdfplumber"):
        try:
            __import__(mod)
            return "使用环境中已安装的 {}".format(mod)
        except Exception:
            continue
    return ("无可用 PDF 解析库。修复方式二选一：\n"
            "        conda create -n researchos python=3.12 pypdf -c conda-forge -y\n"
            "        pip install pypdf")


PDF_BACKEND_NOTE = pdf_backend_note()

# ---- GROBID 的 teiCoordinates 支持哪些元素 -----------------------------------
# 官方参数是**可重复的多值参数**，值是元素名（不是 true/false）：
#     teiCoordinates=figure&teiCoordinates=ref&teiCoordinates=biblStruct
# 传 True 时展开成这个默认列表（覆盖结构分析最需要的几类元素）。
# 想只取部分元素就自己传列表，例如 ["figure", "table"]。
DEFAULT_COORDINATE_ELEMENTS = (
    "title", "paragraph", "head", "figure", "table", "formula",
    "ref", "biblStruct", "persName", "note",
)


def normalize_coordinates(tei_coordinates):
    """把 tei_coordinates 参数规整成**元素名列表**。

    接受三种输入：
        True / 真值           → DEFAULT_COORDINATE_ELEMENTS（展开成元素名）
        ["figure", "table"]   → 原样（去重、去空）
        False / None / ""     → []（不附带坐标）

    ⚠️ 踩过的坑：早期实现在 True 分支直接发送字符串 "true"。
       GROBID 的参数语义是"要坐标的元素名"，发 "true" 既不符合文档，
       也拿不到想要的坐标集合 —— 函数说明写着"附带全部元素坐标"，
       实际发出去的参数是无效值。
    """
    if not tei_coordinates:
        return []
    if tei_coordinates is True:
        return list(DEFAULT_COORDINATE_ELEMENTS)
    if isinstance(tei_coordinates, str):
        return [tei_coordinates]
    out = []
    for el in tei_coordinates:
        if el and el not in out:
            out.append(str(el))
    return out

# ---- 可调参数 ---------------------------------------------------------------
# GROBID 默认端口 8070；若用远程服务，在 Keys.txt 里改 GROBID_URL
DEFAULT_URL = "http://localhost:8070"
# 本地服务通常 1~5 秒；远程服务可能 30s+。consolidate 模式会去 Crossref 查证，更慢
HTTP_TIMEOUT = 120.0

TEI_NS = "{http://www.tei-c.org/ns/1.0}"


# =============================================================================
# 一、环境自检（不依赖 GROBID，也能跑）
# =============================================================================

def find_sample_pdf():
    """在 downloads/ 里找一个可用的样本 PDF。

    ⚠️ 踩过的坑：早期 check() 硬编码文件名 '1603.01121v1.pdf'，
       而 arxiv.py 下载出来的实际叫 '1603.01121.pdf' —— 于是自检每次都
       "跳过样本"，明明有文件却什么都没测到（假跳过）。
       现在改成扫描目录，文件名不一致也能找到。

    返回第一个匹配的绝对路径；找不到返回 ""。
    """
    d = os.path.join(os.path.dirname(os.path.abspath(__file__)), "downloads")
    if not os.path.isdir(d):
        return ""
    for name in sorted(os.listdir(d)):
        if name.lower().endswith(".pdf"):
            return os.path.join(d, name)
    return ""


def find_local_jdk():
    """查找本目录内自带的 JDK（`tools/jdk-*/Contents/Home`）。

    为什么需要它：本机是 macOS，**系统不预装 JDK**，`/usr/bin/java` 只是 Apple 的
    "定位器" —— 找不到 JVM 就打印 "Unable to locate a Java Runtime"（退出码 0）。
    为了让 GROBID 完全可移植、不依赖系统安装，我们把 JDK 解压在
    `tools/jdk-<version>/` 下随目录一起走。部署方式见 grobid-serve.sh。

    返回 JDK 根目录（含 bin/java 的那层）或 ""。
    """
    import glob
    base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tools")
    for p in sorted(glob.glob(os.path.join(base, "jdk-*", "Contents", "Home"))):
        if os.access(os.path.join(p, "bin", "java"), os.X_OK):
            return p
    return ""


def java_available():
    """检测可用的 Java 运行时。优先本目录自带的 JDK，其次系统 PATH。

    ⚠️ 关键坑：macOS 的 /usr/bin/java 在**未安装 JDK** 时也会打印
       "Unable to locate a Java Runtime" 并**以退出码 0 结束**。
       所以必须看输出内容，不能看 returncode。

    返回 (可执行路径 or None, 说明字符串)
    """
    local = find_local_jdk()
    exe = os.path.join(local, "bin", "java") if local else shutil.which("java")
    if not exe:
        return None, "未找到 java（系统未装 JDK，且本目录 tools/ 下也没有）"
    try:
        proc = subprocess.run([exe, "-version"], stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, timeout=20)
    except Exception as exc:
        return None, "执行 java 失败: {}".format(exc)
    out = proc.stdout.decode("utf-8", "replace")
    if "Unable to locate a Java Runtime" in out or "No Java runtime present" in out:
        return None, "{} 是空壳 stub，未安装任何 JRE".format(exe)
    if local:
        return exe, "本目录自带 JDK（{}） | {}".format(
            local, (out.splitlines() or [""])[0].strip())
    if "version" in out:
        first = out.splitlines()[0] if out.splitlines() else ""
        return exe, first.strip()
    return None, "java 输出异常: {}".format(out.strip()[:80])


def env_report():
    """列出所有与 PDF 解析相关的外部依赖状态。

    返回 {"java", "docker", "grobid", "pdftotext", "parsers": {...}, "ready": bool}
      ready=True 表示"至少有一条路可走"（GROBID 服务在线 或 有纯文本兜底工具）
    """
    java_exe, java_msg = java_available()
    report = {
        "java": (java_exe, java_msg),
        "local_jdk": find_local_jdk(),
        "docker": shutil.which("docker"),
        "grobid": shutil.which("grobid"),
        "pdftotext": shutil.which("pdftotext"),
        "mutool": shutil.which("mutool"),
        "parsers": {},
    }
    # Python 侧兜底库：按推荐顺序探测
    for mod in ("pypdf", "PyPDF2", "fitz", "pdfminer", "pdfplumber"):
        try:
            __import__(mod)
            report["parsers"][mod] = True
        except Exception:
            report["parsers"][mod] = False
    have_parser = any(report["parsers"].values())
    report["ready"] = bool(java_exe or report["docker"]) or have_parser or bool(report["pdftotext"])
    return report


# =============================================================================
# 二、GROBID 服务调用
# =============================================================================

class Grobid:
    """GROBID 客户端。

    用法：
        g = Grobid()                 # 默认 http://localhost:8070（或 Keys.txt 的 GROBID_URL）
        if g.isalive():
            tei = g.process_fulltext("paper.pdf")
    """

    def __init__(self, base_url: str = "", timeout: float = HTTP_TIMEOUT):
        self.base_url = (base_url or get_key("GROBID_URL", default=DEFAULT_URL)).rstrip("/")
        self.timeout = timeout

    # ---- 健康检查 -----------------------------------------------------------
    def isalive(self):
        """GET /api/isalive → 返回 (是否在线, 响应文本)。

        没有这个探测就发解析请求的话，会白等 120 秒超时。
        """
        status, body, _ = http_request(self.base_url + "/api/isalive",
                                       timeout=8.0, retries=0)
        return status == 200, body.decode("utf-8", "replace").strip()

    def version(self):
        """GET /api/version → 服务版本（排错时有用）。"""
        status, body, _ = http_request(self.base_url + "/api/version",
                                       timeout=8.0, retries=0)
        return status, body.decode("utf-8", "replace").strip()

    # ---- 解析 ---------------------------------------------------------------
    def _multipart(self, pdf_bytes: bytes, filename: str = "paper.pdf",
                   fields=None):
        """手工构造 multipart/form-data body（标准库没有现成的编码器）。

        参数
        ----
        fields : 支持两种形式
                 - dict  {"consolidateHeader": 1}
                 - **列表** [(name, value), ...]  ← 允许同名参数重复出现

        为什么要支持列表：GROBID 的 `teiCoordinates` 可以重复传多次
            （teiCoordinates=figure&teiCoordinates=ref&teiCoordinates=biblStruct）。
        dict 表达不了重复键，早期实现因此无法发送多值参数。

        为什么不用 requests：本库坚持零第三方依赖，方便迁移到任何环境。
        """
        boundary = "----researchos" + uuid.uuid4().hex
        pairs = list(fields.items()) if isinstance(fields, dict) else list(fields or [])
        out = b""
        for k, v in pairs:
            out += ("--%s\r\n" % boundary).encode()
            out += ('Content-Disposition: form-data; name="%s"\r\n\r\n' % k).encode()
            out += (str(v) + "\r\n").encode()
        out += ("--%s\r\n" % boundary).encode()
        out += ('Content-Disposition: form-data; name="input"; filename="%s"\r\n'
                % filename).encode()
        out += b"Content-Type: application/pdf\r\n\r\n"
        out += pdf_bytes + b"\r\n"
        out += ("--%s--\r\n" % boundary).encode()
        return boundary, out

    def _preflight(self):
        """发解析请求前的探活。

        为什么必须有：解析请求的超时是 120s（远程服务更久），
        如果服务根本没起，POST 会一直等到超时才报错 —— 批量处理时
        每篇论文白等两分钟，看起来像"卡死"。
        先花 8 秒探一下 /api/isalive，不在就直接返回明确错误。

        返回 None 表示服务可用；否则返回一个可直接返回给调用方的错误 dict。
        """
        alive, msg = self.isalive()
        if alive:
            return None
        return {"status": 0, "ok": False, "error":
                "GROBID 服务不可用（{}）：{}。请先启动服务，"
                "或用 grobid.py fallback 做纯文本兜底".format(
                    self.base_url, msg or "无响应")}

    def process_header(self, pdf_path: str, consolidate_header: int = 1):
        """POST /api/processHeaderDocument —— **只解析文档头部**，速度快。

        参数
        ----
        consolidate_header : 0/1/2
            1（默认）去 Crossref 查证作者与 DOI，更准但要联网。
                     实测约 1~6 秒（只有少量作者要查，代价可接受），
                     所以这里保留 1 —— 与 consolidate_citations 不同，后者是逐条
                     参考文献查，必须默认关掉
            0 纯本地解析，最快，适合大批量
            2 更激进的查证
        返回 {"status", "tei": str, "title": str, "authors": [...], "doi": str,
              "abstract": str, "error": str}

        用途：**大批量论文只需要标题/作者/DOI/摘要时用它**，
              比全文解析快一个数量级。也是实体统一的又一条信息来源。
        """
        if not os.path.isfile(pdf_path):
            return {"status": 400, "ok": False, "error": "文件不存在: {}".format(pdf_path)}
        pre = self._preflight()
        if pre is not None:
            return pre
        with open(pdf_path, "rb") as fh:
            pdf_bytes = fh.read()
        boundary, body = self._multipart(
            pdf_bytes, os.path.basename(pdf_path),
            {"consolidateHeader": consolidate_header})
        status, raw, _ = http_request(
            self.base_url + "/api/processHeaderDocument", method="POST", data=body,
            headers={"Content-Type": "multipart/form-data; boundary=" + boundary},
            timeout=self.timeout, retries=0)
        text = raw.decode("utf-8", "replace")
        if status != 200:
            return {"status": status, "ok": False, "tei": "", "error": text[:300]}
        parsed = parse_tei_header(raw)
        if parsed.get("error"):
            return {"status": 200, "ok": False, "tei": text, "error": parsed["error"]}
        return {"status": 200, "ok": True, "tei": text, **parsed}

    def process_fulltext(self, pdf_path: str, tei_coordinates=False,
                         consolidate_citations: int = 0, out_path: str = ""):
        """POST /api/processFulltextDocument —— **完整解析**，得到结构化论文。

        参数
        ----
        pdf_path               : 本地 PDF 路径
        tei_coordinates        : 是否在 TEI 里附带 PDF 坐标（用于高亮回链）。
                                 支持三种写法：
                                   False / None        → 不附带（默认，响应最小）
                                   True                → 附带全部元素坐标
                                   ["figure","ref"]    → ★ 只附带指定元素的坐标
                                 ⚠️ GROBID 的参数名就叫 `teiCoordinates`，是一个
                                    **可重复的多值参数**，官方示例是
                                    `teiCoordinates=figure&teiCoordinates=ref`。
                                    早期实现写死成 "true"/"false" 字符串，
                                    既不符合官方语义，也无法只取部分元素。
                                    现在按列表展开成多个同名参数。
        consolidate_citations  : 0/1，是否去 Crossref 逐条补全参考文献的 DOI。
                                 ★ **默认 0**。实测同一篇 10 页论文（本地 GROBID 0.9.1）：
                                     consolidateCitations=0 → 1.6 秒
                                     consolidateCitations=1 → 220 秒（慢 140 倍）
                                   因为它会对每条参考文献发一次 Crossref 查询，
                                   GROBID 还自带限速（日志可见降到 1 req/1000ms）。
                                   需要 DOI 补全时建议**离线批量**跑，不要放在交互路径上。
        out_path               : 指定则把 TEI XML 落盘（推荐，便于复用）

        返回 {"status", "ok", "tei", "sections", "references",
              "figures", "tables", "equations", "paragraphs_total", "error"}

          sections  : [{'title': 'Introduction', 'level': 1, 'paragraphs': 3,
                        'paragraph_list': ['段落1', '段落2'], 'text': '...'}]
          references: [{'title', 'authors', 'year', 'doi', 'raw'}]

        用途：论文分析模块的输入。有了结构化章节，
              才能回答"方法是什么（第 3 节）"、"局限在哪（第 5 节）"。
        """
        if not os.path.isfile(pdf_path):
            return {"status": 400, "ok": False, "error": "文件不存在: {}".format(pdf_path)}
        pre = self._preflight()
        if pre is not None:
            return pre
        with open(pdf_path, "rb") as fh:
            pdf_bytes = fh.read()

        # 多值参数：同名重复出现
        fields = [("consolidateCitations", consolidate_citations)]
        if tei_coordinates:
            for el in normalize_coordinates(tei_coordinates):
                fields.append(("teiCoordinates", el))

        boundary, body = self._multipart(pdf_bytes, os.path.basename(pdf_path), fields)
        status, raw, _ = http_request(
            self.base_url + "/api/processFulltextDocument", method="POST", data=body,
            headers={"Content-Type": "multipart/form-data; boundary=" + boundary},
            timeout=self.timeout, retries=0)
        text = raw.decode("utf-8", "replace")
        if status != 200:
            return {"status": status, "ok": False, "tei": "", "sections": [],
                    "references": [], "error": text[:300]}

        # ★ 必须真的解析一遍才能判定成功。
        #   早期实现只查前 2000 字节有没有 "<TEI"：截断的 XML（有开头没结尾）
        #   也能通过这个子串检查，然后 parse_tei_fulltext 内部把 ET.ParseError
        #   吞掉、返回空 sections，上层看到 ok=True + 0 章节，
        #   完全分不清"这篇真的没有正文"和"响应被截断了"。
        parsed = parse_tei_fulltext(raw)
        if parsed.get("error"):
            return {"status": 200, "ok": False, "tei": text, "sections": [],
                    "references": [], "error": parsed["error"]}

        if out_path:
            with open(out_path, "w", encoding="utf-8") as fh:
                fh.write(text)
        return {"status": 200, "ok": True, "tei": text, "error": "", **parsed}

    def process_references(self, pdf_path: str, consolidate: int = 0):
        """POST /api/processReferences —— 只抽参考文献。

        consolidate : 0/1，是否去 Crossref 补 DOI。**默认 0**，理由同 process_fulltext
                      （开 1 会对每条参考文献发一次远程查询，慢两个数量级）。

        用途：专门补引用图。比全文解析快，且 references 的结构最完整
              （含 DOI、年份、作者、题名）。
        """
        if not os.path.isfile(pdf_path):
            return {"status": 400, "ok": False, "references": [],
                    "error": "文件不存在: {}".format(pdf_path)}
        pre = self._preflight()
        if pre is not None:
            return pre
        with open(pdf_path, "rb") as fh:
            pdf_bytes = fh.read()
        boundary, body = self._multipart(pdf_bytes, os.path.basename(pdf_path),
                                         {"consolidateCitations": consolidate})
        status, raw, _ = http_request(
            self.base_url + "/api/processReferences", method="POST", data=body,
            headers={"Content-Type": "multipart/form-data; boundary=" + boundary},
            timeout=self.timeout, retries=0)
        text = raw.decode("utf-8", "replace")
        if status != 200:
            return {"status": status, "ok": False, "references": [], "error": text[:300]}
        # 用完整解析做校验（根元素必须是 TEI），避免把损坏响应当成"没有参考文献"
        parsed = parse_tei_fulltext(raw)
        if parsed.get("error"):
            return {"status": 200, "ok": False, "references": [],
                    "error": parsed["error"]}
        return {"status": 200, "ok": True,
                "references": parse_tei_references(raw), "error": ""}


# =============================================================================
# 三、TEI XML 解析（把 GROBID 的输出变成 Python 结构）
# =============================================================================

def _text_of(el) -> str:
    """把一个 TEI 元素的所有文本（含内联子元素）拼成规范化的字符串。

    必须用 itertext() 而不是 .text —— 正文段落里到处都是内联标记：
        <p>Hello <ref target="#b0">world</ref>.</p>
    只看 .text 会得到 "Hello "，把 <ref>/<hi>/<formula> 里的内容全丢掉。
    """
    if el is None:
        return ""
    return " ".join(" ".join(el.itertext()).split())


def parse_tei_header(xml_bytes):
    """从 TEI 头部抽出 title / authors / doi / abstract。

    ⚠️ 三个必须避开的坑（都会**静默**给出错误结果）：

    1. **作者要跨两个位置找**。GROBID 常见两种放法：
           <teiHeader><fileDesc><titleStmt><author><persName>...      （标准位置）
           <teiHeader><fileDesc><sourceDesc><biblStruct><analytic><author><persName>
       只查 titleStmt 会漏掉第二种，作者变成空列表。
       所以这里：先取 titleStmt；为空则退回 sourceDesc。

    2. **DOI 必须限定在 teiHeader 内**。TEI 全文里有大量 `<idno type="DOI">`
       藏在参考文献（<text><back><listBibl>）中。早期实现遍历整个文档，
       最后一条覆盖前面，结果把**参考文献的 DOI 当成了本文的 DOI**。

    3. **摘要要取全部段落并保留内联文本**，不能只取第一个 <p> 的 .text。

    返回 {"title", "authors", "doi", "abstract"}
    """
    try:
        root = ET.fromstring(xml_bytes)
    except Exception as exc:
        # ★ 不再静默吞掉：调用方必须能区分"响应损坏"与"这篇确实没有元数据"
        return {"title": "", "authors": [], "doi": "", "abstract": "",
                "error": "TEI XML 解析失败：{}: {}".format(type(exc).__name__, exc)}
    if root.tag.rsplit("}", 1)[-1].lower() != "tei":
        return {"title": "", "authors": [], "doi": "", "abstract": "",
                "error": "根元素不是 TEI（实际为 <{}>）".format(root.tag)}

    # 把搜索范围**限定在 teiHeader**：这是避开参考文献 DOI 的关键
    header = root.find("%steiHeader" % TEI_NS)
    scope = header if header is not None else root

    title = ""
    ts = scope.find(".//%stitleStmt" % TEI_NS)
    if ts is not None:
        title = _text_of(ts.find("%stitle" % TEI_NS))
    if not title:
        title = _text_of(scope.find(".//%stitle" % TEI_NS))

    def collect_persnames(el):
        """从某个子树里抽取 persName → "Forename Surname"。"""
        names = []
        for p in el.findall(".//%spersName" % TEI_NS):
            forenames = p.findtext("%sforename" % TEI_NS, default="")
            surname = p.findtext("%ssurname" % TEI_NS, default="")
            name = " ".join(filter(None, [forenames.strip(), surname.strip()]))
            # 兜底：有些模板直接把全名放在 <persName> 的文本里
            if not name:
                name = _text_of(p)
            if name:
                names.append(name)
        return names

    authors = []
    if ts is not None:
        authors = collect_persnames(ts)
    if not authors:
        # 位置 2：sourceDesc/biblStruct/analytic/author
        sd = scope.find(".//%ssourceDesc" % TEI_NS)
        if sd is not None:
            authors = collect_persnames(sd)

    # DOI：只在 teiHeader 里找，且优先 sourceDesc 里的那条
    doi = ""
    sd = scope.find(".//%ssourceDesc" % TEI_NS)
    for search_root in (sd, scope):
        if search_root is None:
            continue
        for idno in search_root.findall(".//%sidno" % TEI_NS):
            if (idno.get("type") or "").upper() == "DOI":
                doi = (idno.text or "").strip()
                break
        if doi:
            break

    abstract_el = scope.find(".//%sabstract" % TEI_NS)
    if abstract_el is None:
        abstract_el = root.find(".//%sabstract" % TEI_NS)
    abstract = " ".join(_text_of(p) for p in abstract_el.findall(".//%sp" % TEI_NS)) \
        if abstract_el is not None else ""
    if not abstract and abstract_el is not None:
        abstract = _text_of(abstract_el)

    return {"title": " ".join((title or "").split()), "authors": authors,
            "doi": doi, "abstract": abstract.strip(), "error": ""}


def _localname(tag) -> str:
    """去掉 XML 命名空间前缀，取元素本地名。

    TEI 文档里元素可能带默认命名空间（`{http://www.tei-c.org/ns/1.0}figure`），
    也可能不带。用本地名比较，两种写法都能处理。
    """
    return str(tag).rsplit("}", 1)[-1]


def _findall_local(elem, name):
    """按本地名查找后代元素（忽略命名空间）。"""
    name = name.lower()
    return [e for e in elem.iter() if _localname(e.tag).lower() == name]


def parse_tei_figures_tables_formulas(xml_bytes):
    """抽取图表公式的**对象**（不只是计数）。

    ★ 为什么必须给对象而不是数字（验收文档 test.md STEP7 第 3 条：
      「不能只保存总数」）：
      只报"7 张图"对后续论文理解毫无用处 —— 既不知道图在哪一节、
      也不知道图注说了什么。这里保留 xml:id、类型、标签（caption，
      常含图注正文）、所在章节序号，以及原始 TEI 定位。

    返回 {"figures": [...], "tables": [...], "formulas": [...]}
      每项：{"id", "type", "label", "text", "section_index", "raw_tag"}
      公式额外带 "n_formula"（TEI 里的 formula 计数，用于核对）。

    ⚠️ 注意 label 与 text 的取舍：GROBID 有时把图注放在 <label>，
      有时放在 <figDesc>；两个都取并合并，避免"图注明明存在却是空"。
    """
    out = {"figures": [], "tables": [], "formulas": []}
    try:
        root = ET.fromstring(xml_bytes)
    except Exception:
        return out
    if _localname(root.tag).lower() != "tei":
        return out

    # 章节序号：按文档顺序给每个 div 编号，便于把图表关联回章节
    div_index = {}
    for i, d in enumerate(_findall_local(root, "div")):
        div_index[id(d)] = i

    def owner_section(el):
        """向上找最近的 div，返回它的序号；找不到返回 -1。"""
        cur = el
        while cur is not None:
            if _localname(cur.tag).lower() == "div":
                return div_index.get(id(cur), -1)
            cur = getattr(cur, "getparent", lambda: None)()
        return -1

    # ElementTree 没有 getparent，改用显式父子映射
    parent = {}
    for p in root.iter():
        for c in p:
            parent[id(c)] = p

    def owner_section2(el):
        cur = el
        while cur is not None:
            if _localname(cur.tag).lower() == "div":
                return div_index.get(id(cur), -1)
            cur = parent.get(id(cur))
        return -1

    for tag, bucket in (("figure", "figures"), ("table", "tables"),
                        ("formula", "formulas")):
        for el in _findall_local(root, tag):
            txt = " ".join(" ".join(el.itertext()).split())
            label = ""
            desc = ""
            for ch in el:
                ln = _localname(ch.tag).lower()
                if ln == "label":
                    label = " ".join(" ".join(ch.itertext()).split())
                elif ln in ("figdesc", "desc"):
                    desc = " ".join(" ".join(ch.itertext()).split())
            out[bucket].append({
                "id": el.get("{http://www.w3.org/XML/1998/namespace}id")
                      or el.get("xml:id") or "",
                "type": el.get("type") or "",
                "label": label,
                "text": (desc or txt)[:1000],
                "section_index": owner_section2(el),
                "raw_tag": "<{}>".format(_localname(el.tag)),
            })
    return out


def parse_tei_citation_links(xml_bytes):
    """建立**正文 ref target ↔ 被引对象 xml:id** 的连接（test.md STEP7 第 4 条）。

    这是句子级证据链的基础：知道"第 3 段这句引的是 [12]"，才能回答
    "这个方法出自哪篇文献"。

    ★ 关键：`<ref>` 有**多种 type**，必须分开处理（实测统计真实样本）：

        type="bibr"     142 个   → 对参考文献表 biblStruct 的 xml:id
        type="figure"    13 个   → 对 <figure xml:id="fig_N">
        type="foot"       6 个   → 对脚注
        type="formula"    2 个   → 对 <formula xml:id="formula_N">

      第一版把所有 ref 都拿去和参考文献表比对，于是 13 个图引用 + 6 个脚注
      全被算成"未连接" —— **连接率被严重低估**（19/29 而不是正确的 19/19）。
      只有 `bibr` 才该对参考文献表；其余类型对的是别的对象。

    返回
    ----
    {
      "links":       [bibr 引用中**成功**连到参考条目的],
      "unlinked":    [bibr 引用中参考条目缺 xml:id、连不上的]（显式保留，不丢弃）,
      "object_links":{"figure": n, "formula": n, "foot": n, "other": n},
                     # 非 bibr 的引用标记计数（它们指向图表公式脚注）
      "n_refs":      正文 ref 标记总数,
      "n_bibr_refs": 其中 type=bibr 的数量 ← **连接率的分母**,
      "n_bib":       参考文献条目总数（含无 id 的）,
      "n_bib_with_id": 带 xml:id 的条目数,
    }

    每条 link：{"marker", "bibliography_id", "ref_type", "section_index",
                "marker_text", "context"}
      context = 标记在全文中的前后各 80 字，供人工核对（test.md要求）。

    ⚠️ 实测连接率不是 100%：真实样本里 biblStruct 有 15/16、14/15、48/49、
       30/31 条带 xml:id。缺 id 的条目连不上属正常（GROBID 没识别出来），
       必须保留这个差额，不能假装全覆盖。
    """
    res = {"links": [], "unlinked": [], "n_refs": 0, "n_bibr_refs": 0,
           "n_bib": 0, "n_bib_with_id": 0,
           "object_links": {"figure": 0, "formula": 0, "foot": 0, "other": 0}}
    try:
        root = ET.fromstring(xml_bytes)
    except Exception:
        return res
    if _localname(root.tag).lower() != "tei":
        return res

    XML_ID = "{http://www.w3.org/XML/1998/namespace}id"
    parent = {}
    for p in root.iter():
        for c in p:
            parent[id(c)] = p

    divs = _findall_local(root, "div")
    div_index = {id(d): i for i, d in enumerate(divs)}

    def owner_section(el):
        """最近的祖先 div 序号。

        ⚠️ 返回 -1 表示"不在任何 div 里"，这是**正常情况不是缺陷**：
           实测 GROBID 把 <figure> 直接放在 <body> 下
           （父链 figure < body < text < TEI），此时确实没有所属章节。
        """
        cur = el
        while cur is not None:
            if _localname(cur.tag).lower() == "div":
                return div_index.get(id(cur), -1)
            cur = parent.get(id(cur))
        return -1

    # 参考文献条目的 xml:id 集合
    bib_ids = set()
    bibl_structs = _findall_local(root, "biblStruct")
    res["n_bib"] = len(bibl_structs)
    for bib in bibl_structs:
        bid = bib.get(XML_ID) or bib.get("xml:id") or ""
        if bid:
            bib_ids.add(bid)
    res["n_bib_with_id"] = len(bib_ids)

    full_text = " ".join(" ".join(root.itertext()).split())
    for ref in _findall_local(root, "ref"):
        target = (ref.get("target") or "").strip()
        if not target:
            continue
        rtype = (ref.get("type") or "").lower()
        res["n_refs"] += 1

        marker_text = " ".join(" ".join(ref.itertext()).split())
        ctx = ""
        if marker_text:
            i = full_text.find(marker_text)
            if i >= 0:
                ctx = full_text[max(0, i - 80): i + len(marker_text) + 80]
        item = {"marker": target, "bibliography_id": target.lstrip("#"),
                "ref_type": rtype, "section_index": owner_section(ref),
                "marker_text": marker_text, "context": ctx}

        if rtype == "bibr":
            # 只有 bibr 才对参考文献表
            res["n_bibr_refs"] += 1
            if item["bibliography_id"] in bib_ids:
                res["links"].append(item)
            else:
                res["unlinked"].append(item)
        else:
            # 图表公式脚注等：分开计数，不混进参考文献的连接率
            key = rtype if rtype in res["object_links"] else "other"
            res["object_links"][key] += 1
    return res



# =============================================================================
# PDF 坐标（teiCoordinates）
# =============================================================================
# GROBID 的 `coords` 属性格式（来自源码 BoundingBox.toString()：
#     String.format("%d,%.2f,%.2f,%.2f,%.2f", page, x, y, width, height)
#   即 **页号,x,y,宽,高**）；一个元素可能有**多个框**，用 `;` 分隔。
#
# ★ 为什么一个元素会有多个框（实测关键认知）：
#   GROBID 是**按行**给框的。一个 <figure> 若含 16 行文字，就有 16 个小框，
#   每个框只覆盖那一行。直接取第一个框会得到一个又扁又窄的矩形
#   （例如 238x9 像素），**不是图的真实区域**。
#   要拿到"这张图占哪块地方"，必须把同一页上的多个框合并成**包围盒**。
#
# 坐标系：原点在**页面左上角**，x 向右、y 向下，单位是 PDF 点（1/72 英寸）。
#   注意与 PDF 原生的"原点在左下角"相反 —— 用坐标画框时要换算。

_COORDS_ATTR = "coords"


def _xml_id(el) -> str:
    """取元素的 xml:id（兼容带命名空间与不带两种写法）。"""
    return (el.get("{http://www.w3.org/XML/1998/namespace}id")
            or el.get("xml:id") or "")


def parse_coords_string(raw: str):
    """把 coords 字符串解析成框列表。

    输入：`"2,319.50,452.12,238.50,9.03;2,319.50,463.47,238.51,8.64"`
    输出：[{"page": 2, "x": 319.5, "y": 452.12, "width": 238.5, "height": 9.03}, ...]

    容错：单个框格式不对就跳过（不因为一个坏框丢掉整个元素的坐标）。
    GROBID 偶尔会输出空串或只有分号，都返回空列表。
    """
    boxes = []
    for part in (raw or "").split(";"):
        part = part.strip()
        if not part:
            continue
        f = part.split(",")
        if len(f) != 5:
            continue
        try:
            boxes.append({"page": int(float(f[0])),
                          "x": round(float(f[1]), 2),
                          "y": round(float(f[2]), 2),
                          "width": round(float(f[3]), 2),
                          "height": round(float(f[4]), 2)})
        except ValueError:
            continue
    return boxes


def merge_boxes(boxes):
    """把同一页的多个行框合并成一个**包围盒**；跨页时每页各一个。

    参数
    ----
    boxes : parse_coords_string() 的输出

    返回 {"boxes_per_page": [...], "bbox": {...} | None, "n_pages": n}
      boxes_per_page : 每页一个合并后的框，按页码升序
      bbox           : 全部页的并集（跨页元素用；单页时等于那一页的框）。
                       **跨页元素没有"单一真实矩形"**，所以同时给出每页的框，
                       由调用方按需选择 —— 不要假装跨页图有一个矩形。
    """
    if not boxes:
        return {"boxes_per_page": [], "bbox": None, "n_pages": 0}
    per = {}
    for b in boxes:
        pg = b["page"]
        cur = per.get(pg)
        if cur is None:
            per[pg] = {"page": pg, "x": b["x"], "y": b["y"],
                       "x2": b["x"] + b["width"], "y2": b["y"] + b["height"]}
        else:
            cur["x"] = min(cur["x"], b["x"])
            cur["y"] = min(cur["y"], b["y"])
            cur["x2"] = max(cur["x2"], b["x"] + b["width"])
            cur["y2"] = max(cur["y2"], b["y"] + b["height"])
    pages = []
    for pg in sorted(per):
        c = per[pg]
        pages.append({"page": pg, "x": round(c["x"], 2), "y": round(c["y"], 2),
                      "x2": round(c["x2"], 2), "y2": round(c["y2"], 2),
                      "width": round(c["x2"] - c["x"], 2),
                      "height": round(c["y2"] - c["y"], 2),
                      "n_raw_boxes": sum(1 for b in boxes if b["page"] == pg)})
    allx = [p["x"] for p in pages]
    ally = [p["y"] for p in pages]
    allx2 = [p["x2"] for p in pages]
    ally2 = [p["y2"] for p in pages]
    bbox = {"page_first": pages[0]["page"], "page_last": pages[-1]["page"],
            "x": round(min(allx), 2), "y": round(min(ally), 2),
            "x2": round(max(allx2), 2), "y2": round(max(ally2), 2),
            "width": round(max(allx2) - min(allx), 2),
            "height": round(max(ally2) - min(ally), 2)}
    return {"boxes_per_page": pages, "bbox": bbox, "n_pages": len(pages)}


def parse_tei_coordinates(xml_bytes, elements=None):
    """抽取 TEI 里带 `coords` 属性的元素及其 PDF 坐标。

    ★ 前提：调用 process_fulltext 时必须传 `tei_coordinates=[...]`。
      没传的话 TEI 里**根本没有 coords 属性**（不是解析失败，是数据不存在）——
      这时函数返回空结果，并在 `note` 里说明原因，**不假装成功**。

    参数
    ----
    xml_bytes : TEI XML 字节
    elements  : 只取这些元素名（本地名，大小写不敏感）；None = 全部带坐标的元素

    返回
    ----
    {
      "elements": [
        {"xml_id", "element", "page_first", "page_last",
         "bbox": {x,y,x2,y2,width,height},
         "boxes_per_page": [...],          # 合并后的每页框
         "raw_boxes": [...],               # GROBID 给的原始行框（保留证据）
         "n_raw_boxes": n,
         "text": "元素文字（截断）",
         "section_index": i}               # 所属 div 序号，-1 = 不在 div 内
      ],
      "count": n,
      "by_element": {"figure": 4, "p": 33, ...},
      "coordinate_space": "pdf_points_origin_top_left",
      "note": "" | "TEI 中无 coords 属性……"
    }

    用途（的句子级证据链 / 你后续要做的高亮回链）：
      知道"这是第 3 页 (x=81, y=172) 到 (x=265, y=203) 的段落"，
      就能在 PDF 上画框、做跳转、或者把分析结果定位回原文。
    """
    res = {"elements": [], "count": 0, "by_element": {},
           "coordinate_space": "pdf_points_origin_top_left", "note": ""}
    try:
        root = ET.fromstring(xml_bytes)
    except Exception as exc:
        res["note"] = "TEI 解析失败：{}".format(str(exc)[:120])
        return res
    if _localname(root.tag).lower() != "tei":
        res["note"] = "根元素不是 TEI"
        return res

    want = None
    if elements:
        want = {str(e).lower() for e in (elements if not isinstance(elements, str)
                                         else [elements])}

    # 建立父子映射（ElementTree 没有 getparent）
    parent = {}
    for p in root.iter():
        for c in p:
            parent[id(c)] = p
    divs = _findall_local(root, "div")
    div_index = {id(d): i for i, d in enumerate(divs)}

    def owner_section(el):
        cur = el
        while cur is not None:
            if _localname(cur.tag).lower() == "div":
                return div_index.get(id(cur), -1)
            cur = parent.get(id(cur))
        return -1

    for el in root.iter():
        raw = el.get(_COORDS_ATTR)
        if not raw:
            continue
        name = _localname(el.tag).lower()
        if want is not None and name not in want:
            continue
        boxes = parse_coords_string(raw)
        if not boxes:
            continue
        merged = merge_boxes(boxes)
        item = {
            "xml_id": _xml_id(el),
            "element": name,
            "page_first": merged["bbox"]["page_first"],
            "page_last": merged["bbox"]["page_last"],
            "bbox": merged["bbox"],
            "boxes_per_page": merged["boxes_per_page"],
            "raw_boxes": boxes,
            "n_raw_boxes": len(boxes),
            "text": " ".join(" ".join(el.itertext()).split())[:300],
            "section_index": owner_section(el),
        }
        res["elements"].append(item)
        res["by_element"][name] = res["by_element"].get(name, 0) + 1

    res["count"] = len(res["elements"])
    if not res["count"]:
        res["note"] = ("TEI 中没有任何 coords 属性 —— 解析时未传 "
                       "tei_coordinates（坐标不是默认输出，必须显式请求）")
    return res


def attach_coordinates(objects, coord_result, elements=("figure", "table", "formula")):
    """把坐标按 xml:id 贴回图表公式对象（就地修改 `objects` 并返回它）。

    参数
    ----
    objects      : parse_tei_figures_tables_formulas() 的返回值
                   （{"figures": [...], "tables": [...], "formulas": [...]}）
    coord_result : parse_tei_coordinates() 的返回值
    elements     : 要贴哪几类

    匹配规则：**按 xml:id 精确匹配**。没有 xml:id 的对象贴不上 ——
    这时保留 `"coords": None` 并记 `"coords_missing_reason"`，
    **不按顺序臆测对应关系**（顺序错配会把 A 图的坐标安到 B 图上，
    这种错误在下游做高亮时极难发现）。
    """
    by_id = {}
    for it in coord_result.get("elements", []):
        if it.get("xml_id"):
            by_id.setdefault(it["xml_id"], it)
    for key in elements:
        bucket = objects.get(key if key.endswith("s") else key + "s")
        if not bucket:
            continue
        for obj in bucket:
            oid = obj.get("id") or ""
            hit = by_id.get(oid)
            if hit:
                obj["coords"] = hit["bbox"]
                obj["coords_boxes_per_page"] = hit["boxes_per_page"]
                obj["coords_page_first"] = hit["page_first"]
                obj["coords_page_last"] = hit["page_last"]
                obj["coords_raw_boxes"] = hit["raw_boxes"]
                obj["coords_n_raw_boxes"] = hit["n_raw_boxes"]
                obj["coords_source"] = "tei:{}@coords".format(hit["element"])
            else:
                obj["coords"] = None
                obj["coords_missing_reason"] = (
                    "该对象没有 xml:id，或无对应坐标" if not oid
                    else "TEI 里没有 xml:id={} 的坐标".format(oid))
    return objects


def boxes_to_dict_list(boxes):
    """把框列表压成紧凑形式，便于存盘与人工核对。

    [{"page":2,"x":..,"y":..,"width":..,"height":..}] → "2,319.50,452.12,238.50,9.03;..."
    与 TEI 原生格式一致，方便和 GROBID 输出直接对照。
    """
    return ";".join("{},{:.2f},{:.2f},{:.2f},{:.2f}".format(
        b["page"], b["x"], b["y"], b["width"], b["height"]) for b in boxes)

def build_spans(xml_bytes, coord_result=None):
    """把 TEI 正文切成**句子级 span**，并带上每句的 PDF 坐标。

    ★ 为什么需要它（下游契约）：
      `` 的 `spans` 段要求每条原文证据带
      `span_id / section / char_start / char_end / text`，并且
      `claims` 表的每条论点**必须**指向一个 span 或 media_id（防幻觉锚点）。
      第一版只给到"整段文本"，粒度太粗 —— 一个 200 字的段落里可能
      混着好几个论点，claim 指向整段等于没定位。

    ★ 坐标怎么来的（这是关键）：
      GROBID 在请求 `teiCoordinates=p` 时会给**段落**级包围盒。
      句子是从段落切出来的，所以：
        - 句子的 `section` 继承段落的章节
        - 句子的 `coords` 用**所属段落**的框（不做句子级框估计 ——
          那需要逐 token 坐标，GROBID 不提供；硬估会给出假精度）
        - 用 `coords_level: "paragraph"` 明确标注坐标精度，**不假装是句子级**

    参数
    ----
    xml_bytes    : TEI XML 字节（解析时需请求过 tei_coordinates，否则无坐标）
    coord_result : parse_tei_coordinates() 的结果（可选；给了就给 span 配坐标）

    返回
    ----
    [{"span_id", "section", "section_index", "paragraph_index",
      "sentence_index", "char_start", "char_end", "text",
      "coords", "coords_level", "page_first", "page_last"}, ...]

    `char_start/char_end` 是**在本篇论文正文全文中的字符偏移**（不是页内偏移），
    这样同篇内的偏移可以互相比较、也能直接切片复核。
    """
    try:
        root = ET.fromstring(xml_bytes)
    except Exception:
        return []
    if _localname(root.tag).lower() != "tei":
        return []

    # 段落坐标：按顺序收集 p 的框（GROBID 的输出顺序 = 文档顺序）
    para_boxes = []
    if coord_result:
        for it in coord_result.get("elements", []):
            if it.get("element") in ("p", "paragraph"):
                para_boxes.append(it)

    parent = {}
    for pp in root.iter():
        for c in pp:
            parent[id(c)] = pp
    divs = _findall_local(root, "div")
    div_index = {id(d): i for i, d in enumerate(divs)}

    def owner_section(el):
        cur = el
        while cur is not None:
            if _localname(cur.tag).lower() == "div":
                return div_index.get(id(cur), -1)
            cur = parent.get(id(cur))
        return -1

    def section_title(el):
        """取所属 div 的 <head> 文本作为 section 名。"""
        cur = el
        while cur is not None:
            if _localname(cur.tag).lower() == "div":
                for ch in cur:
                    if _localname(ch.tag).lower() == "head":
                        return " ".join(" ".join(ch.itertext()).split())
                return ""
            cur = parent.get(id(cur))
        return ""

    spans = []
    cursor = 0          # 全文滚动偏移
    pid_of_box = 0      # 段落框游标（按文档顺序对齐）
    n = 0
    for para in _findall_local(root, "p"):
        text = " ".join(" ".join(para.itertext()).split())
        if not text:
            continue
        sec_i = owner_section(para)
        sec_t = section_title(para)

        # 该段落对应的坐标（按顺序取；取不到就 None）
        box = None
        if pid_of_box < len(para_boxes):
            box = para_boxes[pid_of_box]
            pid_of_box += 1

        for si, sent in enumerate(_split_sentences(text)):
            if not sent.strip():
                continue
            n += 1
            start = cursor
            cursor += len(sent) + 1          # +1 = 句间空格
            item = {
                "span_id": "span_%04d" % n,
                "section": sec_t,
                "section_index": sec_i,
                "paragraph_index": pid_of_box,
                "sentence_index": si,
                "char_start": start,
                "char_end": start + len(sent),
                "text": sent,
                "coords": None,
                "coords_level": "",
                "page_first": None,
                "page_last": None,
            }
            if box and box.get("bbox"):
                # ★ 坐标是**段落级**的，不是句子级 —— 如实标注
                item["coords"] = box["bbox"]
                item["coords_level"] = "paragraph"
                item["page_first"] = box.get("page_first")
                item["page_last"] = box.get("page_last")
            spans.append(item)
    return spans


# ---- 句子切分 ------------------------------------------------------------
# 目标不是"完美分句"，而是**不要在错误的地方断**：下游的 claim 要指向 span，
# 断错位置会让"证据"读起来莫名其妙，比粒度粗更糟。
#
# 策略（按优先级）：
#   1) 保护"点后紧跟非句首字符"的情况 —— 缩写（Fig./Eq./et al.）、编号（3.1）、
#      小数点（0.5）。判据是**点后面的字符**：真正的句末后面
#      通常是大写字母/引号/括号/数字开头的新句。
#   2) 剩下的 `.!?` + 空白 + 大写开头 才切。
#
# ⚠️ 为什么不用"缩写白名单"（第一版的做法，已废弃）：
#   白名单永远列不全，而且会互相打架 —— 实测 `(Lanctot et al. 2009).`
#   在保护了 `Sec.` 之后，`al.` 仍会被切开；把 `al.` 也加进白名单，
#   又会让引文括号里的点被保护、整句不切。
#   改用"看后一个字符"这个**统一判据**，不再依赖穷举。
_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z\"\'\u201c(\[])")


def _split_sentences(text: str):
    """把一段文本切成句子（规则法，不依赖第三方库）。

    判据：`[.!?]` + 空白 + **大写/引号/括号开头** 才切。
    这样：
        "See Fig. 3 for details."   → 'Fig.' 后是 '3'（不是大写）→ 不切 ✅
        "...(Lanctot et al. 2009)." → 'al.' 后是 '2009'（数字，非大写）→ 不切 ✅
        "...bounded. Sec. 3.1 ..."  → 'bounded.' 后是 'Sec'（大写）→ 切 ✅
        "Version 1.2.3 was released." → 编号点后是数字 → 不切 ✅
    局限（已知）：句末恰好以数字或小写开头（如 '... 0.5. the next'）不会切开。
    这种宁可少切不多切 —— 少切只是粒度粗，多切会让证据支离破碎。
    """
    if not text:
        return []
    out = []
    for part in _SENT_SPLIT.split(text):
        part = part.strip()
        if part:
            out.append(part)
    return out


def parse_tei_fulltext(xml_bytes):
    """从完整 TEI 里抽出章节树 / 参考文献 / 图表公式数量。

    返回 {"sections", "references", "figures", "tables", "equations",
          "paragraphs_total"}

      sections 每项：
        {"title": "方法",             章节标题
         "level": 2,                  嵌套深度（1 = 顶层）
         "paragraphs": 5,             段落数（向后兼容保留）
         "paragraph_list": ["...", ...],  ★ 每段独立成串，保留段落边界
         "text": "..."}               ★ 合并后的整节文本（方便粗粒度分析）

    ⚠️ 踩过的坑：早期实现写的是
          " ".join(" ".join(p.itertext()).split() for p in paras)
       外层 join 收到的是 list 而不是 str —— **只要章节里有一个非空段落就 TypeError**。
       正确做法是先把每段归一化成字符串，再聚合（见下面的 segs）。

    ★ 为什么要同时给 paragraph_list 和 text：
要求保留文档结构。只给合并后的 text，
       后续就无法回答"这句结论出自第几段"，也无法给句子级引用回链。
       paragraph_list 保留了段落边界，是通往句子级结构的最小必要信息。

    返回里还有一个 `error` 键：
        成功        → ""（空字符串）
        解析失败    → 具体原因；此时其余字段都是空结构
      ★ 这是为了把「合法但空的文档」与「响应损坏」区分开。
        早期实现直接 `except Exception: return 空结构`，异常被吞掉，
        调用方无法分辨，于是"截断的 XML"被一路当成"成功但没内容"。
    """
    try:
        root = ET.fromstring(xml_bytes)
    except Exception as exc:
        return {"sections": [], "references": [], "figures": 0, "tables": 0,
                "equations": 0, "paragraphs_total": 0,
                "error": "TEI XML 解析失败（响应可能被截断或不是合法 XML）：{}: {}".format(
                    type(exc).__name__, exc)}

    # 根元素必须是 TEI —— 否则可能是一个碰巧合法的 XML 错误响应（如网关错误页）。
    # 用 localname 比较，兼容带命名空间前缀的合法 TEI（不误拒）。
    localname = root.tag.rsplit("}", 1)[-1].lower()
    if localname != "tei":
        return {"sections": [], "references": [], "figures": 0, "tables": 0,
                "equations": 0, "paragraphs_total": 0,
                "error": "根元素不是 TEI（实际为 <{}>），不是有效的 GROBID 响应".format(
                    root.tag)}

    body = root.find(".//%stext/%sbody" % (TEI_NS, TEI_NS))
    sections = []
    if body is not None:
        def walk(div, level):
            for sub in div.findall("%sdiv" % TEI_NS):
                head = sub.findtext("%shead" % TEI_NS, default="")
                title = " ".join((head or "").split())
                # 先逐段归一化为字符串（这一步修掉了 TypeError）
                segs = [_text_of(p) for p in sub.findall("%sp" % TEI_NS)]
                segs = [s for s in segs if s]
                sections.append({
                    "title": title or "(无标题)",
                    "level": level,
                    "paragraphs": len(segs),
                    "paragraph_list": segs,
                    "text": " ".join(segs),
                })
                walk(sub, level + 1)
        walk(body, 1)

    return {
        "sections": sections,
        "references": parse_tei_references(xml_bytes),
        "figures": len(root.findall(".//%sfigure" % TEI_NS)),
        "tables": len(root.findall(".//%stable" % TEI_NS)),
        "equations": len(root.findall(".//%sformula" % TEI_NS)),
        "paragraphs_total": sum(s["paragraphs"] for s in sections),
        "error": "",
    }


def parse_tei_references(xml_bytes):
    """把 TEI 的 <biblStruct> 解析成参考文献列表。

    返回 [{'title', 'authors', 'year', 'doi', 'raw'}]
    用途：直接把 GROBID 抽出的参考文献接进 citation graph ——
          注意它包含**引用方正文里的 citation marker**，
          可以和 S2/OpenAlex 的引用数据交叉验证。
    """
    try:
        root = ET.fromstring(xml_bytes)
    except Exception:
        return []
    out = []
    for bib in root.findall(".//%slistBibl/%sbiblStruct" % (TEI_NS, TEI_NS)):
        title = bib.findtext(".//%stitle" % TEI_NS, default="")
        authors = []
        for p in bib.findall(".//%spersName" % TEI_NS):
            name = " ".join(filter(None, [
                p.findtext("%sforename" % TEI_NS, default=""),
                p.findtext("%ssurname" % TEI_NS, default="")]))
            if name.strip():
                authors.append(name.strip())
        year = bib.findtext(".//%sdate" % TEI_NS, default="")
        if not year:
            y = bib.find(".//%sdate" % TEI_NS)
            year = (y.get("when") if y is not None else "") or ""
        doi = ""
        for idno in bib.findall(".//%sidno" % TEI_NS):
            if idno.get("type") == "DOI":
                doi = (idno.text or "").strip()
        out.append({"title": " ".join((title or "").split()), "authors": authors,
                    "year": year, "doi": doi,
                    "raw": " ".join(" ".join(bib.itertext()).split())[:300]})
    return out


# =============================================================================
# 四、降级方案：不用 GROBID 的纯文本抽取
# =============================================================================

def extract_text_fallback(pdf_path: str):
    """路线 C：用 pypdf / pdfminer / pdftotext 抽纯文本（**会丢失结构**）。

    按顺序尝试，用上第一个可用的：
        1. pypdf        （conda env researchos 自带；缺则 pip install pypdf）
        2. PyPDF2       （老名字，很多环境里已有）
        3. fitz/PyMuPDF （pip install pymupdf，速度最快，但二进制包较大）
        4. pdfminer.six （pip install pdfminer.six，排版信息更细）
        5. pdftotext    （poppler 命令行工具，需系统安装）

    返回 {"method": str, "text": str, "pages": int, "error": str}
      method 为空表示一条路都走不通 → 需要装 pypdf 或 GROBID

    ⚠️ 再次强调：纯文本**没有章节边界**，后续无法回答
       "方法在第几节"、"局限在哪一段"，只能做粗糙的关键词分析。
       它只是 GROBID 不可用时的临时方案。
    """
    if not os.path.isfile(pdf_path):
        return {"method": "", "text": "", "pages": 0, "error": "文件不存在"}

    # 1) pypdf / PyPDF2
    for mod in ("pypdf", "PyPDF2"):
        try:
            m = __import__(mod)
            reader = m.PdfReader(pdf_path)
            pages = []
            for pg in reader.pages:
                try:
                    pages.append(pg.extract_text() or "")
                except Exception:
                    pages.append("")
            return {"method": mod, "text": "\n".join(pages),
                    "pages": len(reader.pages), "error": ""}
        except ImportError:
            continue
        except Exception as exc:
            return {"method": mod, "text": "", "pages": 0,
                    "error": "{} 解析失败: {}".format(mod, exc)}

    # 2) PyMuPDF
    try:
        import fitz
        doc = fitz.open(pdf_path)
        text = "\n".join(page.get_text() for page in doc)
        return {"method": "fitz(PyMuPDF)", "text": text,
                "pages": doc.page_count, "error": ""}
    except ImportError:
        pass
    except Exception as exc:
        return {"method": "fitz", "text": "", "pages": 0, "error": str(exc)}

    # 3) pdfminer
    try:
        from pdfminer.high_level import extract_text
        text = extract_text(pdf_path)
        return {"method": "pdfminer.six", "text": text,
                "pages": text.count("\f") + 1, "error": ""}
    except ImportError:
        pass
    except Exception as exc:
        return {"method": "pdfminer", "text": "", "pages": 0, "error": str(exc)}

    # 4) pdftotext 命令行
    if shutil.which("pdftotext"):
        try:
            proc = subprocess.run(["pdftotext", "-layout", pdf_path, "-"],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  timeout=120)
            if proc.returncode == 0:
                text = proc.stdout.decode("utf-8", "replace")
                return {"method": "pdftotext", "text": text,
                        "pages": text.count("\f") + 1, "error": ""}
            return {"method": "pdftotext", "text": "", "pages": 0,
                    "error": proc.stderr.decode("utf-8", "replace")[:200]}
        except Exception as exc:
            return {"method": "pdftotext", "text": "", "pages": 0, "error": str(exc)}

    return {"method": "", "text": "", "pages": 0,
            "error": "无可用解析器。装一个即可：conda create -n researchos python=3.12 pypdf"
                     "（要结构而不仅是文本，请部署 GROBID）"}


# =============================================================================
# 自检与命令行入口
# =============================================================================

def check() -> int:
    failed = 0

    section("1) 本机环境（GROBID 的部署前置）")
    env = env_report()
    java_exe, java_msg = env["java"]
    kv("java", "{} ({})".format(java_exe or "无", java_msg))
    kv("docker", env["docker"] or "未安装")
    kv("grobid", env["grobid"] or "未安装")
    kv("pdftotext", env["pdftotext"] or "未安装")
    kv("python 解析库", {k: v for k, v in env["parsers"].items()})
    kv("踩坑记录", "macOS 的 /usr/bin/java 无 JDK 时也返回退出码 0，必须解析输出")
    if java_exe:
        kv("结论", "JRE 可用{}，可部署 GROBID".format(
            "（本目录自带）" if env.get("local_jdk") else "（系统 PATH）"))
    else:
        kv("结论", "缺少 JRE 与 Docker → 无法部署 GROBID")
        failed += 1
    if not any(env["parsers"].values()) and not env["pdftotext"]:
        kv("兜底结论", "纯文本兜底不可用 → 用 conda 环境 researchos（或 pip install pypdf）")

    section("2) GROBID 服务")
    g = Grobid()
    kv("地址", g.base_url)
    alive, msg = g.isalive()
    kv("isalive", "{} ({})".format(alive, msg[:40]))
    if alive:
        status, ver = g.version()
        kv("version", "HTTP {} {}".format(status, ver))
        sample = find_sample_pdf()
        if sample:
            kv("样本 PDF", os.path.basename(sample))
            r = g.process_header(sample)
            kv("processHeader", "HTTP {} ok={} | title={} | 作者 {} | doi={}".format(
                r["status"], r.get("ok", r["status"] == 200),
                (r.get("title") or "")[:40], len(r.get("authors") or []), r.get("doi")))
            full = g.process_fulltext(sample)
            kv("processFulltext", "HTTP {} ok={} | 章节 {} | 段落 {} | 参考文献 {} | 图表公式 {}/{}/{}".format(
                full["status"], full.get("ok"), len(full.get("sections") or []),
                full.get("paragraphs_total"), len(full.get("references") or []),
                full.get("figures"), full.get("tables"), full.get("equations")))
            refs = g.process_references(sample)
            kv("processReferences", "HTTP {} | {} 条".format(
                refs["status"], len(refs.get("references") or [])))
            if not full.get("ok"):
                kv("错误", (full.get("error") or "")[:120])
                failed += 1
        else:
            kv("解析实测", "跳过（downloads/ 下没有样本 PDF，先跑：arxiv.py pdf 1603.01121）")
    else:
        kv("结论", "服务未启动。本目录自带的启动方式：")
        print("      ./grobid-serve.sh start      # 启动（JDK 与依赖都在本目录内）")
        print("      ./grobid-serve.sh status     # 查看状态")
        failed += 1

    section("3) 纯文本兜底（不依赖 GROBID）")
    sample = find_sample_pdf()
    if sample:
        kv("样本 PDF", os.path.basename(sample))
        fb = extract_text_fallback(sample)
        kv("使用的方法", fb["method"] or "无")
        kv("页数 / 字符数", "{} / {}".format(fb["pages"], len(fb["text"])))
        kv("错误", fb["error"] or "无")
        if fb["method"]:
            print("      预览: {}…".format(fb["text"][:90].replace("\n", " ")))
        else:
            failed += 1
    else:
        kv("状态", "跳过（无样本 PDF）")

    print("\n" + "-" * 78)
    print("GROBID 自检：{}".format("通过" if failed == 0 else "{} 项不可用".format(failed)))
    return failed


def main(argv) -> int:
    if not argv or argv[0] == "check":
        return check()

    cmd = argv[0]

    if cmd == "env":
        section("PDF 解析环境")
        env = env_report()
        java_exe, java_msg = env["java"]
        kv("java", "{} ({})".format(java_exe or "无", java_msg))
        for k in ("docker", "grobid", "pdftotext", "mutool"):
            kv(k, env[k] or "未安装")
        kv("python 解析库", env["parsers"])
        kv("至少一条路可走", env["ready"])
        return 0

    if cmd == "isalive":
        g = Grobid()
        section("GROBID 服务状态")
        kv("地址", g.base_url)
        alive, msg = g.isalive()
        kv("isalive", "{} | {}".format(alive, msg[:60]))
        if alive:
            kv("version", g.version())
        else:
            kv("提示", "未启动。部署方式：")
            print("      docker run -t --rm -p 8070:8070 lfoppiano/grobid:0.8.1")
        return 0 if alive else 1

    if cmd in ("header", "fulltext", "refs"):
        if len(argv) < 2:
            print("用法: python3 grobid.py {} paper.pdf [--out out.xml]".format(cmd))
            return 2
        g = Grobid()
        alive, _ = g.isalive()
        if not alive:
            kv("错误", "GROBID 服务未启动（{}）".format(g.base_url))
            return 1
        if cmd == "header":
            r = g.process_header(argv[1])
            section("解析头部")
            kv("状态", "HTTP {}".format(r["status"]))
            kv("标题", r.get("title"))
            kv("作者", ", ".join(r.get("authors") or [])[:120])
            kv("DOI", r.get("doi"))
            kv("摘要", (r.get("abstract") or "")[:200])
        elif cmd == "fulltext":
            out = argv[argv.index("--out") + 1] if "--out" in argv else ""
            r = g.process_fulltext(argv[1], out_path=out)
            section("解析全文")
            kv("状态", "HTTP {}".format(r["status"]))
            kv("章节数", len(r.get("sections") or []))
            kv("参考文献", len(r.get("references") or []))
            kv("图/表/公式", "{}/{}/{}".format(r.get("figures"), r.get("tables"),
                                              r.get("equations")))
            for s in (r.get("sections") or [])[:12]:
                print("      {} {:<40} ({} 段)".format(
                    "  " * (s["level"] - 1), s["title"][:40], s["paragraphs"]))
            # ★ 只有真的落了盘才说"已保存" —— 早前无条件打印，
            #   解析失败时也会显示"TEI 已保存"，属于假通过。
            if out and r.get("ok"):
                kv("TEI 已保存", out)
            elif out:
                kv("TEI 未保存", "解析失败，未写盘")
        else:
            r = g.process_references(argv[1])
            section("抽取参考文献")
            kv("状态", "HTTP {}".format(r["status"]))
            kv("条数", len(r.get("references") or []))
            for ref in (r.get("references") or [])[:8]:
                print("      - {} ({}) doi={}".format(
                    (ref["title"] or ref["raw"])[:60], ref["year"], ref["doi"] or "-"))
        return 0

    if cmd == "fallback":
        if len(argv) < 2:
            print("用法: python3 grobid.py fallback paper.pdf")
            return 2
        r = extract_text_fallback(argv[1])
        section("纯文本兜底（丢失结构）")
        kv("方法", r["method"] or "无可用解析器")
        kv("页数", r["pages"])
        kv("字符数", len(r["text"]))
        kv("错误", r["error"] or "无")
        if r["text"]:
            print("\n  预览:\n{}".format(r["text"][:800]))
        return 0 if r["method"] else 1

    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
