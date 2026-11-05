#!/bin/bash
# =============================================================================
# grobid-serve.sh —— 在本目录内启动本地 GROBID 服务
# =============================================================================
# 设计原则：**所有东西都在本目录里，不写任何系统路径**，删掉整个 ExamplePy 就干净了。
#
#   tools/jdk-21.0.12.1+1/   JDK 21（Temurin，解压即用，无需安装）
#   .gradle-home/            Gradle 缓存与依赖（显式重定向，不落 ~/.gradle）
#   grobid-src/grobid-0.9.1/ GROBID 源码 + CRF 模型（470MB，源码包自带，不需联网下模型）
#   grobid-run/              运行日志与 PID
#
# 用法：
#   ./grobid-serve.sh start     后台启动（默认）
#   ./grobid-serve.sh stop      停止
#   ./grobid-serve.sh status    查看状态 + 健康检查
#   ./grobid-serve.sh fg        前台启动（调试用，能直接看日志）
#
# 启动后服务在 http://localhost:8070，可用 grobid.py 直接调用：
#   python3 grobid.py isalive
#   python3 grobid.py fulltext downloads/1603.01121.pdf
# =============================================================================
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
RUN_DIR="$HERE/grobid-run"
LOG="$RUN_DIR/grobid.log"
PIDFILE="$RUN_DIR/grobid.pid"
PORT="${GROBID_PORT:-8070}"

# 用 glob 找 JDK，避免把版本号写死（换 JDK 版本不用改脚本）
JDK_HOME=""
for d in "$HERE/tools"/jdk-*/Contents/Home; do
  [ -x "$d/bin/java" ] && JDK_HOME="$d" && break
done
if [ -z "$JDK_HOME" ]; then
  echo "✗ 找不到 JDK。期望 $HERE/tools/jdk-*/Contents/Home"
  echo "  重新下载："
  echo "    mkdir -p tools && cd tools"
  echo "    curl -sSL -o jdk21.tar.gz 'https://api.adoptium.net/v3/binary/latest/21/ga/mac/aarch64/jdk/hotspot/normal/eclipse'"
  echo "    tar xzf jdk21.tar.gz && rm jdk21.tar.gz"
  exit 1
fi

SRC="$HERE/grobid-src/grobid-0.9.1"
[ -d "$SRC" ] || { echo "✗ 找不到 GROBID 源码：$SRC"; exit 1; }

export JAVA_HOME="$JDK_HOME"
export GRADLE_USER_HOME="$HERE/.gradle-home"    # ★ 必须重定向，否则写 ~/.gradle
export PATH="$JAVA_HOME/bin:$PATH"
export GROBID_PORT_PARAM="$PORT"

mkdir -p "$RUN_DIR" "$GRADLE_USER_HOME"

is_up() { curl -sS -m 5 -o /dev/null "http://localhost:$PORT/api/isalive" 2>/dev/null; }

case "${1:-start}" in
  start)
    if is_up; then echo "● 服务已在运行：http://localhost:$PORT"; exit 0; fi
    if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
      echo "● 启动进程仍在（PID $(cat "$PIDFILE")），等待它就绪…"
    else
      echo "▶ 启动 GROBID（首次会下载 Gradle 依赖，可能几分钟）…"
      echo "  日志：$LOG"
      nohup "$0" fg > "$LOG" 2>&1 &
      echo $! > "$PIDFILE"
    fi
    for i in $(seq 1 180); do
      if is_up; then
        echo "✅ GROBID 已就绪：http://localhost:$PORT  （等待 ${i}0s 内）"
        exit 0
      fi
      sleep 10
    done
    echo "✗ 30 分钟内未就绪，看日志：tail -50 $LOG"
    exit 1
    ;;
  fg)
    cd "$SRC"
    exec ./gradlew run --no-daemon
    ;;
  stop)
    if [ -f "$PIDFILE" ]; then
      PID="$(cat "$PIDFILE")"
      # gradle 会派生 java 子进程，先杀进程组
      pkill -P "$PID" 2>/dev/null
      kill "$PID" 2>/dev/null
      rm -f "$PIDFILE"
      # ⚠️ 变量必须写成 ${PID}：后面紧跟的是**全角括号（多字节字符）**，
      #    `set -u` 下 shell 会把该字符的首字节当成变量名的一部分，
      #    于是报 "PID?: unbound variable" —— 停止其实成功了，
      #    却打出一条吓人的报错（实测踩到）。
      echo "■ 已停止（PID ${PID}）"
    else
      echo "○ 没有 PID 文件"
    fi
    if pkill -f "org.grobid.service.GrobidRestService" 2>/dev/null; then
      echo "   （同时清理了残留的 java 服务进程）"
    fi
    ;;
  status)
    if is_up; then
      echo "● 运行中  http://localhost:$PORT"
      curl -sS -m 5 "http://localhost:$PORT/api/version" 2>/dev/null | head -c 200; echo
    else
      echo "○ 未运行（端口 $PORT 无响应）"
    fi
    [ -f "$PIDFILE" ] && echo "  PID 文件：$(cat "$PIDFILE")"
    [ -f "$LOG" ] && echo "  日志行数：$(wc -l < "$LOG")  最近一行：$(tail -1 "$LOG" | cut -c1-100)"
    ;;
  *)
    echo "用法: $0 {start|stop|status|fg}"; exit 2;;
esac
