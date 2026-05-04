from __future__ import annotations

import argparse
import builtins
import json
import logging
import os
import queue
import re
import subprocess
import sys
import threading
import uuid
import webbrowser
from datetime import datetime
from io import TextIOBase
from pathlib import Path

from flask import Flask, Response, jsonify, request, send_from_directory, stream_with_context

SCRIPT_DIR = Path(__file__).parent.resolve()
FRONTEND_DIR = SCRIPT_DIR / "frontend"
PORT = int(os.environ.get("WEB_PORT", "7788"))
CONFIG_FILE = SCRIPT_DIR / "config.json"


def _load_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_config(cfg: dict) -> None:
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")


# Apply saved config to env on startup
_startup_cfg = _load_config()
if _startup_cfg.get("api_key"):
    os.environ.setdefault("CIVITAI_API_KEY", _startup_cfg["api_key"])

# ── Shared state ───────────────────────────────────────────────
TASKS: dict[str, dict] = {}
TASKS_LOCK = threading.Lock()

SSE_CLIENTS: list[queue.Queue] = []
CLIENTS_LOCK = threading.Lock()

CMD_LABELS = {
    1: ("Split post",     "Local"),
    2: ("Dual upload",    "Civitai + Pixiv"),
    3: ("Pixiv only",     "Pixiv only"),
    4: ("Setup R-18 mosaic", "Local"),
    5: ("Check update",   "Local"),
}

# ── Flask app ──────────────────────────────────────────────────
app = Flask(__name__, static_folder=None)

# ── SSE helpers ────────────────────────────────────────────────
def _broadcast_sse(event_type: str, data: dict) -> None:
    payload = {"type": event_type, "data": data}
    with CLIENTS_LOCK:
        dead = []
        for q in SSE_CLIENTS:
            try:
                q.put_nowait(payload)
            except queue.Full:
                dead.append(q)
        for q in dead:
            SSE_CLIENTS.remove(q)


_PROGRESS_RE = re.compile(r'\[(\d+)/(\d+)\]')
_TOTAL_RE    = re.compile(r'upload/ 有 (\d+) 张图片')


def _push_log_line(task_id: str, lvl: str, src: str, msg: str) -> None:
    entry = {
        "t":       datetime.now().strftime("%H:%M:%S.%f")[:12],
        "lvl":     lvl.upper().replace("WARNING", "WARN").replace("ERROR", "ERR"),
        "src":     src,
        "msg":     msg,
        "task_id": task_id,
    }
    with TASKS_LOCK:
        if task_id in TASKS:
            TASKS[task_id]["log_lines"].append(entry)
            # parse progress from [N/M] pattern
            m = _PROGRESS_RE.search(msg)
            if m:
                cur, total = int(m.group(1)), int(m.group(2))
                TASKS[task_id]["progress"] = cur / total if total > 0 else 0
                TASKS[task_id]["count"] = f"{cur} / {total} imgs"
            # parse total from "upload/ 有 X 张图片"
            tm = _TOTAL_RE.search(msg)
            if tm and TASKS[task_id]["count"] == "—":
                TASKS[task_id]["count"] = f"0 / {tm.group(1)} imgs"
    _broadcast_sse("log", entry)
    # broadcast updated task snapshot if progress changed
    if _PROGRESS_RE.search(msg) or _TOTAL_RE.search(msg):
        with TASKS_LOCK:
            if task_id in TASKS:
                snap = {k: v for k, v in TASKS[task_id].items()
                        if k not in ("thread", "log_lines", "pending_input", "cancel_event")}
        _broadcast_sse("task_update", snap)


def _set_task_status(task_id: str, status: str, progress: float | None = None) -> None:
    with TASKS_LOCK:
        if task_id not in TASKS:
            return
        TASKS[task_id]["status"] = status
        if progress is not None:
            TASKS[task_id]["progress"] = progress
        snap = {k: v for k, v in TASKS[task_id].items() if k not in ("thread", "log_lines", "pending_input", "cancel_event")}
    _broadcast_sse("task_update", snap)


