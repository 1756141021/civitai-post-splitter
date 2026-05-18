"""X (Twitter) publishing support.

  - load_x_* config helpers
  - pure transforms: pick_x_tags, build_text, build_alt_text, prepare_x_image
  - browser layer (Playwright, persistent profile): open_x_browser, create_x_post

Tag picker is intentionally simple (2 hashtags, hard cap):
  - have entity (角色/作品 tag from pixiv) → [entity_top1, template.core]
  - no entity                              → [template.core, template.social]
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

X_DIR = Path(__file__).parent
X_BASE = "https://x.com"
X_COMPOSE_URL = f"{X_BASE}/compose/post"
X_PROFILE_DIR = Path.home() / ".civitai_splitter_x_chrome"
_LAST_PUBLISH_FILE = Path(__file__).parent / ".last_publish_ts"

# Templates: each has `core` (the big-pool AI tag) and `social` (the community
# connection tag, used only when no entity tag is available).
DEFAULT_TEMPLATES = {
    "jp_sfw":  {"core": "#AIイラスト", "lang": "ja", "social": "#AIイラスト好きさんと繋がりたい"},
    "en_sfw":  {"core": "#AIart",      "lang": "en", "social": "#AIArtCommunity"},
    "zh_sfw":  {"core": "#AI绘画",     "lang": "zh", "social": "#AI插画"},
    "jp_nsfw": {"core": "#AIイラスト", "lang": "ja", "social": "#NSFW"},
    "en_nsfw": {"core": "#AIart",      "lang": "en", "social": "#NSFW"},
    "zh_nsfw": {"core": "#AI绘画",     "lang": "zh", "social": "#NSFW"},
}

DEFAULT_SETTINGS = {
    "tag_limit": 3,
    "text_max_chars": 280,
    "alt_text_max_chars": 1000,
    "image_long_side_max": 2048,
    "png_size_threshold_mb": 5,
    "jpg_quality": 90,
    "default_template": "en_sfw",
    "publish_timeout_sec": 90,
    "post_delay_sec": 4,
    "min_interval_seconds": 600,
}

X_SELECTORS = {
    "compose_textarea": '[data-testid="tweetTextarea_0"]',
    "file_input": 'input[data-testid="fileInput"]',
    "post_button": '[data-testid="tweetButton"]',
    "media_thumbnail": '[data-testid="attachments"] img',
    "alt_button": 'button[aria-label*="description" i]',
    "alt_textarea": '[data-testid="altTextInput"]',
    "alt_save_button": '[data-testid="altTextSaveButton"]',
    "sensitive_menu_button": 'button[aria-label*="content warning" i]',
    "typeahead_result": '[data-testid="typeaheadResult"]',
}


# ----- config IO --------------------------------------------------------------


def _load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except Exception as exc:
        log.warning(f"x: failed to read {path.name}: {exc}; using default")
        return default


def load_x_settings(root: Path | None = None) -> dict[str, Any]:
    base = root or X_DIR
    raw = _load_json(base / "x_settings.json", {})
    merged = dict(DEFAULT_SETTINGS)
    if isinstance(raw, dict):
        merged.update({k: v for k, v in raw.items() if v is not None})
    return merged


def load_x_templates(root: Path | None = None) -> dict[str, dict[str, Any]]:
    base = root or X_DIR
    raw = _load_json(base / "x_templates.json", {})
    merged = {k: dict(v) for k, v in DEFAULT_TEMPLATES.items()}
    if isinstance(raw, dict):
        for k, v in raw.items():
            if not isinstance(v, dict):
                continue
            entry = merged.setdefault(k, {"core": "", "lang": "en", "social": ""})
            entry.update(v)
            entry.setdefault("social", "")
    return merged


# ----- pure transforms --------------------------------------------------------


def normalize_hashtag(raw: str) -> str:
    """Strip whitespace and existing # marks, return as a single #tag.

    Returns empty string if nothing usable remains.
    """
    if not raw:
        return ""
    cleaned = raw.strip().lstrip("#").strip()
    if not cleaned:
        return ""
    cleaned = re.sub(r"\s+", "", cleaned)
    return f"#{cleaned}"


def choose_template(age_restriction: str, base_template: str, templates: dict[str, dict[str, Any]]) -> str:
    """Upgrade sfw→nsfw for r18/r18g; downgrade nsfw→sfw for non-r18."""
    is_nsfw = age_restriction in {"r18", "r18g"}
    if is_nsfw and not base_template.endswith("_nsfw"):
        nsfw_name = base_template.replace("_sfw", "_nsfw") if base_template.endswith("_sfw") else f"{base_template}_nsfw"
        if nsfw_name in templates:
            return nsfw_name
    elif not is_nsfw and base_template.endswith("_nsfw"):
        sfw_name = base_template.replace("_nsfw", "_sfw")
        if sfw_name in templates:
            return sfw_name
    if base_template in templates:
        return base_template
    for candidate in ("en_sfw", "jp_sfw"):
        if candidate in templates:
            return candidate
    return base_template


def pick_x_tags(
    entity_tags: Iterable[str],
    template: dict[str, Any],
    limit: int = 2,
) -> dict[str, Any]:
    """Pick 1..limit hashtags.

    Rule:
      - have entity_tags → [entity_top1, template.core]
      - no entity_tags   → [template.core, template.social]

    The picker hard-caps at `limit` (default 2 per X 2026 sweet-spot research:
    1-2 = +21% engagement, 3+ = -17%, 5+ = -40%).

    Returns {"tags": [...], "sources": {character_tags, template_tag, social_tag}}.
    """
    picked: list[str] = []
    seen: set[str] = set()
    sources = {"character_tags": [], "template_tag": "", "social_tag": ""}

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
        return True

    has_entity = False
    for ent in entity_tags or []:
        if len(picked) >= max(1, limit - 1):
            break
        if _try_add(ent, "character"):
            has_entity = True

    _try_add(template.get("core", ""), "core")

    if not has_entity:
        _try_add(template.get("social", ""), "social")

    return {"tags": picked, "sources": sources}


def _trim_to_chars(text: str, max_chars: int) -> str:
    if max_chars <= 0:
        return ""
    if len(text) <= max_chars:
        return text
    return text[: max(0, max_chars - 1)] + "…"


def build_text(title: str, caption: str, tags: list[str], max_chars: int) -> str:
    """Concatenate title + caption + tag line, hard-cap to max_chars.

    Overflow: drop caption first, then trim title. Tag line preserved.
    """
    title = (title or "").strip()
    caption = (caption or "").strip()
    if title.lower() in {"無題", "无题", "untitled", ""}:
        title = ""
    tag_line = " ".join(tags).strip()

    pieces = [p for p in (title, caption, tag_line) if p]
    full = "\n\n".join(pieces)
    if len(full) <= max_chars:
        return full

    pieces = [p for p in (title, tag_line) if p]
    full = "\n\n".join(pieces)
    if len(full) <= max_chars:
        return full

    if tag_line and len(tag_line) <= max_chars:
        remaining = max_chars - len(tag_line) - 2
        if remaining > 4 and title:
            return f"{_trim_to_chars(title, remaining)}\n\n{tag_line}"
        return tag_line

    return _trim_to_chars(tag_line or title, max_chars)


def build_alt_text(pixiv_payload: dict[str, Any] | None, max_chars: int) -> str:
    """Alt text from pixiv payload caption, falling back to top tags."""
    if not pixiv_payload:
        return ""
    caption = (pixiv_payload.get("caption_ja") or pixiv_payload.get("caption_zh") or "").strip()
    if caption:
        return _trim_to_chars(caption, max_chars)
    final_tags = pixiv_payload.get("final_tags") or []
    if final_tags:
        return _trim_to_chars("、".join(final_tags[:20]), max_chars)
    return ""


def prepare_x_image(src: Path, out_dir: Path, *, long_side_max: int, png_size_threshold_mb: int, jpg_quality: int) -> Path:
    """Resize and re-encode for X.

    Always re-encodes via PIL so EXIF / PNG text chunks (a1111 prompts,
    workflow JSON, comfy metadata, GPS) never reach X.
    """
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
            dst = out_dir / (src.stem + "_x.jpg")
            rgb = im.convert("RGB") if im.mode != "RGB" else im
            rgb.save(dst, format="JPEG", quality=jpg_quality, optimize=True)
            return dst

        dst = out_dir / (src.stem + "_x.png")
        from PIL import PngImagePlugin
        im.save(dst, format="PNG", optimize=True, pnginfo=PngImagePlugin.PngInfo())
        return dst


def build_x_payload(
    *,
    pixiv_payload: dict[str, Any] | None,
    image_path: Path,
    x_dir: Path,
    settings: dict[str, Any],
    templates: dict[str, dict[str, Any]],
    base_template: str | None = None,
    age_restriction: str = "all_ages",
    copy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble manifest['x'] for a single image. Pure transform (no browser).

    `copy` is the universal manifest.copy block (title.{ja,en,zh},
    caption.{ja,en,zh}). When provided, X reads its title/caption from the
    target template's language slot. Falls back to pixiv_payload's ja/zh
    title_ja/title_zh/caption_ja/caption_zh fields when copy is missing or
    empty for the chosen language.
    """
    base = base_template or settings.get("default_template", "en_sfw")
    template_name = choose_template(age_restriction, base, templates)
    template = templates[template_name]
    lang = template.get("lang", "en")

    entity_tags = (pixiv_payload or {}).get("entity_tags") or []
    tag_pick = pick_x_tags(
        entity_tags=entity_tags,
        template=template,
        limit=int(settings.get("tag_limit", 2)),
    )
    tags = tag_pick["tags"]

    # Title / caption: prefer manifest.copy when present, fall back to
    # pixiv_payload's legacy fields. Pixiv pipeline only outputs ja and zh, so
    # English templates rely on copy.title.en / copy.caption.en being set.
    copy_title = (copy or {}).get("title") or {}
    copy_caption = (copy or {}).get("caption") or {}

    title = str(copy_title.get(lang, "") or "").strip()
    caption = str(copy_caption.get(lang, "") or "").strip()

    if not title:
        if lang == "ja":
            title = (pixiv_payload or {}).get("title_ja", "") or ""
        elif lang == "zh":
            title = (pixiv_payload or {}).get("title_zh", "") or ""
    if not caption:
        if lang == "ja":
            caption = (pixiv_payload or {}).get("caption_ja", "") or ""
        elif lang == "zh":
            caption = (pixiv_payload or {}).get("caption_zh", "") or ""

    if not title:
        for _fb in ("zh", "ja", "en"):
            if _fb != lang:
                title = str(copy_title.get(_fb, "") or "").strip()
                if title:
                    break
    if not title:
        title = str(((copy or {}).get("xhs") or {}).get("title", "") or "").strip()

    if not caption:
        for _fb in ("zh", "ja", "en"):
            if _fb != lang:
                caption = str(copy_caption.get(_fb, "") or "").strip()
                if caption:
                    break
    if not caption:
        caption = str(((copy or {}).get("xhs") or {}).get("body", "") or "").strip()

    text = build_text(
        title="",
        caption=caption,
        tags=tags,
        max_chars=int(settings.get("text_max_chars", 280)),
    )

    alt = build_alt_text(pixiv_payload, int(settings.get("alt_text_max_chars", 1000)))

    out_image = prepare_x_image(
        Path(image_path),
        x_dir,
        long_side_max=int(settings.get("image_long_side_max", 2048)),
        png_size_threshold_mb=int(settings.get("png_size_threshold_mb", 5)),
        jpg_quality=int(settings.get("jpg_quality", 90)),
    )

    sensitive = age_restriction in {"r18", "r18g"}

    return {
        "clean_copy_paths": [str(out_image)],
        "text": text,
        "tags": tags,
        "tag_sources": tag_pick["sources"],
        "template": template_name,
        "sensitive": sensitive,
        "alt_text": alt,
        "group_id": None,
        "post_url": "",
    }


