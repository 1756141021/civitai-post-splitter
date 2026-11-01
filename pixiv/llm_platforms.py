from __future__ import annotations

from copy import deepcopy
from typing import Any

PLATFORM_SPECS: dict[str, dict[str, Any]] = {
    "pixiv": {
        "label": "Pixiv",
        "fields": [
            {"key": "title_ja",   "label": "日文标题", "kind": "text",      "max": 32},
            {"key": "title_zh",   "label": "中文标题", "kind": "text",      "max": 32},
            {"key": "caption_ja", "label": "日文简介", "kind": "multiline", "max": 280},
            {"key": "caption_zh", "label": "中文简介", "kind": "multiline", "max": 280},
            {"key": "keywords",   "label": "Pixiv热点日文Tag", "kind": "tags", "max_count": 10, "max": 40},
        ],
        "extra_fields": [
            {"key": "description", "label": "画面描述", "kind": "multiline", "max": 400},
        ],
        "prompt_intro": (
            "Analyze the anime illustration visually (combining any character/IP hints from metadata) "
            "and write Pixiv post copy plus high-traffic Japanese Pixiv tags (`keywords`)."
        ),
        "policy_notes": (
            "For `keywords`, output 6-10 authentic, high-search-volume Japanese Pixiv tags (NO # prefix): "
            "include official Japanese IP/copyright name, character name (e.g. 胡桃(原神)), and trending Pixiv "
            "visual/aesthetic/fetish tags that match the image (e.g. 女の子, 魅惑のふともも, ドヤ顔, ツインテール, "
            "光と影, 極上の乳, 透明感, 背景美). Never identify real people."
        ),
    },
    "x": {
        "label": "X / Twitter",
        "fields": [
            {"key": "tweet",  "label": "推文正文(不含#标签)", "kind": "multiline", "max": 220},
            {"key": "x_tags", "label": "X热点标签",          "kind": "tags",      "max_count": 3, "max": 40},
        ],
        "extra_fields": [
            {"key": "alt_text", "label": "替代文本", "kind": "multiline", "max": 1000},
        ],
        "prompt_intro": (
            "Write a high-engagement X (Twitter) post (`tweet`, without hashtags in body) "
            "and 2-3 trending X hashtags (`x_tags`) for this illustration."
        ),
        "policy_notes": (
            "Keep `tweet` concise and engaging (1-2 lines, no hashtags inside `tweet`). "
            "For `x_tags`, output 2-3 high-traffic X hashtags with # prefix matching the character/IP and art community "
            "(e.g. #HuTao, #GenshinImpact, #原神, #illustration, #イラスト). Do NOT include AI-related hashtags like #AIart or #AIイラスト."
        ),
    },
    "xhs": {
        "label": "小红书",
        "fields": [
            {"key": "xhs_title", "label": "标题", "kind": "text",      "max": 20},
            {"key": "xhs_body",  "label": "正文", "kind": "multiline", "max": 1000},
            {"key": "xhs_tags",  "label": "话题", "kind": "tags",      "max_count": 10, "max": 30},
        ],
        "extra_fields": [],
        "prompt_intro": "为这张图写一条小红书种草短文。语言用中文。",
        "policy_notes": "标题最多 20 字。话题用 # 前缀，每个话题不要超过 30 字。",
    },
}

DEFAULT_PLATFORM_ID = "pixiv"


def list_platform_ids() -> list[str]:
    return list(PLATFORM_SPECS.keys())


def get_platform_spec(platform_id: str) -> dict[str, Any]:
    return deepcopy(PLATFORM_SPECS.get(platform_id) or PLATFORM_SPECS[DEFAULT_PLATFORM_ID])


def normalize_platform_id(value: Any) -> str:
    pid = str(value or "").strip().lower()
    return pid if pid in PLATFORM_SPECS else DEFAULT_PLATFORM_ID


def normalize_platform_ids(value: Any) -> list[str]:
    """Normalize platform field to a deduplicated list of valid platform IDs."""
    raw = value if isinstance(value, list) else [value]
    seen: set[str] = set()
    result: list[str] = []
    for v in raw:
        pid = str(v or "").strip().lower()
        pid = pid if pid in PLATFORM_SPECS else DEFAULT_PLATFORM_ID
        if pid not in seen:
            seen.add(pid)
            result.append(pid)
    return result or [DEFAULT_PLATFORM_ID]


def get_merged_spec(platform_ids: list[str]) -> dict[str, Any]:
    """Merge platform specs for multiple platforms into one combined spec."""
    if not platform_ids:
        return get_platform_spec(DEFAULT_PLATFORM_ID)
    if len(platform_ids) == 1:
        return get_platform_spec(platform_ids[0])
    merged_fields: list[dict] = []
    merged_extra: list[dict] = []
    seen_keys: set[str] = set()
    intros: list[str] = []
    policy_notes: list[str] = []
    labels: list[str] = []
    for pid in platform_ids:
        spec = PLATFORM_SPECS.get(pid) or PLATFORM_SPECS[DEFAULT_PLATFORM_ID]
        labels.append(str(spec.get("label", pid)))
        intro = spec.get("prompt_intro", "")
        if intro:
            intros.append(intro)
        note = spec.get("policy_notes", "")
        if note:
            policy_notes.append(note)
        for f in (spec.get("fields") or []):
            key = str(f.get("key") or "")
            if key and key not in seen_keys:
                seen_keys.add(key)
                merged_fields.append(deepcopy(f))
        for f in (spec.get("extra_fields") or []):
            key = str(f.get("key") or "")
            if key and key not in seen_keys:
                seen_keys.add(key)
                merged_extra.append(deepcopy(f))
    return {
        "label": " / ".join(labels),
        "fields": merged_fields,
        "extra_fields": merged_extra,
        "prompt_intro": " ".join(intros),
        "policy_notes": " ".join(p for p in policy_notes if p),
    }


def all_field_keys(platform_id: str) -> list[str]:
    spec = PLATFORM_SPECS.get(platform_id) or {}
    keys: list[str] = []
    for field in (spec.get("fields") or []) + (spec.get("extra_fields") or []):
        key = str(field.get("key") or "").strip()
        if key and key not in keys:
            keys.append(key)
    return keys


def required_field_keys(platform_id: str) -> list[str]:
    spec = PLATFORM_SPECS.get(platform_id) or {}
    return [str(f.get("key") or "").strip() for f in (spec.get("fields") or []) if f.get("key")]


def empty_sample_fields(platform_id: str) -> dict[str, Any]:
    spec = PLATFORM_SPECS.get(platform_id) or {}
    out: dict[str, Any] = {}
    for field in (spec.get("fields") or []):
        kind = field.get("kind")
        out[str(field.get("key"))] = [] if kind == "tags" else ""
    return out
