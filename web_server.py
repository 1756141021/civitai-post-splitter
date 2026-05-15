from __future__ import annotations

import argparse
import builtins
import json
import logging
import os
import queue
import re
import shutil
import subprocess
import sys
import random
import threading
import time
import uuid
import webbrowser
from datetime import datetime, timedelta
from io import TextIOBase
from pathlib import Path

from flask import Flask, Response, jsonify, request, send_from_directory, stream_with_context
from pixiv.llm_platforms import PLATFORM_SPECS
from pixiv.llm_reverse import (
    default_llm_reverse_config,
    infer_image_copy,
    mask_llm_config,
    normalize_llm_reverse_config,
    validate_llm_reverse_config,
)
from pixiv.support import PIXIV_PROFILE_DIR
from x.support   import X_DIR,   X_PROFILE_DIR
from xhs.support import XHS_DIR, XHS_PROFILE_DIR

SCRIPT_DIR = Path(__file__).parent.resolve()
CIVITAI_PROFILE_DIR = Path.home() / ".civitai_splitter_chrome"
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

_scheduler_timer: threading.Timer | None = None
_scheduler_lock = threading.Lock()
_shutdown_timer: threading.Timer | None = None
_shutdown_lock = threading.Lock()
# Serializes tasks that replace process-global sys.stdout/stderr/builtins.input
_EXEC_LOCK = threading.Lock()

SSE_CLIENTS: list[queue.Queue] = []
CLIENTS_LOCK = threading.Lock()

CMD_LABELS = {
    1: ("Split post",     "Local"),
    2: ("Dual upload",    "Civitai + Pixiv"),
    3: ("Pixiv only",     "Pixiv only"),
    4: ("Setup R-18 mosaic", "Local"),
    5: ("Check update",   "Local"),
    6: ("LLM reverse",    "Local"),
}

# ── Flask app ──────────────────────────────────────────────────
app = Flask(__name__, static_folder=None)

def _has_active_tasks() -> bool:
    with TASKS_LOCK:
        return any(t.get("status") in ("queued", "running") for t in TASKS.values())


def _cancel_scheduler() -> dict:
    global _scheduler_timer
    cfg = _load_config()
    sched = {**_sched_default(), **(cfg.get("scheduler") or {})}
    sched["enabled"] = False
    sched["next_fire_at"] = None
    cfg["scheduler"] = sched
    _save_config(cfg)
    with _scheduler_lock:
        if _scheduler_timer is not None:
            _scheduler_timer.cancel()
            _scheduler_timer = None
    return sched


def _exit_when_idle(force: bool = False) -> None:
    with CLIENTS_LOCK:
        has_clients = bool(SSE_CLIENTS)
    if has_clients and not force:
        return
    _cancel_scheduler()
    if _has_active_tasks():
        _schedule_idle_shutdown(force=force)
        return
    time.sleep(0.2)
    os._exit(0)


def _schedule_idle_shutdown(force: bool = False) -> None:
    global _shutdown_timer
    with _shutdown_lock:
        if _shutdown_timer is not None:
            _shutdown_timer.cancel()
        _shutdown_timer = threading.Timer(5.0, _exit_when_idle, kwargs={"force": force})
        _shutdown_timer.daemon = True
        _shutdown_timer.start()


def _cancel_idle_shutdown() -> None:
    global _shutdown_timer
    with _shutdown_lock:
        if _shutdown_timer is not None:
            _shutdown_timer.cancel()
            _shutdown_timer = None


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


def _is_task_canceled(task_id: str) -> bool:
    with TASKS_LOCK:
        task = TASKS.get(task_id)
        if not task:
            return False
        ev = task.get("cancel_event")
        return bool(ev and ev.is_set())


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
            task = TASKS.get(self._task_id)
            if task:
                task.pop("pending_input", None)
                if task.get("cancel_event") and task["cancel_event"].is_set():
                    return ""
        return result[0]


# ── Task runner ────────────────────────────────────────────────
def _run_task(task_id: str, cmd: int, params: dict) -> None:
    with TASKS_LOCK:
        cancel_event = TASKS[task_id].get("cancel_event")

    if cancel_event and cancel_event.is_set():
        _push_log_line(task_id, "INFO", "worker", "任务已取消")
        _set_task_status(task_id, "canceled")
        return

    with _EXEC_LOCK:
        _set_task_status(task_id, "running")
        _run_task_locked(task_id, cmd, params)


