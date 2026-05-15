"""小红书 (xiaohongshu / xhs) publishing support.

Mirrors x/support.py shape:
  - load_xhs_* config helpers
  - pure transforms: pick_xhs_tags, build_xhs_payload, prepare_xhs_image
  - browser layer (Playwright, persistent profile): open_xhs_browser, create_xhs_post

NSFW images never reach this module — civitai_splitter drops xhs from targets
when age_restriction is r18/r18g (PLATFORM_RULES max_age = all_ages).
"""

from __future__ import annotations

import json
import logging
import random
import re
import time
import traceback
from pathlib import Path
from typing import Any, Iterable

from PIL import Image

log = logging.getLogger("civitai_splitter")

XHS_DIR = Path(__file__).parent
XHS_BASE = "https://www.xiaohongshu.com"
# Web 版发布入口；creator.xiaohongshu.com 改版频繁，先用 web 端 explore 的发布。
XHS_PUBLISH_URL = "https://creator.xiaohongshu.com/publish/publish?source=official"
XHS_PROFILE_DIR = Path.home() / ".civitai_splitter_xhs_chrome"

DEFAULT_TEMPLATES = {
    "default": {"core": "#AI绘画", "lang": "zh", "social": "#治愈系插画"},
}

DEFAULT_SETTINGS = {
    "title_max_chars": 20,
    "caption_max_chars": 1000,
    # Per 2025-2026 xhs research: 3-5 topics is the sweet spot (vertical +
    # related-trending + niche mix). Hard cap at 5; >10 risks "tag spam" demote.
    "tag_limit": 5,
    "image_long_side_max": 2160,
    "image_count_max": 18,
    "publish_timeout_sec": 120,
    "post_delay_sec": 8,
    # Same-account safety threshold: xhs throttles accounts that post >3
    # notes/day at <30min intervals.
    "min_interval_seconds": 1800,
    "auto_append_ai_tag": True,
    "ai_declaration_tag": "#AI创作",
    # GB45438-2025 (effective 2025-09-01) requires AI content to tick the
    # publisher's "AI synthesised content" declaration. Hashtag alone is NOT
    # compliance — algo auto-flags + demotes if missing.
    "auto_check_ai_declaration": True,
    "auto_declare_original": True,
    "recommended_tag_count": 3,
    "topic_dropdown_wait_sec": 3,
    "default_template": "default",
    "jpg_quality": 90,
    "png_size_threshold_mb": 8,
}

# Selectors confirmed at run time. xhs creator UI uses Element Plus
# components; class names rotate but role/aria/data-* attrs are more stable.
XHS_SELECTORS = {
    # Tab to switch from video → image-text mode (default is video).
    "image_text_tab": 'div.creator-tab:has-text("上传图文"), [role="tab"]:has-text("上传图文")',
    "file_input": 'input[type="file"]',
    "title_input": 'input[placeholder*="标题"], input[placeholder*="title" i]',
    "body_editor": '[contenteditable="true"], .editor-content, textarea[placeholder*="描述"]',
    "publish_button": 'button:has-text("发布")',
    # Topic suggestion dropdown shown after typing `#`. Must click the first
    # option for the topic to be registered as a real topic (not plain text).
    "topic_option": (
        '.publish-topic-item, .topic-suggest-item,'
        ' [class*="topic"][class*="item"],'
        ' [class*="suggest"][class*="item"],'
        ' [class*="mention"][class*="item"],'
        ' [class*="topic-list"] > *, [class*="suggest-list"] > *,'
        ' [role="option"]'
    ),
    # GB45438-2025: 内容类型声明面板需先展开，再点"笔记含AI合成内容"选项
    "ai_declaration_expand": 'div:has-text("添加内容类型声明"), [class*="content-type"]:has-text("添加")',
    "ai_declaration_ai_content": '[class*="item"]:has-text("笔记含AI合成内容"), li:has-text("笔记含AI合成内容")',
    # Recommended tag chips shown below editor after content is typed
    "recommended_tag_chip": 'span.tag[data-impression*="recommend"]',
}


