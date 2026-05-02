"""自动打码 (R-18 mosaic) 安装向导。

跑一次，引导用户完成：
  1. 安装 ultralytics + opencv-python
  2. 从 Civitai 下载 YOLO 模型到 models/auto_censor.pt
  3. 验证模型加载

不依赖项目里其它模块，可独立运行。Windows 控制台默认 GBK，所以
所有 print 走 ASCII / 简单 Unicode（不用 emoji）。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent.resolve()
MODEL_DIR = SCRIPT_DIR / "models"
MODEL_PATH = MODEL_DIR / "auto_censor.pt"
CIVITAI_MODEL_VERSION_ID = "1965032"
CIVITAI_DOWNLOAD_URL = f"https://civitai.com/api/download/models/{CIVITAI_MODEL_VERSION_ID}"
CIVITAI_PAGE_URL = "https://civitai.com/models/1736285?modelVersionId=1965032"
REQUIRED_PACKAGES = ["ultralytics", "opencv-python"]


def hr():
    print("-" * 56)


def ask(prompt: str, default: str = "y") -> bool:
    suffix = " [Y/n] " if default.lower() == "y" else " [y/N] "
    while True:
        try:
            ans = input(prompt + suffix).strip().lower()
        except EOFError:
            return default.lower() == "y"
        if not ans:
            return default.lower() == "y"
        if ans in ("y", "yes", "是"):
            return True
        if ans in ("n", "no", "否"):
            return False


def check_packages() -> tuple[list[str], list[str]]:
    """Return (installed, missing)."""
    installed = []
    missing = []
    for pkg in REQUIRED_PACKAGES:
        mod = pkg.replace("-", "_")
        # opencv-python 的 import 名是 cv2
        if pkg == "opencv-python":
            mod = "cv2"
        try:
            __import__(mod)
            installed.append(pkg)
        except ImportError:
            missing.append(pkg)
    return installed, missing


def install_packages(packages: list[str]) -> bool:
    print(f"\n开始安装: {', '.join(packages)}")
    print("（首次安装会下 ~1GB pytorch + ultralytics 体积，慢一点正常）\n")
    cmd = [sys.executable, "-m", "pip", "install"] + packages
    try:
        ret = subprocess.call(cmd)
    except Exception as exc:
        print(f"安装失败: {exc}")
        return False
    return ret == 0


def download_model_civitai(api_key: str | None) -> bool:
    """从 Civitai 下载 YOLO 模型到 MODEL_PATH。需要 API key。"""
    if not api_key:
        print("没有 CIVITAI_API_KEY，无法自动下载。")
        return False
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    url = f"{CIVITAI_DOWNLOAD_URL}?token={api_key}"
    print(f"\n开始下载: {CIVITAI_DOWNLOAD_URL}")
    print(f"目标路径: {MODEL_PATH}")
    print("（模型 ~50MB，进度条不动是正常的，等到底）\n")
    tmp_path = MODEL_PATH.with_suffix(".pt.partial")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "civitai-post-splitter-setup/1.0"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            total = resp.headers.get("Content-Length")
            total_mb = f"{int(total) / 1024 / 1024:.1f}MB" if total else "未知大小"
            print(f"  服务端报告大小: {total_mb}")
            with open(tmp_path, "wb") as f:
                shutil.copyfileobj(resp, f, length=1024 * 256)
        # 简单校验：文件大小不为 0 且看起来像 binary
        size = tmp_path.stat().st_size
        if size < 1024:
            print(f"下载失败: 文件大小仅 {size} 字节，疑似返回错误页")
            tmp_path.unlink(missing_ok=True)
            return False
        # Civitai often wraps .pt models in a zip archive. Detect and extract.
        if zipfile.is_zipfile(tmp_path):
            print("\n  检测到 zip 包装，正在解压...")
            try:
                with zipfile.ZipFile(tmp_path) as z:
                    pt_names = [n for n in z.namelist() if n.lower().endswith(".pt")]
                    if not pt_names:
                        print(f"  zip 里没找到 .pt 文件，只有: {z.namelist()}")
                        tmp_path.unlink(missing_ok=True)
                        return False
                    target = pt_names[0]
                    if len(pt_names) > 1:
                        print(f"  zip 里有多个 .pt，取第一个: {target}")
                    with z.open(target) as src:
                        data = src.read()
                MODEL_PATH.write_bytes(data)
                tmp_path.unlink(missing_ok=True)
                print(f"  解压完成 ({len(data) / 1024 / 1024:.1f} MB, 来自 {target})")
                return True
            except Exception as exc:
                print(f"  解压失败: {type(exc).__name__}: {exc}")
                tmp_path.unlink(missing_ok=True)
                return False
        # Not a zip — treat as raw .pt
        tmp_path.replace(MODEL_PATH)
        print(f"\n下载完成 ({size / 1024 / 1024:.1f} MB)。")
        return True
    except Exception as exc:
        print(f"下载失败: {type(exc).__name__}: {exc}")
        tmp_path.unlink(missing_ok=True)
        return False


def verify_model() -> bool:
    print(f"\n验证模型加载 ({MODEL_PATH.name}) ...")
    # PyTorch .pt files are themselves zip archives, so is_zipfile() is true on
    # any valid model. Only treat the file as a Civitai wrapper when there's a
    # top-level *.pt entry inside (real model would have data.pkl etc., no .pt).
    if MODEL_PATH.exists() and zipfile.is_zipfile(MODEL_PATH):
        try:
            with zipfile.ZipFile(MODEL_PATH) as z:
                top_pts = [n for n in z.namelist()
                           if n.lower().endswith(".pt") and "/" not in n.strip("/")]
            if top_pts:
                print(f"  检测到 Civitai wrapper zip，正在解压 {top_pts[0]} ...")
                with zipfile.ZipFile(MODEL_PATH) as z, z.open(top_pts[0]) as src:
                    data = src.read()
                MODEL_PATH.write_bytes(data)
                print(f"  解压完成 ({len(data) / 1024 / 1024:.1f} MB)")
        except Exception as exc:
            print(f"  解压检测失败: {type(exc).__name__}: {exc}")
            return False
    try:
        from ultralytics import YOLO
    except ImportError:
        print("  ultralytics 未安装，跳过验证")
        return False
    try:
        model = YOLO(str(MODEL_PATH))
        names = getattr(model, "names", None)
        if names:
            print(f"  模型类别: {names}")
        print("  加载成功")
        return True
    except Exception as exc:
        print(f"  加载失败: {type(exc).__name__}: {exc}")
        return False


def main() -> int:
    print("=" * 56)
    print("Pixiv 自动打码 安装向导")
    print("=" * 56)

    # Step 1: 依赖
    hr()
    print("[1/3] 检查 Python 依赖")
    installed, missing = check_packages()
    if installed:
        print("  已装: " + ", ".join(installed))
    if missing:
        print("  缺失: " + ", ".join(missing))
        if ask("\n是否现在安装缺失的包？", default="y"):
            ok = install_packages(missing)
            if not ok:
                print("\n安装失败。请手动跑：\n  " + " ".join([sys.executable, "-m", "pip", "install"] + missing))
                return 1
            # 重新检查
            installed, missing = check_packages()
            if missing:
                print(f"\n安装后仍缺失: {missing}。请手动处理。")
                return 1
        else:
            print("\n跳过依赖安装。无依赖时打码不会启用。")
            return 1
    else:
        print("  全部已装")

    # Step 2: 模型文件
    hr()
    print("[2/3] 检查 YOLO 模型文件")
    if MODEL_PATH.exists():
        size_mb = MODEL_PATH.stat().st_size / 1024 / 1024
        print(f"  已存在: {MODEL_PATH} ({size_mb:.1f} MB)")
        if not ask("\n要重新下载吗？", default="n"):
            pass
        else:
            api_key = os.environ.get("CIVITAI_API_KEY")
            if not api_key:
                api_key = input("\n输入 CIVITAI_API_KEY: ").strip()
            if not download_model_civitai(api_key):
                return 1
    else:
        print(f"  未找到: {MODEL_PATH}")
        print(f"\n  模型主页: {CIVITAI_PAGE_URL}")
        print(f"  目标路径: {MODEL_PATH}")
        api_key = os.environ.get("CIVITAI_API_KEY")
        if api_key:
            print(f"\n  环境变量 CIVITAI_API_KEY 已设置，可自动下载。")
            if ask("自动下载？", default="y"):
                if not download_model_civitai(api_key):
                    print("\n自动下载失败。请手动从下面这个 URL 下载，重命名为 auto_censor.pt 放到 models/ 目录：")
                    print(f"  {CIVITAI_PAGE_URL}")
                    return 1
            else:
                print(f"\n请手动下载放到: {MODEL_PATH}")
                return 1
        else:
            print("\n未设置 CIVITAI_API_KEY 环境变量。两种方式：")
            print("  A) 浏览器打开主页下载，重命名为 auto_censor.pt 放到 models/ 目录:")
            print(f"       {CIVITAI_PAGE_URL}")
            print("  B) 设置 CIVITAI_API_KEY 环境变量后重跑此脚本自动下载。")
            print("       Civitai → 个人设置 → API Keys 创建一个。")
            entered = input("\n或现在直接输入 API key 自动下载（留空跳过）: ").strip()
            if entered:
                if not download_model_civitai(entered):
                    return 1
            else:
                return 1

    # Step 3: 验证
    hr()
    print("[3/3] 验证")
    if verify_model():
        hr()
        print("\n安装成功！下次跑 upload.bat / upload_pixiv_only.bat 时会自动启用打码。")
        print(f"\n模型路径: {MODEL_PATH}")
        print("默认打码类别: dick, pussy, cum, anus（不打 breasts）")
        print("不想用了？删掉模型文件即可，脚本不会阻断上传。")
        return 0
    else:
        print("\n模型验证失败。可能原因：")
        print("  - 文件损坏（重新下载）")
        print("  - ultralytics 版本不兼容（pip install -U ultralytics）")
        return 1


if __name__ == "__main__":
    sys.exit(main())
