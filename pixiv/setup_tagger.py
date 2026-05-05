"""cl_tagger (WD14) setup wizard.

Guides new users through:
  1. Setting the haintag project root (saved to config.json["haintag_root"])
  2. Setting the tagger model directory (saved to %APPDATA%\\HainTag\\settings.json)
  3. Verifying the configuration works

Windows console may be GBK, so all output uses ASCII-safe characters only.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

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
        elif fl.endswith(".json") and ("tag" in fl or "mapping" in fl or "label" in fl) and not mapping_file:
            mapping_file = os.path.join(path, f)
    if model_file and mapping_file:
        return model_file, mapping_file
    return None, None


def _check_haintag_root(root: Path) -> tuple[bool, str]:
    if not root.exists():
        return False, "directory does not exist"
    tagger_mod = root / "native_app" / "tagger.py"
    if not tagger_mod.exists():
        return False, f"native_app/tagger.py not found in {root}"
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
    print("  cl_tagger (WD14) Setup Wizard")
    print(" " + "=" * 46)
    print()
    print("  This wizard configures the image tagger used to")
    print("  generate richer Pixiv tags from image content.")
    print()
    print("  You can skip any step; uploads will still work.")
    print("  Re-run anytime from launcher menu [6].")

    haintag_root = step1_haintag_root()
    if haintag_root is not None:
        step1b_python_path(haintag_root)
    model_dir = step2_model_dir()
    step3_verify(haintag_root, model_dir)

    print()
    hr()
    print("  Setup complete.")
    if model_dir:
        if haintag_root:
            print("  Path: haintag (TaggerEngine). Next upload will use WD14 tags.")
        else:
            print("  Path: standalone (onnxruntime). Next upload will use WD14 tags.")
    else:
        print("  Model directory not configured.")
        print("  Re-run setup and set a directory containing .onnx + tag mapping json.")
        print()
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
