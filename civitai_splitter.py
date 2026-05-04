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
from playwright.sync_api import sync_playwright

from pixiv.censor import CensorEngine, DEFAULT_CENSOR_CLASSES, parse_class_set
from pixiv.support import (
    HainTagBridge,
    HainTagTaggerBridge,
    append_validation_case,
    build_pixiv_payload,
    collect_rule_fit_sample_manifests,
    compare_rule_fit_samples,
    create_manifest_path,
    create_rule_fit_report_path,
    create_pixiv_post,
    find_target_successes,
    open_pixiv_browser,
    PIXIV_RULE_FIT_PROFILE_DIR,
    summarize_rule_fit_report,
    ensure_runtime_files,
    load_json,
    sanitize_image_for_pixiv,
    save_json,
    write_manifest,
)

SCRIPT_DIR = Path(__file__).parent.resolve()
UPLOAD_DIR = SCRIPT_DIR / "upload"
DONE_DIR = SCRIPT_DIR / "done"
LOG_DIR = SCRIPT_DIR / "logs"
PROGRESS_DIR = SCRIPT_DIR / "progress"
TMP_DIR = SCRIPT_DIR / ".tmp"
CHROME_PROFILE_DIR = Path.home() / ".civitai_splitter_chrome"
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
CIVITAI_BASE = "https://civitai.com"
CIVITAI_API = "https://civitai.com/api/v1"
DONE_DAYS = 7
_LORA_RE = re.compile(r"<lora:([^:>]+):([^>]+)>")
TARGETS = {"civitai", "pixiv"}

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
        dest = dest_dir / image_path.name
        shutil.copy2(str(image_path), str(dest))
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
        parts.append(", ".join(f"<lora:{name}:{weight}>" for name, weight in lora_tags))
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
        args=["--disable-blink-features=AutomationControlled", "--window-position=-32000,-32000"],
        ignore_default_args=["--enable-automation"],
    )
    page = context.pages[0] if context.pages else context.new_page()
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


