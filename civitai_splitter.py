from __future__ import annotations

import argparse
import json
import logging
import os
import random
import re
import shutil
import sys
import time
import traceback
from datetime import datetime, timedelta
from pathlib import Path

import httpx
from PIL import Image, PngImagePlugin
from patchright.sync_api import sync_playwright

from pixiv.censor import CensorEngine, DEFAULT_CENSOR_CLASSES, parse_class_set
from pixiv.llm_reverse import (
    account_can_handle_age,
    apply_llm_result_to_copy_block,
    apply_llm_result_to_pixiv_payload,
    content_mode_can_handle_age,
    default_llm_reverse_config,
    empty_copy_block,
    infer_image_copy,
    normalize_llm_reverse_config,
    resolve_account,
    resolve_persona,
)

# Per-platform behaviour table. Drives:
#   needs_sanitize: PIL-reencode to strip metadata (PNG text chunks, EXIF)
#   needs_censor:   run auto_censor model on the sanitized image
#   needs_copy:     consumes LLM-reversed title/caption (triggers LLM call)
#   max_age:        highest age_restriction this platform will accept
#                   ("all_ages" means NSFW gets silently dropped from targets)
_NSFW_TIER_PLAT = {"all_ages": 0, "sfw": 0, "r18": 1, "r18g": 2}
PLATFORM_RULES: dict[str, dict] = {
    "civitai": {"needs_sanitize": False, "needs_censor": False, "needs_copy": False, "max_age": "r18g"},
    "pixiv":   {"needs_sanitize": True,  "needs_censor": True,  "needs_copy": True,  "max_age": "r18g"},
    "x":       {"needs_sanitize": True,  "needs_censor": True,  "needs_copy": True,  "max_age": "r18g"},
    "xhs":     {"needs_sanitize": True,  "needs_censor": True,  "needs_copy": True,  "max_age": "all_ages"},
}


def _targets_need_copy(targets) -> bool:
    return any(PLATFORM_RULES.get(t, {}).get("needs_copy") for t in targets)


def _build_llm_extra_context(
    pixiv_payload: dict | None,
    source_meta: dict | None = None,
) -> str:
    if not pixiv_payload and not source_meta:
        return ""
    parts: list[str] = []
    if pixiv_payload:
        domain = str(pixiv_payload.get("domain", "") or "").strip()
        if domain:
            parts.append(f"domain={domain}")
        entity_tags = pixiv_payload.get("entity_tags") or []
        if entity_tags:
            parts.append("entity tags: " + ", ".join(str(t) for t in entity_tags[:10]))
        hits = pixiv_payload.get("metadata_entity_hits") or []
        if hits:
            hit_names = [str(h.get("name") or h) for h in hits[:5] if h]
            if hit_names:
                parts.append("metadata entities: " + ", ".join(hit_names))
    if source_meta:
        metadata = source_meta.get("metadata")
        if metadata:
            raw_prompt = re.sub(r",\s*,", ",", _LORA_RE.sub("", getattr(metadata, "positive_prompt", "") or "")).strip().strip(",")
            if raw_prompt:
                parts.append(
                    "generation prompt (identify characters from this): " + raw_prompt
                )
    return "; ".join(parts)


def _platform_accepts_age(platform: str, image_age: str) -> bool:
    """Whether `platform` accepts an image at `image_age`. Hard rule, no override."""
    rule = PLATFORM_RULES.get(platform, {})
    max_age = rule.get("max_age", "r18g")
    return _NSFW_TIER_PLAT.get(image_age, 0) <= _NSFW_TIER_PLAT.get(max_age, 2)
from pixiv.standalone import StandaloneMetadataReader, StandaloneTaggerBridge
from pixiv.pixai_tagger import PixAITaggerBridge
from pixiv.support import (
    HainTagBridge,
    HainTagTaggerBridge,
    append_validation_case,
    build_pixiv_payload,
    collect_artwork_urls_from_source,
    collect_rule_fit_sample_manifests,
    compare_rule_fit_samples,
    create_manifest_path,
    create_rule_fit_report_path,
    create_pixiv_post,
    ensure_pixiv_logged_in,
    extract_artwork_id,
    fetch_pixiv_illust_data,
    find_target_successes,
    force_pixiv_age_restriction,
    infer_age_restriction,
    open_pixiv_browser,
    PIXIV_RULE_FIT_PROFILE_DIR,
    summarize_rule_fit_report,
    ensure_runtime_files,
    load_json,
    sanitize_image_for_pixiv,
    save_json,
    write_manifest,
    PIXIV_BASE,
)

SCRIPT_DIR = Path(__file__).parent.resolve()
UPLOAD_DIR = SCRIPT_DIR / "upload"
XHS_UPLOAD_DIR = SCRIPT_DIR / "xhs_upload"
DONE_DIR = SCRIPT_DIR / "done"
LOG_DIR = SCRIPT_DIR / "logs"
PROGRESS_DIR = SCRIPT_DIR / "progress"
TMP_DIR = SCRIPT_DIR / ".tmp"
CHROME_PROFILE_DIR = Path.home() / ".civitai_splitter_chrome"
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
CIVITAI_BASE = "https://civitai.red"
CIVITAI_API = "https://civitai.red/api/v1"
DONE_DAYS = 7
_LORA_RE = re.compile(r"<lora:([^:>]+):([^>]+)>")
TARGETS = {"civitai", "pixiv", "x", "xhs"}


def _raise_if_canceled(cancel_event) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise InterruptedError("task canceled")


def _sleep_with_cancel(seconds: float, cancel_event, poll: float = 0.2) -> None:
    deadline = time.monotonic() + max(0.0, seconds)
    while True:
        _raise_if_canceled(cancel_event)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(poll, remaining))


def check_civitai_safety(
    image_path: Path,
    source_meta: dict,
    age_rules: dict,
    safety_cfg: dict,
) -> tuple[bool, str]:
    rating = infer_age_restriction(image_path, age_rules)
    unsafe = {r.lower() for r in safety_cfg.get("unsafe_ratings", ["r18", "r18g"])}
    if rating.lower() not in unsafe:
        return False, ""

    tokens: set[str] = set()
    phrases: set[str] = set()
    stem = image_path.stem.lower().replace("_", " ")
    phrases.add(stem)
    for part in re.split(r"[\s_\-\[\](){}|]+", image_path.stem.lower()):
        tok = part.strip()
        if tok:
            tokens.add(tok)
    metadata = source_meta.get("metadata")
    if metadata:
        prompt = _LORA_RE.sub("", getattr(metadata, "positive_prompt", "") or "")
        for part in re.split(r"[,|\n]+", prompt.lower()):
            tok = re.sub(r":\s*[\d.]+$", "", part.strip().replace("_", " ")).strip()
            if tok:
                tokens.add(tok)
                phrases.add(tok)

    haystack = "\n".join(phrases)
    minor_tags = {t.lower().replace("_", " ") for t in safety_cfg.get("minor_tags", [])}
    school_tags = {t.lower().replace("_", " ") for t in safety_cfg.get("school_tags", [])}
    hit_minor = {tag for tag in minor_tags if tag in tokens or tag in haystack}
    hit_school = {tag for tag in school_tags if tag in tokens or tag in haystack}
    if hit_minor:
        return True, f"rating={rating}, loli/minor tags: {sorted(hit_minor)}"
    if hit_school:
        return True, f"rating={rating}, school tags: {sorted(hit_school)}"
    return False, ""


