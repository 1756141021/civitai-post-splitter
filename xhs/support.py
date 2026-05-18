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
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from PIL import Image

log = logging.getLogger("civitai_splitter")

XHS_DIR = Path(__file__).parent
XHS_BASE = "https://www.xiaohongshu.com"
# Web 版发布入口；creator.xiaohongshu.com 改版频繁，先用 web 端 explore 的发布。
XHS_PUBLISH_URL = "https://creator.xiaohongshu.com/publish/publish?source=official"
XHS_PROFILE_DIR = Path.home() / ".civitai_splitter_xhs_chrome"
_LAST_PUBLISH_FILE = Path(__file__).parent / ".last_publish_ts"

DEFAULT_TEMPLATES = {
    "default": {"core": "#插画", "lang": "zh", "social": "#治愈系插画"},
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
    "active_hours_start": 8,
    "active_hours_end": 23,
}

# Selectors confirmed at run time. xhs creator UI uses Element Plus
# components; class names rotate but role/aria/data-* attrs are more stable.
XHS_SELECTORS = {
    # Tab to switch from video → image-text mode (default is video).
    "image_text_tab": 'div.creator-tab:has-text("上传图文"), [role="tab"]:has-text("上传图文")',
    "file_input": 'input[type="file"]',
    "title_input": 'input[placeholder*="标题"], input[placeholder*="title" i]',
    "body_editor": '[contenteditable="true"], .editor-content, textarea[placeholder*="描述"]',
    "publish_button": 'button.ce-btn.bg-red, button.ce-btn:has-text("发布"), button:has-text("发布")',
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
    tag_limit = int(settings.get("tag_limit", 5))

    copy_xhs = (copy or {}).get("xhs") or {}
    llm_tags = [t for t in (copy_xhs.get("tags") or []) if t]

    if llm_tags:
        # LLM generated Chinese tags — use directly, append ai_tag if room
        seen: set[str] = set()
        tags: list[str] = []
        for t in llm_tags:
            norm = normalize_hashtag(t)
            if norm and norm.lower() not in seen and len(tags) < tag_limit:
                tags.append(norm)
                seen.add(norm.lower())
        if ai_tag:
            norm_ai = normalize_hashtag(ai_tag)
            if norm_ai and norm_ai.lower() not in seen and len(tags) < tag_limit:
                tags.append(norm_ai)
        tag_pick = {"tags": tags, "sources": {"character_tags": [], "template_tag": "", "social_tag": "", "ai_tag": ai_tag}}
    else:
        # Prefer Chinese entity tags; fall back to Japanese form if zh not available.
        xhs_entity_tags = (pixiv_payload or {}).get("entity_tags_zh") or \
                          (pixiv_payload or {}).get("entity_tags") or []
        tag_pick = pick_xhs_tags(
            entity_tags=xhs_entity_tags,
            template=template,
            ai_tag=ai_tag,
            limit=tag_limit,
        )
    tags = tag_pick["tags"]
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


def _find_chrome() -> str | None:
    """Find Chrome executable on Windows."""
    import os, winreg
    try:
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                             r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe")
        val, _ = winreg.QueryValueEx(key, "")
        winreg.CloseKey(key)
        if val and Path(val).exists():
            return val
    except Exception:
        pass
    for candidate in [
        Path(os.environ.get("PROGRAMFILES", "")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("PROGRAMFILES(X86)", "")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
    ]:
        if candidate.exists():
            return str(candidate)
    return None


_cdp_chrome_proc = None


def _ensure_chrome_cdp(cdp_url: str, profile_dir: Path) -> None:
    """If no Chrome is listening on *cdp_url*, launch one."""
    global _cdp_chrome_proc
    import subprocess, urllib.request
    try:
        resp = urllib.request.urlopen(cdp_url + "/json/version", timeout=2)
        data = json.loads(resp.read())
        if "webSocketDebuggerUrl" in data:
            log.info("xhs: CDP 端口已有 Chrome 在监听")
            return
        port = cdp_url.rsplit(":", 1)[-1].split("/")[0]
        raise RuntimeError(f"端口 {port} 已被其他服务占用（不是 Chrome CDP），请关闭占用该端口的程序")
    except RuntimeError:
        raise
    except Exception:
        pass

    chrome = _find_chrome()
    if not chrome:
        raise RuntimeError("找不到 Chrome，请安装 Chrome 浏览器")

    port = cdp_url.rsplit(":", 1)[-1].split("/")[0]
    profile_dir.mkdir(parents=True, exist_ok=True)
    log.info(f"xhs: 自动启动 Chrome (CDP :{port}) ...")
    _cdp_chrome_proc = subprocess.Popen([
        chrome,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--disable-sync",
        "--start-maximized",
    ])
    for i in range(20):
        time.sleep(1)
        if _cdp_chrome_proc.poll() is not None:
            raise RuntimeError(f"Chrome 启动后立即退出 (exit code {_cdp_chrome_proc.returncode})")
        try:
            urllib.request.urlopen(cdp_url + "/json/version", timeout=2)
            log.info("xhs: Chrome CDP 就绪")
            return
        except Exception:
            pass
    raise RuntimeError(f"Chrome 启动后 CDP 端口 {port} 未就绪，等了 20 秒")


_DEFAULT_CDP_URL = "http://localhost:9222"


def open_xhs_browser(pw, profile_dir: Path | None = None, *, cdp_url: str | None = None):
    """Auto-launch Chrome with CDP and connect for xhs.

    Chrome is started as a clean process (not via Playwright), then
    connected over CDP using Patchright — minimal automation fingerprint.
    """
    from xhs.stealth_scripts import FINGERPRINT_INIT_SCRIPT

    target_profile = profile_dir or XHS_PROFILE_DIR
    url = cdp_url or _DEFAULT_CDP_URL
    _ensure_chrome_cdp(url, target_profile)
    log.info(f"xhs: CDP 连接 → {url}")
    browser = pw.chromium.connect_over_cdp(url)
    if not browser.contexts:
        raise RuntimeError("CDP 连接成功但没有 context，浏览器可能没打开页面")
    context = browser.contexts[0]
    context.add_init_script(FINGERPRINT_INIT_SCRIPT)
    page = context.pages[0] if context.pages else context.new_page()
    ensure_xhs_logged_in(page)
    _warmup_browse(page)
    return context, page, browser


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


_DETECTION_PHRASES = [
    "检测到第三方",
    "第三方脚本",
    "操作异常",
    "请完成验证",
    "操作过于频繁",
    "账号存在异常",
    "安全验证",
]

_detection_triggered = False


def _check_detection(page) -> str | None:
    """Return warning phrase if a risk-control dialog/toast is visible, else None."""
    selectors = [
        '[class*="dialog"]', '[class*="modal"]', '[role="dialog"]',
        '[role="alertdialog"]', '[class*="toast"]', '[class*="notice"]',
        '[class*="captcha"]',
    ]
    for sel in selectors:
        try:
            loc = page.locator(sel)
            for i in range(loc.count()):
                el = loc.nth(i)
                if not el.is_visible():
                    continue
                text = el.inner_text(timeout=1000)
                for phrase in _DETECTION_PHRASES:
                    if phrase in text:
                        return phrase
        except Exception:
            pass
    return None


def _enforce_active_hours(settings: dict, cancel_event=None) -> None:
    """Block until we're within configured active posting hours."""
    start = int(settings.get("active_hours_start", 8))
    end = int(settings.get("active_hours_end", 23))
    if start == end:
        return
    while True:
        _raise_if_canceled(cancel_event)
        hour = datetime.now().hour
        if start < end:
            in_range = start <= hour < end
        else:
            in_range = hour >= start or hour < end
        if in_range:
            return
        now = datetime.now()
        target = now.replace(hour=start, minute=random.randint(0, 15), second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        wait_sec = (target - now).total_seconds()
        log.info(f"xhs: 当前 {hour}:00 不在活跃时段 ({start}:00-{end}:00)，等待 {wait_sec:.0f}s")
        _sleep_with_cancel(min(wait_sec, 300), cancel_event)


def _warmup_browse(page, cancel_event=None) -> None:
    """Browse XHS feed briefly to establish normal browsing pattern."""
    log.info("xhs: 浏览预热中...")
    try:
        page.goto("https://www.xiaohongshu.com/explore", wait_until="commit", timeout=20000)
    except Exception:
        return
    time.sleep(random.uniform(3, 6))

    scroll_count = random.randint(2, 4)
    for _ in range(scroll_count):
        direction = random.choice([-1, 1])
        amount = random.randint(200, 600) * direction
        try:
            page.evaluate(f"window.scrollBy(0, {amount})")
        except Exception:
            pass
        time.sleep(random.uniform(2, 5))

    if random.random() < 0.3:
        try:
            notes = page.locator('a[href*="/explore/"]')
            if notes.count() > 3:
                idx = random.randint(0, min(notes.count() - 1, 8))
                notes.nth(idx).click()
                time.sleep(random.uniform(3, 8))
                page.go_back()
                time.sleep(random.uniform(1, 3))
        except Exception:
            pass

    log.info("xhs: 预热完成")


def _human_type(page, text: str, *, cancel_event=None) -> None:
    """Keystroke-by-keystroke typing with human-like timing variance."""
    for i, ch in enumerate(text):
        _raise_if_canceled(cancel_event)
        delay_s = max(0.02, random.gauss(0.08, 0.03))
        if i > 0 and random.random() < 0.08:
            time.sleep(random.uniform(0.2, 0.5))
        if random.random() < 0.015 and ch.isascii() and ch.isalpha():
            wrong = chr(ord(ch) + random.choice([-1, 1]))
            page.keyboard.type(wrong)
            time.sleep(random.uniform(0.1, 0.3))
            page.keyboard.press("Backspace")
            time.sleep(random.uniform(0.05, 0.15))
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


def _random_scroll(page) -> None:
    """Randomly scroll page a small amount to simulate browsing."""
    direction = random.choice([-1, 1])
    amount = random.randint(50, 200) * direction
    try:
        page.evaluate(f"window.scrollBy(0, {amount})")
    except Exception:
        pass
    time.sleep(random.uniform(0.3, 0.8))


def _enforce_min_interval(settings: dict, cancel_event=None) -> None:
    """Block until min_interval_seconds have elapsed since last publish."""
    min_sec = float(settings.get("min_interval_seconds", 1800))
    actual_min = min_sec * random.uniform(0.8, 1.2)
    try:
        last_ts = float(_LAST_PUBLISH_FILE.read_text(encoding="utf-8").strip())
    except (FileNotFoundError, ValueError, OSError):
        return
    elapsed = time.time() - last_ts
    if elapsed < actual_min:
        wait = actual_min - elapsed
        log.info(f"    xhs: 距上次发布 {elapsed:.0f}s，需等待 {wait:.0f}s")
        _sleep_with_cancel(wait, cancel_event)


def _record_publish_time() -> None:
    try:
        _LAST_PUBLISH_FILE.write_text(str(time.time()), encoding="utf-8")
    except OSError:
        pass


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
    global _detection_triggered
    settings = settings or DEFAULT_SETTINGS
    _raise_if_canceled(cancel_event)

    if _detection_triggered:
        log.critical("xhs: 之前检测到风控，本次会话内不再发帖")
        return None

    _enforce_active_hours(settings, cancel_event)
    _enforce_min_interval(settings, cancel_event)

    # Random pre-publish detour (20% chance)
    if random.random() < 0.2:
        detour = random.choice([
            "https://www.xiaohongshu.com/user/profile/self",
            "https://creator.xiaohongshu.com/creator/home",
        ])
        try:
            page.goto(detour, wait_until="commit", timeout=15000)
            _sleep_with_cancel(random.uniform(3, 8), cancel_event)
            page.evaluate(f"window.scrollBy(0, {random.randint(100, 400)})")
            _sleep_with_cancel(random.uniform(1, 3), cancel_event)
        except Exception:
            pass

    try:
        page.goto(XHS_PUBLISH_URL, wait_until="commit", timeout=30000)
    except Exception as exc:
        log.error(f"    xhs: publish 跳转失败: {exc}")
        return None
    _sleep_with_cancel(random.uniform(3, 6), cancel_event)

    detected = _check_detection(page)
    if detected:
        log.critical(f"xhs: 检测到风控关键词「{detected}」，中止发布")
        _detection_triggered = True
        return None

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
                    _human_move_and_click(page, el, cancel_event=cancel_event)
                    clicked = True
                    break
            except Exception:
                continue
        if not clicked and tab_loc.count() > 0:
            tab_loc.first.click(force=True)
        if clicked or tab_loc.count() > 0:
            _sleep_with_cancel(random.uniform(1.5, 3.5), cancel_event)
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
    _sleep_with_cancel(random.uniform(6, 12), cancel_event)

    detected = _check_detection(page)
    if detected:
        log.critical(f"xhs: 上传后检测到风控「{detected}」，中止")
        _detection_triggered = True
        return None

    # 3. Fill title
    if payload.get("title"):
        try:
            title_input = page.locator(XHS_SELECTORS["title_input"]).first
            _human_move_and_click(page, title_input, cancel_event=cancel_event)
            _human_type(page, payload["title"], cancel_event=cancel_event)
            if random.random() < 0.2:
                _sleep_with_cancel(random.uniform(1, 3), cancel_event)
        except Exception as exc:
            log.warning(f"    xhs: 写标题失败: {exc}")

    # 4. Fill caption + topics — topics MUST go through the dropdown selector,
    # because xhs only registers them as real topics (with topic IDs) when
    # picked from the suggestion list. Pure-text `#xxx` becomes plain text and
    # doesn't enter the topic algorithm.
    editor_clicked = False
    try:
        editor = page.locator(XHS_SELECTORS["body_editor"]).first
        _human_move_and_click(page, editor, cancel_event=cancel_event)
        editor_clicked = True
    except Exception as exc:
        log.warning(f"    xhs: 定位正文编辑器失败: {exc}")

    if editor_clicked:
        if random.random() < 0.3:
            _random_scroll(page)
        caption_text = payload.get("caption_text") or ""
        if caption_text:
            try:
                _human_type(page, caption_text, cancel_event=cancel_event)
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
                _human_type(page, " ", cancel_event=cancel_event)
                _human_type(page, "#" + topic_text, cancel_event=cancel_event)
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
                            _human_move_and_click(page, loc.first, cancel_event=cancel_event)
                            clicked = True
                            _sleep_with_cancel(random.uniform(0.3, 1.0), cancel_event)
                            break
                    except Exception:
                        pass
                    if time.time() > deadline:
                        break
                if not clicked:
                    page.keyboard.press("Enter")
                    _sleep_with_cancel(random.uniform(0.3, 0.8), cancel_event)
                    log.warning(f"    xhs: 话题 {tag} selector 未命中，已按 Enter 兜底")
            except Exception as exc:
                log.warning(f"    xhs: 话题 {tag} 插入失败: {exc}")

    # 5/6/7: Recommended tags, AI declaration, original declaration — randomized order
    def _do_recommended_tags():
        rec_count = int(settings.get("recommended_tag_count", 3))
        if rec_count <= 0:
            return
        try:
            rec_loc = page.locator(XHS_SELECTORS["recommended_tag_chip"])
            clicked_rec = 0
            for i in range(min(rec_loc.count(), rec_count)):
                try:
                    el = rec_loc.nth(i)
                    if el.is_visible():
                        _human_move_and_click(page, el, cancel_event=cancel_event)
                        _sleep_with_cancel(random.uniform(0.3, 0.8), cancel_event)
                        clicked_rec += 1
                except Exception:
                    continue
            if clicked_rec:
                log.info(f"    xhs: 点击了 {clicked_rec} 个推荐话题")
            else:
                log.info("    xhs: 未找到推荐话题芯片（selector 待确认）")
        except Exception as exc:
            log.warning(f"    xhs: 推荐话题点击失败: {exc}")

    def _do_ai_declaration():
        if not payload.get("ai_declaration_required"):
            return
        try:
            expand_loc = page.get_by_text("添加内容类型声明", exact=False)
            if expand_loc.count() > 0:
                _human_move_and_click(page, expand_loc.first, cancel_event=cancel_event)
                _sleep_with_cancel(random.uniform(0.8, 2.0), cancel_event)
            ai_loc = page.get_by_text("笔记含AI合成内容", exact=True)
            if ai_loc.count() == 0:
                ai_loc = page.get_by_text("笔记含AI合成内容", exact=False)
            if ai_loc.count() == 0:
                ai_loc = page.locator(XHS_SELECTORS["ai_declaration_ai_content"])
            if ai_loc.count() > 0:
                _human_move_and_click(page, ai_loc.first, cancel_event=cancel_event)
                log.info("    xhs: 已勾选「笔记含AI合成内容」")
            else:
                log.warning("    xhs: 未找到「笔记含AI合成内容」选项，跳过")
        except Exception as exc:
            log.warning(f"    xhs: AI 内容声明失败（跳过）: {exc}")

    def _do_original_declaration():
        if not settings.get("auto_declare_original", True):
            return
        try:
            orig_loc = page.locator(
                ':has-text("原创声明") span.d-switch-simulator,'
                ' :has-text("原创声明") span.d-switch-indicator,'
                ' :has-text("原创声明") span[class*="switch-simulator"]'
            )
            if orig_loc.count() > 0:
                _human_move_and_click(page, orig_loc.first, cancel_event=cancel_event)
                _sleep_with_cancel(random.uniform(1.0, 2.5), cancel_event)
                confirm_btn = page.get_by_text("声明原创", exact=True)
                if confirm_btn.count() > 0:
                    agree_label = page.get_by_text("我已阅读并同意", exact=False)
                    if agree_label.count() > 0:
                        _human_move_and_click(page, agree_label.first, cancel_event=cancel_event)
                        _sleep_with_cancel(random.uniform(0.3, 1.0), cancel_event)
                    _human_move_and_click(page, confirm_btn.first, cancel_event=cancel_event)
                    log.info("    xhs: 原创声明协议已确认")
                log.info("    xhs: 已勾选原创声明")
            else:
                log.warning("    xhs: 未找到原创声明 toggle，跳过")
        except Exception as exc:
            log.warning(f"    xhs: 原创声明失败（跳过）: {exc}")

    _post_steps = [_do_recommended_tags, _do_ai_declaration, _do_original_declaration]
    random.shuffle(_post_steps)
    for _step_fn in _post_steps:
        _step_fn()
        _sleep_with_cancel(random.uniform(0.5, 1.5), cancel_event)

    # 8. Publish
    _sleep_with_cancel(random.uniform(0.8, 2.0), cancel_event)
    try:
        try:
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            _sleep_with_cancel(random.uniform(0.3, 1.0), cancel_event)
        except Exception:
            pass
        # Button lives inside an iframe — search all frames
        publish_frame = None
        publish_btn = None
        for _frame in page.frames:
            for _sel in [
                'button.ce-btn.bg-red',
                'button[class*="publish"]',
                'button.ce-btn:has-text("发布")',
                'button:has-text("发布")',
                'div.ce-btn:has-text("发布")',
            ]:
                try:
                    _loc = _frame.locator(_sel)
                    if _loc.count() > 0:
                        publish_frame = _frame
                        publish_btn = _loc.first
                        log.info(f"    xhs: 找到发布按钮 sel={_sel!r} frame={_frame.url[:60]!r}")
                        break
                except Exception:
                    pass
            if publish_btn:
                break
            _role = _frame.get_by_role("button", name="发布", exact=True)
            if _role.count() > 0:
                publish_frame = _frame
                publish_btn = _role.first
                log.info(f"    xhs: 找到发布按钮(role) frame={_frame.url[:60]!r}")
                break
        if publish_btn is None:
            # Last resort: shadow DOM click or coordinate click
            log.warning("    xhs: 所有 frame 里都找不到发布按钮，尝试 shadow DOM / 坐标点击")
            shadow_clicked = page.evaluate("""
                () => {
                    function find(root) {
                        for (const el of root.querySelectorAll('button, [role="button"], [class*="btn"]')) {
                            if (el.shadowRoot) { const r = find(el.shadowRoot); if (r) return r; }
                            const t = (el.textContent || '').trim().replace(/\\s+/g, '');
                            if (t === '发布' || t.endsWith('发布')) return el;
                        }
                        return null;
                    }
                    const el = find(document);
                    if (!el) return false;
                    el.scrollIntoView({block:'nearest'});
                    el.click();
                    return true;
                }
            """)
            if shadow_clicked:
                log.info("    xhs: JS 点击成功")
            else:
                # Coordinate fallback: scan bottom of viewport with elementFromPoint
                clicked_coord = page.evaluate("""
                    () => {
                        const h = window.innerHeight;
                        const w = window.innerWidth;
                        for (const yOff of [20, 28, 36, 45, 60, 80, 100]) {
                            for (const xPct of [0.55, 0.52, 0.58, 0.50, 0.60, 0.45, 0.65]) {
                                const x = Math.round(w * xPct);
                                const y = h - yOff;
                                const el = document.elementFromPoint(x, y);
                                if (el && (el.textContent||'').trim().replace(/\\s+/g,'').includes('发布')) {
                                    el.click();
                                    return `clicked at (${x},${y}) el=${el.tagName}.${el.className}`;
                                }
                            }
                        }
                        return null;
                    }
                """)
                if clicked_coord:
                    log.info(f"    xhs: 坐标点击成功: {clicked_coord}")
                else:
                    # Last resort: scroll to bottom, blind click bottom area
                    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    _sleep_with_cancel(0.3, cancel_event)
                    actual_h = page.evaluate("window.innerHeight") or 720
                    actual_w = page.evaluate("window.innerWidth") or 1280
                    for _y_off in [30, 50, 70]:
                        bx = int(actual_w * 0.55)
                        by = actual_h - _y_off
                        log.warning(f"    xhs: 盲点 ({bx}, {by}) viewport=({actual_w},{actual_h})")
                        page.mouse.click(bx, by)
                        _sleep_with_cancel(0.3, cancel_event)
        else:
            try:
                publish_btn.scroll_into_view_if_needed(timeout=3000)
            except Exception:
                pass
            try:
                publish_btn.click(timeout=5000)
                log.info("    xhs: 发布按钮已点击")
            except Exception as click_exc:
                log.warning(f"    xhs: click 失败，尝试 force: {click_exc}")
                try:
                    publish_btn.click(force=True, timeout=5000)
                    log.info("    xhs: 发布按钮已点击 (force)")
                except Exception as force_exc:
                    log.warning(f"    xhs: force 失败，尝试 JS: {force_exc}")
                    publish_frame.evaluate("""
                        () => {
                            const btn = document.querySelector('button.ce-btn.bg-red')
                                || [...document.querySelectorAll('button')]
                                    .find(b => b.textContent.trim() === '发布');
                            if (btn) btn.click();
                        }
                    """)
                    log.info("    xhs: 发布按钮已点击（JS fallback）")
    except Exception as exc:
        log.error(f"    xhs: 点击发布失败: {exc}")
        return None

    # 9. Detect success
    timeout = int(settings.get("publish_timeout_sec", 120))
    for _ in range(timeout // 2):
        _sleep_with_cancel(2, cancel_event)
        url = page.url or ""
        if "/publish/success" in url or "/note" in url:
            _record_publish_time()
            wait = float(delay) + random.uniform(1, 3)
            _sleep_with_cancel(wait, cancel_event)
            return url
        try:
            if page.locator(':text("发布成功"), :text("发布中")').count() > 0:
                _record_publish_time()
                _sleep_with_cancel(3, cancel_event)
                wait = float(delay) + random.uniform(1, 3)
                _sleep_with_cancel(wait, cancel_event)
                return page.url or "https://www.xiaohongshu.com/"
        except Exception:
            pass

    log.error("    xhs: 发布超时未检测到成功标志")
    return None