def create_civitai_post(page, image_path: Path, delay: float) -> str | None:
    ensure_on_create_page(page)

    # Wait for file input to appear — safe_goto uses wait_until="commit" which
    # only waits for response headers, so the DOM may still be loading.
    file_input = None
    for _ in range(12):
        loc = page.locator('input[type="file"]')
        if loc.count() > 0:
            file_input = loc.first
            break
        time.sleep(1)
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
        time.sleep(2)
        if publish_btn.count() > 0 and publish_btn.first.is_enabled():
            enabled = True
            break

    if not enabled:
        log.error("    Publish 按钮未启用（等待 120 秒），跳过")
        return None

    publish_btn.first.click()
    log.info("    已点击 Publish，等待跳转...")

    for _ in range(30):
        time.sleep(2)
        current_url = page.url
        if "/posts/create" not in current_url and "/posts/" in current_url:
            post_url = re.sub(r"/edit$", "", current_url)
            wait = delay + random.uniform(1, 3)
            time.sleep(wait)
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
) -> tuple[dict, bool]:
    source_meta = hain_bridge.read_metadata(image_path)
    civitai_copy = strip_prompts_keep_lora(image_path, civitai_dir) if "civitai" in targets else None
    pixiv_clean = sanitize_image_for_pixiv(image_path, pixiv_dir) if "pixiv" in targets else None

    # Run auto-censor on the sanitized pixiv copy if engine present.
    censor_result = None
    if pixiv_clean is not None and censor_engine is not None:
        censor_result = censor_engine.detect_and_censor(
            Path(pixiv_clean.output_path),
            output_path=Path(pixiv_clean.output_path),
            enabled_classes=censor_classes,
        )
        if censor_result.applied:
            log.info(f"    censor: 打码完成 — {censor_result.detail}")
        elif censor_result.status == "ok":
            log.info(f"    censor: 无需打码 — {censor_result.detail}")
        # other statuses already logged by engine

    pixiv_metadata_check = (
        hain_bridge.read_metadata(pixiv_clean.output_path) if pixiv_clean is not None
        else {"status": "skipped", "detected_types": [], "details": []}
    )
    if "pixiv" in targets and tagger_bridge is not None:
        tagger_result = tagger_bridge.predict_tags(image_path)
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
        )
        if "pixiv" in targets else None
    )
    if pixiv_payload is not None:
        pixiv_payload["privacy"] = pixiv_privacy
        pixiv_payload["allow_tag_edits"] = pixiv_allow_tag_edits
        # If censor applied mosaic, this image contains exposed genitals/fluids —
        # force R-18 (overrides any filename-based inference that returned all_ages).
        if censor_result is not None and censor_result.applied:
            if pixiv_payload.get("age_restriction") not in {"r18", "r18g"}:
                log.info("    censor: 检测到露出，强制 age_restriction=r18")
                pixiv_payload["age_restriction"] = "r18"

    manifest = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "source_path": str(image_path),
        "targets": targets,
        "dry_run": False,
        "status_by_target": {target: "pending" for target in targets},
        "errors": [],
        "civitai": {
            "clean_copy_path": str(civitai_copy) if civitai_copy else "",
            "post_url": "",
        },
        "pixiv": {
            "clean_copy_path": str(pixiv_clean.output_path) if pixiv_clean else "",
            "metadata_check": {
                "status": pixiv_metadata_check.get("status", "skipped"),
                "detected_types": pixiv_metadata_check.get("detected_types", []),
                "details": pixiv_metadata_check.get("details", []),
            },
            "raw_candidates": pixiv_payload["raw_candidates"] if pixiv_payload else [],
            "popularity_decisions": pixiv_payload["popularity_decisions"] if pixiv_payload else [],
            "final_tags": pixiv_payload["final_tags"] if pixiv_payload else [],
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
            "tagger": {
                "status": tagger_result.get("status", "disabled"),
                "available": tagger_result.get("available", False),
                "top_tags": list(tagger_result.get("flat_tags", []))[:30],
            },
            "censor": censor_result.to_dict() if censor_result is not None else {"status": "disabled", "applied": False},
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
            pixiv_ready = False
            manifest["status_by_target"]["pixiv"] = "failed"
            manifest["errors"].append("HainTag metadata validator unavailable")
        elif status != "clean":
            pixiv_ready = False
            manifest["status_by_target"]["pixiv"] = "failed"
            manifest["errors"].append(
                f"Pixiv clean copy metadata validation failed: {status} {pixiv_metadata_check.get('details', [])}"
            )
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

            log.info("  打开浏览器...")
            with sync_playwright() as pw:
                context, page = open_civitai_browser(pw)
                for idx, (img, local_path) in enumerate(zip(remaining_images, local_paths), 1):
                    log.info(f"\n  [{idx}/{len(remaining_images)}] 上传图片 {img['id']}...")
                    try:
                        new_url = create_civitai_post(page, local_path, args.delay)
                    except Exception as exc:
                        log.error(f"    图片 {img['id']} 发布异常: {exc}")
                        log.debug(traceback.format_exc())
                        new_url = None
                    if new_url:
                        progress["completed"].append({"image_id": img["id"], "new_post_url": new_url})
                    else:
                        progress["failed"].append({"image_id": img["id"], "error": "发布失败"})
                    save_progress(progress_path, progress)
                context.close()
        except Exception as exc:
            log.error(f"  Post {post_id} 处理异常: {exc}")
            log.debug(traceback.format_exc())
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)
        log.info(f"\n  Post {post_id} 完成: {len(progress['completed'])} 成功, {len(progress['failed'])} 失败")