def load_app_config() -> dict:
    cfg_file = SCRIPT_DIR / "config.json"
    if cfg_file.exists():
        try:
            payload = json.loads(cfg_file.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                return payload
        except Exception:
            pass
    return {}


def load_llm_reverse_config() -> dict:
    return normalize_llm_reverse_config(load_app_config().get("llm_reverse") or default_llm_reverse_config())


def _resolve_haintag_root() -> Path:
    override = load_app_config().get("haintag_root", "")
    if override:
        return Path(override)
    return SCRIPT_DIR.parent / "haintag"


def _load_haintag_settings() -> dict:
    appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    cfg = Path(appdata) / "HainTag" / "settings.json"
    if cfg.exists():
        try:
            payload = json.loads(cfg.read_text(encoding="utf-8"))
            s = payload.get("settings", payload) if isinstance(payload, dict) else {}
            return s if isinstance(s, dict) else {}
        except Exception:
            pass
    return {}


def _make_bridges():
    """
    返回 (metadata_bridge, tagger_bridge)。
    metadata reader: haintag 存在时用 HainTagBridge，否则 StandaloneMetadataReader。
    tagger bridge: PixAI → CL(Standalone) → None 优先链，均不可用时返回 None。
    """
    # metadata reader 保持现有逻辑
    root = _resolve_haintag_root()
    if root.exists():
        metadata_reader = HainTagBridge(root)
    else:
        metadata_reader = StandaloneMetadataReader()

    # tagger bridge: PixAI → CL → None (with runtime fallback)
    settings = _load_haintag_settings()
    tagger = None
    pixai_dir = settings.get("pixai_tagger_model_dir", "")
    if pixai_dir and (Path(pixai_dir) / "model.onnx").exists():
        try:
            t = PixAITaggerBridge(Path(pixai_dir))
            sample = Path(pixai_dir) / "sample.webp"
            if sample.exists():
                probe = t.predict_tags(sample)
                if probe.get("available"):
                    tagger = t
                else:
                    log.info(f"PixAI tagger 加载失败 ({probe.get('status')}), 尝试 WD14 回退")
            else:
                tagger = t
        except Exception as exc:
            log.info(f"PixAI tagger 异常 ({exc}), 尝试 WD14 回退")
    if tagger is None and settings.get("tagger_model_dir"):
        tagger = StandaloneTaggerBridge()

    return metadata_reader, tagger


MODEL_HASH_PATCHES = {
    "anima-preview3-base": "14fffe8ad5",
}


def _inject_model_hash(settings_line: str) -> str:
    if not settings_line or "Model hash:" in settings_line:
        return settings_line
    for model_name, hash_value in MODEL_HASH_PATCHES.items():
        target = f"Model: {model_name}"
        if target in settings_line:
            return settings_line.replace(
                target, f"Model hash: {hash_value}, {target}", 1
            )
    return settings_line


def setup_logging():
    LOG_DIR.mkdir(exist_ok=True)
    log_file = LOG_DIR / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"

    logger = logging.getLogger("civitai_splitter")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()

    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(fh)

    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(ch)

    return logger


log = logging.getLogger("civitai_splitter")


def parse_post_id(value: str) -> int:
    match = re.search(r"(\d+)", value)
    if not match:
        raise ValueError(f"无法从 '{value}' 提取 post ID")
    return int(match.group(1))


def fetch_post_images(post_id: int, api_key: str) -> list[dict]:
    headers = {"Authorization": f"Bearer {api_key}"}
    seen = {}

    with httpx.Client(timeout=10, follow_redirects=True) as client:
        for nsfw in ["None", "Soft", "Mature", "X"]:
            url = f"{CIVITAI_API}/images"
            params = {"postId": post_id, "limit": 200, "nsfw": nsfw}
            try:
                while url:
                    resp = client.get(url, headers=headers, params=params)
                    resp.raise_for_status()
                    data = resp.json()
                    for item in data.get("items", []):
                        seen[item["id"]] = item
                    next_page = data.get("metadata", {}).get("nextPage")
                    if next_page:
                        url = next_page
                        params = {}
                    else:
                        break
            except (httpx.HTTPStatusError, httpx.TimeoutException):
                pass

    before = len(seen)
    with httpx.Client(timeout=5, follow_redirects=True) as client2:
        for level in range(1, 32):
            try:
                resp = client2.get(
                    f"{CIVITAI_API}/images",
                    headers=headers,
                    params={"postId": post_id, "limit": 200, "browsingLevel": level},
                )
                if resp.status_code == 200:
                    for item in resp.json().get("items", []):
                        seen[item["id"]] = item
            except (httpx.HTTPStatusError, httpx.TimeoutException):
                pass
    if len(seen) > before:
        log.info(f"  browsingLevel 补扫发现 {len(seen) - before} 张额外图片")

    images = sorted(seen.values(), key=lambda item: item["id"])
    log.info(f"  Post {post_id}: 找到 {len(images)} 张图片")
    return images


def best_image_url(img: dict) -> str:
    url = img["url"]
    width = img.get("width")
    return re.sub(r"/width=\d+/", f"/width={width}/" if width else "/", url)


def build_a1111_params(meta: dict) -> str:
    parts = []

    lora_tags = _LORA_RE.findall(meta.get("prompt", ""))
    if lora_tags:
        parts.append(", ".join(f"<lora:{name}:{weight}>" for name, weight in lora_tags))

    settings = []
    mapping = [
        ("steps", "Steps"), ("sampler", "Sampler"), ("cfgScale", "CFG scale"),
        ("seed", "Seed"), ("Size", "Size"), ("Model", "Model"),
        ("Clip skip", "Clip skip"),
    ]
    for key, label in mapping:
        value = meta.get(key)
        if value is not None:
            settings.append(f"{label}: {value}")

    if settings:
        parts.append(", ".join(settings))
    return "\n".join(parts)


def strip_prompts_keep_lora(image_path: Path, dest_dir: Path) -> Path:
    pil_img = Image.open(image_path)
    old_params = pil_img.info.get("parameters", "")
    if not old_params:
        dest = dest_dir / f"{image_path.stem}.png"
        pnginfo = PngImagePlugin.PngInfo()
        pil_img.save(dest, "PNG", pnginfo=pnginfo)
        return dest

    steps_idx = old_params.rfind("\nSteps:")
    if steps_idx == -1:
        steps_idx = old_params.rfind("Steps:")
    if steps_idx != -1:
        prompt_block = old_params[:steps_idx]
        settings_line = old_params[steps_idx:].strip()
    else:
        prompt_block = old_params
        settings_line = ""

    settings_line = _inject_model_hash(settings_line)

    lora_tags = _LORA_RE.findall(prompt_block)
    parts = []
    if lora_tags:
        parts.append(", ".join(f"<lora:{name}:1>" for name, weight in lora_tags))
    if settings_line:
        parts.append(settings_line)

    new_params = "\n".join(parts)
    pnginfo = PngImagePlugin.PngInfo()
    if new_params:
        pnginfo.add_text("parameters", new_params)

    dest = dest_dir / f"{image_path.stem}.png"
    pil_img.save(dest, "PNG", pnginfo=pnginfo)
    return dest


def download_and_embed_metadata(images: list[dict], api_key: str, dest_dir: Path) -> list[Path]:
    headers = {"Authorization": f"Bearer {api_key}"}
    paths = []

    with httpx.Client(timeout=60, follow_redirects=True) as client:
        for idx, img in enumerate(images):
            url = best_image_url(img)
            log.info(f"  下载 [{idx + 1}/{len(images)}] {img['id']}...")
            resp = client.get(url, headers=headers)
            resp.raise_for_status()

            jpeg_path = dest_dir / f"{img['id']}.jpeg"
            jpeg_path.write_bytes(resp.content)

            meta = img.get("meta") or {}
            if meta:
                png_path = dest_dir / f"{img['id']}.png"
                pil_img = Image.open(jpeg_path)
                pnginfo = PngImagePlugin.PngInfo()
                pnginfo.add_text("parameters", build_a1111_params(meta))
                pil_img.save(png_path, "PNG", pnginfo=pnginfo)
                jpeg_path.unlink()
                paths.append(png_path)
            else:
                paths.append(jpeg_path)
    return paths


LOG_KEEP_PER_GROUP = 100
LOG_KEEP_DAYS = 10


def prune_logs():
    """Keep, per group, all files newer than N days OR among newest M files."""
    if not LOG_DIR.exists():
        return
    cutoff = (datetime.now() - timedelta(days=LOG_KEEP_DAYS)).timestamp()
    groups = [
        "[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]_*.log",  # main run logs
        "pixiv_failure_*.png",
        "pixiv_failure_*.html",
        "pixiv_autocomplete_probe_*.html",
    ]
    removed = 0
    for pattern in groups:
        files = sorted(LOG_DIR.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
        # Keep newest LOG_KEEP_PER_GROUP unconditionally; for the rest, keep
        # only those still within the LOG_KEEP_DAYS window.
        for old in files[LOG_KEEP_PER_GROUP:]:
            try:
                if old.stat().st_mtime >= cutoff:
                    continue
                old.unlink()
                removed += 1
            except Exception:
                pass
    if removed:
        log.info(f"清理 logs/：删除 {removed} 个（每类保留最新 {LOG_KEEP_PER_GROUP} 或 {LOG_KEEP_DAYS} 天内）")


def cleanup_done_dir():
    if not DONE_DIR.exists():
        return

    cutoff = datetime.now() - timedelta(days=DONE_DAYS)
    removed = 0

    for file in DONE_DIR.iterdir():
        if not file.is_file():
            continue
        match = re.match(r"^(\d{8})_", file.name)
        if not match:
            continue
        try:
            file_date = datetime.strptime(match.group(1), "%Y%m%d")
        except ValueError:
            continue
        if file_date < cutoff:
            file.unlink()
            removed += 1

    if removed:
        log.info(f"清理 done/ 目录：删除了 {removed} 个超过 {DONE_DAYS} 天的文件")


def migrate_progress_files():
    PROGRESS_DIR.mkdir(exist_ok=True)
    migrated = 0
    for file in SCRIPT_DIR.glob("*_progress.json"):
        dest = PROGRESS_DIR / file.name
        if not dest.exists():
            shutil.move(str(file), str(dest))
            migrated += 1
    if migrated:
        log.info(f"迁移了 {migrated} 个旧进度文件到 progress/")


def move_to_done(src: Path):
    DONE_DIR.mkdir(exist_ok=True)
    prefix = datetime.now().strftime("%Y%m%d")
    dest = DONE_DIR / f"{prefix}_{src.name}"
    counter = 1
    while dest.exists():
        dest = DONE_DIR / f"{prefix}_{src.stem}_{counter}{src.suffix}"
        counter += 1
    shutil.move(str(src), str(dest))
    return dest


def make_temp_dir(prefix: str) -> Path:
    TMP_DIR.mkdir(exist_ok=True)
    stamp = f"{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}_{random.randint(1000, 9999)}"
    path = TMP_DIR / f"{prefix}{stamp}"
    path.mkdir(parents=True, exist_ok=False)
    return path


def open_civitai_browser(pw):
    context = pw.chromium.launch_persistent_context(
        str(CHROME_PROFILE_DIR),
        channel="chrome",
        headless=False,
        args=["--start-minimized"],
        ignore_default_args=["--enable-automation", "--no-sandbox"],
    )
    page = context.pages[0] if context.pages else context.new_page()
    try:
        page.goto(f"{CIVITAI_BASE}/", wait_until="commit", timeout=15000)
    except Exception:
        pass
    return context, page


def safe_goto(page, url, wait=5):
    try:
        page.goto(url, wait_until="commit", timeout=15000)
    except Exception:
        pass
    time.sleep(wait)


def ensure_on_create_page(page):
    safe_goto(page, f"{CIVITAI_BASE}/posts/create", wait=5)
    if "/login" in page.url or "signin" in page.url:
        try:
            page.evaluate("window.moveTo(100, 100); window.resizeTo(1280, 800);")
        except Exception:
            pass
        log.warning("未登录。请在浏览器里登录 Civitai，然后按 Enter 继续...")
        input()
        safe_goto(page, f"{CIVITAI_BASE}/posts/create", wait=5)
        if "/login" in page.url or "signin" in page.url:
            log.warning("仍未登录。请确认登录后再按 Enter...")
            input()
            safe_goto(page, f"{CIVITAI_BASE}/posts/create", wait=5)
        try:
            page.evaluate("window.moveTo(-32000, -32000);")
        except Exception:
            pass


def create_civitai_post(page, image_path: Path, delay: float, cancel_event=None) -> str | None:
    _raise_if_canceled(cancel_event)
    ensure_on_create_page(page)

    # Wait for file input to appear — safe_goto uses wait_until="commit" which
    # only waits for response headers, so the DOM may still be loading.
    file_input = None
    for _ in range(12):
        _raise_if_canceled(cancel_event)
        loc = page.locator('input[type="file"]')
        if loc.count() > 0:
            file_input = loc.first
            break
        _sleep_with_cancel(1, cancel_event)
    if file_input is None:
        log.error("    未找到文件上传输入框（页面可能未加载完成），跳过")
        return None

    try:
        file_input.set_input_files(str(image_path))
    except Exception as exc:
        log.error(f"    上传失败: {exc}")
        log.debug(traceback.format_exc())
        return None

    publish_btn = page.locator('button:has-text("Publish")')
    enabled = False
    for _ in range(60):
        _sleep_with_cancel(2, cancel_event)
        if publish_btn.count() > 0 and publish_btn.first.is_enabled():
            enabled = True
            break

    if not enabled:
        log.error("    Publish 按钮未启用（等待 120 秒），跳过")
        return None

    publish_btn.first.click()
    log.info("    已点击 Publish，等待跳转...")

    for _ in range(30):
        _sleep_with_cancel(2, cancel_event)
        current_url = page.url
        if "/posts/create" not in current_url and "/posts/" in current_url:
            post_url = re.sub(r"/edit$", "", current_url)
            wait = delay + random.uniform(1, 3)
            _sleep_with_cancel(wait, cancel_event)
            return post_url
    log.error("    发布超时（60 秒内未跳转），跳过")
    return None


def load_progress(progress_path: Path) -> dict:
    if progress_path.exists():
        return json.loads(progress_path.read_text(encoding="utf-8"))
    return {"completed": [], "failed": [], "remaining": []}


def save_progress(progress_path: Path, progress: dict):
    progress_path.write_text(json.dumps(progress, indent=2, ensure_ascii=False), encoding="utf-8")


def parse_targets(raw: str) -> list[str]:
    targets = []
    for part in raw.split(","):
        item = part.strip().lower()
        if not item:
            continue
        if item not in TARGETS:
            raise ValueError(f"不支持的 targets 项：{item}")
        if item not in targets:
            targets.append(item)
    return targets or ["civitai"]


def parse_bool_flag(raw: str) -> bool:
    lowered = raw.strip().lower()
    if lowered in {"1", "true", "yes", "on"}:
        return True
    if lowered in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"无法解析布尔值: {raw}")


def render_rule_fit_report_markdown(report: dict) -> str:
    lines = [
        "# Pixiv Rule Fit Report",
        "",
        f"- Generated at: {report.get('generated_at', '')}",
        f"- Sample count: {report.get('sample_count', 0)}",
    ]
    if report.get("stage_counts"):
        lines.append(f"- Stage counts: {report.get('stage_counts', {})}")
    if report.get("tagger_status_counts"):
        lines.append(f"- Tagger statuses: {report.get('tagger_status_counts', {})}")
    lines.extend(["", "## Top Missing"])
    for item in report.get("top_missing", []):
        lines.append(f"- {item['tag']}: {item['count']}")
    lines.extend(["", "## Top Extra"])
    for item in report.get("top_extra", []):
        lines.append(f"- {item['tag']}: {item['count']}")
    lines.extend(["", "## Top Synonym Mismatch"])
    for item in report.get("top_synonym_mismatch", []):
        lines.append(f"- {item['pair']}: {item['count']}")
    lines.extend(["", "## Domain Patterns"])
    for key, stats in report.get("domain_patterns", {}).items():
        lines.append(
            f"- {key}: count={stats.get('count', 0)}, avg_missing={stats.get('avg_missing', 0.0)}, "
            f"avg_extra={stats.get('avg_extra', 0.0)}, avg_synonym={stats.get('avg_synonym', 0.0)}"
        )
    lines.extend(["", "## Age Patterns"])
    for key, stats in report.get("age_patterns", {}).items():
        lines.append(
            f"- {key}: count={stats.get('count', 0)}, avg_missing={stats.get('avg_missing', 0.0)}, "
            f"avg_extra={stats.get('avg_extra', 0.0)}, avg_synonym={stats.get('avg_synonym', 0.0)}"
        )
    lines.append("")
    return "\n".join(lines)


def create_upload_manifest(
    image_path: Path,
    targets: list[str],
    files: dict[str, Path],
    hain_bridge: HainTagBridge,
    alias_data: dict,
    popularity_data: dict,
    age_rules: dict,
    civitai_dir: Path,
    pixiv_dir: Path,
    pixiv_privacy: str,
    pixiv_allow_tag_edits: bool,
    tagger_bridge: HainTagTaggerBridge | None = None,
    jp_alias_cache: dict | None = None,
    general_jp_data: dict | None = None,
    pixiv_page=None,
    censor_engine: CensorEngine | None = None,
    censor_classes=None,
    civitai_safety_cfg: dict | None = None,
    llm_reverse_config: dict | None = None,
    llm_persona_id: str = "",
    llm_account_id: str = "",
    llm_content_mode: str = "",
    llm_personas_by_platform: dict | None = None,
    llm_content_modes_by_platform: dict | None = None,
    x_dir: Path | None = None,
    x_settings: dict | None = None,
    x_templates: dict | None = None,
    x_base_template: str = "en_sfw",
    xhs_dir: Path | None = None,
    xhs_settings: dict | None = None,
    xhs_templates: dict | None = None,
    xhs_base_template: str = "default",
    ai_tags_by_platform: dict | None = None,
    cancel_event=None,
) -> tuple[dict, bool]:
    _raise_if_canceled(cancel_event)
    source_meta = hain_bridge.read_metadata(image_path)

    civitai_blocked = False
    civitai_block_reason = ""
    if "civitai" in targets and civitai_safety_cfg:
        _raise_if_canceled(cancel_event)
        civitai_blocked, civitai_block_reason = check_civitai_safety(
            image_path, source_meta, age_rules, civitai_safety_cfg
        )
        if civitai_blocked:
            log.info(f"    Civitai 安全过滤：{civitai_block_reason}")

    civitai_copy = strip_prompts_keep_lora(image_path, civitai_dir) if "civitai" in targets else None
    _raise_if_canceled(cancel_event)
    # Sanitize / tag pipeline runs if ANY target needs it (PLATFORM_RULES table).
    needs_sanitize = any(PLATFORM_RULES.get(t, {}).get("needs_sanitize") for t in targets)
    needs_pixiv_payload = any(PLATFORM_RULES.get(t, {}).get("needs_copy") for t in targets)
    pixiv_clean = sanitize_image_for_pixiv(image_path, pixiv_dir) if needs_sanitize else None
    _raise_if_canceled(cancel_event)

    # Run auto-censor on the sanitized pixiv copy if engine present.
    censor_result = None
    if pixiv_clean is not None and censor_engine is not None:
        _raise_if_canceled(cancel_event)
        censor_result = censor_engine.detect_and_censor(
            Path(pixiv_clean.output_path),
            output_path=Path(pixiv_clean.output_path),
            enabled_classes=censor_classes,
        )
        _raise_if_canceled(cancel_event)
        if censor_result.applied:
            log.info(f"    censor: 打码完成 — {censor_result.detail}")
        elif censor_result.status == "ok":
            log.info(f"    censor: 无需打码 — {censor_result.detail}")
        # other statuses already logged by engine

    pixiv_metadata_check = (
        hain_bridge.read_metadata(pixiv_clean.output_path) if pixiv_clean is not None
        else {"status": "skipped", "detected_types": [], "details": []}
    )
    _raise_if_canceled(cancel_event)
    if needs_pixiv_payload and tagger_bridge is not None:
        tagger_result = tagger_bridge.predict_tags(image_path)
        if tagger_result.get("status") not in ("ok", "disabled") and not tagger_result.get("available"):
            log.info(f"    tagger: {tagger_result.get('status')} — 仅用 prompt/文件名候选")
    else:
        tagger_result = {"available": False, "status": "disabled", "flat_tags": [], "groups": {}, "details": []}
    extra_candidates: list[str] = []
    extra_groups: dict[str, list[tuple[str, float]]] = {}
    if tagger_result.get("available"):
        extra_candidates = list(tagger_result.get("flat_tags", []))
        for category, entries in (tagger_result.get("groups") or {}).items():
            extra_groups[category] = [(tag, float(score)) for tag, score in entries]
    pixiv_payload = (
        build_pixiv_payload(
            image_path=image_path,
            metadata_info=source_meta,
            alias_data=alias_data,
            popularity_data=popularity_data,
            age_rules=age_rules,
            extra_candidates=extra_candidates,
            extra_groups=extra_groups,
            jp_alias_cache=jp_alias_cache if jp_alias_cache is not None else {},
            general_jp_data=general_jp_data or {},
            pixiv_page=pixiv_page,
            live_lookup=True,
            live_jp_lookup=True,
            include_ai_art=(ai_tags_by_platform or {}).get("pixiv", True),
        )
        if needs_pixiv_payload else None
    )
    _raise_if_canceled(cancel_event)

    # Per-platform max_age hard rule: xhs refuses r18/r18g, drop those targets.
    image_age_for_rule = (pixiv_payload or {}).get("age_restriction", "all_ages")
    nsfw_blocked_targets = {
        t for t in targets
        if not _platform_accepts_age(t, image_age_for_rule)
    }
    if nsfw_blocked_targets:
        log.info(
            f"    NSFW 硬规则拦截 (age={image_age_for_rule}): "
            f"{sorted(nsfw_blocked_targets)} 不接受该分级，自动跳过"
        )

    llm_reverse_result = {"enabled": False, "status": "disabled", "error": ""}
    copy_block = empty_copy_block()
    if pixiv_payload is not None:
        pixiv_payload["privacy"] = pixiv_privacy
        pixiv_payload["allow_tag_edits"] = pixiv_allow_tag_edits
        if censor_result is not None and censor_result.applied:
            if pixiv_payload.get("age_restriction") not in {"r18", "r18g"}:
                log.info("    censor: 检测到露出，强制 age_restriction=r18")
                force_pixiv_age_restriction(pixiv_payload, "r18")
        _rating_scores = tagger_result.get("rating_scores") or {}
        if _rating_scores:
            _best_rating = max(_rating_scores, key=_rating_scores.get)
            _best_score = _rating_scores[_best_rating]
            if _best_rating in ("explicit", "questionable") and _best_score > 0.5:
                if pixiv_payload.get("age_restriction") not in ("r18", "r18g"):
                    log.info(f"    tagger rating: {_best_rating}={_best_score:.2f}，升级 age_restriction → r18")
                    force_pixiv_age_restriction(pixiv_payload, "r18")
        _updated_age = pixiv_payload.get("age_restriction", "all_ages")
        if _updated_age != image_age_for_rule:
            nsfw_blocked_targets = {
                t for t in targets
                if not _platform_accepts_age(t, _updated_age)
            }
            if nsfw_blocked_targets:
                log.info(
                    f"    NSFW 硬规则拦截 (age={_updated_age}, 升级后): "
                    f"{sorted(nsfw_blocked_targets)} 不接受该分级，自动跳过"
                )
        if llm_reverse_config and llm_reverse_config.get("enabled"):
            # Skip LLM if no target consumes copy (e.g. --targets civitai)
            if not _targets_need_copy(targets):
                llm_reverse_result = {
                    "enabled": True,
                    "status": "skipped_no_target_needs",
                    "persona_id": llm_persona_id,
                    "account_id": llm_account_id,
                    "platform": "",
                    "content_mode": "",
                    "fields": {},
                    "error": "no target requires copy (civitai-only or similar)",
                }
                log.info("    LLM 反推: 跳过（当前 targets 都不需要文案）")
            elif llm_personas_by_platform:
                # Per-platform mode: independent LLM call per copy platform
                image_path_for_llm = Path(pixiv_clean.output_path) if pixiv_clean else image_path
                image_age = (pixiv_payload or {}).get("age_restriction", "all_ages")
                _llm_extra_ctx = _build_llm_extra_context(pixiv_payload, source_meta=source_meta)
                copy_targets = [t for t in targets if PLATFORM_RULES.get(t, {}).get("needs_copy")]
                for plat in copy_targets:
                    per_persona_id = llm_personas_by_platform.get(plat, "")
                    if not per_persona_id:
                        log.info(f"    LLM 反推 [{plat}]: 未指定人设，跳过")
                        continue
                    per_content_mode = (llm_content_modes_by_platform or {}).get(plat, "") or llm_content_mode or "sfw"
                    if not content_mode_can_handle_age(per_content_mode, image_age):
                        log.info(f"    LLM 反推 [{plat}]: content_mode={per_content_mode}，跳过 {image_age} 图")
                        continue
                    _raise_if_canceled(cancel_event)
                    per_result = infer_image_copy(
                        image_path=image_path_for_llm,
                        config=llm_reverse_config,
                        persona_id=per_persona_id,
                        account_id=llm_account_id,
                        content_mode=per_content_mode,
                        extra_context=_llm_extra_ctx,
                        cancel_event=cancel_event,
                    )
                    if per_result.get("status") == "ok":
                        if plat == "pixiv":
                            apply_llm_result_to_pixiv_payload(pixiv_payload, per_result)
                        apply_llm_result_to_copy_block(
                            copy_block,
                            per_result,
                            platform=plat,
                            account_id=llm_account_id,
                        )
                        log.info(f"    LLM 反推 [{plat}]: 生成文案 ({per_content_mode})")
                    else:
                        log.warning(
                            f"    LLM 反推 [{plat}]: {per_result.get('status')} — {per_result.get('error', '')}"
                        )
                llm_reverse_result["enabled"] = True
                llm_reverse_result["status"] = "per_platform"
            else:
                # Unified mode
                _, effective_mode = resolve_persona(llm_reverse_config, llm_persona_id, llm_content_mode)
                image_age = (pixiv_payload or {}).get("age_restriction", "all_ages")
                if not content_mode_can_handle_age(effective_mode, image_age):
                    llm_reverse_result = {
                        "enabled": True,
                        "status": "skipped_sfw_mode",
                        "persona_id": llm_persona_id,
                        "content_mode": effective_mode,
                        "fields": {},
                        "error": f"content_mode={effective_mode} does not cover image age={image_age}",
                    }
                    log.info(
                        f"    LLM 反推: 跳过——content_mode={effective_mode}，图分级 {image_age}"
                    )
                else:
                    _raise_if_canceled(cancel_event)
                    _ctx = _build_llm_extra_context(pixiv_payload, source_meta=source_meta)
                    log.info(f"    LLM 反推: extra_context={_ctx!r}")
                    llm_reverse_result = infer_image_copy(
                        image_path=Path(pixiv_clean.output_path) if pixiv_clean else image_path,
                        config=llm_reverse_config,
                        persona_id=llm_persona_id,
                        account_id=llm_account_id,
                        content_mode=llm_content_mode,
                        extra_context=_ctx,
                        cancel_event=cancel_event,
                    )
                    if llm_reverse_result.get("status") == "ok":
                        apply_llm_result_to_pixiv_payload(pixiv_payload, llm_reverse_result)
                        log.info(
                            f"    LLM 反推: 已生成标题/简介 ({llm_reverse_result.get('content_mode', 'sfw')})"
                        )
                    else:
                        log.warning(
                            f"    LLM 反推: {llm_reverse_result.get('status')} — {llm_reverse_result.get('error', '')}"
                        )
            _raise_if_canceled(cancel_event)

    # Apply unified-mode LLM result to copy_block (per-platform mode writes directly).
    if llm_reverse_result.get("status") != "per_platform":
        apply_llm_result_to_copy_block(
            copy_block,
            llm_reverse_result,
            platform=llm_reverse_result.get("platform", ""),
            account_id=llm_account_id,
        )

    x_payload = None
    if "x" in targets:
        try:
            from x.support import build_x_payload as _build_x_payload
            x_source = Path(pixiv_clean.output_path) if pixiv_clean else image_path
            x_payload = _build_x_payload(
                pixiv_payload=pixiv_payload,
                image_path=x_source,
                x_dir=x_dir or (image_path.parent.parent / "x_out"),
                settings=x_settings or {},
                templates=x_templates or {},
                base_template=x_base_template,
                age_restriction=(pixiv_payload or {}).get("age_restriction", "all_ages"),
                copy=copy_block,
                ai_tags_enabled=(ai_tags_by_platform or {}).get("x", True),
            )
        except Exception as exc:
            log.error(f"    X payload 构建失败: {exc}")
            log.debug(traceback.format_exc())
            x_payload = None
        _raise_if_canceled(cancel_event)

    xhs_payload = None
    if "xhs" in targets and "xhs" not in nsfw_blocked_targets:
        try:
            from xhs.support import build_xhs_payload as _build_xhs_payload
            xhs_source = Path(pixiv_clean.output_path) if pixiv_clean else image_path
            _xhs_settings = xhs_settings or {}
            if not (ai_tags_by_platform or {}).get("xhs", True):
                _xhs_settings = dict(_xhs_settings)
                _xhs_settings["auto_append_ai_tag"] = False
            xhs_payload = _build_xhs_payload(
                pixiv_payload=pixiv_payload,
                image_path=xhs_source,
                xhs_dir=xhs_dir or (image_path.parent.parent / "xhs_out"),
                settings=_xhs_settings,
                templates=xhs_templates or {},
                base_template=xhs_base_template,
                age_restriction=(pixiv_payload or {}).get("age_restriction", "all_ages"),
                copy=copy_block,
            )
        except Exception as exc:
            log.error(f"    xhs payload 构建失败: {exc}")
            log.debug(traceback.format_exc())
            xhs_payload = None
        _raise_if_canceled(cancel_event)

    manifest = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "source_path": str(image_path),
        "targets": targets,
        "dry_run": False,
        "status_by_target": {
            target: ("skipped_max_age" if target in nsfw_blocked_targets else "pending")
            for target in targets
        },
        "errors": [],
        "copy": copy_block,
        "civitai": {
            "clean_copy_path": str(civitai_copy) if civitai_copy else "",
            "post_url": "",
            "skip_reason": civitai_block_reason,
        },
        "pixiv": {
            "clean_copy_path": str(pixiv_clean.output_path) if pixiv_clean else "",
            "metadata_check": {
                "status": pixiv_metadata_check.get("status", "skipped"),
                "detected_types": pixiv_metadata_check.get("detected_types", []),
                "details": pixiv_metadata_check.get("details", []),
            },
            "raw_candidates": pixiv_payload["raw_candidates"] if pixiv_payload else [],
            "metadata_entity_hits": pixiv_payload["metadata_entity_hits"] if pixiv_payload else [],
            "popularity_decisions": pixiv_payload["popularity_decisions"] if pixiv_payload else [],
            "final_tags": pixiv_payload["final_tags"] if pixiv_payload else [],
            "entity_tags": pixiv_payload.get("entity_tags", []) if pixiv_payload else [],
            "rejected_tags": pixiv_payload["rejected_tags"] if pixiv_payload else [],
            "domain": pixiv_payload["domain"] if pixiv_payload else "",
            "title_ja": pixiv_payload["title_ja"] if pixiv_payload else "",
            "title_zh": pixiv_payload["title_zh"] if pixiv_payload else "",
            "caption_ja": pixiv_payload["caption_ja"] if pixiv_payload else "",
            "caption_zh": pixiv_payload["caption_zh"] if pixiv_payload else "",
            "age_restriction": pixiv_payload["age_restriction"] if pixiv_payload else "",
            "ai_generated": pixiv_payload["ai_generated"] if pixiv_payload else False,
            "privacy": pixiv_privacy,
            "allow_tag_edits": pixiv_allow_tag_edits,
            "post_url": "",
            "llm_reverse": llm_reverse_result,
            "tagger": {
                "status": tagger_result.get("status", "disabled"),
                "available": tagger_result.get("available", False),
                "top_tags": list(tagger_result.get("flat_tags", []))[:30],
                "tagger_type": tagger_result.get("tagger_type", "cl"),
                "details": tagger_result.get("details", []),
            },
            "censor": censor_result.to_dict() if censor_result is not None else {"status": "disabled", "applied": False},
        },
        "x": x_payload if x_payload else {
            "clean_copy_paths": [],
            "text": "",
            "tags": [],
            "tag_sources": {},
            "template": "",
            "sensitive": False,
            "alt_text": "",
            "group_id": None,
            "post_url": "",
        },
        "xhs": xhs_payload if xhs_payload else {
            "clean_copy_paths": [],
            "title": "",
            "body": "",
            "tags": [],
            "tag_sources": {},
            "template": "",
            "group_id": None,
            "post_url": "",
        },
        "source_metadata": {
            "status": source_meta.get("status", "unknown"),
            "detected_types": source_meta.get("detected_types", []),
            "details": source_meta.get("details", []),
        },
    }

    pixiv_ready = True
    if "pixiv" in targets:
        status = pixiv_metadata_check.get("status")
        if not pixiv_metadata_check.get("available", False):
            # Validator unavailable. sanitize_image_for_pixiv already strips
            # metadata with PIL, so proceeding is safe.
            # Only warn when haintag root exists but the module can't be loaded
            # (unexpected). When haintag is simply not installed, stay silent.
            haintag_root = _resolve_haintag_root()
            if haintag_root.exists():
                log.warning("    metadata validator unavailable (haintag found but import failed); continuing")
        elif status != "clean":
            pixiv_ready = False
            manifest["status_by_target"]["pixiv"] = "failed"
            manifest["errors"].append(
                f"Pixiv clean copy metadata validation failed: {status} {pixiv_metadata_check.get('details', [])}"
            )
    if civitai_blocked:
        manifest["status_by_target"]["civitai"] = "skipped_civitai_safety"
    append_validation_case(image_path, files["validation"], manifest)
    return manifest, pixiv_ready


