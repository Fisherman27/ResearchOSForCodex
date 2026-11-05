#!/usr/bin/env python3
"""Start the read-only ResearchOS workbench, following Codex plugin conventions."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="启动 ResearchOS 工作台")
    parser.add_argument("--workspace", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--port", type=int, default=8767)
    parser.add_argument("--no-open", action="store_true")
    args = parser.parse_args()
    root = Path(args.workspace).expanduser().resolve()
    server = Path(__file__).resolve().parents[1] / "workbench" / "server.py"
    url = f"http://127.0.0.1:{args.port}"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def switch_existing() -> None:
        body = json.dumps({"workspace": str(root)}).encode("utf-8")
        request = urllib.request.Request(
            url + "/api/workspace", data=body, method="POST",
            headers={"Content-Type": "application/json"})
        try:
            with opener.open(request, timeout=3) as response:
                data = json.load(response)
        except urllib.error.HTTPError as exc:
            raise RuntimeError(
                "端口已被旧版或不兼容的工作台占用；请关闭旧工作台或更换 --port") from exc
        if Path(data.get("workspace", "")).resolve() != root:
            raise RuntimeError("工作台拒绝切换到指定目录：{}".format(root))

    def probe() -> bool:
        try:
            with opener.open(url + "/api/workspace", timeout=2) as response:
                data = json.load(response)
            if Path(data.get("workspace", "")).resolve() != root:
                switch_existing()
            return True
        except urllib.error.HTTPError as exc:
            raise RuntimeError("端口上的服务不是兼容的 ResearchOS 工作台") from exc
        except (urllib.error.URLError, TimeoutError):
            return False

    if not probe():
        log_path = root / "workbench-server.log"
        log = open(log_path, "ab")  # noqa: SIM115 - child owns descriptor
        child = subprocess.Popen(
            [sys.executable, str(server), "--workspace", str(root),
             "--port", str(args.port)], cwd=root, stdout=log, stderr=log,
            start_new_session=(os.name != "nt"),
            creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP |
                           subprocess.DETACHED_PROCESS) if os.name == "nt" else 0,
        )
        for _ in range(50):
            if child.poll() is not None:
                raise SystemExit(f"工作台启动失败，请查看 {log_path}")
            if probe():
                break
            time.sleep(0.1)
        else:
            raise SystemExit("工作台未能在 5 秒内启动")
    if not args.no_open:
        webbrowser.open(url)
    print(url)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