def _run_task_locked(task_id: str, cmd: int, params: dict) -> None:
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

    try:
        if cancel_event and cancel_event.is_set():
            _push_log_line(task_id, "INFO", "worker", "任务已取消")
            _set_task_status(task_id, "canceled")
            return

        if cmd == 1:
            from civitai_splitter import cmd_split
            args = argparse.Namespace(
                posts=params.get("posts", []),
                api_key=os.environ.get("CIVITAI_API_KEY", params.get("api_key", "")),
                delay=params.get("delay", 10),
                cancel_event=cancel_event,
            )
            cmd_split(args)
            if _is_task_canceled(task_id):
                _push_log_line(task_id, "INFO", "worker", "任务已取消")
                _set_task_status(task_id, "canceled")
                return

        elif cmd in (2, 3):
            # Both cmd=2 and cmd=3 now route through the same upload entrypoint;
            # the difference (targets) is supplied by the frontend `targets`
            # param. Legacy fallback: cmd=2 → "civitai,pixiv", cmd=3 → "pixiv".
            legacy_default = "civitai,pixiv" if cmd == 2 else "pixiv"
            from civitai_splitter import cmd_upload
            args = argparse.Namespace(
                targets=params.get("targets", legacy_default),
                count=params.get("count", 0),
                files=params.get("files", []),
                sort=params.get("sort", "random"),
                delay=params.get("delay", 10),
                dry_run=False,
                pixiv_privacy="public",
                pixiv_allow_tag_edits="false",
                pixiv_max_retries=1,
                abort_after_failures=3,
                llm_reverse=params.get("llm_reverse", False),
                llm_persona=params.get("llm_persona", ""),
                llm_account=params.get("llm_account", ""),
                llm_content_mode=params.get("llm_content_mode", ""),
                llm_mode=params.get("llm_mode", "unified"),
                llm_personas_by_platform=params.get("llm_personas_by_platform") or {},
                llm_content_modes_by_platform=params.get("llm_content_modes_by_platform") or {},
                x_template=params.get("x_template", ""),
                xhs_template=params.get("xhs_template", ""),
                cancel_event=cancel_event,
            )
            cmd_upload(args)
            if _is_task_canceled(task_id):
                _push_log_line(task_id, "INFO", "worker", "任务已取消")
                _set_task_status(task_id, "canceled")
                return

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
                if _is_task_canceled(task_id):
                    proc.terminate()
                    break
            proc.wait()
            if _is_task_canceled(task_id):
                _push_log_line(task_id, "INFO", "worker", "任务已取消")
                _set_task_status(task_id, "canceled")
                return

        elif cmd == 5:
            import launcher as _launcher
            # Patch _do_pull to capture output into web log (subprocess.call bypasses sys.stdout)
            def _web_do_pull() -> bool:
                try:
                    r = subprocess.run(
                        ["git", "-C", str(SCRIPT_DIR), "pull", "--ff-only"],
                        timeout=60, capture_output=True, text=True,
                    )
                    if r.stdout: print(r.stdout.rstrip())
                    if r.stderr: print(r.stderr.rstrip())
                    return r.returncode == 0
                except Exception as exc:
                    print(f"  pull 失败: {exc}")
                    return False
            _orig_do_pull = _launcher._do_pull
            _launcher._do_pull = _web_do_pull
            try:
                _launcher.cmd_check_update(cancel_event=cancel_event)
            finally:
                _launcher._do_pull = _orig_do_pull
            if _is_task_canceled(task_id):
                _push_log_line(task_id, "INFO", "worker", "任务已取消")
                _set_task_status(task_id, "canceled")
                return

        elif cmd == 6:
            cfg = normalize_llm_reverse_config(_load_config().get("llm_reverse"))
            image_name = str(params.get("image", "")).strip()
            image_path = SCRIPT_DIR / "upload" / Path(image_name).name if image_name else None
            image_url = str(params.get("image_url", "")).strip() or None
            result = infer_image_copy(
                image_path=image_path,
                image_url=image_url,
                config=cfg,
                persona_id=params.get("llm_persona", ""),
                account_id=params.get("llm_account", ""),
                content_mode=params.get("llm_content_mode", ""),
                cancel_event=cancel_event,
            )
            with TASKS_LOCK:
                if task_id in TASKS:
                    TASKS[task_id]["result"] = result
            _push_log_line(task_id, "INFO", "worker", f"LLM 反推: {result.get('status')}")

        if cancel_event and cancel_event.is_set():
            _push_log_line(task_id, "INFO", "worker", "任务已取消")
            _set_task_status(task_id, "canceled")
        else:
            _set_task_status(task_id, "done", 1.0)

    except InterruptedError:
        _push_log_line(task_id, "INFO", "worker", "任务已取消")
        _set_task_status(task_id, "canceled")
    except SystemExit as exc:
        _push_log_line(task_id, "ERR", "worker", f"Task exited: {exc}")
        _set_task_status(task_id, "failed")
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
        "params":     params,
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
        pending = TASKS[task_id].get("pending_input")
    if ev:
        ev.set()
    if pending:
        pending["result"][0] = ""
        pending["event"].set()
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