def cmd_split(args):
    api_key = args.api_key or os.environ.get("CIVITAI_API_KEY")
    if not api_key:
        log.error("需要 API key。用 --api-key 或设置 CIVITAI_API_KEY 环境变量。")
        sys.exit(1)

    posts_input = args.posts
    if not posts_input:
        raw = input("\nPost ID or URL (space-separated for multiple): ").strip()
        if not raw:
            log.info("没有输入。")
            return
        posts_input = raw.split()

    post_ids = [parse_post_id(item) for item in posts_input]
    log.info(f"\n准备拆分 {len(post_ids)} 个 post: {post_ids}")

    all_tasks = []
    for post_id in post_ids:
        try:
            images = fetch_post_images(post_id, api_key)
        except Exception as exc:
            log.error(f"  Post {post_id} 获取图片失败: {exc}")
            log.debug(traceback.format_exc())
            continue
        if not images:
            log.info(f"  Post {post_id} 没有找到图片，跳过")
            continue
        all_tasks.append((post_id, images))

    if not all_tasks:
        log.info("没有需要处理的图片。")
        return

    total_images = sum(len(images) for _, images in all_tasks)
    log.info(f"\n共 {total_images} 张图需要拆分。")

    PROGRESS_DIR.mkdir(exist_ok=True)
    for post_id, images in all_tasks:
        log.info(f"\n--- 处理 Post {post_id} ({len(images)} 张图) ---")
        old_progress = SCRIPT_DIR / f"{post_id}_progress.json"
        progress_path = PROGRESS_DIR / f"{post_id}_progress.json"
        if old_progress.exists() and not progress_path.exists():
            shutil.move(str(old_progress), str(progress_path))
            log.info("  迁移旧进度文件到 progress/")
        progress = load_progress(progress_path)

        completed_ids = {item["image_id"] for item in progress["completed"]}
        remaining_images = [img for img in images if img["id"] not in completed_ids]
        if not remaining_images:
            log.info("  所有图片已处理完毕。")
            continue

        temp_dir = make_temp_dir(f"civitai_split_{post_id}_")
        try:
            log.info(f"  下载 {len(remaining_images)} 张图片...")
            local_paths = download_and_embed_metadata(remaining_images, api_key, temp_dir)

            log.info("  开始上传（每张独立开关浏览器，规避验证码）...")
            with sync_playwright() as pw:
                for idx, (img, local_path) in enumerate(zip(remaining_images, local_paths), 1):
                    log.info(f"\n  [{idx}/{len(remaining_images)}] 上传图片 {img['id']}...")
                    context, page = open_civitai_browser(pw)
                    try:
                        new_url = create_civitai_post(page, local_path, args.delay)
                    except Exception as exc:
                        log.error(f"    图片 {img['id']} 发布异常: {exc}")
                        log.debug(traceback.format_exc())
                        new_url = None
                    finally:
                        context.close()
                    if new_url:
                        progress["completed"].append({"image_id": img["id"], "new_post_url": new_url})
                    else:
                        progress["failed"].append({"image_id": img["id"], "error": "发布失败"})
                    save_progress(progress_path, progress)
        except Exception as exc:
            log.error(f"  Post {post_id} 处理异常: {exc}")
            log.debug(traceback.format_exc())
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)
        log.info(f"\n  Post {post_id} 完成: {len(progress['completed'])} 成功, {len(progress['failed'])} 失败")


