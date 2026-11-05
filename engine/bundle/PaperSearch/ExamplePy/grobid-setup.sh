#!/bin/bash
# =============================================================================
# grobid-setup.sh —— 在**本目录内**从零部署 GROBID（macOS Apple Silicon）
# =============================================================================
# 为什么需要这个脚本：部署过程踩了一串坑，每一步都不是显然的，写下来免得重摸。
# 全部产物都在 ExamplePy/ 内，删掉即回滚，不碰任何系统路径。
#
#   ┌ 最终布局 ────────────────────────────────────────────────┐
#   │ tools/jdk-21*/        JDK 21（解压即用，不需安装）         │
#   │ .gradle-home/         Gradle 缓存与依赖（重定向，不落 ~）   │
#   │ grobid-src/grobid-0.9.1/  GROBID 源码 + 470MB CRF 模型     │
#   │ grobid-home-deps/lib/ pdfalto 需要的 fontconfig 等 15 个库 │
#   │ grobid-run/           运行日志与 PID                       │
#   └──────────────────────────────────────────────────────────┘
#   合计约 2.7GB。
#
# 用法：
#   ./grobid-setup.sh          # 全流程（已完成的步骤会跳过）
#   ./grobid-setup.sh check    # 只检查当前状态
#
# =============================================================================
# 部署过程中遇到的 5 个坑（都已在本脚本里处理）
# =============================================================================
# ① macOS 不预装 JDK。`/usr/bin/java` 只是 Apple 的"定位器"，找不到 JVM 就打印
#    "Unable to locate a Java Runtime" 却**返回退出码 0** —— 不能靠退出码判断。
#    → 这里下载 Temurin JDK 21 解压到 tools/（GROBID 要求 Java 21）。
#
# ② GROBID 的 GitHub release 只发**瘦 jar**（无 Main-Class、无依赖、无模型），
#    不能 `java -jar` 直接跑。必须拿源码包。
#    → 下载 codeload 的 tag 源码包（393MB，**内含 470MB CRF 模型**，
#      所以完全不需要访问 HuggingFace）。
#
# ③ codeload 生成的 tar 包**不支持 Range 请求**（Range 被忽略、返回 200 全量），
#    所以 `curl -C -` 续传无效，断了只能整包重下。
#    → 脚本用重试循环 + gzip 完整性校验（curl 退出码 18 表示传输不完整）。
#
# ④ pdfalto（GROBID 用来做 PDF→XML 的原生二进制）**硬编码依赖 Homebrew 的
#    fontconfig**：/opt/homebrew/opt/fontconfig/lib/libfontconfig.1.dylib。
#    本机没有 Homebrew → SIGABRT，服务返回 BAD_INPUT_DATA / error code 134。
#    → 用 conda 取 fontconfig 及其依赖链（freetype/expat/intl/png/zlib），
#      把所有 dylib 平铺到 grobid-home-deps/lib/（它们内部用 @rpath +
#      LC_RPATH=@loader_path/，同目录即可自解析），再用 install_name_tool
#      把 pdfalto 的绝对路径改成 @loader_path 相对路径，最后 codesign 重签
#      （arm64 二进制改动后必须重签，否则无法执行）。
#
# ⑤ GROBID 的 `consolidateCitations` 默认开启时，会对**每条参考文献**查一次
#    Crossref（自带限速 1 req/s）。实测同一篇 10 页论文：
#        关 → 1.6 秒      开 → 220 秒
#    → grobid.py 里已把默认改成 0；需要 DOI 补全时离线批量跑。
# =============================================================================
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
GROBID_VER="${GROBID_VER:-0.9.1}"
JDK_URL="https://api.adoptium.net/v3/binary/latest/21/ga/mac/aarch64/jdk/hotspot/normal/eclipse"
SRC_TGZ="https://codeload.github.com/grobidOrg/grobid/tar.gz/refs/tags/${GROBID_VER}"
CONDA_BIN="${CONDA_EXE:-$(command -v conda 2>/dev/null || true)}"

log() { printf '\n\033[1m── %s ──\033[0m\n' "$*"; }