# Censor preset levels. Maps preset name → enabled_classes string. Class names
# come from pixiv/censor.py CENSOR_CLASS_NAMES: anus, cum, dick, tits, vagina.
_CENSOR_PRESETS = {
    "off":    [],
    "japan":  ["dick", "vagina", "anus", "cum"],
    "strict": ["dick", "vagina", "anus", "cum", "tits"],
}


@app.route("/api/censor-preset", methods=["POST"])
def api_censor_preset():
    """Switch the auto-censor preset (off / japan / strict).

    Rewrites pixiv/censor.json's `preset` field plus the derived
    `enabled_classes` list so cmd_upload (which reads enabled_classes) picks
    up the new level on the next run without server restart.
    """
    body = request.get_json(silent=True) or {}
    preset = str(body.get("preset", "")).strip().lower()
    if preset not in _CENSOR_PRESETS:
        return jsonify({"ok": False, "error": f"unknown preset: {preset}"}), 400
    censor_path = SCRIPT_DIR / "pixiv" / "censor.json"
    try:
        existing = json.loads(censor_path.read_text(encoding="utf-8")) if censor_path.exists() else {}
    except Exception:
        existing = {}
    if not isinstance(existing, dict):
        existing = {}
    existing["preset"] = preset
    existing["enabled_classes"] = list(_CENSOR_PRESETS[preset])
    censor_path.write_text(json.dumps(existing, indent=2, ensure_ascii=False), encoding="utf-8")
    return jsonify({"ok": True, "preset": preset, "enabled_classes": existing["enabled_classes"]})


@app.route("/api/llm-reverse-platforms", methods=["GET"])
def api_llm_reverse_platforms():
    return jsonify({pid: dict(spec, id=pid) for pid, spec in PLATFORM_SPECS.items()})


@app.route("/api/llm-reverse-models", methods=["GET"])
def api_llm_reverse_models():
    import urllib.request as _ur
    import urllib.error as _ue
    provider = request.args.get("provider", "")
    api_key  = request.args.get("api_key", "").strip()
    base_url = request.args.get("base_url", "").rstrip("/")

    # 空 api_key 时 fallback 到 saved（用户保存过但密码框看不到原值）
    if not api_key:
        saved = normalize_llm_reverse_config(_load_config().get("llm_reverse"))
        api_key = str(saved.get("api_key", ""))

    if provider == "anthropic":
        return jsonify({"models": [
            "claude-opus-4-7",
            "claude-sonnet-4-6",
            "claude-haiku-4-5-20251001",
            "claude-3-5-sonnet-20241022",
            "claude-3-5-haiku-20241022",
        ]})

    if provider == "google_gemini":
        if not api_key:
            return jsonify({"error": "需要先填写或保存 API key"}), 400
        # 支持用户自定义 base_url（本地代理），缺省走官方
        gemini_base = base_url or "https://generativelanguage.googleapis.com"
        url = f"{gemini_base}/v1beta/models?key={api_key}"
        try:
            with _ur.urlopen(url, timeout=10) as resp:
                data = json.loads(resp.read())
            models = [
                m["name"].split("/")[-1]
                for m in data.get("models", [])
                if "generateContent" in m.get("supportedGenerationMethods", [])
            ]
            return jsonify({"models": models})
        except _ue.HTTPError as e:
            return jsonify({"error": f"上游返回 {e.code}（检查 API key 或代理是否可用）"}), 502
        except Exception as e:
            return jsonify({"error": f"无法连接：{e}"}), 502

    # openai_compatible
    if not base_url:
        return jsonify({"error": "需要填写 base URL"}), 400
    try:
        req = _ur.Request(
            f"{base_url}/models",
            headers={"Authorization": f"Bearer {api_key}"} if api_key else {},
        )
        with _ur.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        ids = sorted(m["id"] for m in data.get("data", []) if "id" in m)
        return jsonify({"models": ids})
    except _ue.HTTPError as e:
        return jsonify({"error": f"上游返回 {e.code}（检查 API key 或 base URL）"}), 502
    except Exception as e:
        return jsonify({"error": f"无法连接：{e}"}), 502