def _select_by_sort(images: list, sort_mode: str, count: int) -> list:
    n = min(count, len(images))
    if sort_mode == "name_asc":
        return sorted(images, key=lambda f: f.name.lower())[:n]
    if sort_mode == "name_desc":
        return sorted(images, key=lambda f: f.name.lower(), reverse=True)[:n]
    if sort_mode == "time_asc":
        return sorted(images, key=lambda f: f.stat().st_mtime)[:n]
    if sort_mode == "time_desc":
        return sorted(images, key=lambda f: f.stat().st_mtime, reverse=True)[:n]
    return random.sample(images, n)


def cmd_upload(args):
    files = ensure_runtime_files(SCRIPT_DIR)
    alias_data = load_json(files["aliases"], {})
    popularity_data = load_json(files["popularity"], {})
    age_rules = load_json(files["age_rules"], {})
    hain_bridge, tagger_bridge = _make_bridges()
    _tagger_probe = getattr(tagger_bridge, "_model_dir", None) or getattr(tagger_bridge, "_dir", None)
    if "pixiv" in getattr(args, "targets", ""):
        if not _tagger_probe:
            log.info("tagger: 未配置，将仅用 prompt/文件名候选（可在 web 设置面板或 launcher [6] 配置）")
    jp_alias_cache = load_json(files["jp_aliases"], {})
    general_jp_data = load_json(files["general_jp"], {})
    danbooru_jp_map = load_json(files["danbooru_jp"], {})
    if danbooru_jp_map:
        general_jp_data["_danbooru_map"] = danbooru_jp_map
        log.info(f"Danbooru→JP 词典已加载: {len(danbooru_jp_map)} 条")
    civitai_safety_cfg = load_json(files["civitai_safety"], {})
    llm_reverse_config = load_llm_reverse_config()
    llm_reverse_enabled = bool(getattr(args, "llm_reverse", False)) and llm_reverse_config.get("enabled")
    if getattr(args, "llm_reverse", False) and not llm_reverse_enabled:
        log.warning("LLM 反推: 已请求但未启用或配置不完整，将跳过")

    no_ai_tags = getattr(args, "no_ai_tags", None) or ""
    if not getattr(args, "ai_tags_by_platform", None) and no_ai_tags:
        if no_ai_tags == "all":
            args.ai_tags_by_platform = {"pixiv": False, "x": False, "xhs": False}
        else:
            skip = {p.strip().lower() for p in no_ai_tags.split(",") if p.strip()}
            args.ai_tags_by_platform = {p: p not in skip for p in ("pixiv", "x", "xhs")}

    UPLOAD_DIR.mkdir(exist_ok=True)
    XHS_UPLOAD_DIR.mkdir(exist_ok=True)
    DONE_DIR.mkdir(exist_ok=True)

    targets = parse_targets(args.targets)

    all_images = sorted(
        file for file in UPLOAD_DIR.iterdir()
        if file.is_file() and file.suffix.lower() in IMAGE_EXTENSIONS
    )
    xhs_only = sorted(
        f for f in XHS_UPLOAD_DIR.iterdir()
        if f.is_file() and f.suffix.lower() in IMAGE_EXTENSIONS
    ) if "xhs" in targets else []
    if not all_images and not xhs_only:
        log.info(f"upload/ 和 xhs_upload/ 目录都没有图片。\n  {UPLOAD_DIR}\n  {XHS_UPLOAD_DIR}")
        return

    needs_xhs = "xhs" in targets or bool(xhs_only)
    xhs_manual_mode = bool(getattr(args, "xhs_manual_mode", False))

    # Auto-censor: model file at models/auto_censor.pt opts in. Config tunables
    # live in pixiv_censor.json (auto-created on first run with defaults).
    # X target reuses the pixiv-cleaned image, so censor follows the same toggle.
    censor_engine = None
    censor_classes = DEFAULT_CENSOR_CLASSES
    needs_pixiv_pipeline = any(PLATFORM_RULES.get(t, {}).get("needs_sanitize") for t in targets) or needs_xhs
    if needs_pixiv_pipeline:
        model_path = SCRIPT_DIR / "models" / "auto_censor.pt"
        if model_path.exists():
            cfg = load_json(files["censor_config"], {})
            mode = cfg.get("mode", "mosaic")
            conf = float(cfg.get("conf_threshold", 0.55))
            bar_count = int(cfg.get("bar_count", 4))
            classes_spec = cfg.get("enabled_classes", "")
            if isinstance(classes_spec, list):
                classes_spec = ",".join(str(x) for x in classes_spec)
            censor_classes = parse_class_set(classes_spec)
            censor_engine = CensorEngine(
                model_path,
                conf_threshold=conf,
                mode=mode,
                bar_count=bar_count,
            )
            log.info(
                f"自动打码: 已启用 (mode={mode}, conf={conf}, classes={sorted(censor_classes)})"
            )
        else:
            log.info("自动打码: 未启用（如需放模型到 models/auto_censor.pt + pip install ultralytics opencv-python）")
    sort_mode = getattr(args, "sort", "random")
    selected_names = getattr(args, "files", None) or []
    if selected_names:
        up_map = {f.name.lower(): f for f in all_images}
        xhs_map = {f.name.lower(): f for f in xhs_only}
        image_files = [up_map[n.lower()] for n in selected_names if n.lower() in up_map]
        xhs_files = [xhs_map[n.lower()] for n in selected_names if n.lower() in xhs_map]
        if not image_files and not xhs_files:
            log.warning("指定的文件不在 upload/ 或 xhs_upload/ 目录，改用排序规则")
            image_files = _select_by_sort(all_images, sort_mode, 1)
            xhs_files = _select_by_sort(xhs_only, sort_mode, 1) if xhs_only else []
        mode_desc = f"指定顺序 {len(image_files)}+{len(xhs_files)} 张"
    else:
        requested = max(0, int(getattr(args, "count", 0) or 0))
        if requested > 0:
            count = min(requested, len(all_images)) if all_images else 0
            xhs_count = min(requested, len(xhs_only)) if xhs_only else 0
            mode_desc = f"按 {sort_mode} 选 {count}+{xhs_count} 张"
        else:
            count = min(random.randint(1, 5), len(all_images)) if all_images else 0
            xhs_count = min(random.randint(1, 5), len(xhs_only)) if xhs_only else 0
            mode_desc = f"随机选 {count}+{xhs_count} 张"
        image_files = _select_by_sort(all_images, sort_mode, count) if count else []
        xhs_files = _select_by_sort(xhs_only, sort_mode, xhs_count) if xhs_count else []
    upload_targets = [t for t in targets if t != "xhs"] if xhs_files else targets
    image_queue = [(img, upload_targets) for img in image_files]
    image_queue += [(img, ["xhs"]) for img in xhs_files]
    all_processed_targets = list(dict.fromkeys(t for _, et in image_queue for t in et))
    log.info(
        f"upload/ {len(all_images)} 张本次选 {len(image_files)}；"
        f"xhs_upload/ {len(xhs_only)} 张本次选 {len(xhs_files)}。"
        f"目标：{all_processed_targets}\n"
    )

    temp_dir = make_temp_dir("civitai_upload_")
    civitai_dir = temp_dir / "civitai"
    pixiv_dir = temp_dir / "pixiv"
    x_dir = temp_dir / "x"
    xhs_dir = (SCRIPT_DIR / "xhs_out") if xhs_manual_mode else (temp_dir / "xhs")
    civitai_dir.mkdir(exist_ok=True)
    pixiv_dir.mkdir(exist_ok=True)
    if "x" in targets:
        x_dir.mkdir(exist_ok=True)
    if needs_xhs:
        xhs_dir.mkdir(exist_ok=True)

    x_settings = x_templates = None
    x_base_template = "en_sfw"
    if "x" in targets:
        from x.support import load_x_settings, load_x_templates
        x_settings = load_x_settings()
        x_templates = load_x_templates()
        x_base_template = getattr(args, "x_template", None) or x_settings.get("default_template", "en_sfw")

    xhs_settings = xhs_templates = None
    xhs_base_template = "default"
    if needs_xhs:
        from xhs.support import load_xhs_settings, load_xhs_templates
        xhs_settings = load_xhs_settings()
        xhs_templates = load_xhs_templates()
        xhs_base_template = getattr(args, "xhs_template", None) or xhs_settings.get("default_template", "default")

    civitai_context = pixiv_context = x_context = xhs_context = None
    civitai_page = pixiv_page = x_page = xhs_page = None
    xhs_browser = None
    success_count = 0
    fail_count = 0
    consecutive_failures = 0
    abort_threshold = max(1, int(args.abort_after_failures))
    playwright = None
    target_success_counts = {target: 0 for target in all_processed_targets}
    target_fail_counts = {target: 0 for target in all_processed_targets}

    try:
        needs_browser = [t for t in ("civitai", "pixiv", "x") if t in targets]
        if needs_xhs and not xhs_manual_mode:
            needs_browser.append("xhs")
        if not args.dry_run and needs_browser:
            playwright = sync_playwright().start()
        if playwright is not None and "civitai" in targets:
            civitai_context, civitai_page = open_civitai_browser(playwright)
        if playwright is not None and "pixiv" in targets:
            pixiv_context, pixiv_page = open_pixiv_browser(playwright)
        if playwright is not None and "x" in targets:
            from x.support import open_x_browser
            x_context, x_page = open_x_browser(playwright)
        if playwright is not None and needs_xhs and not xhs_manual_mode:
            from xhs.support import open_xhs_browser
            try:
                xhs_context, xhs_page, xhs_browser = open_xhs_browser(playwright)
            except Exception as exc:
                log.warning(f"XHS 浏览器启动失败，跳过小红书: {exc}")
                log.debug(traceback.format_exc())

        _cancel_ev = getattr(args, "cancel_event", None)
        for index, (orig_path, effective_targets) in enumerate(image_queue, 1):
            if _cancel_ev and _cancel_ev.is_set():
                log.info("收到取消信号，停止上传")
                break
            log.info(f"[{index}/{len(image_queue)}] {orig_path.name}")
            prior_successes = find_target_successes(files["manifests"], orig_path)
            skip_targets = {t for t in effective_targets if t in prior_successes}
            if skip_targets:
                log.info(
                    f"    跳过已成功目标: {sorted(skip_targets)}（继承历史 post_url）"
                )
            manifest_path = create_manifest_path(files["manifests"], orig_path)
            manifest, pixiv_ready = create_upload_manifest(
                image_path=orig_path,
                targets=effective_targets,
                files=files,
                hain_bridge=hain_bridge,
                alias_data=alias_data,
                popularity_data=popularity_data,
                age_rules=age_rules,
                civitai_dir=civitai_dir,
                pixiv_dir=pixiv_dir,
                pixiv_privacy=args.pixiv_privacy,
                pixiv_allow_tag_edits=parse_bool_flag(args.pixiv_allow_tag_edits),
                tagger_bridge=tagger_bridge,
                jp_alias_cache=jp_alias_cache,
                general_jp_data=general_jp_data,
                pixiv_page=pixiv_page,
                censor_engine=censor_engine,
                censor_classes=censor_classes,
                civitai_safety_cfg=civitai_safety_cfg,
                llm_reverse_config=llm_reverse_config if llm_reverse_enabled else None,
                llm_persona_id=getattr(args, "llm_persona", ""),
                llm_account_id=getattr(args, "llm_account", ""),
                llm_content_mode=getattr(args, "llm_content_mode", ""),
                llm_personas_by_platform=getattr(args, "llm_personas_by_platform", None),
                llm_content_modes_by_platform=getattr(args, "llm_content_modes_by_platform", None),
                x_dir=x_dir if "x" in effective_targets else None,
                x_settings=x_settings,
                x_templates=x_templates,
                x_base_template=x_base_template,
                xhs_dir=xhs_dir if "xhs" in effective_targets else None,
                xhs_settings=xhs_settings,
                xhs_templates=xhs_templates,
                xhs_base_template=xhs_base_template,
                ai_tags_by_platform=getattr(args, "ai_tags_by_platform", None),
                cancel_event=_cancel_ev,
            )
            _raise_if_canceled(_cancel_ev)
            # Persist any new JP aliases learned during this image's payload build
            save_json(files["jp_aliases"], jp_alias_cache)
            tagger_status = manifest.get("pixiv", {}).get("tagger", {}).get("status", "disabled")
            if "pixiv" in effective_targets and tagger_status not in {"ok", "disabled", "haintag_root_missing", "model_dir_not_configured", "onnxruntime_not_installed"}:
                log.warning(f"    tagger 不可用: {tagger_status}（继续上传，仅用 prompt/文件名候选）")
            manifest["dry_run"] = bool(args.dry_run)
            write_manifest(manifest_path, manifest)
            if "pixiv" in effective_targets:
                save_json(files["popularity"], popularity_data)

            if args.dry_run:
                for target in effective_targets:
                    if manifest["status_by_target"].get(target) == "pending":
                        manifest["status_by_target"][target] = "dry_run"
                write_manifest(manifest_path, manifest)
                log.info("    dry-run 完成，未执行发布。")
                success_count += 1
                continue

            all_succeeded = True
            cancel_requested = False

            if "civitai" in effective_targets:
                if "civitai" in skip_targets:
                    inherited_url = prior_successes["civitai"]
                    manifest["civitai"]["post_url"] = inherited_url
                    manifest["status_by_target"]["civitai"] = "skipped_already_done"
                    log.info(f"    Civitai 已发过，跳过: {inherited_url}")
                elif manifest["status_by_target"].get("civitai") == "skipped_civitai_safety":
                    reason = manifest["civitai"].get("skip_reason", "")
                    log.info(f"    Civitai 安全跳过: {reason}")
                else:
                    civitai_copy = Path(manifest["civitai"]["clean_copy_path"])
                    try:
                        civitai_url = create_civitai_post(civitai_page, civitai_copy, args.delay, cancel_event=_cancel_ev)
                    except InterruptedError:
                        cancel_requested = True
                        civitai_url = None
                    except Exception as exc:
                        log.error(f"    Civitai 发布异常: {exc}")
                        log.debug(traceback.format_exc())
                        civitai_url = None
                    if civitai_url:
                        manifest["civitai"]["post_url"] = civitai_url
                        manifest["status_by_target"]["civitai"] = "success"
                        log.info(f"    Civitai 发布成功: {civitai_url}")
                    elif cancel_requested and manifest["status_by_target"].get("civitai") == "pending":
                        manifest["status_by_target"]["civitai"] = "canceled"
                        manifest["errors"].append("Civitai upload canceled")
                        all_succeeded = False
                    else:
                        manifest["status_by_target"]["civitai"] = "failed"
                        manifest["errors"].append("Civitai upload failed")
                        all_succeeded = False

            if "pixiv" in effective_targets:
                if "pixiv" in skip_targets:
                    inherited_url = prior_successes["pixiv"]
                    manifest["pixiv"]["post_url"] = inherited_url
                    manifest["status_by_target"]["pixiv"] = "skipped_already_done"
                    log.info(f"    Pixiv 已发过，跳过: {inherited_url}")
                elif not pixiv_ready:
                    all_succeeded = False
                elif cancel_requested:
                    manifest["status_by_target"]["pixiv"] = "canceled"
                    manifest["errors"].append("Pixiv upload canceled")
                    all_succeeded = False
                else:
                    pixiv_copy = Path(manifest["pixiv"]["clean_copy_path"])
                    payload = {
                        "title_ja": manifest["pixiv"]["title_ja"],
                        "title_zh": manifest["pixiv"]["title_zh"],
                        "caption_ja": manifest["pixiv"]["caption_ja"],
                        "caption_zh": manifest["pixiv"]["caption_zh"],
                        "final_tags": manifest["pixiv"]["final_tags"],
                        "age_restriction": manifest["pixiv"]["age_restriction"],
                        "privacy": manifest["pixiv"]["privacy"],
                        "allow_tag_edits": manifest["pixiv"]["allow_tag_edits"],
                        "domain": manifest["pixiv"].get("domain", "original"),
                    }
                    max_retries = max(0, int(args.pixiv_max_retries))
                    pixiv_url = None
                    pixiv_steps: list = []
                    for attempt in range(max_retries + 1):
                        try:
                            pixiv_url, pixiv_steps = create_pixiv_post(
                                pixiv_page, payload, pixiv_copy, args.delay, log_dir=LOG_DIR, cancel_event=_cancel_ev,
                            )
                        except InterruptedError:
                            cancel_requested = True
                            pixiv_url = None
                            pixiv_steps = []
                            break
                        except Exception as exc:
                            log.error(f"    Pixiv 发布异常 (attempt {attempt + 1}): {exc}")
                            log.debug(traceback.format_exc())
                            pixiv_url = None
                            pixiv_steps = []
                        if pixiv_url:
                            break
                        # Don't retry if publish was already clicked — risk of duplicate post
                        already_clicked = any(getattr(s, "name", "") == "publish_click" and getattr(s, "ok", False) for s in pixiv_steps)
                        if already_clicked:
                            log.warning("    publish 已点击，疑似已发布，跳过重试")
                            break
                        captcha_timeout = any(
                            getattr(s, "name", "") == "redirect" and not getattr(s, "ok", False)
                            and "人机验证" in getattr(s, "detail", "")
                            for s in pixiv_steps
                        )
                        if captcha_timeout:
                            log.warning("    pixiv: 人机验证超时，跳过重试（请完成验证后重新上传）")
                            break
                        if attempt < max_retries:
                            log.info(f"    Pixiv 失败，{(attempt + 1) * 3} 秒后重试 ({attempt + 2}/{max_retries + 1})...")
                            _sleep_with_cancel((attempt + 1) * 3, _cancel_ev)
                    manifest["pixiv"]["upload_steps"] = [s.to_dict() for s in pixiv_steps]
                    if cancel_requested and not pixiv_url:
                        manifest["status_by_target"]["pixiv"] = "canceled"
                        manifest["errors"].append("Pixiv upload canceled")
                        all_succeeded = False
                    elif pixiv_url:
                        manifest["pixiv"]["post_url"] = pixiv_url
                        manifest["status_by_target"]["pixiv"] = "success"
                        log.info(f"    Pixiv 发布成功: {pixiv_url}")
                    else:
                        # If publish button was clicked successfully but redirect detection
                        # timed out, the post likely went through on Pixiv's side.
                        # Record as maybe_posted so the next batch skips this image
                        # rather than creating a duplicate.
                        post_was_clicked = any(
                            getattr(s, "name", "") == "publish_click" and getattr(s, "ok", False)
                            for s in pixiv_steps
                        )
                        if post_was_clicked:
                            log.warning(
                                "    publish 已点击但未检测到跳转（网络延迟？），"
                                "记为 maybe_posted — 请手动确认 Pixiv 主页，下次此图将跳过"
                            )
                            manifest["pixiv"]["post_url"] = PIXIV_BASE
                            manifest["status_by_target"]["pixiv"] = "maybe_posted"
                            # Treat as done so the file moves out of upload/
                        else:
                            failed_steps = [s for s in pixiv_steps if not s.ok]
                            if failed_steps:
                                summary = "; ".join(f"{s.name}:{s.reason}" for s in failed_steps)
                                error_msg = f"Pixiv upload failed at [{summary}]"
                            else:
                                error_msg = "Pixiv upload failed (无步骤记录)"
                            if manifest["status_by_target"].get("pixiv") != "failed":
                                manifest["status_by_target"]["pixiv"] = "failed"
                                manifest["errors"].append(error_msg)
                            all_succeeded = False

            if "x" in effective_targets:
                if "x" in skip_targets:
                    inherited_url = prior_successes["x"]
                    manifest["x"]["post_url"] = inherited_url
                    manifest["status_by_target"]["x"] = "skipped_already_done"
                    log.info(f"    X 已发过，跳过: {inherited_url}")
                elif manifest["status_by_target"].get("x") == "skipped_max_age":
                    log.info("    X 已因 NSFW 硬规则跳过")
                elif cancel_requested:
                    manifest["status_by_target"]["x"] = "canceled"
                    manifest["errors"].append("X upload canceled")
                    all_succeeded = False
                elif not manifest["x"]["clean_copy_paths"]:
                    manifest["status_by_target"]["x"] = "failed"
                    manifest["errors"].append("X payload missing (build failed)")
                    all_succeeded = False
                else:
                    from x.support import create_x_post as _create_x_post
                    x_image_paths = [Path(p) for p in manifest["x"]["clean_copy_paths"]]
                    x_url = None
                    try:
                        x_url = _create_x_post(
                            x_page,
                            manifest["x"],
                            x_image_paths,
                            args.delay,
                            settings=x_settings,
                            log_dir=LOG_DIR,
                            cancel_event=_cancel_ev,
                        )
                    except InterruptedError:
                        cancel_requested = True
                        x_url = None
                    except Exception as exc:
                        log.error(f"    X 发布异常: {exc}")
                        log.debug(traceback.format_exc())
                        x_url = None
                    if x_url:
                        manifest["x"]["post_url"] = x_url
                        manifest["status_by_target"]["x"] = "success"
                        log.info(f"    X 发布成功: {x_url}")
                    elif cancel_requested and manifest["status_by_target"].get("x") == "pending":
                        manifest["status_by_target"]["x"] = "canceled"
                        manifest["errors"].append("X upload canceled")
                        all_succeeded = False
                    else:
                        manifest["status_by_target"]["x"] = "failed"
                        manifest["errors"].append("X upload failed")
                        all_succeeded = False

            if "xhs" in effective_targets:
                if "xhs" in skip_targets:
                    inherited_url = prior_successes["xhs"]
                    manifest["xhs"]["post_url"] = inherited_url
                    manifest["status_by_target"]["xhs"] = "skipped_already_done"
                    log.info(f"    xhs 已发过，跳过: {inherited_url}")
                elif manifest["status_by_target"].get("xhs") == "skipped_max_age":
                    log.info("    xhs 已因 NSFW 硬规则跳过（小红书不接受 r18/r18g）")
                elif cancel_requested:
                    manifest["status_by_target"]["xhs"] = "canceled"
                    manifest["errors"].append("xhs upload canceled")
                    all_succeeded = False
                elif not manifest["xhs"]["clean_copy_paths"]:
                    manifest["status_by_target"]["xhs"] = "failed"
                    manifest["errors"].append("xhs payload missing (build failed)")
                    all_succeeded = False
                elif xhs_manual_mode:
                    manifest["status_by_target"]["xhs"] = "manual_ready"
                    log.info("    xhs 手动模式：内容已准备好，请手动发布")
                    all_succeeded = False
                    _xhs_manual_cb = getattr(args, "xhs_manual_callback", None)
                    if _xhs_manual_cb:
                        _xhs_manual_cb(manifest["xhs"], str(manifest_path))
                elif xhs_page is None:
                    manifest["status_by_target"]["xhs"] = "failed"
                    manifest["errors"].append("xhs browser not available")
                    all_succeeded = False
                else:
                    from xhs.support import create_xhs_post as _create_xhs_post
                    xhs_image_paths = [Path(p) for p in manifest["xhs"]["clean_copy_paths"]]
                    xhs_url = None
                    try:
                        xhs_url = _create_xhs_post(
                            xhs_page,
                            manifest["xhs"],
                            xhs_image_paths,
                            args.delay,
                            settings=xhs_settings,
                            log_dir=LOG_DIR,
                            cancel_event=_cancel_ev,
                        )
                    except InterruptedError:
                        cancel_requested = True
                        xhs_url = None
                    except Exception as exc:
                        log.error(f"    xhs 发布异常: {exc}")
                        log.debug(traceback.format_exc())
                        xhs_url = None
                    if xhs_url:
                        manifest["xhs"]["post_url"] = xhs_url
                        manifest["status_by_target"]["xhs"] = "success"
                        log.info(f"    xhs 发布成功: {xhs_url}")
                    elif cancel_requested and manifest["status_by_target"].get("xhs") == "pending":
                        manifest["status_by_target"]["xhs"] = "canceled"
                        manifest["errors"].append("xhs upload canceled")
                        all_succeeded = False
                    else:
                        manifest["status_by_target"]["xhs"] = "failed"
                        manifest["errors"].append("xhs upload failed")
                        all_succeeded = False

            write_manifest(manifest_path, manifest)

            for target in effective_targets:
                status = manifest["status_by_target"].get(target)
                if status in {"success", "skipped_already_done", "skipped_civitai_safety", "maybe_posted"}:
                    target_success_counts[target] += 1
                elif status == "failed":
                    target_fail_counts[target] += 1

            _ok_or_manual = {"success", "skipped_already_done", "skipped_civitai_safety", "maybe_posted", "skipped_max_age", "manual_ready"}
            only_manual_pending = (
                not all_succeeded
                and all(manifest["status_by_target"].get(t) in _ok_or_manual for t in effective_targets)
            )

            if all_succeeded:
                dest = move_to_done(orig_path)
                log.info(f"    已移动到: {dest.name}")
                success_count += 1
                consecutive_failures = 0
            elif only_manual_pending:
                success_count += 1
                consecutive_failures = 0
            else:
                target_summaries = []
                for target in effective_targets:
                    status = manifest["status_by_target"].get(target, "pending")
                    if status == "success":
                        target_summaries.append(f"{target} 成功")
                    elif status == "skipped_already_done":
                        target_summaries.append(f"{target} 已发过")
                    elif status == "skipped_civitai_safety":
                        target_summaries.append(f"{target} 安全过滤跳过")
                    elif status == "manual_ready":
                        target_summaries.append(f"{target} 待手动发布")
                    elif status == "failed":
                        target_summaries.append(f"{target} 失败")
                    else:
                        target_summaries.append(f"{target} {status}")
                log.error(f"    {'，'.join(target_summaries)}，文件保留在 upload/")
                fail_count += 1
                _ok_statuses = {"success", "skipped_already_done", "skipped_civitai_safety", "maybe_posted", "skipped_max_age", "manual_ready"}
                core_targets = [t for t in effective_targets if t != "xhs"]
                core_any_ok = any(manifest["status_by_target"].get(t) in _ok_statuses for t in core_targets) if core_targets else False
                if core_any_ok:
                    consecutive_failures = 0
                else:
                    consecutive_failures += 1
                if consecutive_failures >= abort_threshold:
                    log.error(f"\n连续 {consecutive_failures} 张失败，中断本次批次（避免触发风控）")
                    break

        if args.dry_run:
            log.info(f"\n完成。dry-run 样本 {success_count}，未实际发布。")
        else:
            log.info(f"\n完成。双站都成功 {success_count}，未全部成功 {fail_count}。")
            for target in all_processed_targets:
                log.info(
                    f"  {target}: 成功 {target_success_counts.get(target, 0)}，"
                    f"失败 {target_fail_counts.get(target, 0)}"
                )
    finally:
        if civitai_context is not None:
            try:
                civitai_context.close()
            except Exception:
                pass
        if pixiv_context is not None:
            try:
                pixiv_context.close()
            except Exception:
                pass
        if x_context is not None:
            try:
                x_context.close()
            except Exception:
                pass
        if xhs_browser is not None:
            try:
                xhs_browser.close()
            except Exception:
                pass
        elif xhs_context is not None:
            try:
                xhs_context.close()
            except Exception:
                pass
        if playwright is not None:
            playwright.stop()
        shutil.rmtree(temp_dir, ignore_errors=True)


