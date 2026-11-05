#!/usr/bin/env python3
"""Dependency-free, read-only HTTP view over ResearchOS run artifacts."""
from __future__ import annotations

import argparse
import json
import mimetypes
import threading
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

STATIC = Path(__file__).with_name("static")
TERMINAL = {"completed", "failed", "blocked_research"}
POLL_SECONDS = 8


def _json(path: Path, default=None):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if value is not None else default
    except (OSError, ValueError, TypeError):
        return default


def _jsonl(path: Path, limit=80):
    try:
        lines = path.read_text(encoding="utf-8").splitlines()[-limit:]
    except OSError:
        return []
    rows = []
    for line in lines:
        try:
            value = json.loads(line)
            if isinstance(value, dict):
                rows.append(value)
        except ValueError:
            continue
    return rows


def _files(root: Path, folder: str):
    base = root / folder
    if not base.is_dir():
        return []
    rows = []
    for path in sorted(base.rglob("*")):
        if path.is_file() and not path.name.startswith("."):
            stat = path.stat()
            rows.append({"name": path.name, "path": str(path.relative_to(root)),
                         "size": stat.st_size, "updated_at": datetime.fromtimestamp(
                             stat.st_mtime, timezone.utc).isoformat()})
    return rows


def _orders(run: Path):
    outbox = run / "handoff" / "outbox"
    rows = []
    if outbox.is_dir():
        for path in sorted(outbox.glob("*.json")):
            value = _json(path, {}) or {}
            rows.append({"order_id": value.get("order_id") or path.stem,
                         "agent_id": value.get("agent_id"),
                         "created_at": value.get("created_at")})
    return rows


def _corpus_count(value):
    if not isinstance(value, str):
        return 0
    root = Path(value)
    for name in ("corpus.jsonl", "papers.jsonl"):
        try:
            with (root / name).open(encoding="utf-8") as handle:
                return sum(1 for line in handle if line.strip())
        except OSError:
            continue
    return 0


def _timestamp(value):
    if not value:
        return 0.0
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return 0.0


def _paper_rows(run: Path, paper_ids):
    wanted, rows = set(paper_ids), {}
    paper_dir = run / "kb" / "raw" / "papers"
    if paper_dir.is_dir() and wanted:
        for path in paper_dir.glob("*.json"):
            value = _json(path, {}) or {}
            paper_id = value.get("paper_id")
            if paper_id not in wanted:
                continue
            metadata, understanding = value.get("metadata") or {}, value.get("understanding") or {}
            titles = metadata.get("title_variants") or []
            rows[paper_id] = {"paper_id": paper_id,
                              "title": value.get("title") or (titles[0] if titles else paper_id),
                              "year": metadata.get("year"),
                              "reading_depth": understanding.get("reading_depth") or "read",
                              "method_family": understanding.get("method_family") or ""}
    return [rows.get(pid, {"paper_id": pid, "title": pid}) for pid in paper_ids]


def _session_rows(run: Path):
    bindings = (_json(run / "sessions" / "registry.json", {}) or {}).get("bindings") or {}
    rows = []
    for binding in bindings.values():
        if not isinstance(binding, dict):
            continue
        meta = binding.get("meta") or {}
        rows.append({"role_id": binding.get("role_id"),
                     "subtask_id": binding.get("subtask_id") or "",
                     "state": binding.get("state") or "unknown",
                     "session_id": binding.get("session_id"),
                     "host": meta.get("host"),
                     "child_established": bool(meta.get("child_established")),
                     "last_error": binding.get("last_error") or "",
                     "updated_at": binding.get("updated_at")})
    return sorted(rows, key=lambda row: (row.get("role_id") or "", row.get("subtask_id") or ""))


def _run_roots(workspace: Path):
    roots, seen = [], set()
    for path in (workspace / "runs", workspace / "engine" / "runs"):
        resolved = path.resolve()
        if resolved not in seen and path.is_dir():
            roots.append(path)
            seen.add(resolved)
    return roots


def _valid_workspace(path: Path):
    """Accept only ResearchOS project roots or directories with real runs."""
    try:
        path = path.expanduser().resolve()
    except (OSError, RuntimeError):
        return False
    project = ((path / "AGENTS.md").is_file()
               and (path / "tools" / "codex.py").is_file()
               and (path / "engine" / "researchos").is_dir())
    return path.is_dir() and (project or bool(_run_roots(path)))


