"""Interactive launcher menu. Single entry point for all functions."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent.resolve()
DEFAULT_API_KEY = ""  # 用户可以填自己的；github 发布版留空


def header():
    print()
    print(" " + "=" * 46)
    print("  Civitai Post Splitter & Pixiv Uploader")
    print(" " + "=" * 46)
    print()
    print("  [1] 拆分 Civitai 帖子（一帖多图 -> 多帖单图）")
    print("  [2] 上传到双端 (Civitai + Pixiv)")
    print("  [3] 仅上传到 Pixiv")
    print("  [4] 安装 / 检查 R-18 自动打码")
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


def cmd_upload_dual() -> None:
    run(["civitai_splitter.py", "upload", "--targets", "civitai,pixiv"])


def cmd_upload_pixiv() -> None:
    run(["civitai_splitter.py", "upload", "--targets", "pixiv"])


def cmd_setup_censor() -> None:
    run(["setup_censor.py"])


def main() -> int:
    handlers = {
        "1": ("拆分 Civitai 帖子", cmd_split),
        "2": ("上传到双端", cmd_upload_dual),
        "3": ("仅上传到 Pixiv", cmd_upload_pixiv),
        "4": ("安装 / 检查打码", cmd_setup_censor),
    }
    while True:
        header()
        try:
            choice = input("  请选择 [1-4, Q]: ").strip().lower()
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