def cmd_pixiv_fit_collect(args):
    files = ensure_runtime_files(SCRIPT_DIR)
    alias_data = load_json(files["aliases"], {})

    log.info(
        f"Pixiv 样本采集开始：目标 {args.target_count} 张，流量门槛 "
        f"bookmark>={args.min_bookmarks} 或 like>={args.min_likes} 或 view>={args.min_views}"
    )

    with sync_playwright() as pw:
        context, page = open_pixiv_browser(pw, profile_dir=PIXIV_RULE_FIT_PROFILE_DIR)
        try:
            result = collect_rule_fit_sample_manifests(
                context=context,
                page=page,
                sample_dir=files["rule_fit_samples"],
                manifest_dir=files["rule_fit_manifests"],
                alias_data=alias_data,
                target_count=args.target_count,
                per_source_limit=args.per_source_limit,
                min_bookmarks=args.min_bookmarks,
                min_likes=args.min_likes,
                min_views=args.min_views,
                min_score=args.min_score,
                min_original=args.min_original,
                min_fanart=args.min_fanart,
                min_r18=args.min_r18,
            )
        finally:
            context.close()

    report_path = create_rule_fit_report_path(files["rule_fit_reports"], "collect")
    save_json(report_path, result["stats"])
    log.info(
        f"采集完成：本轮处理 {result['stats']['processed_count']} 张，"
        f"累计有效样本 {result['stats']['effective_count']} 张，统计已写入 {report_path.name}"
    )