status() {
  echo "当前状态："
  for d in tools .gradle-home grobid-src grobid-home-deps grobid-run; do
    [ -e "$HERE/$d" ] && echo "  ✓ $d  ($(du -sh "$HERE/$d" 2>/dev/null | cut -f1))" || echo "  ✗ $d"
  done
  J="$HERE/tools"/jdk-*/Contents/Home/bin/java
  [ -x $J ] && echo "  ✓ JDK: $($J -version 2>&1 | head -1)" || echo "  ✗ JDK"
  P="$HERE/grobid-src/grobid-${GROBID_VER}/grobid-home/pdfalto/mac_arm-64/pdfalto"
  [ -f "$P" ] && echo "  pdfalto 依赖: $(otool -L "$P" 2>/dev/null | grep -c fontconfig) 条 fontconfig 引用" || true
  curl -sS -m 5 -o /dev/null http://localhost:8070/api/isalive 2>/dev/null \
    && echo "  ✓ 服务在线 http://localhost:8070" || echo "  ○ 服务未启动（./grobid-serve.sh start）"
}

[ "${1:-all}" = "check" ] && { status; exit 0; }

# ---------------------------------------------------------------------------
log "步骤 1/5  下载 JDK 21 到 tools/"
# ---------------------------------------------------------------------------
if ls "$HERE"/tools/jdk-*/Contents/Home/bin/java >/dev/null 2>&1; then
  echo "  已存在，跳过"
else
  mkdir -p "$HERE/tools" && cd "$HERE/tools"
  for i in 1 2 3; do
    curl -sSL -C - -m 1800 -o jdk21.tar.gz "$JDK_URL" \
         -w "  http=%{http_code} 收到=%{size_download} 用时=%{time_total}s\n"
    if gzip -t jdk21.tar.gz 2>/dev/null; then break; fi
    echo "  ✗ 不完整，重试…"
  done
  gzip -t jdk21.tar.gz && tar xzf jdk21.tar.gz && rm -f jdk21.tar.gz && echo "  ✅ JDK 就绪"
fi

# ---------------------------------------------------------------------------
log "步骤 2/5  下载 GROBID ${GROBID_VER} 源码（含 CRF 模型）"
# ---------------------------------------------------------------------------
if [ -d "$HERE/grobid-src/grobid-${GROBID_VER}" ]; then
  echo "  已存在，跳过"
else
  mkdir -p "$HERE/grobid-src" && cd "$HERE/grobid-src"
  for i in 1 2 3 4 5 6; do
    # 坑 ③：codeload 不支持 Range，断了只能整包重下，所以用重试循环
    curl -sSL -m 2400 --retry 3 --retry-delay 5 --retry-all-errors \
         -o src.tar.gz "$SRC_TGZ" \
         -w "  第 $i 次：http=%{http_code} 收到=%{size_download}\n"
    if gzip -t src.tar.gz 2>/dev/null; then break; fi
    echo "  ✗ 不完整（curl 18 = 传输中断），重试…"
  done
  gzip -t src.tar.gz && tar xzf src.tar.gz && rm -f src.tar.gz && echo "  ✅ 源码与模型就绪"
fi

# ---------------------------------------------------------------------------
log "步骤 3/5  取 pdfalto 依赖（fontconfig 链）到 grobid-home-deps/lib"
# ---------------------------------------------------------------------------
DEPS_LIB="$HERE/grobid-home-deps/lib"
if [ -f "$DEPS_LIB/libfontconfig.1.dylib" ]; then
  echo "  已存在，跳过"
