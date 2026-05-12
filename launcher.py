"""Interactive launcher menu. Single entry point for all functions."""
from __future__ import annotations

import json
import os
import random
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent.resolve()


def _read_config_key() -> str:
    cfg_file = SCRIPT_DIR / "config.json"
    if cfg_file.exists():
        try:
            return json.loads(cfg_file.read_text(encoding="utf-8")).get("api_key", "")
        except Exception:
            pass
    return ""


DEFAULT_API_KEY = os.environ.get("CIVITAI_API_KEY", "") or _read_config_key()

LAST_UPDATE_CHECK_FILE = SCRIPT_DIR / ".last_update_check"
UPDATE_CHECK_INTERVAL_HOURS = 24  # 最多 24 小时检一次，避免每次启动联网


def _raise_if_canceled(cancel_event) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise InterruptedError("task canceled")


def _terminate_process(proc: subprocess.Popen) -> None:
    try:
        proc.terminate()
        proc.wait(timeout=2)
    except Exception:
        try:
            proc.kill()
            proc.wait(timeout=2)
        except Exception:
            pass


def _run_command_with_cancel(command: list[str], timeout: float, cancel_event=None, capture_output: bool = False):
    stdout = subprocess.PIPE if capture_output else subprocess.DEVNULL
    proc = subprocess.Popen(
        command,
        stdout=stdout,
        stderr=subprocess.DEVNULL,
        text=capture_output,
    )
    deadline = time.monotonic() + timeout
    try:
        while proc.poll() is None:
            _raise_if_canceled(cancel_event)
            if time.monotonic() >= deadline:
                _terminate_process(proc)
                raise subprocess.TimeoutExpired(command, timeout)
            time.sleep(0.2)
    except InterruptedError:
        _terminate_process(proc)
        raise
    out, _ = proc.communicate()
    if capture_output:
        if proc.returncode != 0:
            raise subprocess.CalledProcessError(proc.returncode, command, output=out)
        return (out or "").strip()
    return proc.returncode


def _check_updates(force: bool = False, cancel_event=None) -> tuple[bool, int, str]:
    """检查 git 远端是否有新提交。返回 (有更新, 落后数, 简介)."""
    if not (SCRIPT_DIR / ".git").exists():
        return False, 0, ""
    _raise_if_canceled(cancel_event)
    if not force and LAST_UPDATE_CHECK_FILE.exists():
        age_h = (time.time() - LAST_UPDATE_CHECK_FILE.stat().st_mtime) / 3600
        if age_h < UPDATE_CHECK_INTERVAL_HOURS:
            try:
                out = _run_command_with_cancel(
                    ["git", "-C", str(SCRIPT_DIR), "rev-list", "--count", "HEAD..@{u}"],
                    timeout=5,
                    cancel_event=cancel_event,
                    capture_output=True,
                )
                behind = int(out)
                if behind > 0:
                    log = _run_command_with_cancel(
                        ["git", "-C", str(SCRIPT_DIR), "log", "--oneline", "HEAD..@{u}", "-3"],
                        timeout=5,
                        cancel_event=cancel_event,
                        capture_output=True,
                    )
                    return True, behind, log
            except InterruptedError:
                raise
            except Exception:
                pass
            return False, 0, ""
    try:
        _run_command_with_cancel(
            ["git", "-C", str(SCRIPT_DIR), "fetch", "--quiet", "origin"],
            timeout=15,
            cancel_event=cancel_event,
        )
        out = _run_command_with_cancel(
            ["git", "-C", str(SCRIPT_DIR), "rev-list", "--count", "HEAD..@{u}"],
            timeout=5,
            cancel_event=cancel_event,
            capture_output=True,
        )
        behind = int(out)
        try:
            LAST_UPDATE_CHECK_FILE.touch()
        except Exception:
            pass
        if behind <= 0:
            return False, 0, ""
        log = _run_command_with_cancel(
            ["git", "-C", str(SCRIPT_DIR), "log", "--oneline", "HEAD..@{u}", "-3"],
            timeout=5,
            cancel_event=cancel_event,
            capture_output=True,
        )
        return True, behind, log
    except InterruptedError:
        raise
    except Exception:
        return False, 0, ""


def _do_pull() -> bool:
    try:
        ret = subprocess.call(
            ["git", "-C", str(SCRIPT_DIR), "pull", "--ff-only"],
            timeout=60,
        )
        return ret == 0
    except Exception as exc:
        print(f"  pull 失败: {exc}")
        return False


_update_banner = ""


def header():
    print()
    print(" " + "=" * 46)
    print("  Civitai Post Splitter & Pixiv Uploader")
    print(" " + "=" * 46)
    if _update_banner:
        print()
        print(_update_banner)
    print()
    print("  [1] 拆分 Civitai 帖子（一帖多图 -> 多帖单图）")
    print("  [2] 上传到双端 (Civitai + Pixiv)")
    print("  [3] 仅上传到 Pixiv")
    print("  [4] 安装 / 检查 R-18 自动打码")
    print("  [5] 检查 / 拉取更新")
    print("  [6] 配置图片打标 (cl_tagger / WD14)")
    print("  [7] 切换 Pixiv 账号（清除 + 重新登录）")
    print("  [8] 切换 Civitai 账号（清除 + 重新登录）")
    print("  [9] 定时自动发布（配置 / 启动）")
    print("  [Q] 退出")
    print()