def cmd_pixiv_fit_compare(args):
    files = ensure_runtime_files(SCRIPT_DIR)
    alias_data = load_json(files["aliases"], {})
    popularity_data = load_json(files["popularity"], {})
    age_rules = load_json(files["age_rules"], {})
    jp_alias_cache = load_json(files["jp_aliases"], {})
    general_jp_data = load_json(files["general_jp"], {})
    danbooru_jp_map = load_json(files["danbooru_jp"], {})
    if danbooru_jp_map:
        general_jp_data["_danbooru_map"] = danbooru_jp_map
    metadata_bridge, tagger_bridge = _make_bridges()

    result = compare_rule_fit_samples(
        manifest_dir=files["rule_fit_manifests"],
        alias_data=alias_data,
        popularity_data=popularity_data,
        age_rules=age_rules,
        metadata_bridge=metadata_bridge,
        tagger_bridge=tagger_bridge,
        jp_alias_cache=jp_alias_cache,
        general_jp_data=general_jp_data,
        live_lookup=not args.no_live_lookup,
    )
    save_json(files["jp_aliases"], jp_alias_cache)
    save_json(files["popularity"], popularity_data)
    report_path = create_rule_fit_report_path(files["rule_fit_reports"], "compare")
    save_json(report_path, result)
    log.info(
        f"对比完成：sidecar {result['count']} 份，完整 compare {result['compared_count']} 张，"
        f"缺图跳过 {result['skipped_image_missing_count']} 张，摘要写入 {report_path.name}"
    )


