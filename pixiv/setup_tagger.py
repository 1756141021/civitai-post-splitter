"""cl_tagger (WD14) 配置向导。"""
from __future__ import annotations

import io
import json
import os
import sys
from pathlib import Path

# 强制 UTF-8 输出，避免 Windows GBK 控制台乱码
if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "buffer"):
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

SCRIPT_DIR = Path(__file__).parent.resolve()
PROJECT_DIR = SCRIPT_DIR.parent
CONFIG_PATH = PROJECT_DIR / "config.json"
APPDATA_DIR = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
HAINTAG_SETTINGS_PATH = APPDATA_DIR / "HainTag" / "settings.json"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

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
        if ans in ("y", "yes"):
            return True
        if ans in ("n", "no"):
            return False


def _read_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _write_config(cfg: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def _read_haintag_settings() -> dict:
    if HAINTAG_SETTINGS_PATH.exists():
        try:
            payload = json.loads(HAINTAG_SETTINGS_PATH.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                return payload.get("settings", payload)
        except Exception:
            pass
    return {}


def _write_haintag_settings(settings: dict) -> None:
    HAINTAG_SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    existing = {}
    if HAINTAG_SETTINGS_PATH.exists():
        try:
            existing = json.loads(HAINTAG_SETTINGS_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    if isinstance(existing, dict) and "settings" in existing:
        existing["settings"].update(settings)
    elif isinstance(existing, dict):
        existing.update(settings)
    else:
        existing = settings
    HAINTAG_SETTINGS_PATH.write_text(
        json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _scan_model_dir(path: str) -> tuple[str | None, str | None]:
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
    if model_file and mapping_file:
        return model_file, mapping_file
    return None, None


def _check_haintag_root(root: Path) -> tuple[bool, str]:
    if not root.exists():
        return False, "directory does not exist"
    source_ok = (root / "native_app" / "tagger.py").exists()
    dist_ok   = (root / "_internal" / "native_app" / "tagger_subprocess.py").exists()
    if not source_ok and not dist_ok:
        return False, f"HainTag not found in {root}"
    return True, "ok"


# ---------------------------------------------------------------------------
# steps
# ---------------------------------------------------------------------------

def step1_haintag_root() -> Path | None:
    print()
    hr()
    print("Step 1: haintag project root  (optional)")
    hr()
    print()
    print("  haintag gives the tagger access to ComfyUI/A1111 prompt metadata")
    print("  AND runs WD14 inference via its own TaggerEngine.")
    print("  Without it the tool falls back to a standalone onnxruntime path.")
    print()
    print("  Default location: " + str(PROJECT_DIR.parent / "haintag"))
    print()

    cfg = _read_config()
    current = cfg.get("haintag_root", "")
    if current:
        print(f"  config.json has: {current}")
    else:
        print("  config.json: not set (will try ../haintag)")
    print()

    default_root = Path(current) if current else PROJECT_DIR.parent / "haintag"
    ok, reason = _check_haintag_root(default_root)

    if ok:
        print(f"  [OK] {default_root}")
        print()
        if ask("  Use this path?"):
            return default_root
    else:
        print(f"  [MISS] {default_root}: {reason}")
        print()
        print("  If you don't have haintag, the standalone tagger (onnxruntime)")
        print("  will be used instead — skip this step by pressing Enter.")
        print()

    raw = input("  Enter haintag root path (Enter to skip): ").strip().strip('"')
    if not raw:
        print()
        print("  No haintag path set. Standalone tagger path will be used.")
        return None

    candidate = Path(raw)
    ok, reason = _check_haintag_root(candidate)
    if not ok:
        print(f"  [FAIL] {candidate}: {reason}")
        print("  Skipping. Re-run setup_tagger later once haintag is cloned.")
        return None

    print(f"  [OK] {candidate}")
    cfg["haintag_root"] = str(candidate)
    _write_config(cfg)
    print(f"  Saved to {CONFIG_PATH}")
    return candidate


def step1b_python_path(haintag_root: Path) -> None:
    """
    配置 tagger_python_path —— haintag 的 venv Python。
    TaggerEngine 支持 subprocess 模式：用独立 Python 跑 inference，
    这样主环境不需要装 onnxruntime。
    """
    print()
    hr()
    print("Step 1b: haintag Python (subprocess mode)")
    hr()
    print()

    ht_settings = _read_haintag_settings()
    current_py = ht_settings.get("tagger_python_path", "")

    # Auto-detect haintag venv
    venv_py = haintag_root / ".venv" / "Scripts" / "python.exe"
    venv_py_unix = haintag_root / ".venv" / "bin" / "python"

    auto = None
    if venv_py.exists():
        auto = str(venv_py)
    elif venv_py_unix.exists():
        auto = str(venv_py_unix)

    print("  TaggerEngine can run inference in a subprocess using haintag's")
    print("  own venv Python. This avoids needing onnxruntime in the main env.")
    print()

    if current_py:
        print(f"  Current tagger_python_path: {current_py}")
        if Path(current_py).exists():
            print("  [OK] Python executable found.")
            print()
            if ask("  Keep this setting?"):
                return
        else:
            print("  [MISS] Executable not found at that path.")
        print()

    if auto:
        print(f"  Detected haintag venv: {auto}")
        print()
        if ask("  Use this Python for tagger subprocess?"):
            _write_haintag_settings({"tagger_python_path": auto})
            print(f"  Saved tagger_python_path to {HAINTAG_SETTINGS_PATH}")
            return

    print()
    print("  Enter path to a Python executable that has onnxruntime installed.")
    print("  (e.g. haintag\\.venv\\Scripts\\python.exe)")
    print("  Leave empty to rely on direct import in the current environment.")
    print()
    raw = input("  Python path (Enter to skip): ").strip().strip('"')
    if not raw:
        print("  Skipped. Will try direct onnxruntime import in current env.")
        return

    if not Path(raw).exists():
        print(f"  [FAIL] File not found: {raw}")
        print("  Skipped.")
        return

    _write_haintag_settings({"tagger_python_path": raw})
    print(f"  Saved tagger_python_path to {HAINTAG_SETTINGS_PATH}")


def step2_model_dir() -> str | None:
    print()
    hr()
    print("Step 2: Tagger model directory")
    hr()
    print()
    print("  The directory must contain:")
    print("    - a .onnx model file  (e.g. cl_tagger_1_02.onnx)")
    print("    - a .json tag mapping file  (e.g. *tag*mapping*.json or *labels*.json)")
    print()
    print("  If you use ComfyUI, the model is usually at:")
    print("    <ComfyUI>\\models\\onnx\\cl_tagger\\")
    print()

    ht_settings = _read_haintag_settings()
    current_dir = ht_settings.get("tagger_model_dir", "")

    if current_dir:
        print(f"  Current value: {current_dir}")
        model, mapping = _scan_model_dir(current_dir)
        if model and mapping:
            print(f"  [OK] Found: {os.path.basename(model)} + {os.path.basename(mapping)}")
            print()
            if ask("  Keep this path?"):
                return current_dir
        else:
            print("  [MISS] Directory does not contain the required files.")
        print()
    else:
        print("  No model directory configured yet.")
        print()

    raw = input("  Enter model directory path (or leave empty to skip): ").strip().strip('"')
    if not raw:
        print()
        print("  Skipping model dir. Tagger will fall back to prompt-only tags.")
        return None

    model, mapping = _scan_model_dir(raw)
    if not model:
        print(f"  [FAIL] No .onnx file found in: {raw}")
        return None
    if not mapping:
        print(f"  [FAIL] No tag mapping .json found in: {raw}")
        print("  Expected a file with 'tag', 'mapping', or 'label' in its name.")
        return None

    print(f"  [OK] {os.path.basename(model)} + {os.path.basename(mapping)}")
    _write_haintag_settings({"tagger_model_dir": raw})
    print(f"  Saved tagger_model_dir to {HAINTAG_SETTINGS_PATH}")
    return raw


def _scan_pixai_dir(path: str) -> tuple[bool, bool]:
    """Return (has_model, has_tags) for a PixAI model directory."""
    if not path or not os.path.isdir(path):
        return False, False
    has_model = os.path.isfile(os.path.join(path, "model.onnx"))
    has_tags = os.path.isfile(os.path.join(path, "selected_tags.csv"))
    return has_model, has_tags


def step2_pixai_model_dir() -> str | None:
    print()
    hr()
    print("Step 2-A: PixAI Tagger v0.9 model directory")
    hr()
    print()
    print("  PixAI tagger has broader character coverage than CL/WD14,")
    print("  including newer characters trained on Danbooru data up to v0.9.")
    print()
    print("  The directory must contain:")
    print("    - model.onnx          (~1.27 GB)")
    print("    - selected_tags.csv   (~597 KB)")
    print("    - preprocess.json     (optional, auto-detected)")
    print("    - thresholds.csv      (optional, auto-detected)")
    print()
    print("  Download from:")
    print("    https://huggingface.co/deepghs/pixai-tagger-v0.9-onnx")
    print()

    ht_settings = _read_haintag_settings()
    current_dir = ht_settings.get("pixai_tagger_model_dir", "")

    if current_dir:
        print(f"  Current value: {current_dir}")
        has_model, has_tags = _scan_pixai_dir(current_dir)
        if has_model and has_tags:
            print("  [OK] model.onnx + selected_tags.csv found")
            print()
            if ask("  Keep this path?"):
                return current_dir
        else:
            missing = []
            if not has_model:
                missing.append("model.onnx")
            if not has_tags:
                missing.append("selected_tags.csv")
            print(f"  [MISS] Missing: {', '.join(missing)}")
        print()
    else:
        print("  No PixAI model directory configured yet.")
        print()

    raw = input("  Enter PixAI model directory path (or leave empty to skip): ").strip().strip('"')
    if not raw:
        print()
        print("  Skipping PixAI. Will fall back to CL tagger if configured.")
        return None

    has_model, has_tags = _scan_pixai_dir(raw)
    if not has_model:
        print(f"  [FAIL] model.onnx not found in: {raw}")
        return None
    if not has_tags:
        print(f"  [FAIL] selected_tags.csv not found in: {raw}")
        return None

    print("  [OK] model.onnx + selected_tags.csv found")
    _write_haintag_settings({"pixai_tagger_model_dir": raw})
    print(f"  Saved pixai_tagger_model_dir to {HAINTAG_SETTINGS_PATH}")
    return raw


def step3_pixai_verify(model_dir: str) -> None:
    print()
    hr()
    print("Step 3-A: Verify PixAI tagger")
    hr()
    print()

    if not ask("  Run a quick load test now?", default="y"):
        print()
        print("  Skipped. Run 'upload' to see tagger status in the manifest.")
        return

    if not _check_onnxruntime():
        print("  [MISS] onnxruntime not installed.")
        if ask("  Install now?", default="y"):
            if not _install_onnxruntime():
                print("  [FAIL] Installation failed. Run manually:")
                print("    pip install onnxruntime>=1.16.0")
                return
        else:
            print("  Skipped. Install onnxruntime to enable PixAI tagger.")
            return

    sys.path.insert(0, str(PROJECT_DIR))
    try:
        from pixiv.pixai_tagger import PixAITaggerBridge
        bridge = PixAITaggerBridge(Path(model_dir))
        ok = bridge._ensure_loaded()
        if not ok:
            print(f"  [FAIL] {bridge._status}")
            return
        print(f"  [OK] {len(bridge._tags)} tags loaded, model ready")
        print()
        print("  PixAI tagger is fully configured and working.")
    except Exception as exc:
        print(f"  [FAIL] {type(exc).__name__}: {exc}")


def _check_onnxruntime() -> bool:
    try:
        import onnxruntime  # noqa: F401
        return True
    except ImportError:
        return False


def _install_onnxruntime() -> bool:
    import subprocess
    print()
    print("  Installing onnxruntime ...")
    ret = subprocess.call([sys.executable, "-m", "pip", "install", "onnxruntime>=1.16.0"])
    return ret == 0


def step3_verify(haintag_root: Path | None, model_dir: str | None) -> None:
    print()
    hr()
    print("Step 3: Verify")
    hr()
    print()

    if model_dir is None:
        print("  Skipped (no model directory configured).")
        return

    if not ask("  Run a quick test now?", default="y"):
        print()
        print("  Skipped. Run 'upload' to see tagger status in the manifest.")
        return

    # --- path A: haintag available, use HainTagTaggerBridge ---
    if haintag_root is not None:
        print()
        print("  Path: haintag (HainTagTaggerBridge)")
        root_str = str(haintag_root)
        if root_str not in sys.path:
            sys.path.insert(0, root_str)
        try:
            import importlib
            module = importlib.import_module("native_app.tagger")
            engine_cls = getattr(module, "TaggerEngine", None)
            if engine_cls is None:
                print("  [FAIL] TaggerEngine class not found in native_app.tagger")
                return
            print("  [OK] TaggerEngine imported")
        except Exception as exc:
            print(f"  [FAIL] Import error: {type(exc).__name__}: {exc}")
            print("  Check haintag deps: cd " + root_str + " && pip install -r requirements.txt")
            return

        try:
            engine = engine_cls(model_dir=model_dir)
            appdata_dir = str(APPDATA_DIR / "HainTag")
            model_path, mapping_path = engine.find_model(custom_dir=model_dir, appdata_dir=appdata_dir)
            if not model_path or not mapping_path:
                model_path, mapping_path = _scan_model_dir(model_dir)
            if not model_path or not mapping_path:
                print("  [FAIL] Cannot locate model/mapping files")
                return
            print(f"  [OK] {os.path.basename(model_path)} + {os.path.basename(mapping_path)}")
            engine.load(model_path, mapping_path)
            if not getattr(engine, "is_ready", False):
                print("  [FAIL] Engine loaded but is_ready=False")
                return
            print("  [OK] Engine ready")
        except Exception as exc:
            print(f"  [FAIL] {type(exc).__name__}: {exc}")
            return

    # --- path B: no haintag, use StandaloneTaggerBridge (onnxruntime) ---
    else:
        print()
        print("  Path: standalone (StandaloneTaggerBridge via onnxruntime)")
        if not _check_onnxruntime():
            print("  [MISS] onnxruntime not installed.")
            if ask("  Install now?", default="y"):
                if not _install_onnxruntime():
                    print("  [FAIL] Installation failed. Run manually:")
                    print("    pip install onnxruntime>=1.16.0")
                    return
            else:
                print("  Skipped. Install onnxruntime to enable standalone tagger.")
                return

        sys.path.insert(0, str(PROJECT_DIR))
        try:
            from pixiv.standalone import StandaloneTaggerBridge
            bridge = StandaloneTaggerBridge()
            ok = bridge._ensure_loaded()
            if not ok:
                print(f"  [FAIL] {bridge._status}")
                return
            print(f"  [OK] {len(bridge._tags)} tags loaded, model ready")
        except Exception as exc:
            print(f"  [FAIL] {type(exc).__name__}: {exc}")
            return

    print()
    print("  Tagger is fully configured and working.")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> int:
    print()
    print(" " + "=" * 46)
    print("  Tagger Setup Wizard")
    print(" " + "=" * 46)
    print()
    print("  Priority: PixAI Tagger > CL/WD14 Tagger")
    print()
    print("  Configure which tagger to use for image tagging.")
    print("  You can skip any step; uploads will still work.")
    print("  Re-run anytime from launcher menu.")
    print()

    print("  Which tagger do you want to configure?")
    print("    [1] PixAI Tagger v0.9  (recommended — broader character coverage)")
    print("    [2] CL Tagger / WD14   (lighter, fallback)")
    print("    [3] Both")
    print()
    while True:
        try:
            choice = input("  Choice [1/2/3]: ").strip()
        except EOFError:
            choice = "1"
        if choice in ("1", "2", "3"):
            break
        print("  Please enter 1, 2, or 3.")

    do_pixai = choice in ("1", "3")
    do_cl = choice in ("2", "3")

    pixai_dir: str | None = None
    cl_dir: str | None = None

    if do_pixai:
        pixai_dir = step2_pixai_model_dir()
        if pixai_dir:
            step3_pixai_verify(pixai_dir)

    if do_cl:
        haintag_root = step1_haintag_root()
        if haintag_root is not None:
            step1b_python_path(haintag_root)
        else:
            haintag_root = None
        cl_dir = step2_model_dir()
        step3_verify(haintag_root, cl_dir)

    print()
    hr()
    print("  Setup complete.")
    print()
    ht_settings = _read_haintag_settings()
    effective_pixai = ht_settings.get("pixai_tagger_model_dir", "")
    effective_cl = ht_settings.get("tagger_model_dir", "")
    if effective_pixai and os.path.isfile(os.path.join(effective_pixai, "model.onnx")):
        print(f"  Active tagger : PixAI ({effective_pixai})")
    elif effective_cl:
        print(f"  Active tagger : CL/WD14 ({effective_cl})")
    else:
        print("  Active tagger : none configured")
        print("  Uploads will still work — tagger just won't enrich tag candidates.")
    hr()
    print()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print()
        sys.exit(0)