def _workspace_candidates(current: Path, history=()):
    """Find nearby ResearchOS roots without recursively scanning the computer."""
    current = current.resolve()
    candidates = {current, STATIC.parents[1].resolve()}
    for value in history:
        try:
            candidates.add(Path(value).expanduser().resolve())
        except (OSError, RuntimeError, TypeError):
            continue
    for base in (current.parent, current):
        try:
            children = list(base.iterdir())[:300]
        except OSError:
            continue
        candidates.update(path.resolve() for path in children if path.is_dir())
    rows = []
    for path in sorted(candidates, key=lambda item: str(item).lower()):
        # A project root already owns engine/runs. Listing its engine directory as
        # another workspace is both redundant and easy to select accidentally.
        if path == current / "engine":
            continue
        if not _valid_workspace(path):
            continue
        run_count = sum(1 for root in _run_roots(path) for run in root.iterdir()
                        if run.is_dir() and (run / "state.json").is_file()
                        and not run.name.endswith("-literature"))
        rows.append({"path": str(path), "name": path.name,
                     "current": path == current, "run_count": run_count})
    return rows


def _find_run(workspace: Path, run_id: str):
    if not run_id or run_id in {".", ".."} or "/" in run_id or "\\" in run_id:
        return None, None
    for root in _run_roots(workspace):
        run = root / run_id
        if run.is_dir() and (run / "state.json").is_file():
            return run, str(run.relative_to(workspace))
    return None, None


def _next_step(status, phase, current_stage, pending_orders):
    if status == "completed":
        return "研究已完成，可查看结论、研究方向与全部产物。"
    if status in {"failed", "blocked_research"}:
        return "运行需要 Lead 检查错误并决定恢复路径。"
    if pending_orders:
        roles = "、".join(sorted({(o.get("agent_id") or "智能体").split(".")[0]
                                  for o in pending_orders}))
        return f"等待 {roles} 回答 {len(pending_orders)} 张工单。"
    if phase == "research":
        return "Research 正在扩展关键词并收集、去重语料。"
    if phase == "literature":
        return "Scheduler 将根据知识库状态决定下一批阅读或结束文献循环。"
    if phase == "finalization":
        return f"继续生成并审核终段产物：{current_stage or '下一阶段'}。"
    return "等待 Lead 根据磁盘状态推进运行。"


def run_view(run: Path, detail=False, run_source=None):
    state = _json(run / "state.json", {}) or {}
    literature_run = run.parent / f"{run.name}-literature"
    literature = _json(literature_run / "state.json", {}) or {}
    completed = literature.get("completed_paper_ids") or state.get("completed_paper_ids") or []
    pending = literature.get("pending_tasks") or state.get("pending_tasks") or []
    failed = literature.get("failed_tasks") or state.get("failed_tasks") or []
    pending_stage = state.get("pending_final_task") or {}
    current = state.get("current_stage") or pending_stage.get("stage")
    status = state.get("status") or "unknown"
    phase = state.get("phase") or ("completed" if status == "completed" else "unknown")
    pending_orders = _orders(run) + _orders(literature_run)
    events = _jsonl(run / "events.jsonl", 100 if detail else 15)
    events += _jsonl(literature_run / "events.jsonl", 100 if detail else 15)
    events.sort(key=lambda row: _timestamp(row.get("timestamp") or row.get("created_at")))
    artifacts = (_files(run, "artifacts") + _files(run, "results") +
                 _files(run, "viz") + _files(run, "visualization"))
    updated_at = max((state.get("updated_at"), literature.get("updated_at")),
                     key=_timestamp, default=None)
    age_seconds = max(0, int(datetime.now(timezone.utc).timestamp() - _timestamp(updated_at))) if _timestamp(updated_at) else None
    relation = literature.get("relation_review") or state.get("relation_review") or {}
    final_artifacts = state.get("final_artifacts") or {}
    writing = final_artifacts.get("writing") or {}
    synthesis = (final_artifacts.get("synthesis")
                 or _json(run / "artifacts" / "synthesis.json", {}) or {})
    gap_analysis = (final_artifacts.get("gap_analysis")
                    or _json(run / "artifacts" / "gap_analysis.json", {}) or {})
    ideation = literature.get("ideation") or {}
    view = {
        "run_id": state.get("run_id") or run.name, "run_source": run_source or run.name,
        "research_question": state.get("research_question") or literature.get("research_goal") or "",
        "status": status, "phase": phase, "terminal": status in TERMINAL,
        "created_at": state.get("created_at"), "updated_at": updated_at,
        "age_seconds": age_seconds,
        "stale": status not in TERMINAL and age_seconds is not None and age_seconds > 120,
        "cycle": literature.get("cycle") or state.get("cycle") or 0, "current_stage": current,
        "paper_counts": {"corpus": _corpus_count(state.get("research_output") or literature.get("research_output")),
                         "completed": len(completed), "pending": len(pending), "failed": len(failed)},
        "completed_paper_ids": completed[-50:], "pending_orders": pending_orders,
        "artifacts": artifacts, "recent_events": events[-40:],
        "final_artifact": state.get("final_artifact"), "relation_review": relation,
        "next_step": _next_step(status, phase, current, pending_orders),
        "summary": writing.get("abstract") or synthesis.get("summary") or "",
        "report_title": writing.get("title") or "", "ideation": ideation,
        "literature_review": ideation.get("literature_review") or synthesis,
        "research_gaps": ideation.get("gap_analysis") or gap_analysis}
    if detail:
        view["sessions"] = _session_rows(run)
        view["scheduler_decisions"] = literature.get("scheduler_decisions") or state.get("scheduler_decisions") or []
        view["reread_log"] = literature.get("reread_log") or state.get("reread_log") or []
        view["completed_papers"] = _paper_rows(run, completed[-50:])
    return view