# ── Log capture ────────────────────────────────────────────────
class _ThreadWriter(TextIOBase):
    def __init__(self, original, task_id: str, lvl: str) -> None:
        self._orig = original
        self._task_id = task_id
        self._lvl = lvl

    def write(self, text: str) -> int:
        if text and text.strip():
            _push_log_line(self._task_id, self._lvl, "worker", text.rstrip())
        return self._orig.write(text)

    def flush(self) -> None:
        self._orig.flush()


class _SseLogHandler(logging.Handler):
    def __init__(self, task_id: str) -> None:
        super().__init__()
        self._task_id = task_id

    def emit(self, record: logging.LogRecord) -> None:
        lvl = record.levelname
        _push_log_line(self._task_id, lvl, "civitai", self.format(record))


# ── Input capture (挂起线程等前端回复) ─────────────────────────
class _WebInput:
    def __init__(self, task_id: str) -> None:
        self._task_id = task_id

    def __call__(self, prompt: str = "") -> str:
        ev = threading.Event()
        result = ["\n"]
        with TASKS_LOCK:
            if self._task_id in TASKS:
                TASKS[self._task_id]["pending_input"] = {"prompt": prompt, "event": ev, "result": result}
        _broadcast_sse("input_required", {"task_id": self._task_id, "prompt": prompt})
        ev.wait(timeout=300)
        with TASKS_LOCK:
            if self._task_id in TASKS:
                TASKS[self._task_id].pop("pending_input", None)
        return result[0]


