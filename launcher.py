"""Interactive launcher menu. Single entry point for all functions."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
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


def _check_updates(force: bool = False) -> tuple[bool, int, str]:
    """检查 git 远端是否有新提交。返回 (有更新, 落后数, 简介)."""
    if not (SCRIPT_DIR / ".git").exists():
        return False, 0, ""
    if not force and LAST_UPDATE_CHECK_FILE.exists():
        age_h = (time.time() - LAST_UPDATE_CHECK_FILE.stat().st_mtime) / 3600
        if age_h < UPDATE_CHECK_INTERVAL_HOURS:
            # 用上次缓存结果（如果有 stash 的 behind 信息）
            try:
                out = subprocess.check_output(
                    ["git", "-C", str(SCRIPT_DIR), "rev-list", "--count", "HEAD..@{u}"],
                    timeout=5, text=True, stderr=subprocess.DEVNULL,
                ).strip()
                behind = int(out)
                if behind > 0:
                    log = subprocess.check_output(
                        ["git", "-C", str(SCRIPT_DIR), "log", "--oneline", "HEAD..@{u}", "-3"],
                        timeout=5, text=True, stderr=subprocess.DEVNULL,
                    ).strip()
                    return True, behind, log
            except Exception:
                pass
            return False, 0, ""
    try:
        subprocess.call(
            ["git", "-C", str(SCRIPT_DIR), "fetch", "--quiet", "origin"],
            timeout=15, stderr=subprocess.DEVNULL,
        )
        out = subprocess.check_output(
            ["git", "-C", str(SCRIPT_DIR), "rev-list", "--count", "HEAD..@{u}"],
            timeout=5, text=True, stderr=subprocess.DEVNULL,
        ).strip()
        behind = int(out)
        try:
            LAST_UPDATE_CHECK_FILE.touch()
        except Exception:
            pass
        if behind <= 0:
            return False, 0, ""
        log = subprocess.check_output(
            ["git", "-C", str(SCRIPT_DIR), "log", "--oneline", "HEAD..@{u}", "-3"],
            timeout=5, text=True, stderr=subprocess.DEVNULL,
        ).strip()
        return True, behind, log
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
    print("  [7] 切换 Pixiv 账号（清除登录状态）")
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
    from pixiv.support import PIXIV_PROFILE_DIR
    shutil.rmtree(PIXIV_PROFILE_DIR, ignore_errors=True)
    print(f"已清除 Pixiv 登录状态（{PIXIV_PROFILE_DIR}）。")
    print("下次上传时会重新打开登录页。")


def cmd_check_update() -> None:
    global _update_banner
    print("正在检查更新...")
    has, n, info = _check_updates(force=True)
    if not has:
        print("已是最新版本。")
        _update_banner = ""
        return
    print(f"\n[!] 有 {n} 个新提交：")
    print(info)
    print()
    ans = input("现在拉取更新？[Y/n] ").strip().lower()
    if ans in ("", "y", "yes"):
        if _do_pull():
            print("\n更新完成。建议重启此菜单生效。")
            _update_banner = ""
        else:
            print("\n拉取失败。可能本地有未提交改动；请手动跑 `git status` 查看。")
    else:
        print("\n取消。下次启动还会提示。")


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
        "7": ("切换 Pixiv 账号", cmd_pixiv_logout),
    }
    while True:
        header()
        try:
            choice = input("  请选择 [1-7, Q]: ").strip().lower()
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
