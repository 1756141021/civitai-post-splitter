from __future__ import annotations

from copy import deepcopy
from typing import Any

# Platform-specific output schema and prompt scaffolding for LLM image-reverse.
#
# Adding a new platform = adding one entry here. Backend prompt template,
# output normalization, and frontend persona editor all read from this map.
# No platform-specific if/else should appear in business code; everything
# routes through PLATFORM_SPECS[persona["platform"]].

PLATFORM_SPECS: dict[str, dict[str, Any]] = {
    "pixiv": {
        "label": "Pixiv",
        "fields": [
            {"key": "title_ja",   "label": "标题（日）", "kind": "text",      "max": 60},
            {"key": "title_zh",   "label": "标题（中）", "kind": "text",      "max": 60},
            {"key": "caption_ja", "label": "简介（日）", "kind": "multiline", "max": 500},
            {"key": "caption_zh", "label": "简介（中）", "kind": "multiline", "max": 500},
        ],
        "extra_fields": [
            {"key": "description", "label": "扩展描述", "kind": "multiline", "max": 1000},
            {"key": "keywords",    "label": "关键词",   "kind": "tags",      "max_count": 20, "max": 50},
        ],
        "prompt_intro": "Analyze the image and write Pixiv post copy.",
        "policy_notes": "Never identify real people. Avoid copyrighted character names unless visually obvious.",
    },
    "x": {
        "label": "X / Twitter",
        "fields": [
            {"key": "tweet", "label": "推文", "kind": "multiline", "max": 280},
        ],
        "extra_fields": [
            {"key": "alt_text", "label": "替代文本", "kind": "multiline", "max": 1000},
        ],
        "prompt_intro": "Write a single English or Japanese tweet describing this image.",
        "policy_notes": "Stay within 280 characters total. Use 1-3 hashtags max.",
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
    """Normalize platform field to a deduplicated list of valid platform IDs.

    Accepts a single string (legacy) or a list. Invalid IDs fall back to
    DEFAULT_PLATFORM_ID. Always returns at least one element.
    """
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
    """Merge platform specs for multiple platforms into one combined spec.

    Fields and extra_fields are deduplicated by key in order of appearance.
    For a single platform, equivalent to get_platform_spec.
    """
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