def run(args: list[str], extra_env: dict | None = None) -> int:
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)
    return subprocess.call([sys.executable, *args], cwd=str(SCRIPT_DIR), env=env)


def cmd_split() -> None:
    api_key = os.environ.get("CIVITAI_API_KEY", "") or DEFAULT_API_KEY
    if not api_key:
        print()
        print("[!] 未检测到 CIVITAI_API_KEY 环境变量。")
        print("    永久设置（推荐）：在 cmd 跑   setx CIVITAI_API_KEY 你的key")
        print("    然后重新打开此窗口。")
        print()
        api_key = input("    或现在临时输入 key（留空返回菜单）: ").strip()
        if not api_key:
            return
    run(["civitai_splitter.py", "split"], extra_env={"CIVITAI_API_KEY": api_key})


def _ask_count() -> list[str]:
    """问用户上传几张。留空 -> 随机 1-5（不传 --count）。"""
    raw = input("  本次上传几张？（留空=随机 1-5）: ").strip()
    if not raw:
        return []
    try:
        n = int(raw)
        if n > 0:
            return ["--count", str(n)]
    except ValueError:
        pass
    print("  无效数字，使用默认随机 1-5")
    return []


def cmd_upload_dual() -> None:
    extra = _ask_count()
    run(["civitai_splitter.py", "upload", "--targets", "civitai,pixiv", *extra])


def cmd_upload_pixiv() -> None:
    extra = _ask_count()
    run(["civitai_splitter.py", "upload", "--targets", "pixiv", *extra])


def cmd_setup_censor() -> None:
    run([str(Path("pixiv") / "setup_censor.py")])


def cmd_setup_tagger() -> None:
    run([str(Path("pixiv") / "setup_tagger.py")])


def cmd_pixiv_logout() -> None:
    import shutil
    from playwright.sync_api import sync_playwright
    from pixiv.support import PIXIV_PROFILE_DIR
    shutil.rmtree(PIXIV_PROFILE_DIR, ignore_errors=True)
    print(f"已清除旧登录状态（{PIXIV_PROFILE_DIR}）。")
    print("正在打开浏览器，请登录 Pixiv 后回到此窗口按 Enter...")
    with sync_playwright() as pw:
        context = pw.chromium.launch_persistent_context(
            str(PIXIV_PROFILE_DIR),
            channel="chrome",
            headless=False,
            args=["--disable-sync", "--no-first-run"],
            ignore_default_args=["--enable-automation", "--no-sandbox"],
        )
        page = context.pages[0] if context.pages else context.new_page()
        try:
            page.goto("https://accounts.pixiv.net/login", wait_until="commit", timeout=15000)
        except Exception:
            pass
        input("\n>>> 登录完成后按 Enter 关闭浏览器并保存登录状态... ")
        context.close()
    print("浏览器已关闭，登录状态已保存。")


def cmd_civitai_login() -> None:
    import shutil
    from playwright.sync_api import sync_playwright
    profile = Path.home() / ".civitai_splitter_chrome"
    shutil.rmtree(profile, ignore_errors=True)
    print(f"已清除旧登录状态（{profile}）。")
    print("正在打开浏览器，请登录 Civitai 后回到此窗口按 Enter...")
    with sync_playwright() as pw:
        context = pw.chromium.launch_persistent_context(
            str(profile),
            channel="chrome",
            headless=False,
            args=[],
            ignore_default_args=["--enable-automation", "--no-sandbox"],
        )
        page = context.pages[0] if context.pages else context.new_page()
        try:
            page.goto("https://civitai.red", wait_until="commit", timeout=15000)
        except Exception:
            pass
        input("\n>>> 登录完成后按 Enter 关闭浏览器并保存登录状态... ")
        context.close()
    print("浏览器已关闭，登录状态已保存。")


def cmd_check_update(cancel_event=None) -> None:
    global _update_banner
    print("正在检查更新...")
    has, n, info = _check_updates(force=True, cancel_event=cancel_event)
    if not has:
        print("已是最新版本。")
        _update_banner = ""
        return
    print(f"\n[!] 有 {n} 个新提交：")
    print(info)
    print()
    _raise_if_canceled(cancel_event)
    ans = input("现在拉取更新？[Y/n] ").strip().lower()
    _raise_if_canceled(cancel_event)
    if ans in ("", "y", "yes"):
        if _do_pull():
            print("\n更新完成。建议重启此菜单生效。")
            _update_banner = ""
        else:
            print("\n拉取失败。可能本地有未提交改动；请手动跑 `git status` 查看。")
    else:
        print("\n取消。下次启动还会提示。")


def _sched_config() -> dict:
    cfg_file = SCRIPT_DIR / "config.json"
    cfg = {}
    if cfg_file.exists():
        try:
            cfg = json.loads(cfg_file.read_text(encoding="utf-8"))
        except Exception:
            pass
    return cfg.get("scheduler") or {
        "enabled": False, "targets": "civitai,pixiv",
        "count": 1, "min_hours": 1.0, "max_hours": 3.0, "next_fire_at": None,
    }