# ----- config IO --------------------------------------------------------------


def _load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except Exception as exc:
        log.warning(f"xhs: failed to read {path.name}: {exc}; using default")
        return default


def load_xhs_settings(root: Path | None = None) -> dict[str, Any]:
    base = root or XHS_DIR
    raw = _load_json(base / "xhs_settings.json", {})
    merged = dict(DEFAULT_SETTINGS)
    if isinstance(raw, dict):
        merged.update({k: v for k, v in raw.items() if v is not None})
    return merged


def load_xhs_templates(root: Path | None = None) -> dict[str, dict[str, Any]]:
    base = root or XHS_DIR
    raw = _load_json(base / "xhs_templates.json", {})
    merged = {k: dict(v) for k, v in DEFAULT_TEMPLATES.items()}
    if isinstance(raw, dict):
        for k, v in raw.items():
            if not isinstance(v, dict):
                continue
            entry = merged.setdefault(k, {"core": "", "lang": "zh", "social": ""})
            entry.update(v)
            entry.setdefault("social", "")
    return merged


# ----- pure transforms --------------------------------------------------------


def normalize_hashtag(raw: str) -> str:
    """Normalize a hashtag for xhs. Strip leading #, collapse whitespace, re-add #."""
    if not raw:
        return ""
    cleaned = raw.strip().lstrip("#").strip()
    if not cleaned:
        return ""
    cleaned = re.sub(r"\s+", "", cleaned)
    return f"#{cleaned}"


def pick_xhs_tags(
    entity_tags: Iterable[str],
    template: dict[str, Any],
    ai_tag: str = "",
    limit: int = 3,
) -> dict[str, Any]:
    """Pick 1..limit hashtags for xhs.

    Rule:
      - have entity → [entity_top1, template.core, ai_tag]
      - no entity   → [template.core, template.social, ai_tag]
    `ai_tag` is the AI declaration tag (e.g. #AI创作), included if non-empty and
    there is room within `limit`.
    """
    picked: list[str] = []
    seen: set[str] = set()
    sources = {"character_tags": [], "template_tag": "", "social_tag": "", "ai_tag": ""}

    def _try_add(tag: str, bucket: str) -> bool:
        normalized = normalize_hashtag(tag)
        if not normalized or normalized.lower() in seen:
            return False
        if len(picked) >= limit:
            return False
        picked.append(normalized)
        seen.add(normalized.lower())
        if bucket == "character":
            sources["character_tags"].append(normalized)
        elif bucket == "core":
            sources["template_tag"] = normalized
        elif bucket == "social":
            sources["social_tag"] = normalized
        elif bucket == "ai":
            sources["ai_tag"] = normalized
        return True

    has_entity = False
    for ent in entity_tags or []:
        if _try_add(ent, "character"):
            has_entity = True
            break

    _try_add(template.get("core", ""), "core")

    if not has_entity:
        _try_add(template.get("social", ""), "social")

    if ai_tag:
        _try_add(ai_tag, "ai")

    return {"tags": picked, "sources": sources}


def _trim_to_chars(text: str, max_chars: int) -> str:
    if max_chars <= 0:
        return ""
    if len(text) <= max_chars:
        return text
    return text[: max(0, max_chars - 1)] + "…"


def build_xhs_body(caption: str, tags: list[str], max_chars: int) -> str:
    """Build the xhs body: caption + topic tags appended. Caps at max_chars."""
    caption = (caption or "").strip()
    tag_line = " ".join(tags).strip()
    pieces = [p for p in (caption, tag_line) if p]
    full = "\n\n".join(pieces)
    if len(full) <= max_chars:
        return full
    # Overflow: keep tag line, trim caption
    if tag_line and len(tag_line) <= max_chars:
        remaining = max_chars - len(tag_line) - 2
        if remaining > 4 and caption:
            return f"{_trim_to_chars(caption, remaining)}\n\n{tag_line}"
        return tag_line
    return _trim_to_chars(tag_line or caption, max_chars)


