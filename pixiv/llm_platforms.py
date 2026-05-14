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