@app.route("/api/templates", methods=["GET"])
def api_templates():
    try:
        from x.support import load_x_templates, load_x_settings
        x_keys = list(load_x_templates().keys())
        x_default = load_x_settings().get("default_template", "en_sfw")
    except Exception:
        x_keys = ["jp_sfw", "en_sfw", "zh_sfw", "jp_nsfw", "en_nsfw", "zh_nsfw"]
        x_default = "en_sfw"
    try:
        from xhs.support import load_xhs_templates, load_xhs_settings
        xhs_keys = list(load_xhs_templates().keys())
        xhs_default = load_xhs_settings().get("default_template", "default")
    except Exception:
        xhs_keys = ["default"]
        xhs_default = "default"
    return jsonify({"x": x_keys, "x_default": x_default, "xhs": xhs_keys, "xhs_default": xhs_default})


@app.route("/api/llm-reverse-config", methods=["GET"])
def api_llm_reverse_config_get():
    cfg = _load_config()
    return jsonify(mask_llm_config(cfg.get("llm_reverse")))


@app.route("/api/llm-reverse-config", methods=["POST"])
def api_llm_reverse_config_post():
    body = request.get_json(silent=True) or {}
    cfg = _load_config()
    current = normalize_llm_reverse_config(cfg.get("llm_reverse"))
    if body.pop("clear_api_key", False):
        body["api_key"] = ""
    else:
        api_key = body.get("api_key", None)
        if api_key == "":
            body.pop("api_key", None)
        elif api_key is None:
            body["api_key"] = current.get("api_key", "")
    next_cfg = normalize_llm_reverse_config({**current, **body})
    errors = validate_llm_reverse_config(next_cfg)
    if errors:
        return jsonify({"error": "; ".join(errors)}), 400
    cfg["llm_reverse"] = next_cfg
    _save_config(cfg)
    return jsonify(mask_llm_config(next_cfg))


@app.route("/api/images")
def api_images():
    upload_dir = SCRIPT_DIR / "upload"
    if not upload_dir.exists():
        return jsonify([])
    exts = {'.jpg', '.jpeg', '.png', '.webp'}
    files = []
    for f in sorted(upload_dir.iterdir()):
        if f.is_file() and f.suffix.lower() in exts:
            files.append({"name": f.name, "size": f.stat().st_size, "mtime": f.stat().st_mtime})
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
    _img_exts = {'.png', '.jpg', '.jpeg', '.webp'}
    upload_count = sum(1 for f in upload_dir.iterdir() if f.is_file() and f.suffix.lower() in _img_exts) if upload_dir.exists() else 0
    api_key = os.environ.get("CIVITAI_API_KEY", "")
    if len(api_key) > 4:
        masked = "*" * (len(api_key) - 4) + api_key[-4:]
    else:
        masked = "*" * len(api_key)
    cfg = _load_config()
    llm_cfg = normalize_llm_reverse_config(cfg.get("llm_reverse"))
    llm_key = str(llm_cfg.get("api_key", ""))
    llm_masked = "*" * (len(llm_key) - 4) + llm_key[-4:] if len(llm_key) > 4 else "*" * len(llm_key)
    censor_path = SCRIPT_DIR / "pixiv" / "censor.json"
    censor_preset = "japan"
    try:
        if censor_path.exists():
            cdata = json.loads(censor_path.read_text(encoding="utf-8"))
            if isinstance(cdata, dict):
                p = str(cdata.get("preset", "")).strip().lower()
                if p in _CENSOR_PRESETS:
                    censor_preset = p
    except Exception:
        pass
    return jsonify({
        "mosaic_installed":  model_path.exists(),
        "upload_count":      upload_count,
        "has_api_key":       bool(api_key),
        "api_key_masked":    masked,
        "pixiv_logged_in":   (PIXIV_PROFILE_DIR   / ".session_valid").exists(),
        "civitai_logged_in": (CIVITAI_PROFILE_DIR / ".session_valid").exists(),
        "x_logged_in":       (X_DIR   / "cookies.json").exists(),
        "xhs_logged_in":     (XHS_PROFILE_DIR     / ".session_valid").exists(),
        "scheduler":         {**_sched_default(), **(cfg.get("scheduler") or {})},
        "llm_reverse_enabled": bool(llm_cfg.get("enabled")),
        "llm_reverse_configured": bool(llm_cfg.get("base_url") and llm_cfg.get("api_key") and llm_cfg.get("model")),
        "llm_reverse_model": llm_cfg.get("model", ""),
        "llm_reverse_api_key_masked": llm_masked,
        "censor_preset":      censor_preset,
        "upload_defaults":    cfg.get("upload_defaults") or {},
    })