def cmd_upload(args):
    files = ensure_runtime_files(SCRIPT_DIR)
    alias_data = load_json(files["aliases"], {})
    popularity_data = load_json(files["popularity"], {})
    age_rules = load_json(files["age_rules"], {})
    hain_bridge = HainTagBridge(SCRIPT_DIR.parent / "haintag")
    tagger_bridge = HainTagTaggerBridge(SCRIPT_DIR.parent / "haintag")
    jp_alias_cache = load_json(files["jp_aliases"], {})
    general_jp_data = load_json(files["general_jp"], {})
    danbooru_jp_map = load_json(files["danbooru_jp"], {})
    if danbooru_jp_map:
        general_jp_data["_danbooru_map"] = danbooru_jp_map
        log.info(f"Danbooru→JP 词典已加载: {len(danbooru_jp_map)} 条")

    UPLOAD_DIR.mkdir(exist_ok=True)
    DONE_DIR.mkdir(exist_ok=True)

    all_images = sorted(
        file for file in UPLOAD_DIR.iterdir()
        if file.is_file() and file.suffix.lower() in IMAGE_EXTENSIONS
    )
    if not all_images:
        log.info(f"upload/ 目录没有图片。把图片放到：\n  {UPLOAD_DIR}")
        return

    targets = parse_targets(args.targets)

    # Auto-censor: model file at models/auto_censor.pt opts in. Config tunables
    # live in pixiv_censor.json (auto-created on first run with defaults).
    censor_engine = None
    censor_classes = DEFAULT_CENSOR_CLASSES
    if "pixiv" in targets:
        model_path = SCRIPT_DIR / "models" / "auto_censor.pt"
        if model_path.exists():
            cfg = load_json(files["censor_config"], {})
            mode = cfg.get("mode", "mosaic")
            conf = float(cfg.get("conf_threshold", 0.45))
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
    selected_names = getattr(args, "files", None) or []
    if selected_names:
        name_lower = {n.lower() for n in selected_names}
        image_files = [f for f in all_images if f.name.lower() in name_lower]
        if not image_files:
            log.warning("指定的文件不在 upload/ 目录，改用随机选取")
            image_files = random.sample(all_images, min(random.randint(1, 5), len(all_images)))
        mode_desc = f"指定 {len(image_files)} 张"
    else:
        requested = max(0, int(args.count or 0))
        if requested > 0:
            count = min(requested, len(all_images))
            mode_desc = f"按指定数量选 {count} 张"
        else:
            count = min(random.randint(1, 5), len(all_images))
            mode_desc = f"随机选 {count} 张"
        image_files = random.sample(all_images, count)
    log.info(f"upload/ 有 {len(all_images)} 张图片，本次{mode_desc}上传。目标：{targets}\n")

    temp_dir = make_temp_dir("civitai_upload_")
    civitai_dir = temp_dir / "civitai"
    pixiv_dir = temp_dir / "pixiv"
    civitai_dir.mkdir(exist_ok=True)
    pixiv_dir.mkdir(exist_ok=True)

    civitai_context = pixiv_context = None
    civitai_page = pixiv_page = None
    success_count = 0
    fail_count = 0
    consecutive_failures = 0
    abort_threshold = max(1, int(args.abort_after_failures))
    playwright = None
    target_success_counts = {target: 0 for target in targets}
    target_fail_counts = {target: 0 for target in targets}

    try:
        if not args.dry_run and any(target in targets for target in ("civitai", "pixiv")):
            playwright = sync_playwright().start()
        if playwright is not None and "civitai" in targets:
            civitai_context, civitai_page = open_civitai_browser(playwright)
        if playwright is not None and "pixiv" in targets:
            pixiv_context, pixiv_page = open_pixiv_browser(playwright)

        _cancel_ev = getattr(args, "cancel_event", None)
        for index, orig_path in enumerate(image_files, 1):
            if _cancel_ev and _cancel_ev.is_set():
                log.info("收到取消信号，停止上传")
                break
            log.info(f"[{index}/{len(image_files)}] {orig_path.name}")
            prior_successes = find_target_successes(files["manifests"], orig_path)
            skip_targets = {t for t in targets if t in prior_successes}
            if skip_targets:
                log.info(
                    f"    跳过已成功目标: {sorted(skip_targets)}（继承历史 post_url）"
                )
            manifest_path = create_manifest_path(files["manifests"], orig_path)
            manifest, pixiv_ready = create_upload_manifest(
                image_path=orig_path,
                targets=targets,
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
            )
            # Persist any new JP aliases learned during this image's payload build
            save_json(files["jp_aliases"], jp_alias_cache)
            tagger_status = manifest.get("pixiv", {}).get("tagger", {}).get("status", "disabled")
            if "pixiv" in targets and tagger_status not in {"ok", "disabled"}:
                log.warning(f"    tagger 不可用: {tagger_status}（继续上传，仅用 prompt/文件名候选）")
            manifest["dry_run"] = bool(args.dry_run)
            write_manifest(manifest_path, manifest)
            if "pixiv" in targets:
                save_json(files["popularity"], popularity_data)

            if args.dry_run:
                for target in targets:
                    if manifest["status_by_target"].get(target) == "pending":
                        manifest["status_by_target"][target] = "dry_run"
                write_manifest(manifest_path, manifest)
                log.info("    dry-run 完成，未执行发布。")
                success_count += 1
                continue

            all_succeeded = True

            if "civitai" in targets:
                if "civitai" in skip_targets:
                    inherited_url = prior_successes["civitai"]
                    manifest["civitai"]["post_url"] = inherited_url
                    manifest["status_by_target"]["civitai"] = "skipped_already_done"
                    log.info(f"    Civitai 已发过，跳过: {inherited_url}")
                else:
                    civitai_copy = Path(manifest["civitai"]["clean_copy_path"])
                    try:
                        civitai_url = create_civitai_post(civitai_page, civitai_copy, args.delay)
                    except Exception as exc:
                        log.error(f"    Civitai 发布异常: {exc}")
                        log.debug(traceback.format_exc())
                        civitai_url = None
                    if civitai_url:
                        manifest["civitai"]["post_url"] = civitai_url
                        manifest["status_by_target"]["civitai"] = "success"
                        log.info(f"    Civitai 发布成功: {civitai_url}")
                    else:
                        manifest["status_by_target"]["civitai"] = "failed"
                        manifest["errors"].append("Civitai upload failed")
                        all_succeeded = False

            if "pixiv" in targets:
                if "pixiv" in skip_targets:
                    inherited_url = prior_successes["pixiv"]
                    manifest["pixiv"]["post_url"] = inherited_url
                    manifest["status_by_target"]["pixiv"] = "skipped_already_done"
                    log.info(f"    Pixiv 已发过，跳过: {inherited_url}")
                elif not pixiv_ready:
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
                                pixiv_page, payload, pixiv_copy, args.delay, log_dir=LOG_DIR,
                            )
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
                        if attempt < max_retries:
                            log.info(f"    Pixiv 失败，{(attempt + 1) * 3} 秒后重试 ({attempt + 2}/{max_retries + 1})...")
                            time.sleep((attempt + 1) * 3)
                    manifest["pixiv"]["upload_steps"] = [s.to_dict() for s in pixiv_steps]
                    if pixiv_url:
                        manifest["pixiv"]["post_url"] = pixiv_url
                        manifest["status_by_target"]["pixiv"] = "success"
                        log.info(f"    Pixiv 发布成功: {pixiv_url}")
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

            write_manifest(manifest_path, manifest)

            for target in targets:
                status = manifest["status_by_target"].get(target)
                if status in {"success", "skipped_already_done"}:
                    target_success_counts[target] += 1
                elif status == "failed":
                    target_fail_counts[target] += 1

            if all_succeeded:
                dest = move_to_done(orig_path)
                log.info(f"    已移动到: {dest.name}")
                success_count += 1
                consecutive_failures = 0
            else:
                target_summaries = []
                for target in targets:
                    status = manifest["status_by_target"].get(target, "pending")
                    if status == "success":
                        target_summaries.append(f"{target} 成功")
                    elif status == "skipped_already_done":
                        target_summaries.append(f"{target} 已发过")
                    elif status == "failed":
                        target_summaries.append(f"{target} 失败")
                    else:
                        target_summaries.append(f"{target} {status}")
                log.error(f"    {'，'.join(target_summaries)}，文件保留在 upload/")
                fail_count += 1
                consecutive_failures += 1
                if consecutive_failures >= abort_threshold:
                    log.error(f"\n连续 {consecutive_failures} 张失败，中断本次批次（避免触发风控）")
                    break

        if args.dry_run:
            log.info(f"\n完成。dry-run 样本 {success_count}，未实际发布。")
        else:
            log.info(f"\n完成。双站都成功 {success_count}，未全部成功 {fail_count}。")
            for target in targets:
                log.info(
                    f"  {target}: 成功 {target_success_counts.get(target, 0)}，"
                    f"失败 {target_fail_counts.get(target, 0)}"
                )
    finally:
        if civitai_context is not None:
            civitai_context.close()
        if pixiv_context is not None:
            pixiv_context.close()
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
    metadata_bridge = HainTagBridge(SCRIPT_DIR.parent / "haintag")
    tagger_bridge = HainTagTaggerBridge(SCRIPT_DIR.parent / "haintag")

    result = compare_rule_fit_samples(
        manifest_dir=files["rule_fit_manifests"],
        alias_data=alias_data,
        popularity_data=popularity_data,
        age_rules=age_rules,
        metadata_bridge=metadata_bridge,
        tagger_bridge=tagger_bridge,
        live_lookup=not args.no_live_lookup,
    )
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
    sp_upload.add_argument("--targets", default="civitai", help="发布目标，逗号分隔：civitai,pixiv")
    sp_upload.add_argument("--dry-run", action="store_true", help="只生成 manifest 和清洗副本，不实际发布")
    sp_upload.add_argument("--pixiv-privacy", default="public", choices=["public", "logged_in", "mypixiv", "private"])
    sp_upload.add_argument("--pixiv-allow-tag-edits", default="false", help="Pixiv 是否允许他人编辑标签（true/false）")
    sp_upload.add_argument("--pixiv-max-retries", type=int, default=1, help="Pixiv 失败重试次数（默认 1，publish 已点击则不重试）")
    sp_upload.add_argument("--abort-after-failures", type=int, default=3, help="连续失败 N 张后中断批次，避免触发风控（默认 3）")
    sp_upload.add_argument("--count", type=int, default=0, help="本次发几张（默认 0 = 随机 1-5）")

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