def prepare_xhs_image(src: Path, out_dir: Path, *, long_side_max: int, png_size_threshold_mb: int, jpg_quality: int) -> Path:
    """Re-encode for xhs (strips EXIF / PNG text chunks). Long-side caps at
    long_side_max; large PNGs become JPGs."""
    out_dir.mkdir(parents=True, exist_ok=True)
    src = Path(src)
    suffix = src.suffix.lower()
    is_png = suffix == ".png"
    is_jpg = suffix in {".jpg", ".jpeg"}
    size_mb = src.stat().st_size / (1024 * 1024)

    with Image.open(src) as im:
        im.load()
        w, h = im.size
        long_side = max(w, h)
        if long_side > long_side_max:
            scale = long_side_max / long_side
            new_size = (max(1, int(w * scale)), max(1, int(h * scale)))
            im = im.resize(new_size, Image.LANCZOS)

        recode_to_jpg = is_jpg or (is_png and size_mb > png_size_threshold_mb)

        if recode_to_jpg:
            dst = out_dir / (src.stem + "_xhs.jpg")
            rgb = im.convert("RGB") if im.mode != "RGB" else im
            rgb.save(dst, format="JPEG", quality=jpg_quality, optimize=True)
            return dst

        dst = out_dir / (src.stem + "_xhs.png")
        from PIL import PngImagePlugin
        im.save(dst, format="PNG", optimize=True, pnginfo=PngImagePlugin.PngInfo())
        return dst