# ----- Playwright (browser layer) --------------------------------------------


def _load_cookies_into_context(context, cookies_path: Path) -> int:
    """Import cookies exported by Cookie-Editor / EditThisCookie (JSON array)."""
    try:
        raw = json.loads(cookies_path.read_text(encoding="utf-8"))
    except Exception as exc:
        log.error(f"X: 读取 cookies.json 失败: {exc}")
        return 0
    if not isinstance(raw, list):
        log.error("X: cookies.json 应为数组，跳过")
        return 0

    same_site_map = {
        "no_restriction": "None",
        "none": "None",
        "lax": "Lax",
        "strict": "Strict",
    }
    cookies: list[dict[str, Any]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        value = entry.get("value")
        if not name or value is None:
            continue
        domain = entry.get("domain") or ""
        path = entry.get("path") or "/"
        cookie: dict[str, Any] = {
            "name": str(name),
            "value": str(value),
            "domain": str(domain),
            "path": str(path),
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
        ss_raw = str(entry.get("sameSite") or "").lower()
        ss_normal = same_site_map.get(ss_raw)
        if ss_normal:
            cookie["sameSite"] = ss_normal
        cookies.append(cookie)

    if not cookies:
        return 0
    try:
        context.add_cookies(cookies)
    except Exception as exc:
        log.error(f"X: context.add_cookies 失败: {exc}")
        return 0
    return len(cookies)


def open_x_browser(pw, profile_dir: Path | None = None):
    """Launch persistent Chrome context for X with light anti-detection."""
    target_profile = profile_dir or X_PROFILE_DIR
    context = pw.chromium.launch_persistent_context(
        str(target_profile),
        channel="chrome",
        headless=False,
        args=[
            "--start-maximized",
            "--disable-sync",
            "--no-first-run",
        ],
        ignore_default_args=["--enable-automation", "--no-sandbox"],
    )

    cookies_path = X_DIR / "cookies.json"
    if cookies_path.exists():
        n = _load_cookies_into_context(context, cookies_path)
        if n > 0:
            log.info(f"X: 注入 {n} 条 cookies（来自 x/cookies.json）")

    page = context.pages[0] if context.pages else context.new_page()
    page.set_viewport_size({"width": 1920, "height": 1080})
    ensure_x_logged_in(page)
    return context, page


def ensure_x_logged_in(page) -> None:
    """Open compose; if textarea doesn't appear, block on manual login."""
    try:
        page.goto(X_COMPOSE_URL, wait_until="commit", timeout=30000)
    except Exception:
        pass

    textarea_sel = X_SELECTORS["compose_textarea"]
    for _ in range(8):
        time.sleep(1)
        try:
            if page.locator(textarea_sel).count() > 0:
                return
        except Exception:
            pass

    print()
    print("=" * 64)
    print("X 未登录（或 compose 页未加载完成）")
    print("请在弹出的 Chrome 窗口里登录 X (Twitter)。")
    print()
    print("【重要】不能用 Google 登录 —— Google 会拦自动化浏览器，提示")
    print("『此浏览器或应用可能不安全』。请用 邮箱/手机号 + 密码 登录。")
    print("如果账号原本只绑了 Google，先去 X 设置里加一个密码。")
    print()
    print("登录成功 + 能看到 X 主页后，回到这里按 Enter 继续...")
    print("=" * 64)
    try:
        input()
    except EOFError:
        pass

    try:
        page.goto(X_COMPOSE_URL, wait_until="commit", timeout=30000)
    except Exception:
        pass
    for _ in range(8):
        time.sleep(1)
        try:
            if page.locator(textarea_sel).count() > 0:
                return
        except Exception:
            pass
    log.warning("登录后 compose textarea 仍未出现 — 继续发布流程，可能会失败")


def _raise_if_canceled(cancel_event) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise InterruptedError("task canceled")


def _sleep_with_cancel(sec: float, cancel_event) -> None:
    end = time.time() + sec
    while time.time() < end:
        _raise_if_canceled(cancel_event)
        time.sleep(min(0.5, max(0.0, end - time.time())))


def _human_type(page, text: str, *, cancel_event=None) -> None:
    """Keystroke-by-keystroke typing with human-like timing variance."""
    for i, ch in enumerate(text):
        _raise_if_canceled(cancel_event)
        delay_s = max(0.02, random.gauss(0.07, 0.025))
        if i > 0 and random.random() < 0.06:
            time.sleep(random.uniform(0.15, 0.4))
        if random.random() < 0.012 and ch.isascii() and ch.isalpha():
            wrong = chr(ord(ch) + random.choice([-1, 1]))
            page.keyboard.type(wrong)
            time.sleep(random.uniform(0.08, 0.25))
            page.keyboard.press("Backspace")
            time.sleep(random.uniform(0.05, 0.12))
        page.keyboard.type(ch)
        time.sleep(delay_s)


def _human_move_and_click(page, locator, *, cancel_event=None) -> None:
    """Move mouse along a bezier curve to the element, then click."""
    _raise_if_canceled(cancel_event)
    try:
        box = locator.bounding_box(timeout=3000)
    except Exception:
        box = None
    if not box:
        locator.click()
        return
    target_x = box["x"] + box["width"] / 2 + random.uniform(-3, 3)
    target_y = box["y"] + box["height"] / 2 + random.uniform(-3, 3)
    try:
        current = page.evaluate(
            "() => ({x: window._lastMouseX || 640, y: window._lastMouseY || 360})"
        )
        cx, cy = current["x"], current["y"]
    except Exception:
        cx, cy = 640.0, 360.0
    steps = random.randint(15, 30)
    cp1x = cx + (target_x - cx) * 0.3 + random.uniform(-50, 50)
    cp1y = cy + (target_y - cy) * 0.3 + random.uniform(-30, 30)
    cp2x = cx + (target_x - cx) * 0.7 + random.uniform(-30, 30)
    cp2y = cy + (target_y - cy) * 0.7 + random.uniform(-20, 20)
    for i in range(steps + 1):
        t = i / steps
        x = (1 - t) ** 3 * cx + 3 * (1 - t) ** 2 * t * cp1x + 3 * (1 - t) * t ** 2 * cp2x + t ** 3 * target_x
        y = (1 - t) ** 3 * cy + 3 * (1 - t) ** 2 * t * cp1y + 3 * (1 - t) * t ** 2 * cp2y + t ** 3 * target_y
        page.mouse.move(x, y)
        time.sleep(random.uniform(0.005, 0.02))
    page.evaluate(
        f"() => {{ window._lastMouseX = {target_x}; window._lastMouseY = {target_y}; }}"
    )
    time.sleep(random.uniform(0.05, 0.15))
    page.mouse.click(target_x, target_y)


def _enforce_min_interval(settings: dict, cancel_event=None) -> None:
    """Block until min_interval_seconds have elapsed since last publish."""
    min_sec = float(settings.get("min_interval_seconds", 600))
    actual_min = min_sec * random.uniform(0.8, 1.2)
    try:
        last_ts = float(_LAST_PUBLISH_FILE.read_text(encoding="utf-8").strip())
    except (FileNotFoundError, ValueError, OSError):
        return
    elapsed = time.time() - last_ts
    if elapsed < actual_min:
        wait = actual_min - elapsed
        log.info(f"    X: 距上次发布 {elapsed:.0f}s，需等待 {wait:.0f}s")
        _sleep_with_cancel(wait, cancel_event)


def _record_publish_time() -> None:
    try:
        _LAST_PUBLISH_FILE.write_text(str(time.time()), encoding="utf-8")
    except OSError:
        pass


def create_x_post(
    page,
    payload: dict[str, Any],
    image_paths: list[Path],
    delay: float,
    *,
    settings: dict[str, Any] | None = None,
    log_dir: Path | None = None,
    cancel_event=None,
) -> str | None:
    """Post one tweet with 1..4 images. Returns the tweet URL or None."""
    settings = settings or DEFAULT_SETTINGS
    _raise_if_canceled(cancel_event)
    _enforce_min_interval(settings, cancel_event)

    try:
        page.goto(X_COMPOSE_URL, wait_until="commit", timeout=30000)
    except Exception as exc:
        log.error(f"    X: compose 跳转失败: {exc}")
        return None
    _sleep_with_cancel(random.uniform(2, 5), cancel_event)

    file_input = None
    for _ in range(12):
        _raise_if_canceled(cancel_event)
        loc = page.locator(X_SELECTORS["file_input"])
        if loc.count() > 0:
            file_input = loc.first
            break
        _sleep_with_cancel(1, cancel_event)
    if file_input is None:
        log.error("    X: 未找到文件上传输入框，跳过")
        return None
    try:
        file_input.set_input_files([str(p) for p in image_paths])
    except Exception as exc:
        log.error(f"    X: 文件上传失败: {exc}")
        log.debug(traceback.format_exc())
        return None

    for _ in range(int(settings.get("publish_timeout_sec", 90)) // 2):
        if page.locator(X_SELECTORS["media_thumbnail"]).count() >= len(image_paths):
            break
        _sleep_with_cancel(2, cancel_event)

    if payload.get("text"):
        try:
            ta = page.locator(X_SELECTORS["compose_textarea"]).first
            _human_move_and_click(page, ta, cancel_event=cancel_event)
            _human_type(page, payload["text"], cancel_event=cancel_event)
            page.keyboard.press("Space")
            _sleep_with_cancel(random.uniform(0.3, 1.0), cancel_event)
        except Exception as exc:
            log.warning(f"    X: 写正文失败: {exc}")

    alt = payload.get("alt_text") or ""
    if alt:
        try:
            alt_btns = page.locator(X_SELECTORS["alt_button"])
            for i in range(min(alt_btns.count(), len(image_paths))):
                _human_move_and_click(page, alt_btns.nth(i), cancel_event=cancel_event)
                _sleep_with_cancel(random.uniform(0.8, 2.0), cancel_event)
                page.locator(X_SELECTORS["alt_textarea"]).fill(alt)
                _human_move_and_click(
                    page, page.locator(X_SELECTORS["alt_save_button"]).first,
                    cancel_event=cancel_event,
                )
                _sleep_with_cancel(random.uniform(0.8, 2.0), cancel_event)
        except Exception as exc:
            log.warning(f"    X: alt 文本填充失败（跳过）: {exc}")

    if payload.get("sensitive"):
        log.info("    X: 该推标记为 sensitive（实测阶段补 selector）")

    try:
        post_btn = page.locator(X_SELECTORS["post_button"]).first
        for _ in range(20):
            try:
                if post_btn.is_enabled():
                    break
            except Exception:
                pass
            _sleep_with_cancel(random.uniform(0.8, 1.5), cancel_event)
    except Exception:
        pass

    posted = False
    try:
        _human_move_and_click(
            page, page.locator(X_SELECTORS["post_button"]).first,
            cancel_event=cancel_event,
        )
        log.info("    X: 点击 Post 按钮")
        posted = True
    except Exception as exc:
        log.warning(f"    X: 点击 Post 失败（{exc}），尝试 Ctrl+Enter")

    if not posted:
        try:
            ta = page.locator(X_SELECTORS["compose_textarea"]).first
            _human_move_and_click(page, ta, cancel_event=cancel_event)
            page.keyboard.press("Control+Enter")
            log.info("    X: 已发送 Ctrl+Enter")
            posted = True
        except Exception as exc:
            log.error(f"    X: 发推失败: {exc}")
            return None

    status_re = re.compile(r"https?://(?:x|twitter)\.com/[^/]+/status/(\d+)")
    for _ in range(int(settings.get("publish_timeout_sec", 90)) // 2):
        _sleep_with_cancel(2, cancel_event)
        try:
            if page.locator(X_SELECTORS["post_button"]).count() == 0:
                _record_publish_time()
                _sleep_with_cancel(float(delay) + random.uniform(1, 3), cancel_event)
                return page.url or "https://x.com/"
        except Exception:
            pass
        m = status_re.search(page.url or "")
        if m:
            _record_publish_time()
            _sleep_with_cancel(float(delay) + random.uniform(1, 3), cancel_event)
            return m.group(0)

    log.error("    X: 发布超时未确认")
    return None