def _save_sched_config(sched: dict) -> None:
    cfg_file = SCRIPT_DIR / "config.json"
    cfg = {}
    if cfg_file.exists():
        try:
            cfg = json.loads(cfg_file.read_text(encoding="utf-8"))
        except Exception:
            pass
    cfg["scheduler"] = sched
    cfg_file.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def cmd_scheduler() -> None:
    sched = _sched_config()
    print()
    print("  当前定时发布配置：")
    print(f"    目标平台：{sched.get('targets', 'civitai,pixiv')}")
    print(f"    每次张数：{sched.get('count', 1)}")
    print(f"    间隔范围：{sched.get('min_hours', 1.0)} ~ {sched.get('max_hours', 3.0)} 小时")
    next_fire = sched.get("next_fire_at")
    if next_fire:
        try:
            dt = datetime.fromisoformat(next_fire).astimezone()
            print(f"    下次触发：{dt.strftime('%H:%M:%S')}")
        except Exception:
            pass
    print()

    # Configure
    raw = input("  目标平台（civitai / pixiv / civitai,pixiv，留空保持）: ").strip()
    if raw:
        sched["targets"] = raw

    raw = input(f"  每次上传几张（留空保持 {sched.get('count', 1)}）: ").strip()
    if raw.isdigit() and int(raw) > 0:
        sched["count"] = int(raw)

    raw = input(f"  最短间隔小时（留空保持 {sched.get('min_hours', 1.0)}）: ").strip()
    if raw:
        try:
            sched["min_hours"] = float(raw)
        except ValueError:
            pass

    raw = input(f"  最长间隔小时（留空保持 {sched.get('max_hours', 3.0)}）: ").strip()
    if raw:
        try:
            sched["max_hours"] = float(raw)
        except ValueError:
            pass

    _save_sched_config(sched)
    print()
    print("  配置已保存。")
    print()
    ans = input("  现在启动调度循环？启动后保持此窗口运行，Ctrl-C 停止。[Y/n] ").strip().lower()
    if ans not in ("", "y", "yes"):
        return

    targets = sched.get("targets", "civitai,pixiv")
    count = sched.get("count", 1)
    min_h = float(sched.get("min_hours", 1.0))
    max_h = float(sched.get("max_hours", 3.0))
    if min_h > max_h:
        min_h, max_h = max_h, min_h

    upload_dir = SCRIPT_DIR / "upload"
    print()
    print("  调度循环已启动，Ctrl-C 停止。")
    print()
    try:
        while True:
            delay = random.uniform(min_h, max_h) * 3600
            fire_at = datetime.now(timezone.utc).astimezone()
            fire_at_ts = fire_at.timestamp() + delay
            fire_dt = datetime.fromtimestamp(fire_at_ts).strftime("%H:%M:%S")
            h = int(delay // 3600)
            m = int((delay % 3600) // 60)
            print(f"  下次触发：{fire_dt}（约 {h}h {m}m 后）")
            time.sleep(delay)

            imgs = [f for f in upload_dir.iterdir() if f.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}] if upload_dir.exists() else []
            if not imgs:
                print("  upload/ 无图片，跳过本次触发。")
                continue

            print(f"  触发！上传 {count} 张 → {targets}")
            run(["civitai_splitter.py", "upload", "--targets", targets, "--count", str(count)])
    except KeyboardInterrupt:
        print("\n  调度循环已停止。")


def main() -> int:
    global _update_banner
    has, n, info = _check_updates(force=False)
    if has:
        first_line = info.splitlines()[0] if info else ""
        _update_banner = f"  [!] 远端有 {n} 个新提交可拉取（最新: {first_line[:60]}）。选 [5] 更新"

    handlers = {
        "1": ("拆分 Civitai 帖子", cmd_split),
        "2": ("上传到双端", cmd_upload_dual),
        "3": ("仅上传到 Pixiv", cmd_upload_pixiv),
        "4": ("安装 / 检查打码", cmd_setup_censor),
        "5": ("检查 / 拉取更新", cmd_check_update),
        "6": ("配置图片打标 (cl_tagger)", cmd_setup_tagger),
        "7": ("切换 Pixiv 账号（清除 + 重新登录）", cmd_pixiv_logout),
        "8": ("切换 Civitai 账号（清除 + 重新登录）", cmd_civitai_login),
        "9": ("定时自动发布", cmd_scheduler),
    }
    while True:
        header()
        try:
            choice = input("  请选择 [1-9, Q]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if choice in ("q", "quit", "exit"):
            return 0
        if choice in handlers:
            name, fn = handlers[choice]
            print()
            print(f"--- 执行: {name} ---")
            print()
            try:
                fn()
            except KeyboardInterrupt:
                print("\n(用户中断)")
            print()
            input("按 Enter 返回菜单...")
        else:
            print(f"\n无效选择: {choice!r}\n")
            input("按 Enter 继续...")


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