def cmd_pixiv_fit_report(args):
    files = ensure_runtime_files(SCRIPT_DIR)
    compare_paths = sorted(
        path for path in files["rule_fit_manifests"].iterdir()
        if path.is_file() and path.name.endswith(".compare.json")
    )
    if not compare_paths:
        log.info("没有找到 compare sidecar，请先运行 pixiv-fit-compare。")
        return

    compare_results = [load_json(path, {}) for path in compare_paths]
    report = summarize_rule_fit_report(compare_results)
    json_path = create_rule_fit_report_path(files["rule_fit_reports"], "summary")
    md_path = json_path.with_suffix(".md")
    save_json(json_path, report)
    md_path.write_text(render_rule_fit_report_markdown(report), encoding="utf-8")
    log.info(f"汇总报告已写入：{json_path.name} / {md_path.name}")


def main():
    setup_logging()
    log.info(f"=== 启动 civitai_splitter {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ===")

    prune_logs()
    cleanup_done_dir()
    migrate_progress_files()

    parser = argparse.ArgumentParser(description="Civitai Post Splitter & Uploader")
    subparsers = parser.add_subparsers(dest="command", required=True)

    sp_split = subparsers.add_parser("split", help="拆分已发布的多图 post")
    sp_split.add_argument("posts", nargs="*", help="Post ID 或 URL（支持多个，不填则交互输入）")
    sp_split.add_argument("--api-key", help="Civitai API key")
    sp_split.add_argument("--delay", type=float, default=10, help="每个 post 间隔秒数（默认10）")

    sp_upload = subparsers.add_parser("upload", help="批量上传 upload/ 目录的图片")
    sp_upload.add_argument("--delay", type=float, default=10, help="每个 post 间隔秒数（默认10）")
    sp_upload.add_argument("--targets", default="civitai", help="发布目标，逗号分隔：civitai,pixiv,x,xhs")
    sp_upload.add_argument("--dry-run", action="store_true", help="只生成 manifest 和清洗副本，不实际发布")
    sp_upload.add_argument("--pixiv-privacy", default="public", choices=["public", "logged_in", "mypixiv", "private"])
    sp_upload.add_argument("--pixiv-allow-tag-edits", default="false", help="Pixiv 是否允许他人编辑标签（true/false）")
    sp_upload.add_argument("--pixiv-max-retries", type=int, default=1, help="Pixiv 失败重试次数（默认 1，publish 已点击则不重试）")
    sp_upload.add_argument("--abort-after-failures", type=int, default=3, help="连续失败 N 张后中断批次，避免触发风控（默认 3）")
    sp_upload.add_argument("--llm-reverse", action="store_true", help="用 LLM 为 Pixiv 生成标题和简介")
    sp_upload.add_argument("--llm-persona", default="", help="LLM 人设 ID")
    sp_upload.add_argument("--llm-account", default="", help="LLM 账号 ID")
    sp_upload.add_argument("--llm-content-mode", default="", choices=["", "sfw", "nsfw"], help="LLM 文案模式")
    sp_upload.add_argument("--x-template", default="", choices=["", "jp_sfw", "en_sfw", "zh_sfw", "jp_nsfw", "en_nsfw", "zh_nsfw"], help="X 模板（默认 en_sfw；r18/r18g 自动切到 *_nsfw）")
    sp_upload.add_argument("--x-group", type=int, default=1, choices=[1, 2, 3, 4], help="X 多图组队大小（1=每图单推；2-4=按文件名相邻组队，一条推挂多图）")
    sp_upload.add_argument("--xhs-template", default="", help="小红书模板（默认 default）")
    sp_upload.add_argument("--xhs-manual", action="store_true", default=False, dest="xhs_manual_mode",
                           help="小红书手动模式：只生成内容，不启动浏览器")
    sp_upload.add_argument("--no-ai-tags", default="", nargs="?", const="all",
                           help="不打 AI 标签。不带值=全部平台；带值=指定平台（逗号分隔，如 pixiv,x）")
    sp_upload.add_argument("--count", type=int, default=0, help="本次发几张（默认 0 = 随机 1-5）")
    sp_upload.add_argument(
        "--sort", default="random",
        choices=["random", "name_asc", "name_desc", "time_asc", "time_desc"],
        help="选图排序（默认 random）",
    )

    sp_collect = subparsers.add_parser("pixiv-fit-collect", help="采集 Pixiv 规则拟合样本")
    sp_collect.add_argument("--target-count", type=int, default=50, help="目标样本数（默认50）")
    sp_collect.add_argument("--per-source-limit", type=int, default=40, help="每个入口最多抓取的作品数（默认40）")
    sp_collect.add_argument("--min-bookmarks", type=int, default=800, help="高流量最低书签数（默认800）")
    sp_collect.add_argument("--min-likes", type=int, default=200, help="高流量最低爱心数（默认200）")
    sp_collect.add_argument("--min-views", type=int, default=12000, help="高流量最低浏览量（默认12000）")
    sp_collect.add_argument("--min-score", type=float, default=12000, help="综合流量分最低值（默认12000）")
    sp_collect.add_argument("--min-original", type=int, default=15, help="原创样本最少数量（默认15）")
    sp_collect.add_argument("--min-fanart", type=int, default=15, help="二创样本最少数量（默认15）")
    sp_collect.add_argument("--min-r18", type=int, default=10, help="R-18/R-18G 样本最少数量（默认10）")

    sp_compare = subparsers.add_parser("pixiv-fit-compare", help="对 Pixiv 拟合样本执行本地检测与差异对比")
    sp_compare.add_argument("--no-live-lookup", action="store_true", help="不实时刷新 Pixiv 标签热度缓存")

    subparsers.add_parser("pixiv-fit-report", help="汇总样本对比结果并输出报告")

    args = parser.parse_args()
    try:
        if args.command == "split":
            cmd_split(args)
        elif args.command == "upload":
            cmd_upload(args)
        elif args.command == "pixiv-fit-collect":
            cmd_pixiv_fit_collect(args)
        elif args.command == "pixiv-fit-compare":
            cmd_pixiv_fit_compare(args)
        elif args.command == "pixiv-fit-report":
            cmd_pixiv_fit_report(args)
    except KeyboardInterrupt:
        log.info("\n用户中断。")
    except Exception as exc:
        log.error(f"\n致命错误: {exc}")
        log.debug(traceback.format_exc())
        raise

    log.info("=== 结束 ===")


if __name__ == "__main__":
    main()