def build_xhs_payload(
    *,
    pixiv_payload: dict[str, Any] | None,
    image_path: Path,
    xhs_dir: Path,
    settings: dict[str, Any],
    templates: dict[str, dict[str, Any]],
    base_template: str | None = None,
    age_restriction: str = "all_ages",
    copy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble manifest['xhs'] for a single image. Pure transform.

    Reads localized title/caption from `copy` (manifest.copy universal area),
    falling back to pixiv_payload's title_zh / caption_zh. Tags merge: top
    entity (zh form if available, else passthrough) + template core + AI tag.
    """
    base = base_template or settings.get("default_template", "default")
    template = templates.get(base) or templates.get("default") or {"core": "#AI绘画", "lang": "zh", "social": "#AI插画"}

    entity_tags = (pixiv_payload or {}).get("entity_tags") or []
    ai_tag = settings.get("ai_declaration_tag", "#AI创作") if settings.get("auto_append_ai_tag", True) else ""
    tag_pick = pick_xhs_tags(
        entity_tags=entity_tags,
        template=template,
        ai_tag=ai_tag,
        limit=int(settings.get("tag_limit", 3)),
    )
    tags = tag_pick["tags"]

    copy_xhs = (copy or {}).get("xhs") or {}
    copy_title = (copy or {}).get("title") or {}
    copy_caption = (copy or {}).get("caption") or {}

    title = str(copy_xhs.get("title", "") or copy_title.get("zh", "") or "").strip()
    caption = str(copy_xhs.get("body", "") or copy_caption.get("zh", "") or "").strip()
    if not title:
        title = (pixiv_payload or {}).get("title_zh", "") or ""
    if not caption:
        caption = (pixiv_payload or {}).get("caption_zh", "") or ""

    # Drop placeholder titles that pixiv emits when no LLM reverse ran.
    if title.strip().lower() in {"無題", "无题", "untitled", ""}:
        title = ""

    title = _trim_to_chars(title.strip(), int(settings.get("title_max_chars", 20)))
    caption_clean = _trim_to_chars(
        caption.strip(),
        max(0, int(settings.get("caption_max_chars", 1000)) - sum(len(t) + 1 for t in tags))
    )
    body = build_xhs_body(caption, tags, int(settings.get("caption_max_chars", 1000)))

    out_image = prepare_xhs_image(
        Path(image_path),
        xhs_dir,
        long_side_max=int(settings.get("image_long_side_max", 2160)),
        png_size_threshold_mb=int(settings.get("png_size_threshold_mb", 8)),
        jpg_quality=int(settings.get("jpg_quality", 90)),
    )

    return {
        "clean_copy_paths": [str(out_image)],
        "title": title,
        # `caption_text` is the bare caption without topic hashtags appended,
        # so create_xhs_post can type it first then append each topic via the
        # dropdown selector (required for xhs to register them as real topics
        # rather than plain text).
        "caption_text": caption_clean,
        # `body` is the joined preview (for dry-run / debug / manifest display).
        "body": body,
        "tags": tags,
        "tag_sources": tag_pick["sources"],
        "template": base,
        "ai_declaration_required": bool(settings.get("auto_check_ai_declaration", True)),
        "group_id": None,
        "post_url": "",
    }


# ----- Playwright (browser layer) --------------------------------------------


def _load_cookies_into_context(context, cookies_path: Path) -> int:
    """Import Cookie-Editor JSON; same shape as x.support._load_cookies_into_context."""
    try:
        raw = json.loads(cookies_path.read_text(encoding="utf-8"))
    except Exception as exc:
        log.error(f"xhs: 读取 cookies.json 失败: {exc}")
        return 0
    if not isinstance(raw, list):
        log.error("xhs: cookies.json 应为数组，跳过")
        return 0
    same_site_map = {"no_restriction": "None", "none": "None", "lax": "Lax", "strict": "Strict"}
    cookies: list[dict[str, Any]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        value = entry.get("value")
        if not name or value is None:
            continue
        cookie: dict[str, Any] = {
            "name": str(name), "value": str(value),
            "domain": str(entry.get("domain") or ""), "path": str(entry.get("path") or "/"),
        }
        expiration = entry.get("expirationDate")
        if expiration is not None and not entry.get("session"):
            try:
                cookie["expires"] = int(float(expiration))
            except (TypeError, ValueError):
                pass
        if "httpOnly" in entry:
            cookie["httpOnly"] = bool(entry["httpOnly"])
        if "secure" in entry:
            cookie["secure"] = bool(entry["secure"])
        ss = same_site_map.get(str(entry.get("sameSite") or "").lower())
        if ss:
            cookie["sameSite"] = ss
        cookies.append(cookie)
    if not cookies:
        return 0
    try:
        context.add_cookies(cookies)
    except Exception as exc:
        log.error(f"xhs: context.add_cookies 失败: {exc}")
        return 0
    return len(cookies)


_STEALTH_INIT_JS = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
window.chrome = window.chrome || { runtime: {} };
Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh', 'en-US', 'en'] });
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
"""


def open_xhs_browser(pw, profile_dir: Path | None = None):
    """Launch persistent Chrome context for xhs."""
    target_profile = profile_dir or XHS_PROFILE_DIR
    context = pw.chromium.launch_persistent_context(
        str(target_profile),
        channel="chrome",
        headless=False,
        args=[
            "--start-maximized",
            "--disable-sync",
            "--no-first-run",
            "--disable-blink-features=AutomationControlled",
        ],
        ignore_default_args=["--enable-automation", "--no-sandbox"],
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
    )
    try:
        context.add_init_script(_STEALTH_INIT_JS)
    except Exception:
        pass

    cookies_path = XHS_DIR / "cookies.json"
    if cookies_path.exists():
        n = _load_cookies_into_context(context, cookies_path)
        if n > 0:
            log.info(f"xhs: 注入 {n} 条 cookies（来自 xhs/cookies.json）")

    page = context.pages[0] if context.pages else context.new_page()
    page.set_viewport_size({"width": 1920, "height": 1080})
    ensure_xhs_logged_in(page)
    return context, page


def ensure_xhs_logged_in(page) -> None:
    """Open publish page; if file input doesn't appear, block on manual login."""
    try:
        page.goto(XHS_PUBLISH_URL, wait_until="commit", timeout=30000)
    except Exception:
        pass
    for _ in range(8):
        time.sleep(1)
        try:
            if page.locator(XHS_SELECTORS["file_input"]).count() > 0:
                return
        except Exception:
            pass

    print()
    print("=" * 64)
    print("小红书 未登录（或 publish 页未加载完成）")
    print("请在弹出的 Chrome 窗口里登录小红书。")
    print("登录成功 + 能看到创作者中心后，回到这里按 Enter 继续...")
    print("=" * 64)
    try:
        input()
    except EOFError:
        pass
    try:
        page.goto(XHS_PUBLISH_URL, wait_until="commit", timeout=30000)
    except Exception:
        pass
    for _ in range(8):
        time.sleep(1)
        try:
            if page.locator(XHS_SELECTORS["file_input"]).count() > 0:
                return
        except Exception:
            pass
    log.warning("xhs: 登录后仍未找到文件上传输入框，继续，可能会失败")


def _raise_if_canceled(cancel_event) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise InterruptedError("task canceled")


def _sleep_with_cancel(sec: float, cancel_event) -> None:
    end = time.time() + sec
    while time.time() < end:
        _raise_if_canceled(cancel_event)
        time.sleep(min(0.5, max(0.0, end - time.time())))


def create_xhs_post(
    page,
    payload: dict[str, Any],
    image_paths: list[Path],
    delay: float,
    *,
    settings: dict[str, Any] | None = None,
    log_dir: Path | None = None,
    cancel_event=None,
) -> str | None:
    """Post one xhs note. Returns the note URL or None.

    NOTE: xhs creator UI is class-name-volatile and uses Element Plus + custom
    contenteditable rich-text editor. The selectors below are best-effort
    starting points — real-world testing will likely require tweaking
    XHS_SELECTORS in xhs/support.py.
    """
    settings = settings or DEFAULT_SETTINGS
    _raise_if_canceled(cancel_event)

    try:
        page.goto(XHS_PUBLISH_URL, wait_until="commit", timeout=30000)
    except Exception as exc:
        log.error(f"    xhs: publish 跳转失败: {exc}")
        return None
    _sleep_with_cancel(4, cancel_event)

    # 1. Switch to image-text tab (default is video)
    try:
        tab_loc = (
            page.locator('div.creator-tab:has-text("上传图文")')
            .or_(page.locator('[role="tab"]:has-text("上传图文")'))
            .or_(page.get_by_text("上传图文", exact=True))
        )
        clicked = False
        for i in range(tab_loc.count()):
            el = tab_loc.nth(i)
            try:
                if el.is_visible():
                    el.scroll_into_view_if_needed(timeout=3000)
                    el.click(timeout=5000)
                    clicked = True
                    break
            except Exception:
                continue
        if not clicked and tab_loc.count() > 0:
            tab_loc.first.click(force=True)
        if clicked or tab_loc.count() > 0:
            _sleep_with_cancel(2, cancel_event)
    except Exception as exc:
        log.warning(f"    xhs: 切换图文 tab 失败（可能已在图文模式）: {exc}")

    # 2. Upload images
    file_input = None
    for _ in range(12):
        _raise_if_canceled(cancel_event)
        loc = page.locator(XHS_SELECTORS["file_input"])
        if loc.count() > 0:
            file_input = loc.first
            break
        _sleep_with_cancel(1, cancel_event)
    if file_input is None:
        log.error("    xhs: 未找到文件上传输入框")
        return None
    try:
        file_input.set_input_files([str(p) for p in image_paths])
    except Exception as exc:
        log.error(f"    xhs: 文件上传失败: {exc}")
        log.debug(traceback.format_exc())
        return None

    # Wait for images to upload (heuristic: title input becomes available)
    _sleep_with_cancel(8, cancel_event)

    # 3. Fill title
    if payload.get("title"):
        try:
            title_input = page.locator(XHS_SELECTORS["title_input"]).first
            title_input.click()
            title_input.fill(payload["title"])
        except Exception as exc:
            log.warning(f"    xhs: 写标题失败: {exc}")

    # 4. Fill caption + topics — topics MUST go through the dropdown selector,
    # because xhs only registers them as real topics (with topic IDs) when
    # picked from the suggestion list. Pure-text `#xxx` becomes plain text and
    # doesn't enter the topic algorithm.
    editor_clicked = False
    try:
        editor = page.locator(XHS_SELECTORS["body_editor"]).first
        editor.click()
        editor_clicked = True
    except Exception as exc:
        log.warning(f"    xhs: 定位正文编辑器失败: {exc}")

    if editor_clicked:
        caption_text = payload.get("caption_text") or ""
        if caption_text:
            try:
                page.keyboard.type(caption_text, delay=10)
            except Exception as exc:
                log.warning(f"    xhs: 写正文失败: {exc}")

        topic_wait = float(settings.get("topic_dropdown_wait_sec", 3))
        _TOPIC_EXTRA_SELS = [
            '[class*="topic"][class*="item"]',
            '[class*="suggest"][class*="item"]',
            '[class*="mention-list"] li',
            '[class*="mention-list"] > *',
            '[class*="topic"] li',
            '[class*="dropdown"] li',
            '[role="option"]',
            '[role="listitem"]',
        ]
        for tag in payload.get("tags") or []:
            if not tag:
                continue
            topic_text = tag.lstrip("#").strip()
            if not topic_text:
                continue
            try:
                page.keyboard.type(" ", delay=10)
                page.keyboard.type("#" + topic_text, delay=20)
                # Try each selector candidate with a short timeout window
                clicked = False
                deadline = time.time() + topic_wait
                all_sels = [XHS_SELECTORS["topic_option"]] + _TOPIC_EXTRA_SELS
                for sel in all_sels:
                    remaining_ms = max(500, int((deadline - time.time()) * 1000))
                    try:
                        page.wait_for_selector(sel, timeout=remaining_ms)
                        loc = page.locator(sel)
                        if loc.count() > 0:
                            loc.first.click()
                            clicked = True
                            _sleep_with_cancel(0.5, cancel_event)
                            break
                    except Exception:
                        pass
                    if time.time() > deadline:
                        break
                if not clicked:
                    # Fallback: Enter accepts first highlighted suggestion in most editors
                    page.keyboard.press("Enter")
                    _sleep_with_cancel(0.3, cancel_event)
                    log.warning(f"    xhs: 话题 {tag} selector 未命中，已按 Enter 兜底")
            except Exception as exc:
                log.warning(f"    xhs: 话题 {tag} 插入失败: {exc}")

    # 5. 推荐话题：点击编辑器下方 XHS 推荐的 tag 芯片
    rec_count = int(settings.get("recommended_tag_count", 3))
    if rec_count > 0:
        try:
            rec_loc = page.locator(XHS_SELECTORS["recommended_tag_chip"])
            clicked_rec = 0
            for i in range(min(rec_loc.count(), rec_count)):
                try:
                    el = rec_loc.nth(i)
                    if el.is_visible():
                        el.click()
                        _sleep_with_cancel(0.3, cancel_event)
                        clicked_rec += 1
                except Exception:
                    continue
            if clicked_rec:
                log.info(f"    xhs: 点击了 {clicked_rec} 个推荐话题")
            else:
                log.info("    xhs: 未找到推荐话题芯片（selector 待确认）")
        except Exception as exc:
            log.warning(f"    xhs: 推荐话题点击失败: {exc}")

    # 6. AI 内容声明（GB45438-2025，2025-09-01 起）
    # 流程：展开"添加内容类型声明"折叠面板 → 点"笔记含AI合成内容"
    if payload.get("ai_declaration_required"):
        try:
            expand_loc = page.get_by_text("添加内容类型声明", exact=False)
            if expand_loc.count() > 0:
                expand_loc.first.click()
                _sleep_with_cancel(1, cancel_event)
            ai_loc = page.get_by_text("笔记含AI合成内容", exact=True)
            if ai_loc.count() == 0:
                ai_loc = page.get_by_text("笔记含AI合成内容", exact=False)
            if ai_loc.count() == 0:
                ai_loc = page.locator(XHS_SELECTORS["ai_declaration_ai_content"])
            if ai_loc.count() > 0:
                ai_loc.first.click()
                log.info("    xhs: 已勾选「笔记含AI合成内容」")
            else:
                log.warning("    xhs: 未找到「笔记含AI合成内容」选项，跳过")
        except Exception as exc:
            log.warning(f"    xhs: AI 内容声明失败（跳过）: {exc}")

    # 7. 原创声明 toggle
    if settings.get("auto_declare_original", True):
        try:
            orig_loc = page.locator(
                ':has-text("原创声明") span.d-switch-simulator,'
                ' :has-text("原创声明") span.d-switch-indicator,'
                ' :has-text("原创声明") span[class*="switch-simulator"]'
            )
            if orig_loc.count() > 0:
                orig_loc.first.click()
                _sleep_with_cancel(1.5, cancel_event)
                # First-time: dialog "笔记完成原创声明后..." appears
                confirm_btn = page.get_by_text("声明原创", exact=True)
                if confirm_btn.count() > 0:
                    # Check the agreement checkbox first
                    agree_cb = page.get_by_text("我已阅读并同意", exact=False).locator("xpath=..").locator("input[type='checkbox'], span.d-checkbox-simulator, span[class*='checkbox']")
                    if agree_cb.count() > 0:
                        agree_cb.first.click()
                        _sleep_with_cancel(0.5, cancel_event)
                    confirm_btn.first.click()
                    log.info("    xhs: 原创声明协议已确认")
                log.info("    xhs: 已勾选原创声明")
            else:
                log.warning("    xhs: 未找到原创声明 toggle，跳过")
        except Exception as exc:
            log.warning(f"    xhs: 原创声明失败（跳过）: {exc}")

    # 8. Publish
    try:
        publish_btn = page.locator(XHS_SELECTORS["publish_button"]).first
        for _ in range(20):
            try:
                if publish_btn.is_enabled():
                    break
            except Exception:
                pass
            _sleep_with_cancel(1, cancel_event)
        try:
            publish_btn.scroll_into_view_if_needed(timeout=3000)
        except Exception:
            pass
        publish_btn.click()
    except Exception as exc:
        log.error(f"    xhs: 点击发布失败: {exc}")
        return None

    # 6. Detect success
    timeout = int(settings.get("publish_timeout_sec", 120))
    for _ in range(timeout // 2):
        _sleep_with_cancel(2, cancel_event)
        url = page.url or ""
        # Heuristic 1: URL changes back to manage page or home
        if "/publish/success" in url or "/note" in url:
            wait = float(delay) + random.uniform(1, 3)
            time.sleep(wait)
            return url
        # Heuristic 2: visible success toast
        try:
            if page.locator(':text("发布成功"), :text("发布中")').count() > 0:
                _sleep_with_cancel(3, cancel_event)
                # take whatever URL the page has now
                wait = float(delay) + random.uniform(1, 3)
                time.sleep(wait)
                return page.url or "https://www.xiaohongshu.com/"
        except Exception:
            pass

    log.error("    xhs: 发布超时未检测到成功标志")
    return None