class Handler(BaseHTTPRequestHandler):
    workspace: Path
    workspace_history = set()
    workspace_lock = threading.RLock()

    def log_message(self, fmt, *args):
        return

    def _send(self, body, content_type="application/json; charset=utf-8", status=200):
        if not isinstance(body, bytes):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, value, status=200):
        self._send(json.dumps(value, ensure_ascii=False, default=str), status=status)

    def _safe(self, base: Path, relative: str):
        target = (base / unquote(relative)).resolve()
        return target if target == base.resolve() or base.resolve() in target.parents else None

    @classmethod
    def _current_workspace(cls):
        with cls.workspace_lock:
            return cls.workspace.resolve()

    def _workspace_payload(self, workspace: Path):
        runs = []
        for runs_dir in _run_roots(workspace):
            for path in runs_dir.iterdir():
                if (path.is_dir() and (path / "state.json").is_file()
                        and not path.name.endswith("-literature")):
                    runs.append(run_view(
                        path, run_source=str(path.relative_to(workspace))))
        runs.sort(key=lambda row: row.get("updated_at") or row.get("created_at") or "",
                  reverse=True)
        return {"workspace": str(workspace), "workspace_name": workspace.name,
                "runs": runs, "poll_seconds": POLL_SECONDS, "read_only": True,
                "server_time": datetime.now(timezone.utc).isoformat()}

    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        workspace = self._current_workspace()
        if parsed.path == "/api/workspace":
            return self._send_json(self._workspace_payload(workspace))
        if parsed.path == "/api/workspaces":
            return self._send_json({
                "current": str(workspace),
                "workspaces": _workspace_candidates(workspace, self.workspace_history)})
        if parsed.path.startswith("/api/runs/"):
            run_id = unquote(parsed.path[len("/api/runs/"):]).strip("/")
            run, source = _find_run(workspace, run_id)
            if run:
                return self._send_json(run_view(run, detail=True, run_source=source))
            return self._send_json({"error": "run_not_found"}, HTTPStatus.NOT_FOUND)
        if parsed.path == "/api/file":
            relative = (parse_qs(parsed.query).get("path") or [""])[0]
            target = self._safe(workspace, relative)
            allowed = target and any(part in {"artifacts", "results", "viz", "visualization"} for part in target.parts)
            if not allowed or not target.is_file():
                return self._send_json({"error": "file_not_found"}, HTTPStatus.NOT_FOUND)
            mime = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            return self._send(target.read_bytes(), mime)
        relative = "index.html" if parsed.path == "/" else parsed.path.lstrip("/")
        target = self._safe(STATIC, relative)
        if target and target.is_file():
            mime = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            return self._send(target.read_bytes(), mime)
        return self._send_json({"error": "not_found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self):  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path != "/api/workspace":
            return self._send_json({"error": "not_found"}, HTTPStatus.NOT_FOUND)
        if not (self.headers.get("Content-Type") or "").startswith("application/json"):
            return self._send_json({"error": "json_required"}, HTTPStatus.BAD_REQUEST)
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > 16384:
            return self._send_json({"error": "invalid_request_size"},
                                   HTTPStatus.BAD_REQUEST)
        try:
            value = json.loads(self.rfile.read(length).decode("utf-8"))
            target = Path(str(value.get("workspace") or "")).expanduser().resolve()
        except (ValueError, UnicodeDecodeError, OSError, RuntimeError, AttributeError):
            return self._send_json({"error": "invalid_workspace"},
                                   HTTPStatus.BAD_REQUEST)
        if not _valid_workspace(target):
            return self._send_json({
                "error": "invalid_workspace",
                "message": "目录不是 ResearchOS 项目，也不包含可识别的运行记录。"},
                HTTPStatus.BAD_REQUEST)
        with self.workspace_lock:
            self.workspace_history.add(str(self.workspace.resolve()))
            self.workspace_history.add(str(target))
            type(self).workspace = target
        return self._send_json(self._workspace_payload(target))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8767)
    args = parser.parse_args()
    Handler.workspace = Path(args.workspace).expanduser().resolve()
    Handler.workspace_history = {str(Handler.workspace)}
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"ResearchOS workbench: http://{args.host}:{args.port}", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