else
  mkdir -p "$DEPS_LIB"
  if [ -x "$CONDA_BIN" ]; then
    # 坑 ④：pdfalto 要 Homebrew 的 fontconfig，本机没有。
    # 用 conda 只做「下载+解包」，不依赖它的环境链接（那一步可能被沙箱拦）。
    export CONDA_PKGS_DIRS="$HERE/.conda-pkgs-tmp"
    "$CONDA_BIN" create -p "$HERE/.conda-deps-tmp" -c conda-forge fontconfig -y >/dev/null 2>&1
    for d in "$HERE"/.conda-pkgs-tmp/*/lib; do
      [ -d "$d" ] || continue
      for f in "$d"/*.dylib; do
        [ -f "$f" ] && [ ! -e "$DEPS_LIB/$(basename "$f")" ] && cp "$f" "$DEPS_LIB/"
      done
    done
    rm -rf "$HERE/.conda-deps-tmp" "$HERE/.conda-pkgs-tmp"
    echo "  从 conda 包缓存取了 $(ls "$DEPS_LIB" | wc -l | tr -d ' ') 个 dylib"
  else
    echo "  ✗ 未找到 conda，无法自动取 fontconfig。请手工提供 libfontconfig.1.dylib 到 $DEPS_LIB"
  fi
  # 校验依赖闭包：每个 dylib 的非系统依赖都应能在同目录找到
  miss=0
  for f in "$DEPS_LIB"/*.dylib; do
    for dep in $(otool -L "$f" 2>/dev/null | tail -n +2 | awk '{print $1}' | grep -v "^/usr/lib\|^/System"); do
      [ -e "$DEPS_LIB/$(basename "$dep")" ] || { echo "  ✗ 缺 $(basename "$dep")"; miss=$((miss+1)); }
    done
  done
  [ "$miss" -eq 0 ] && echo "  ✅ 依赖闭包完整（0 缺失）" || echo "  ✗ 仍缺 $miss 个依赖"
fi

# ---------------------------------------------------------------------------
log "步骤 4/5  给 pdfalto 打补丁（改 fontconfig 路径 + 重签名）"
# ---------------------------------------------------------------------------
PFDIR="$HERE/grobid-src/grobid-${GROBID_VER}/grobid-home/pdfalto/mac_arm-64"
if [ -f "$PFDIR/pdfalto" ]; then
  if otool -L "$PFDIR/pdfalto" | grep -q "/opt/homebrew/opt/fontconfig"; then
    [ -f "$PFDIR/pdfalto.orig" ] || cp "$PFDIR/pdfalto" "$PFDIR/pdfalto.orig"
    # 坑 ④：把硬编码的 Homebrew 绝对路径改成相对本目录的 @loader_path。
    # mac_arm-64 → pdfalto → grobid-home → grobid-0.9.1 → grobid-src → ExamplePy
    REL="@loader_path/../../../../../grobid-home-deps/lib/libfontconfig.1.dylib"
    install_name_tool -change \
      /opt/homebrew/opt/fontconfig/lib/libfontconfig.1.dylib "$REL" "$PFDIR/pdfalto" \
      && echo "  ✓ install_name_tool 改写依赖路径"
    codesign --force --sign - "$PFDIR/pdfalto" 2>&1 | sed 's/^/  /'
    echo "  ✓ 已重签名（arm64 改动后必须重签，否则无法执行）"
  else
    echo "  已打过补丁，跳过"
  fi
  # 冒烟测试：直接用样本 PDF 跑一次 pdfalto
  SAMPLE="$HERE/downloads/1603.01121.pdf"
  if [ -f "$SAMPLE" ]; then
    ( cd "$PFDIR" && ./pdfalto "$SAMPLE" /tmp/_pdfalto_smoke.xml >/dev/null 2>&1 ) \
      && echo "  ✅ pdfalto 冒烟测试通过（$(du -h /tmp/_pdfalto_smoke.xml | cut -f1) ALTO）" \
      || echo "  ✗ pdfalto 仍无法运行"
  fi
else
  echo "  ✗ 未找到 mac_arm-64/pdfalto"
fi

# ---------------------------------------------------------------------------
log "步骤 5/5  完成"
# ---------------------------------------------------------------------------
cat <<EOF
  启动服务：  ./grobid-serve.sh start
  查看状态：  ./grobid-serve.sh status
  自检查验：  python3 grobid.py check
  停止服务：  ./grobid-serve.sh stop

  注意：首次 ./grobid-serve.sh start 会用 Gradle 拉依赖并编译，
        耗时几分钟；依赖全部落在 .gradle-home/（不写 ~/.gradle）。
EOF
status