# ── Task runner ────────────────────────────────────────────────
def _run_task(task_id: str, cmd: int, params: dict) -> None:
    orig_stdout = sys.stdout
    orig_stderr = sys.stderr
    orig_input  = builtins.input
    sys.stdout  = _ThreadWriter(orig_stdout, task_id, "INFO")
    sys.stderr  = _ThreadWriter(orig_stderr, task_id, "ERR")
    builtins.input = _WebInput(task_id)

    cs_logger = logging.getLogger("civitai_splitter")
    cs_logger.setLevel(logging.DEBUG)
    sse_handler = _SseLogHandler(task_id)
    sse_handler.setFormatter(logging.Formatter('%(message)s'))
    cs_logger.addHandler(sse_handler)

    with TASKS_LOCK:
        cancel_event = TASKS[task_id].get("cancel_event")

    _set_task_status(task_id, "running")
    try:
        if cmd == 1:
            from civitai_splitter import cmd_split
            args = argparse.Namespace(
                posts=params.get("posts", []),
                api_key=os.environ.get("CIVITAI_API_KEY", params.get("api_key", "")),
                delay=params.get("delay", 10),
            )
            cmd_split(args)

        elif cmd == 2:
            from civitai_splitter import cmd_upload
            args = argparse.Namespace(
                targets="civitai,pixiv",
                count=params.get("count", 0),
                files=params.get("files", []),
                delay=params.get("delay", 10),
                dry_run=False,
                pixiv_privacy="public",
                pixiv_allow_tag_edits="false",
                pixiv_max_retries=1,
                abort_after_failures=3,
                cancel_event=cancel_event,
            )
            cmd_upload(args)

        elif cmd == 3:
            from civitai_splitter import cmd_upload
            args = argparse.Namespace(
                targets="pixiv",
                count=params.get("count", 0),
                files=params.get("files", []),
                delay=params.get("delay", 10),
                dry_run=False,
                pixiv_privacy="public",
                pixiv_allow_tag_edits="false",
                pixiv_max_retries=1,
                abort_after_failures=3,
                cancel_event=cancel_event,
            )
            cmd_upload(args)

        elif cmd == 4:
            # setup_censor has interactive pip install — run as subprocess
            setup_path = SCRIPT_DIR / "pixiv" / "setup_censor.py"
            proc = subprocess.Popen(
                [sys.executable, str(setup_path)],
                cwd=str(SCRIPT_DIR),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            for line in proc.stdout:
                _push_log_line(task_id, "INFO", "setup", line.rstrip())
            proc.wait()

        elif cmd == 5:
            import launcher as _launcher
            _launcher.cmd_check_update()

        if cancel_event and cancel_event.is_set():
            _push_log_line(task_id, "INFO", "worker", "任务已取消")
            _set_task_status(task_id, "failed")
        else:
            _set_task_status(task_id, "done", 1.0)

    except Exception as exc:
        _push_log_line(task_id, "ERR", "worker", f"Task error: {exc}")
        _set_task_status(task_id, "failed")

    finally:
        cs_logger.removeHandler(sse_handler)
        sys.stdout    = orig_stdout
        sys.stderr    = orig_stderr
        builtins.input = orig_input


# ── Routes ─────────────────────────────────────────────────────
@app.route("/")
def index():
    return send_from_directory(FRONTEND_DIR, "index.html")


@app.route("/frontend/<path:filename>")
def frontend_static(filename):
    return send_from_directory(FRONTEND_DIR, filename)


@app.route("/api/run/<int:cmd>", methods=["POST"])
def api_run(cmd):
    if cmd not in CMD_LABELS:
        return jsonify({"error": "invalid cmd"}), 400
    params = request.get_json(silent=True) or {}
    task_id = uuid.uuid4().hex[:8]
    label, target = CMD_LABELS[cmd]
    task = {
        "id":         task_id,
        "title":      label,
        "status":     "queued",
        "progress":   0.0,
        "target":     target,
        "count":      "—",
        "eta":        "—",
        "cmd":        cmd,
        "cancel_flag": False,
        "cancel_event": threading.Event(),
        "created_at": datetime.now().strftime("%H:%M:%S"),
        "log_lines":  [],
        "pending_input": None,
        "thread":     None,
    }
    with TASKS_LOCK:
        TASKS[task_id] = task
    snap = {k: v for k, v in task.items() if k not in ("thread", "log_lines", "pending_input", "cancel_event")}
    _broadcast_sse("task_update", snap)

    t = threading.Thread(target=_run_task, args=(task_id, cmd, params), daemon=True)
    task["thread"] = t
    t.start()
    return jsonify({"task_id": task_id})


@app.route("/api/tasks")
def api_tasks():
    with TASKS_LOCK:
        result = [
            {k: v for k, v in t.items() if k not in ("thread", "log_lines", "pending_input", "cancel_event")}
            for t in TASKS.values()
        ]
    return jsonify(result)


@app.route("/api/tasks/<task_id>/cancel", methods=["POST"])
def api_cancel(task_id):
    with TASKS_LOCK:
        if task_id not in TASKS:
            return jsonify({"error": "not found"}), 404
        TASKS[task_id]["cancel_flag"] = True
        ev = TASKS[task_id].get("cancel_event")
    if ev:
        ev.set()
    return jsonify({"ok": True})


@app.route("/api/tasks/<task_id>/resume", methods=["POST"])
def api_resume(task_id):
    body = request.get_json(silent=True) or {}
    answer = body.get("answer", "\n")
    with TASKS_LOCK:
        pending = TASKS.get(task_id, {}).get("pending_input")
    if not pending:
        return jsonify({"error": "no pending input"}), 404
    pending["result"][0] = answer
    pending["event"].set()
    return jsonify({"ok": True})


@app.route("/api/tasks/<task_id>/remove", methods=["POST"])
def api_remove(task_id):
    with TASKS_LOCK:
        if task_id not in TASKS:
            return jsonify({"error": "not found"}), 404
        del TASKS[task_id]
    _broadcast_sse("task_remove", {"id": task_id})
    return jsonify({"ok": True})


@app.route("/api/settings", methods=["POST"])
def api_settings():
    body = request.get_json(silent=True) or {}
    cfg = _load_config()
    if "api_key" in body:
        cfg["api_key"] = body["api_key"].strip()
        os.environ["CIVITAI_API_KEY"] = cfg["api_key"]
    _save_config(cfg)
    return jsonify({"ok": True})


@app.route("/api/images")
def api_images():
    upload_dir = SCRIPT_DIR / "upload"
    if not upload_dir.exists():
        return jsonify([])
    exts = {'.jpg', '.jpeg', '.png', '.gif', '.webp'}
    files = []
    for f in sorted(upload_dir.iterdir()):
        if f.is_file() and f.suffix.lower() in exts:
            files.append({"name": f.name, "size": f.stat().st_size})
    return jsonify(files)


@app.route("/upload/<path:filename>")
def upload_file(filename):
    return send_from_directory(SCRIPT_DIR / "upload", filename)


@app.route("/api/add-upload-files", methods=["POST"])
def api_add_upload_files():
    upload_dir = SCRIPT_DIR / "upload"
    upload_dir.mkdir(exist_ok=True)
    saved = []
    for f in request.files.getlist("files"):
        if f.filename:
            fname = Path(f.filename).name
            dest = upload_dir / fname
            f.save(str(dest))
            saved.append(fname)
    return jsonify({"saved": saved})


@app.route("/api/open-folder")
def api_open_folder():
    upload_dir = SCRIPT_DIR / "upload"
    upload_dir.mkdir(exist_ok=True)
    try:
        os.startfile(str(upload_dir))
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    return jsonify({"ok": True})


@app.route("/api/status")
def api_status():
    model_path  = SCRIPT_DIR / "models" / "auto_censor.pt"
    upload_dir  = SCRIPT_DIR / "upload"
    upload_count = sum(1 for f in upload_dir.iterdir() if f.is_file()) if upload_dir.exists() else 0
    api_key = os.environ.get("CIVITAI_API_KEY", "")
    if len(api_key) > 4:
        masked = "*" * (len(api_key) - 4) + api_key[-4:]
    else:
        masked = "*" * len(api_key)
    return jsonify({
        "mosaic_installed": model_path.exists(),
        "upload_count":     upload_count,
        "has_api_key":      bool(api_key),
        "api_key_masked":   masked,
    })


@app.route("/api/stream")
def api_stream():
    client_q: queue.Queue = queue.Queue(maxsize=500)
    with CLIENTS_LOCK:
        SSE_CLIENTS.append(client_q)

    def generate():
        # Push current state snapshot on connect
        with TASKS_LOCK:
            all_tasks = [
                {k: v for k, v in t.items() if k not in ("thread", "log_lines", "pending_input", "cancel_event")}
                for t in TASKS.values()
            ]
            recent_logs = []
            for t in TASKS.values():
                recent_logs.extend(t.get("log_lines", [])[-10:])
        recent_logs.sort(key=lambda x: x.get("t", ""))

        for snap in all_tasks:
            yield f"event: task_update\ndata: {json.dumps(snap, ensure_ascii=False)}\n\n"
        for entry in recent_logs[-50:]:
            yield f"event: log\ndata: {json.dumps(entry, ensure_ascii=False)}\n\n"

        try:
            while True:
                try:
                    item = client_q.get(timeout=25)
                    yield f"event: {item['type']}\ndata: {json.dumps(item['data'], ensure_ascii=False)}\n\n"
                except queue.Empty:
                    yield ": heartbeat\n\n"
        finally:
            with CLIENTS_LOCK:
                if client_q in SSE_CLIENTS:
                    SSE_CLIENTS.remove(client_q)

    return Response(
        stream_with_context(generate()),
        content_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Entry point ────────────────────────────────────────────────
def main() -> None:
    url = f"http://localhost:{PORT}"
    print(f"Starting web server at {url}")
    print("Press Ctrl+C to stop.")
    threading.Timer(1.5, webbrowser.open, args=[url]).start()
    app.run(host="0.0.0.0", port=PORT, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