APPDATA_DIR = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
HAINTAG_SETTINGS_PATH = APPDATA_DIR / "HainTag" / "settings.json"


def _load_haintag_settings() -> dict:
    if HAINTAG_SETTINGS_PATH.exists():
        try:
            payload = json.loads(HAINTAG_SETTINGS_PATH.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                return payload.get("settings", payload)
        except Exception:
            pass
    return {}


def _save_haintag_settings(settings: dict) -> None:
    HAINTAG_SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    existing: dict = {}
    if HAINTAG_SETTINGS_PATH.exists():
        try:
            existing = json.loads(HAINTAG_SETTINGS_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    if isinstance(existing, dict) and "settings" in existing:
        existing["settings"].update(settings)
    else:
        existing = settings
    HAINTAG_SETTINGS_PATH.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")


def _scan_model_dir(path: str) -> tuple:
    if not path or not os.path.isdir(path):
        return None, None
    model_file = mapping_file = None
    for f in os.listdir(path):
        fl = f.lower()
        if fl.endswith(".onnx") and not model_file:
            model_file = os.path.join(path, f)
        elif fl.endswith(".json") and any(x in fl for x in ("tag", "mapping", "label")) and not mapping_file:
            mapping_file = os.path.join(path, f)
        elif fl.endswith(".csv") and any(x in fl for x in ("tag", "label")) and not mapping_file:
            mapping_file = os.path.join(path, f)
    return model_file, mapping_file


@app.route("/api/tagger-config", methods=["GET"])
def api_tagger_config_get():
    cfg = _load_config()
    ht = _load_haintag_settings()
    haintag_root = cfg.get("haintag_root", "")
    model_dir = ht.get("tagger_model_dir", "")

    haintag_ok = False
    if haintag_root:
        root_p = Path(haintag_root)
        haintag_ok = (root_p / "native_app" / "tagger.py").exists() or \
                     (root_p / "_internal" / "native_app" / "tagger_subprocess.py").exists()

    model_ok = False
    if model_dir:
        m, mp = _scan_model_dir(model_dir)
        model_ok = bool(m and mp)

    return jsonify({
        "haintag_root": haintag_root,
        "haintag_ok": haintag_ok,
        "model_dir": model_dir,
        "model_ok": model_ok,
        "needs_setup": not model_dir,
    })


@app.route("/api/tagger-config", methods=["POST"])
def api_tagger_config_post():
    body = request.get_json(silent=True) or {}
    changed = []

    if "haintag_root" in body:
        cfg = _load_config()
        val = body["haintag_root"].strip()
        if val:
            cfg["haintag_root"] = val
        else:
            cfg.pop("haintag_root", None)
        _save_config(cfg)
        changed.append("haintag_root")

    if "model_dir" in body:
        val = body["model_dir"].strip()
        _save_haintag_settings({"tagger_model_dir": val})
        changed.append("model_dir")

    return jsonify({"ok": True, "changed": changed})


@app.route("/api/pixiv-logout", methods=["POST"])
def api_pixiv_logout():
    with TASKS_LOCK:
        running_pixiv = any(
            t.get("status") == "running" and t.get("cmd") in (2, 3)
            for t in TASKS.values()
        )
    if running_pixiv:
        return jsonify({"error": "pixiv task is running"}), 400
    (PIXIV_PROFILE_DIR / ".session_valid").unlink(missing_ok=True)
    shutil.rmtree(PIXIV_PROFILE_DIR, ignore_errors=True)
    return jsonify({"ok": True})


@app.route("/api/civitai-logout", methods=["POST"])
def api_civitai_logout():
    with TASKS_LOCK:
        running_civitai = any(
            t.get("status") == "running" and t.get("cmd") in (1, 2)
            for t in TASKS.values()
        )
    if running_civitai:
        return jsonify({"error": "civitai task is running"}), 400
    (CIVITAI_PROFILE_DIR / ".session_valid").unlink(missing_ok=True)
    shutil.rmtree(CIVITAI_PROFILE_DIR, ignore_errors=True)
    return jsonify({"ok": True})


@app.route("/api/pixiv-open-login", methods=["POST"])
def api_pixiv_open_login():
    def _launch():
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as pw:
                context = pw.chromium.launch_persistent_context(
                    str(PIXIV_PROFILE_DIR),
                    channel="chrome",
                    headless=False,
                    args=["--start-maximized", "--disable-sync", "--no-first-run",
                          "--disable-blink-features=AutomationControlled"],
                    ignore_default_args=["--enable-automation", "--no-sandbox"],
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
                    ),
                )
                page = context.pages[0] if context.pages else context.new_page()
                try:
                    page.goto("https://www.pixiv.net/", wait_until="commit", timeout=30000)
                except Exception:
                    pass
                while context.pages:
                    time.sleep(1)
                (PIXIV_PROFILE_DIR / ".session_valid").touch()
        except Exception as exc:
            import logging as _log
            _log.getLogger(__name__).warning(f"pixiv login browser: {exc}")

    import threading as _th
    _th.Thread(target=_launch, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/civitai-open-login", methods=["POST"])
def api_civitai_open_login():
    def _launch():
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as pw:
                context = pw.chromium.launch_persistent_context(
                    str(CIVITAI_PROFILE_DIR),
                    channel="chrome",
                    headless=False,
                    args=["--start-maximized", "--disable-sync", "--no-first-run",
                          "--disable-blink-features=AutomationControlled"],
                    ignore_default_args=["--enable-automation", "--no-sandbox"],
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
                    ),
                )
                page = context.pages[0] if context.pages else context.new_page()
                try:
                    page.goto("https://civitai.com/", wait_until="commit", timeout=30000)
                except Exception:
                    pass
                while context.pages:
                    time.sleep(1)
                (CIVITAI_PROFILE_DIR / ".session_valid").touch()
        except Exception as exc:
            import logging as _log
            _log.getLogger(__name__).warning(f"civitai login browser: {exc}")

    import threading as _th
    _th.Thread(target=_launch, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/upload-defaults", methods=["GET"])
def api_upload_defaults_get():
    return jsonify(_load_config().get("upload_defaults") or {})


@app.route("/api/upload-defaults", methods=["POST"])
def api_upload_defaults_set():
    data = request.get_json(force=True) or {}
    cfg = _load_config()
    cfg["upload_defaults"] = data
    _save_config(cfg)
    return jsonify({"ok": True})


@app.route("/api/x-save-cookies", methods=["POST"])
def api_x_save_cookies():
    data = request.get_json(force=True) or {}
    raw  = data.get("cookies", "")
    try:
        parsed = json.loads(raw)
        if not isinstance(parsed, list):
            raise ValueError("not a list")
    except Exception:
        return jsonify({"error": "不是合法的 JSON 数组"}), 400
    (X_DIR / "cookies.json").write_text(
        json.dumps(parsed, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return jsonify({"ok": True})


@app.route("/api/x-logout", methods=["POST"])
def api_x_logout():
    (X_DIR / "cookies.json").unlink(missing_ok=True)
    shutil.rmtree(X_PROFILE_DIR, ignore_errors=True)
    return jsonify({"ok": True})


@app.route("/api/xhs-save-cookies", methods=["POST"])
def api_xhs_save_cookies():
    data = request.get_json(force=True) or {}
    raw  = data.get("cookies", "")
    try:
        parsed = json.loads(raw)
        if not isinstance(parsed, list):
            raise ValueError("not a list")
    except Exception:
        return jsonify({"error": "不是合法的 JSON 数组"}), 400
    (XHS_DIR / "cookies.json").write_text(
        json.dumps(parsed, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return jsonify({"ok": True})


@app.route("/api/xhs-logout", methods=["POST"])
def api_xhs_logout():
    (XHS_DIR / "cookies.json").unlink(missing_ok=True)
    (XHS_PROFILE_DIR / ".session_valid").unlink(missing_ok=True)
    shutil.rmtree(XHS_PROFILE_DIR, ignore_errors=True)
    return jsonify({"ok": True})


@app.route("/api/xhs-open-login", methods=["POST"])
def api_xhs_open_login():
    def _launch():
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as pw:
                context = pw.chromium.launch_persistent_context(
                    str(XHS_PROFILE_DIR),
                    channel="chrome",
                    headless=False,
                    args=["--start-maximized", "--disable-sync", "--no-first-run",
                          "--disable-blink-features=AutomationControlled"],
                    ignore_default_args=["--enable-automation", "--no-sandbox"],
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
                    ),
                )
                page = context.pages[0] if context.pages else context.new_page()
                try:
                    page.goto("https://www.xiaohongshu.com/", wait_until="commit", timeout=30000)
                except Exception:
                    pass
                while context.pages:
                    time.sleep(1)
                (XHS_PROFILE_DIR / ".session_valid").touch()
        except Exception as exc:
            import logging as _log
            _log.getLogger(__name__).warning(f"xhs login browser: {exc}")

    import threading as _th
    _th.Thread(target=_launch, daemon=True).start()
    return jsonify({"ok": True})


def _sched_default() -> dict:
    return {"enabled": False, "targets": "civitai,pixiv", "count": 1, "sort": "random",
            "min_hours": 0.4, "max_hours": 0.8, "next_fire_at": None}


def _broadcast_scheduler(sched: dict) -> None:
    _broadcast_sse("scheduler_update", dict(sched))


def _arm_scheduler(cfg: dict) -> None:
    global _scheduler_timer
    sched = {**_sched_default(), **(cfg.get("scheduler") or {})}
    with _scheduler_lock:
        if _scheduler_timer is not None:
            _scheduler_timer.cancel()
            _scheduler_timer = None
        if sched.get("enabled"):
            now = datetime.now()
            delay: float | None = None
            next_fire = sched.get("next_fire_at")
            if next_fire:
                try:
                    rem = (datetime.fromisoformat(next_fire) - now).total_seconds()
                    if rem > 0:
                        delay = rem
                except Exception:
                    pass
            if delay is None:
                min_h = max(0.001, float(sched.get("min_hours", 1.0)))
                max_h = max(min_h, float(sched.get("max_hours", 3.0)))
                delay = random.uniform(min_h, max_h) * 3600
                sched["next_fire_at"] = (now + timedelta(seconds=delay)).isoformat(timespec="seconds")
            cfg["scheduler"] = sched
            _save_config(cfg)
            t = threading.Timer(delay, _scheduler_fire)
            t.daemon = True
            _scheduler_timer = t
            t.start()
    _broadcast_scheduler(sched)


def _scheduler_fire() -> None:
    global _scheduler_timer
    with _scheduler_lock:
        _scheduler_timer = None
    cfg = _load_config()
    sched = {**_sched_default(), **(cfg.get("scheduler") or {})}
    if not sched.get("enabled"):
        return
    with TASKS_LOCK:
        any_running = any(t.get("status") in ("running", "queued") for t in TASKS.values())
    upload_dir = SCRIPT_DIR / "upload"
    img_exts = {'.jpg', '.jpeg', '.png', '.webp'}
    has_images = upload_dir.exists() and any(
        f.is_file() and f.suffix.lower() in img_exts for f in upload_dir.iterdir()
    )
    sched["next_fire_at"] = None
    cfg["scheduler"] = sched
    _save_config(cfg)
    if not any_running and has_images:
        targets_str = sched.get("targets", "civitai,pixiv")
        count = max(1, int(sched.get("count", 1)))
        sort_mode = sched.get("sort", "random")
        tl = targets_str.lower()
        cmd = 3 if ("pixiv" in tl and "civitai" not in tl) else 2
        params = {"count": count, "files": [], "targets": targets_str, "sort": sort_mode}
        task_id = uuid.uuid4().hex[:8]
        label, target = CMD_LABELS[cmd]
        task = {
            "id": task_id, "title": f"{label} (auto)",
            "status": "queued", "progress": 0.0, "target": target,
            "count": "—", "eta": "—", "cmd": cmd,
            "cancel_flag": False, "cancel_event": threading.Event(),
            "created_at": datetime.now().strftime("%H:%M:%S"),
            "log_lines": [], "pending_input": None, "thread": None,
        }
        with TASKS_LOCK:
            TASKS[task_id] = task
        snap = {k: v for k, v in task.items() if k not in ("thread", "log_lines", "pending_input", "cancel_event")}
        _broadcast_sse("task_update", snap)
        t = threading.Thread(target=_run_task, args=(task_id, cmd, params), daemon=True)
        task["thread"] = t
        t.start()
    _arm_scheduler(cfg)


@app.route("/api/shutdown", methods=["POST"])
def api_shutdown():
    _schedule_idle_shutdown(force=True)
    return jsonify({"ok": True})


@app.route("/api/scheduler", methods=["POST"])
def api_scheduler():
    body = request.get_json(silent=True) or {}
    cfg = _load_config()
    sched = {**_sched_default(), **(cfg.get("scheduler") or {})}
    if "enabled" in body:
        sched["enabled"] = bool(body["enabled"])
    if "min_hours" in body:
        sched["min_hours"] = max(0.001, float(body["min_hours"]))
    if "max_hours" in body:
        sched["max_hours"] = max(0.001, float(body["max_hours"]))
    if "count" in body:
        sched["count"] = max(1, int(body["count"]))
    if "targets" in body:
        sched["targets"] = body["targets"]
    if "sort" in body:
        sched["sort"] = body["sort"] if body["sort"] in ("random", "name_asc", "name_desc", "time_asc", "time_desc") else "random"
    if sched.get("min_hours", 1.0) > sched.get("max_hours", 3.0):
        return jsonify({"error": "min_hours > max_hours"}), 400
    if any(k in body for k in ("enabled", "min_hours", "max_hours")):
        sched["next_fire_at"] = None
    cfg["scheduler"] = sched
    _save_config(cfg)
    if sched.get("enabled"):
        _arm_scheduler(cfg)
    else:
        with _scheduler_lock:
            global _scheduler_timer
            if _scheduler_timer is not None:
                _scheduler_timer.cancel()
                _scheduler_timer = None
        _broadcast_scheduler(sched)
    return jsonify({"ok": True, "scheduler": sched})


@app.route("/api/stream")
def api_stream():
    client_q: queue.Queue = queue.Queue(maxsize=500)
    _cancel_idle_shutdown()
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

        pending_inputs = []
        with TASKS_LOCK:
            for t in TASKS.values():
                pi = t.get("pending_input")
                if pi:
                    pending_inputs.append({"task_id": t["id"], "prompt": pi.get("prompt", "")})

        for snap in all_tasks:
            yield f"event: task_update\ndata: {json.dumps(snap, ensure_ascii=False)}\n\n"
        yield f"event: scheduler_update\ndata: {json.dumps({**_sched_default(), **(_load_config().get('scheduler') or {})}, ensure_ascii=False)}\n\n"
        for entry in recent_logs[-50:]:
            yield f"event: log\ndata: {json.dumps(entry, ensure_ascii=False)}\n\n"
        for pi in pending_inputs:
            yield f"event: input_required\ndata: {json.dumps(pi, ensure_ascii=False)}\n\n"

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
                has_clients = bool(SSE_CLIENTS)
            if not has_clients:
                _schedule_idle_shutdown()

    return Response(
        stream_with_context(generate()),
        content_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Entry point ────────────────────────────────────────────────
def main() -> None:
    _init_cfg = _load_config()
    if _init_cfg.get("scheduler", {}).get("enabled"):
        _arm_scheduler(_init_cfg)
    url = f"http://localhost:{PORT}"
    print(f"Starting web server at {url}")
    print("Press Ctrl+C to stop.")
    threading.Timer(1.5, webbrowser.open, args=[url]).start()
    app.run(host="0.0.0.0", port=PORT, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
