from __future__ import annotations

import base64
import importlib
import json
import logging
import os
import random
import re
import subprocess
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

import httpx
from PIL import Image

log = logging.getLogger("civitai_splitter")

PIXIV_BASE = "https://www.pixiv.net"
PIXIV_UPLOAD_URL = f"{PIXIV_BASE}/upload.php"
PIXIV_PROFILE_DIR = Path.home() / ".civitai_splitter_pixiv_chrome"
PIXIV_RULE_FIT_PROFILE_DIR = Path.home() / ".civitai_splitter_pixiv_rule_fit_chrome"
H_UINT_RE = re.compile(r"^\d+$")
LORA_RE = re.compile(r"<lora:([^:>]+):([^>]+)>")
ARTWORK_ID_RE = re.compile(r"/artworks/(\d+)")
PIXIV_COUNT_PATTERNS = [
    re.compile(r'"illustManga"\s*:\s*\{\s*"total"\s*:\s*(\d+)', re.IGNORECASE),
    re.compile(r'"total"\s*:\s*(\d+)', re.IGNORECASE),
    re.compile(r"イラストやマンガは\s*(\d[\d,]*)\s*件", re.IGNORECASE),
    re.compile(r"(\d[\d,]*)\s*(?:作品|artworks)", re.IGNORECASE),
]
PIXIV_COUNT_TIMEOUT_SECONDS = 4.0
PIXIV_COUNT_LIVE_BUDGET = 12
PIXIV_COUNT_SELLING_BUDGET = 4
PROMPT_TOKEN_SPLIT_RE = re.compile(r"[,|\n]")
FILENAME_SPLIT_RE = re.compile(r"[_\-\s\[\]\(\){}]+")
SAFE_STEM_RE = re.compile(r"[^0-9A-Za-z\u3040-\u30ff\u3400-\u9fff._-]+")
METADATA_ENTITY_PAREN_RE = re.compile(r"^(?P<name>.+?)\s*\((?P<scope>[^()]+)\)$")
R18_AGE_RESTRICTIONS = {"r18", "r18g"}
DEFAULT_RULE_FIT_SOURCES = [
    {
        "name": "ranking_daily",
        "kind": "ranking",
        "url": f"{PIXIV_BASE}/ranking.php?mode=daily&content=illust",
        "domain_hint": "mixed",
        "age_hint": "all_ages",
    },
    {
        "name": "ranking_weekly",
        "kind": "ranking",
        "url": f"{PIXIV_BASE}/ranking.php?mode=weekly&content=illust",
        "domain_hint": "mixed",
        "age_hint": "all_ages",
    },
    {
        "name": "ranking_daily_r18",
        "kind": "ranking",
        "url": f"{PIXIV_BASE}/ranking.php?mode=daily_r18&content=illust",
        "domain_hint": "mixed",
        "age_hint": "r18",
    },
    {
        "name": "tag_ai_illustration",
        "kind": "tag",
        "tag": "AIイラスト",
        "url": f"{PIXIV_BASE}/tags/{quote('AIイラスト', safe='')}/artworks?s_mode=s_tag",
        "domain_hint": "mixed",
        "age_hint": "mixed",
    },
    {
        "name": "tag_glasses",
        "kind": "tag",
        "tag": "眼鏡",
        "url": f"{PIXIV_BASE}/tags/{quote('眼鏡', safe='')}/artworks?s_mode=s_tag",
        "domain_hint": "mixed",
        "age_hint": "mixed",
    },
    {
        "name": "tag_blonde",
        "kind": "tag",
        "tag": "金髪",
        "url": f"{PIXIV_BASE}/tags/{quote('金髪', safe='')}/artworks?s_mode=s_tag",
        "domain_hint": "mixed",
        "age_hint": "mixed",
    },
    {
        "name": "tag_smile",
        "kind": "tag",
        "tag": "笑顔",
        "url": f"{PIXIV_BASE}/tags/{quote('笑顔', safe='')}/artworks?s_mode=s_tag",
        "domain_hint": "mixed",
        "age_hint": "mixed",
    },
    {
        "name": "fanart_miku",
        "kind": "fanart_tag",
        "tag": "初音ミク",
        "url": f"{PIXIV_BASE}/tags/{quote('初音ミク', safe='')}/artworks?s_mode=s_tag",
        "domain_hint": "fanart",
        "age_hint": "mixed",
    },
    {
        "name": "fanart_genshin",
        "kind": "fanart_tag",
        "tag": "原神",
        "url": f"{PIXIV_BASE}/tags/{quote('原神', safe='')}/artworks?s_mode=s_tag",
        "domain_hint": "fanart",
        "age_hint": "mixed",
    },
    {
        "name": "fanart_hsr",
        "kind": "fanart_tag",
        "tag": "崩壊スターレイル",
        "url": f"{PIXIV_BASE}/tags/{quote('崩壊スターレイル', safe='')}/artworks?s_mode=s_tag",
        "domain_hint": "fanart",
        "age_hint": "mixed",
    },
]


@dataclass
class PixivStep:
    name: str
    ok: bool
    reason: str = ""
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "ok": self.ok, "reason": self.reason, "detail": self.detail}


PIXIV_SELECTORS: dict[str, Any] = {
    "file_input": ['input[type="file"]'],
    "title": [
        'input[name="title"]',
        'input[placeholder*="タイトル"]',
        'input[placeholder*="Title"]',
        'input[placeholder*="标题"]',
    ],
    "caption": [
        'textarea[name="comment"]',
        'textarea[placeholder*="キャプション"]',
        'textarea[placeholder*="Caption"]',
        'textarea[placeholder*="说明"]',
    ],
    "tag_input": [
        'input[placeholder="标签"]',
        'input[placeholder="タグ"]',
        'input[placeholder="Tags"]',
        'input[placeholder*="タグ"]',
        'input[placeholder*="tag"]',
    ],
    # Autocomplete dropdown that appears under the tag input after typing.
    # Pixiv uses data-tag/data-type/data-index attrs (not ARIA listbox).
    # Container has data-type="front_matching" children when there are matches.
    "tag_autocomplete_listbox": [
        '[data-tag][data-type="front_matching"]',
    ],
    "tag_autocomplete_first_option": [
        '[data-tag][data-type="front_matching"][data-index="0"]',
    ],
    # name + value attribute → playwright selector
    "age_radio_attr": {
        "name": "x_restrict",
        "values": {
            "all_ages": "general",
            "mild_sensitive": "general",
            "r18": "r18",
            "r18g": "r18g",
        },
    },
    "ai_radio_attr": {
        "name": "ai_type",
        "values": {True: "aiGenerated", False: "notAiGenerated"},
    },
    "sexual_radio_attr": {
        "name": "sexual",
        "values": {False: "false", True: "true"},
    },
    "privacy_radio_attr": {
        "name": "restrict",
        "values": {
            "public": "public",
            "logged_in": "loginOnly",
            "mypixiv": "mypixivOnly",
            "private": "private",
        },
    },
    "original_checkbox_attr": "original",
    "allow_tag_edit_checkbox_attr": "allow_tag_edit",
    # text-based fallbacks (used if name= selector misses)
    "age_radio_text": {
        "all_ages": ["全年龄", "全年齢", "All ages"],
        "mild_sensitive": ["轻度性描写", "軽度な性的描写", "Mild sexual content"],
        "r18": ["R-18"],
        "r18g": ["R-18G"],
    },
    "privacy_radio_text": {
        "public": ["公开", "公開", "Public"],
        "logged_in": ["登录用户可见", "ログインユーザーのみ", "Logged-in users only"],
        "mypixiv": ["仅 My pixiv", "マイピクのみ", "My pixiv only"],
        "private": ["私密", "非公開", "Private"],
    },
    "publish_button": [
        'button[data-variant="Primary"][data-full-width="true"]:has-text("投稿")',
        'button[data-variant="Primary"][data-full-width="true"]:has-text("Post")',
        'button[data-variant="Primary"][data-full-width="true"]:has-text("Publish")',
        'button[type="submit"]:has-text("投稿")',
        'button[type="submit"]:has-text("Post")',
    ],
}


DEFAULT_ALIAS_DATA = {
    "ambiguous_tags": ["one-piece", "one piece", "ワンピース"],
    "drop_tags": [
        "1girl", "1boy", "2girls", "multiple girls", "multiple boys", "solo",
        "solo focus", "looking at viewer", "looking back", "facing viewer",
        "absurdres", "highres", "lowres", "masterpiece", "best quality",
        "amazing quality", "very aesthetic", "quality", "safe", "sensitive",
        "questionable", "explicit", "artist name", "signature", "watermark",
        "commentary", "speech bubble", "text", "jpeg artifacts",
    ],
    "filename_drop_tokens": [
        "底图", "通用放大", "00001", "0001", "0000", "copy", "cleaned", "pixiv", "civitai",
    ],
    "semantics": {
        "original": {
            "candidates": ["オリジナル", "創作"],
            "default": "オリジナル",
            "zh": "原创",
            "class": "identity",
            "domain": "original",
        },
        "oc": {
            "candidates": ["うちの子", "オリキャラ"],
            "default": "うちの子",
            "zh": "自设角色",
            "class": "identity",
            "domain": "original",
        },
        "ai_art": {
            "candidates": ["AIイラスト", "AI生成"],
            "default": "AIイラスト",
            "zh": "AI插画",
            "class": "meta",
            "domain": "both",
        },
        "r18": {
            "candidates": ["R-18"],
            "default": "R-18",
            "zh": "R18",
            "class": "rating",
            "domain": "both",
        },
        "r18g": {
            "candidates": ["R-18G"],
            "default": "R-18G",
            "zh": "R18G",
            "class": "rating",
            "domain": "both",
        },
        "glasses": {
            "candidates": ["眼鏡", "メガネ", "めがね"],
            "default": "眼鏡",
            "zh": "眼镜",
            "class": "feature",
            "domain": "both",
        },
        "twintails": {
            "candidates": ["ツインテール"],
            "default": "ツインテール",
            "zh": "双马尾",
            "class": "feature",
            "domain": "both",
        },
        "blonde_hair": {
            "candidates": ["金髪"],
            "default": "金髪",
            "zh": "金发",
            "class": "feature",
            "domain": "both",
        },
        "blue_eyes": {
            "candidates": ["碧眼", "青い目"],
            "default": "碧眼",
            "zh": "蓝眼",
            "class": "feature",
            "domain": "both",
        },
        "purple_eyes": {
            "candidates": ["紫目", "紫眼"],
            "default": "紫目",
            "zh": "紫眼",
            "class": "feature",
            "domain": "both",
        },
        "school_uniform": {
            "candidates": ["制服", "学生服"],
            "default": "制服",
            "zh": "制服",
            "class": "theme",
            "domain": "both",
        },
        "maid": {
            "candidates": ["メイド"],
            "default": "メイド",
            "zh": "女仆",
            "class": "theme",
            "domain": "both",
        },
        "swimsuit": {
            "candidates": ["水着"],
            "default": "水着",
            "zh": "泳装",
            "class": "theme",
            "domain": "both",
        },
        "bikini": {
            "candidates": ["ビキニ"],
            "default": "ビキニ",
            "zh": "比基尼",
            "class": "theme",
            "domain": "both",
        },
        "dress": {
            "candidates": ["ドレス"],
            "default": "ドレス",
            "zh": "连衣裙",
            "class": "theme",
            "domain": "both",
        },
        "kimono": {
            "candidates": ["着物", "和服"],
            "default": "着物",
            "zh": "和服",
            "class": "theme",
            "domain": "both",
        },
        "cat_ears": {
            "candidates": ["猫耳", "ねこみみ", "ネコミミ"],
            "default": "猫耳",
            "zh": "猫耳",
            "class": "feature",
            "domain": "both",
        },
        "fox_ears": {
            "candidates": ["狐耳", "きつね耳", "キツネ耳"],
            "default": "狐耳",
            "zh": "狐耳",
            "class": "feature",
            "domain": "both",
        },
        "fox_tail": {
            "candidates": ["狐しっぽ", "狐尻尾", "きつねしっぽ"],
            "default": "狐しっぽ",
            "zh": "狐尾",
            "class": "feature",
            "domain": "both",
        },
        "fox_girl": {
            "candidates": ["狐っ娘", "狐娘"],
            "default": "狐っ娘",
            "zh": "狐娘",
            "class": "theme",
            "domain": "both",
        },
        "kitsune": {
            "candidates": ["狐っ娘", "狐娘", "狐"],
            "default": "狐っ娘",
            "zh": "狐狸角色",
            "class": "theme",
            "domain": "both",
        },
        "rabbit_ears": {
            "candidates": ["うさみみ", "兎耳", "ウサ耳"],
            "default": "うさみみ",
            "zh": "兔耳",
            "class": "feature",
            "domain": "both",
        },
        "wolf_ears": {
            "candidates": ["狼耳", "オオカミ耳"],
            "default": "狼耳",
            "zh": "狼耳",
            "class": "feature",
            "domain": "both",
        },
        "dragon_horns": {
            "candidates": ["ドラゴンの角", "竜角"],
            "default": "ドラゴンの角",
            "zh": "龙角",
            "class": "feature",
            "domain": "both",
        },
        "demon_horns": {
            "candidates": ["悪魔の角", "鬼の角"],
            "default": "悪魔の角",
            "zh": "恶魔角",
            "class": "feature",
            "domain": "both",
        },
        "halo": {
            "candidates": ["天使の輪", "ヘイロー", "光輪"],
            "default": "天使の輪",
            "zh": "光环",
            "class": "feature",
            "domain": "both",
        },
        "vampire": {
            "candidates": ["吸血鬼", "ヴァンパイア"],
            "default": "吸血鬼",
            "zh": "吸血鬼",
            "class": "theme",
            "domain": "both",
        },
        "one_piece_swimsuit": {
            "candidates": ["ワンピース水着", "ワンピ水着"],
            "default": "ワンピース水着",
            "zh": "连体泳装",
            "class": "theme",
            "domain": "both",
        },
        "long_hair": {
            "candidates": ["ロングヘア"],
            "default": "ロングヘア",
            "zh": "长发",
            "class": "feature",
            "domain": "both",
        },
        "white_hair": {
            "candidates": ["白髪"],
            "default": "白髪",
            "zh": "白发",
            "class": "feature",
            "domain": "both",
        },
        "barefeet": {
            "candidates": ["素足"],
            "default": "素足",
            "zh": "赤脚",
            "class": "theme",
            "domain": "both",
        },
        "bed": {
            "candidates": ["ベッド"],
            "default": "ベッド",
            "zh": "床",
            "class": "theme",
            "domain": "both",
        },
        "smile": {
            "candidates": ["笑顔"],
            "default": "笑顔",
            "zh": "微笑",
            "class": "theme",
            "domain": "both",
        },
        "blush": {
            "candidates": ["赤面"],
            "default": "赤面",
            "zh": "脸红",
            "class": "theme",
            "domain": "both",
        },
        "hatsune_miku": {
            "candidates": ["初音ミク"],
            "default": "初音ミク",
            "zh": "初音未来",
            "class": "character",
            "domain": "fanart",
        },
        "vocaloid": {
            "candidates": ["VOCALOID"],
            "default": "VOCALOID",
            "zh": "VOCALOID",
            "class": "franchise",
            "domain": "fanart",
        },
        "genshin_impact": {
            "candidates": ["原神"],
            "default": "原神",
            "zh": "原神",
            "class": "franchise",
            "domain": "fanart",
        },
        "honkai_star_rail": {
            "candidates": ["崩壊スターレイル", "崩坏：星穹铁道"],
            "default": "崩壊スターレイル",
            "zh": "崩坏：星穹铁道",
            "class": "franchise",
            "domain": "fanart",
        },
    },
    "mappings": {
        "original": {"semantic": "original"},
        "創作": {"semantic": "original"},
        "オリジナル": {"semantic": "original"},
        "oc": {"semantic": "oc"},
        "オリキャラ": {"semantic": "oc"},
        "うちの子": {"semantic": "oc"},
        "ai art": {"semantic": "ai_art"},
        "ai generated": {"semantic": "ai_art"},
        "glasses": {"semantic": "glasses"},
        "megane": {"semantic": "glasses"},
        "メガネ": {"semantic": "glasses"},
        "めがね": {"semantic": "glasses"},
        "眼鏡": {"semantic": "glasses"},
        "twintails": {"semantic": "twintails"},
        "twin tails": {"semantic": "twintails"},
        "ツインテール": {"semantic": "twintails"},
        "blonde hair": {"semantic": "blonde_hair"},
        "金髪": {"semantic": "blonde_hair"},
        "blue eyes": {"semantic": "blue_eyes"},
        "碧眼": {"semantic": "blue_eyes"},
        "purple eyes": {"semantic": "purple_eyes"},
        "紫目": {"semantic": "purple_eyes"},
        "紫眼": {"semantic": "purple_eyes"},
        "school uniform": {"semantic": "school_uniform"},
        "制服": {"semantic": "school_uniform"},
        "学生服": {"semantic": "school_uniform"},
        "maid": {"semantic": "maid"},
        "メイド": {"semantic": "maid"},
        "swimsuit": {"semantic": "swimsuit"},
        "水着": {"semantic": "swimsuit"},
        "bikini": {"semantic": "bikini"},
        "ビキニ": {"semantic": "bikini"},
        "dress": {"semantic": "dress"},
        "kimono": {"semantic": "kimono"},
        "着物": {"semantic": "kimono"},
        "和服": {"semantic": "kimono"},
        "cat ears": {"semantic": "cat_ears"},
        "猫耳": {"semantic": "cat_ears"},
        "fox ears": {"semantic": "fox_ears"},
        "fox tail": {"semantic": "fox_tail"},
        "fox girl": {"semantic": "fox_girl"},
        "kitsune": {"semantic": "kitsune"},
        "rabbit ears": {"semantic": "rabbit_ears"},
        "wolf ears": {"semantic": "wolf_ears"},
        "dragon horns": {"semantic": "dragon_horns"},
        "demon horns": {"semantic": "demon_horns"},
        "halo": {"semantic": "halo"},
        "vampire": {"semantic": "vampire"},
        "one-piece swimsuit": {"semantic": "one_piece_swimsuit"},
        "one piece swimsuit": {"semantic": "one_piece_swimsuit"},
        "long hair": {"semantic": "long_hair"},
        "very long hair": {"semantic": "long_hair"},
        "ロングヘア": {"semantic": "long_hair"},
        "white hair": {"semantic": "white_hair"},
        "白髪": {"semantic": "white_hair"},
        "bare feet": {"semantic": "barefeet"},
        "素足": {"semantic": "barefeet"},
        "bed": {"semantic": "bed"},
        "ベッド": {"semantic": "bed"},
        "round eyewear": {"semantic": "glasses"},
        "gold-framed glasses": {"semantic": "glasses"},
        "smile": {"semantic": "smile"},
        "笑顔": {"semantic": "smile"},
        "blush": {"semantic": "blush"},
        "slight blush": {"semantic": "blush"},
        "赤面": {"semantic": "blush"},
        "hatsune miku": {"semantic": "hatsune_miku", "extras": ["vocaloid"]},
        "初音ミク": {"semantic": "hatsune_miku", "extras": ["vocaloid"]},
        "vocaloid": {"semantic": "vocaloid"},
        "genshin impact": {"semantic": "genshin_impact"},
        "原神": {"semantic": "genshin_impact"},
        "honkai star rail": {"semantic": "honkai_star_rail"},
        "崩壊スターレイル": {"semantic": "honkai_star_rail"},
        "崩坏：星穹铁道": {"semantic": "honkai_star_rail"},
    },
}

DEFAULT_POPULARITY_DATA = {
    "groups": {
        semantic: {
            "winner": info["default"],
            "counts": {candidate: 0 for candidate in info["candidates"]},
            "updated_at": "",
        }
        for semantic, info in DEFAULT_ALIAS_DATA["semantics"].items()
        if len(info["candidates"]) > 1
    }
}

DEFAULT_AGE_RULES = {
    "default": "all_ages",
    "rules": [
        {"match": "r18g", "age_restriction": "r18g"},
        {"match": "guro", "age_restriction": "r18g"},
        {"match": "r18", "age_restriction": "r18"},
        {"match": "nsfw", "age_restriction": "r18"},
        {"match": "sensitive", "age_restriction": "mild_sensitive"},
        {"match": "mild", "age_restriction": "mild_sensitive"},
    ],
    "candidate_rules": [
        {"match": "guro", "age_restriction": "r18g"},
        {"match": "nipple", "age_restriction": "r18"},
        {"match": "nipples", "age_restriction": "r18"},
        {"match": "pussy", "age_restriction": "r18"},
        {"match": "vagina", "age_restriction": "r18"},
        {"match": "penis", "age_restriction": "r18"},
        {"match": "cum", "age_restriction": "r18"},
        {"match": "sex", "age_restriction": "r18"},
        {"match": "oral sex", "age_restriction": "r18"},
        {"match": "pubic hair", "age_restriction": "r18"},
        {"match": "uncensored", "age_restriction": "r18"},
    ],
}

DEFAULT_VALIDATION_CASES = {"cases": []}


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return json.loads(json.dumps(default, ensure_ascii=False))
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return json.loads(json.dumps(default, ensure_ascii=False))


def save_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


DEFAULT_GENERAL_JP = {
    "_comment": "Danbooru / WD14 tag → Pixiv 高频日文形式。Pixiv ajax 翻不出来的通用词用这个表。删了重启会重生默认值。",
    "_help": {
        "mappings": "Danbooru 形式 → 想在 Pixiv 上显示的日文 hashtag。新词加在这里",
        "selling_points": "tagger 命中任一 trigger（>=min_score）就额外加 tag。Pixiv 高人气图常用卖点",
        "force_original": "true 时 domain=original 自动 prepend オリジナル tag",
    },
    "force_original": True,
    "mappings": {
        "1girl": "女の子", "1boy": "男の子",
        "multiple_girls": "複数人", "multiple_boys": "複数人",
        "long_hair": "ロングヘア", "short_hair": "ショートヘア", "very_long_hair": "ロングヘア",
        "medium_hair": "ミディアムヘア",
        "twin_tails": "ツインテール", "twintails": "ツインテール", "ponytail": "ポニーテール",
        "braid": "三つ編み", "bun": "お団子", "ahoge": "アホ毛",
        "bangs": "前髪", "sidelocks": "もみあげ",
        "hair_ornament": "髪飾り", "hair_ribbon": "髪リボン", "hair_between_eyes": "前髪",
        "blonde_hair": "金髪", "black_hair": "黒髪", "white_hair": "白髪", "silver_hair": "銀髪",
        "pink_hair": "ピンク髪", "red_hair": "赤髪", "purple_hair": "紫髪",
        "green_hair": "緑髪", "brown_hair": "茶髪", "multicolored_hair": "メッシュ",
        "blue_eyes": "青眼", "red_eyes": "赤眼", "green_eyes": "緑眼",
        "yellow_eyes": "金眼", "purple_eyes": "紫眼", "pink_eyes": "ピンク眼",
        "pointy_ears": "尖り耳", "animal_ears": "けものみみ",
        "cat_ears": "猫耳", "fox_ears": "狐耳", "rabbit_ears": "うさみみ",
        "wings": "翼", "ice_wings": "氷の翼", "halo": "天使の輪",
        "skirt": "スカート", "dress": "ドレス", "bow": "リボン", "ribbon": "リボン",
        "bra": "ブラ", "panties": "パンツ", "thigh_highs": "ニーソックス", "thighhighs": "ニーソックス",
        "white_panties": "白パンツ", "school_uniform": "制服", "swimsuit": "水着", "bikini": "ビキニ",
        "gloves": "手袋", "fingerless_gloves": "フィンガーレスグローブ",
        "short_sleeves": "半袖", "sleeveless": "ノースリーブ", "long_sleeves": "長袖",
        "shirt": "シャツ", "shoes": "靴", "boots": "ブーツ", "socks": "ソックス",
        "stockings": "ストッキング", "necktie": "ネクタイ", "scarf": "スカーフ",
        "hat": "帽子", "cap": "帽子", "earrings": "ピアス", "jewelry": "ジュエリー",
        "necklace": "ネックレス", "bracelet": "ブレスレット", "choker": "チョーカー",
        "collar": "首輪", "belt": "ベルト", "glasses": "眼鏡", "sunglasses": "サングラス",
        "spread_legs": "M字開脚", "on_back": "仰向け", "on_stomach": "うつ伏せ",
        "lying": "寝そべり", "sitting": "座り", "standing": "立ち",
        "kneeling": "膝立ち", "squatting": "しゃがみ",
        "bent_over": "屈み", "leaning_forward": "前かがみ",
        "looking_at_viewer": "見つめる", "looking_back": "振り向き",
        "looking_to_the_side": "横目", "looking_up": "上目遣い", "looking_down": "下目遣い",
        "open_mouth": "開口", "closed_mouth": "口閉じ", "smile": "笑顔",
        "frown": "しかめ顔", "grin": "ニッコリ",
        "tongue_out": "舌出し", "tongue": "舌", "teeth": "歯",
        "fingering": "指マン", "handjob": "手コキ", "blowjob": "フェラ",
        "paizuri": "パイズリ", "thighjob": "太股コキ", "anal": "アナル", "uncensored": "無修正",
        "nude": "裸", "nipples": "乳首", "navel": "おへそ",
        "breasts": "胸", "ass": "お尻", "thighs": "ふともも", "armpits": "腋",
        "back": "背中", "shoulders": "肩", "collarbone": "鎖骨", "neck": "首",
        "feet": "足", "barefoot": "裸足",
        "cum": "ザーメン", "saliva": "唾液", "sweat": "汗", "tears": "涙",
        "blush": "赤面",
        "outdoors": "屋外", "indoors": "室内", "beach": "ビーチ", "bedroom": "ベッドルーム",
        "night": "夜", "day": "昼", "morning": "朝", "evening": "夕方",
        "forest": "森", "sky": "空", "cloud": "雲", "ocean": "海",
        "water": "水", "tree": "木", "grass": "草原", "flower": "花",
        "white_background": "白背景", "simple_background": "シンプル背景",
        "black_background": "黒背景", "gradient_background": "グラデーション背景",
        "city": "街", "street": "通り", "park": "公園", "school": "学校",
        "bedroom": "ベッドルーム", "bed": "ベッド", "bathroom": "浴室",
        "weapon": "武器", "sword": "刀", "katana": "刀", "gun": "銃",
        "holding": "持つ", "holding_weapon": "武器を持つ", "holding_sword": "刀を持つ",
        "holding_gun": "銃を持つ", "holding_cigarette": "タバコを持つ",
        "cigarette": "タバコ", "bandaid": "絆創膏", "bottle": "ボトル",
        "food": "食べ物", "drink": "飲み物", "book": "本", "phone": "携帯",
        "petite": "ロリ", "loli": "ロリ",
        "flat_chest": "貧乳", "small_breasts": "小ぶり", "medium_breasts": "美乳",
        "fang": "牙", "small_fangs": "牙",
        "pale_skin": "白肌", "dark_skin": "褐色肌", "tan": "日焼け",
        "sparkle": "キラキラ", "petals": "花びら", "snow": "雪", "rain": "雨",
        "fingerless_gloves": "フィンガーレスグローブ",
    },
    "selling_points": [
        {"trigger": ["large_breasts", "huge_breasts", "gigantic_breasts", "oversized_breasts"], "tag": "巨乳", "min_score": 0.5},
        {"trigger": ["thick_thighs", "thicc_thighs", "thick_legs"], "tag": "魅惑のふともも", "min_score": 0.5},
        {"trigger": ["huge_ass", "big_ass", "large_ass", "plump_ass", "bubble_butt"], "tag": "お尻", "min_score": 0.5},
        {"trigger": ["futanari", "futa"], "tag": "ふたなり", "min_score": 0.5},
        {"trigger": ["lactation", "breast_milk", "milk"], "tag": "母乳", "min_score": 0.5},
        {"trigger": ["pregnant", "pregnancy"], "tag": "妊娠", "min_score": 0.5},
        {"trigger": ["bondage", "tied_up", "rope_bondage"], "tag": "緊縛", "min_score": 0.5},
        {"trigger": ["maid", "maid_outfit", "maid_uniform"], "tag": "メイド", "min_score": 0.5},
        {"trigger": ["nun", "nun_outfit"], "tag": "シスター", "min_score": 0.5},
        {"trigger": ["yandere"], "tag": "ヤンデレ", "min_score": 0.5},
        {"trigger": ["nipple_piercing"], "tag": "乳首ピアス", "min_score": 0.5},
        {"trigger": ["nude", "naked", "topless", "completely_nude", "nude_filter"], "tag": "極上女体", "min_score": 0.6},
        {"trigger": ["thighs", "bare_thighs", "long_legs", "bare_legs", "legs_apart"], "tag": "美脚", "min_score": 0.5},
        {"trigger": ["nipples", "breasts"], "tag": "おっぱい", "min_score": 0.5},
        {"trigger": ["medium_breasts"], "tag": "美乳", "min_score": 0.55},
        {"trigger": ["pale_skin", "fair_skin", "white_skin", "light_skin"], "tag": "白肌", "min_score": 0.5},
        {"trigger": ["ass", "buttocks", "bare_ass", "ass_focus"], "tag": "美尻", "min_score": 0.5},
        {"trigger": ["lingerie", "underwear", "see-through", "negligee"], "tag": "ランジェリー", "min_score": 0.5},
        {"trigger": ["sailor_uniform", "serafuku"], "tag": "セーラー服", "min_score": 0.7},
        {"trigger": ["cat_ears", "cat_girl", "nekomimi"], "tag": "猫娘", "min_score": 0.6},
        {"trigger": ["black_thighhighs", "black_stockings", "black_legwear"], "tag": "黒ニーソ", "min_score": 0.6},
        {"trigger": ["white_thighhighs", "white_stockings", "white_legwear"], "tag": "白ニーソ", "min_score": 0.6},
        {"trigger": ["glasses", "eyewear"], "tag": "眼鏡っ娘", "min_score": 0.7},
        {"trigger": ["kimono", "japanese_clothes", "yukata", "hakama"], "tag": "和服", "min_score": 0.6},
        {"trigger": ["twin_tails", "twintails"], "tag": "ツインテール", "min_score": 0.8},
        {"trigger": ["swimsuit", "one-piece_swimsuit", "competition_swimsuit"], "tag": "水着", "min_score": 0.7},
        {"trigger": ["bikini", "micro_bikini", "string_bikini"], "tag": "ビキニ", "min_score": 0.7},
        {"trigger": ["drill_hair", "ringlets", "spiral_curls"], "tag": "縦ロール", "min_score": 0.6},
        {"trigger": ["animal_ears", "kemonomimi"], "tag": "ケモ耳", "min_score": 0.6},
        {"trigger": ["vampire", "vampire_girl", "bat_wings"], "tag": "吸血鬼", "min_score": 0.5},
        {"trigger": ["dragon_girl", "dragon_horns", "dragon_tail", "dragon_wings"], "tag": "ドラゴンガール", "min_score": 0.5},
    ],
}


DEFAULT_CENSOR_CONFIG = {
    "_comment": "自动打码配置。删了重启会重生默认值。",
    "_help": {
        "mode": "mosaic / blur / bar",
        "conf_threshold": "0.0~1.0；越高漏检越多但误检越少（推荐 0.4-0.5）",
        "enabled_classes": "要打码的类别：dick / vagina(=pussy) / anus / cum / tits(=breasts)",
        "bar_count": "仅 mode=bar 时生效，横条 bar 的数量"
    },
    "mode": "mosaic",
    "conf_threshold": 0.45,
    "enabled_classes": ["dick", "vagina", "anus", "cum"],
    "bar_count": 4
}


DEFAULT_CIVITAI_SAFETY = {
    "_comment": "Civitai 安全过滤。命中 minor_tags 或 school_tags 且评级在 unsafe_ratings 内时跳过上传。删了重启会重生默认值。",
    "minor_tags": [
        "loli", "lolicon", "shota", "shotacon",
        "child", "children", "young child", "little girl", "underage"
    ],
    "school_tags": [
        "school uniform", "school_uniform",
        "sailor uniform", "sailor_uniform",
        "serafuku", "randoseru",
        "school swimsuit", "school_swimsuit",
        "gym uniform", "gym_uniform",
        "jk", "js", "jc"
    ],
    "unsafe_ratings": ["r18", "r18g"]
}


def ensure_runtime_files(script_dir: Path) -> dict[str, Path]:
    pixiv_dir = script_dir / "pixiv"
    pixiv_dir.mkdir(exist_ok=True)
    rule_fit_root = pixiv_dir / "rule_fit"
    files = {
        "aliases":      pixiv_dir / "tag_aliases.json",
        "popularity":   pixiv_dir / "tag_popularity.json",
        "validation":   pixiv_dir / "validation_cases.json",
        "age_rules":    pixiv_dir / "age_rules.json",
        "jp_aliases":   pixiv_dir / "jp_aliases.json",
        "general_jp":   pixiv_dir / "general_jp.json",
        "danbooru_jp":  pixiv_dir / "danbooru_jp.json",
        "censor_config": pixiv_dir / "censor.json",
        "civitai_safety": script_dir / "civitai_safety.json",
        "manifests":    script_dir / "manifests",
        "rule_fit_root":      rule_fit_root,
        "rule_fit_samples":   rule_fit_root / "samples",
        "rule_fit_manifests": rule_fit_root / "manifests",
        "rule_fit_reports":   rule_fit_root / "reports",
    }
    for key in ("manifests", "rule_fit_root", "rule_fit_samples", "rule_fit_manifests", "rule_fit_reports"):
        files[key].mkdir(exist_ok=True)
    defaults = {
        "aliases": DEFAULT_ALIAS_DATA,
        "popularity": DEFAULT_POPULARITY_DATA,
        "validation": DEFAULT_VALIDATION_CASES,
        "age_rules": DEFAULT_AGE_RULES,
        "jp_aliases": {},
        "general_jp": DEFAULT_GENERAL_JP,
        "censor_config": DEFAULT_CENSOR_CONFIG,
        "civitai_safety": DEFAULT_CIVITAI_SAFETY,
    }
    for key, payload in defaults.items():
        if not files[key].exists():
            save_json(files[key], payload)
    return files


def safe_stem(name: str) -> str:
    return SAFE_STEM_RE.sub("_", name).strip("._") or "image"


def now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


@dataclass
class CleanResult:
    output_path: Path
    width: int
    height: int


class HainTagBridge:
    def __init__(self, root: Path) -> None:
        self._root = root
        self._reader_cls = None

    def is_available(self) -> bool:
        return self._load_reader_cls() is not None

    def read_metadata(self, path: Path) -> dict[str, Any]:
        reader_cls = self._load_reader_cls()
        if reader_cls is None:
            return {"available": False, "status": "unavailable", "detected_types": [], "details": []}
        try:
            reader = reader_cls()
            raw_chunks = {}
            if path.suffix.lower() == ".png" and hasattr(reader, "_read_png_text_chunks"):
                raw_chunks = reader._read_png_text_chunks(str(path)) or {}
            result = reader.read_metadata(str(path))
        except Exception as exc:
            return {
                "available": True,
                "status": "error",
                "detected_types": [],
                "details": [f"{type(exc).__name__}: {exc}"],
            }

        if result is None:
            if raw_chunks:
                return {
                    "available": True,
                    "status": "failed",
                    "detected_types": sorted(raw_chunks.keys()),
                    "details": ["raw_text_chunks"],
                }
            return {"available": True, "status": "clean", "detected_types": [], "details": []}

        detected_types = []
        if getattr(result, "generator", None):
            detected_types.append(str(result.generator.value))
        result_chunks = getattr(result, "raw_chunks", {}) or {}
        merged_chunks = dict(raw_chunks)
        merged_chunks.update(result_chunks)
        if merged_chunks:
            detected_types.extend(sorted(merged_chunks.keys()))
        has_content = bool(getattr(result, "has_content", False))
        details = []
        if getattr(result, "positive_prompt", ""):
            details.append("positive_prompt")
        if getattr(result, "negative_prompt", ""):
            details.append("negative_prompt")
        if getattr(result, "parameters", {}):
            details.append("parameters")
        if merged_chunks:
            details.append("raw_text_chunks")
        return {
            "available": True,
            "status": "failed" if (has_content or merged_chunks) else "clean",
            "detected_types": sorted(set(detected_types)),
            "details": details,
            "metadata": result,
        }

    def _load_reader_cls(self):
        if self._reader_cls is not None:
            return self._reader_cls
        if not self._root.exists():
            return None
        root_str = str(self._root)
        if root_str not in sys.path:
            sys.path.insert(0, root_str)
        try:
            module = importlib.import_module("native_app.metadata")
            self._reader_cls = getattr(module, "MetadataReader", None)
        except Exception:
            self._reader_cls = None
        return self._reader_cls


class _DistSubprocessEngine:
    """Calls tagger_subprocess.py from a compiled HainTag distribution."""
    is_ready = True

    def __init__(self, script: str, model_path: str, mapping_path: str, python: str) -> None:
        self._script = script
        self._model_path = model_path
        self._mapping_path = mapping_path
        self._python = python

    def predict(
        self,
        image_path: str,
        gen_threshold: float = 0.35,
        char_threshold: float = 0.70,
        enabled_categories=None,
        blacklist=None,
    ) -> dict:
        import subprocess
        import json as _json
        cats = ",".join(enabled_categories or {"general", "character", "copyright"})
        bl   = ",".join(blacklist or [])
        cmd  = [
            self._python, "-E", self._script, str(image_path),
            self._model_path, self._mapping_path,
            str(gen_threshold), str(char_threshold), cats, bl,
        ]
        clean_env = dict(os.environ)
        for v in ("PYTHONHOME", "PYTHONPATH", "_MEIPASS"):
            clean_env.pop(v, None)
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=60,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            env=clean_env,
        )
        if result.returncode != 0:
            raise RuntimeError(f"tagger_subprocess error: {result.stderr.strip()[:200]}")
        data = _json.loads(result.stdout.strip())
        if "error" in data:
            raise RuntimeError(data["error"])
        return data.get("results", data)


class HainTagTaggerBridge:
    def __init__(self, root: Path) -> None:
        self._root = root
        self._engine = None
        self._settings = None
        self._status = "uninitialized"

    @property
    def _model_dir(self) -> str:
        return self._load_settings().get("tagger_model_dir", "")

    def predict_tags(self, path: Path) -> dict[str, Any]:
        engine = self._ensure_engine()
        if engine is None:
            return {"available": False, "status": self._status, "flat_tags": [], "groups": {}, "details": [], "rating_scores": {}}

        settings = self._settings or {}
        enabled_categories = set(settings.get("tagger_local_enabled_categories") or ["general", "character", "copyright"])
        enabled_with_rating = enabled_categories | {"rating"}
        gen_threshold = float(settings.get("tagger_local_general_threshold", 55)) / 100.0
        char_threshold = float(settings.get("tagger_local_character_threshold", 60)) / 100.0
        try:
            groups = engine.predict(
                str(path),
                gen_threshold=gen_threshold,
                char_threshold=char_threshold,
                enabled_categories=enabled_with_rating,
            )
        except Exception as exc:
            return {
                "available": True,
                "status": "error",
                "flat_tags": [],
                "groups": {},
                "details": [f"{type(exc).__name__}: {exc}"],
                "rating_scores": {},
            }

        rating_scores: dict[str, float] = {}
        for tag, score in groups.pop("rating", []):
            rating_scores[str(tag)] = round(float(score), 4)

        flat = []
        for category, entries in groups.items():
            for tag, score in entries:
                flat.append({"tag": tag, "score": round(float(score), 4), "category": category})
        flat.sort(key=lambda item: item["score"], reverse=True)
        return {
            "available": True,
            "status": "ok",
            "flat_tags": [item["tag"] for item in flat],
            "groups": {key: [(tag, round(float(score), 4)) for tag, score in value] for key, value in groups.items()},
            "details": [],
            "scored_tags": flat,
            "rating_scores": rating_scores,
        }

    def _ensure_engine(self):
        if self._engine is not None:
            return self._engine
        if not self._root.exists():
            self._status = "haintag_root_missing"
            return None

        root_str = str(self._root)
        if root_str not in sys.path:
            sys.path.insert(0, root_str)

        settings = self._load_settings()
        self._settings = settings
        model_dir = settings.get("tagger_model_dir", "")
        external_python = settings.get("tagger_python_path", "") or None
        appdata_dir = os.environ.get("APPDATA", "") or str(Path.home() / "AppData" / "Roaming")
        _source_engine = None
        try:
            module = importlib.import_module("native_app.tagger")
            engine_cls = getattr(module, "TaggerEngine", None)
            if engine_cls is None:
                self._status = "tagger_engine_missing"
                return None
            engine = engine_cls(model_dir=model_dir or None)
            model_path, mapping_path = engine.find_model(
                custom_dir=model_dir or None,
                appdata_dir=str(Path(appdata_dir) / "HainTag"),
            )
            if (not model_path or not mapping_path) and model_dir:
                model_path, mapping_path = self._scan_model_dir(model_dir)
            if not model_path or not mapping_path:
                self._status = "model_not_found"
                return None
            engine.load(model_path, mapping_path, external_python=external_python)
            if not getattr(engine, "is_ready", False):
                self._status = "engine_not_ready"
                return None
            _source_engine = engine
        except Exception:
            pass

        if _source_engine is not None:
            self._engine = _source_engine
            self._status = "ok"
            return self._engine

        # Dist mode: _internal/native_app/tagger_subprocess.py
        dist_script = self._root / "_internal" / "native_app" / "tagger_subprocess.py"
        if dist_script.exists():
            return self._init_dist_engine(dist_script)

        self._status = "import_error"
        return None

    def _init_dist_engine(self, dist_script: Path):
        import sys as _sys
        settings = self._load_settings()
        self._settings = settings
        model_dir = settings.get("tagger_model_dir", "")
        appdata_dir = os.environ.get("APPDATA", "") or str(Path.home() / "AppData" / "Roaming")
        model_path, mapping_path = self._scan_model_dir(model_dir) if model_dir else (None, None)
        if not model_path or not mapping_path:
            model_sub = str(Path(appdata_dir) / "HainTag" / "models" / "cl_tagger")
            model_path, mapping_path = self._scan_model_dir(model_sub)
        if not model_path or not mapping_path:
            self._status = "model_not_found"
            return None
        engine = _DistSubprocessEngine(
            script=str(dist_script),
            model_path=model_path,
            mapping_path=mapping_path,
            python=_sys.executable,
        )
        self._engine = engine
        self._status = "ok"
        return engine

    @staticmethod
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

    @staticmethod
    def _load_settings() -> dict[str, Any]:
        appdata_root = os.environ.get("APPDATA")
        base_path = Path(appdata_root) if appdata_root else Path.home() / "AppData" / "Roaming"
        settings_path = base_path / "HainTag" / "settings.json"
        if not settings_path.exists():
            return {}
        try:
            payload = json.loads(settings_path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        return payload.get("settings", payload) if isinstance(payload, dict) else {}


def sanitize_image_for_pixiv(src: Path, dest_dir: Path) -> CleanResult:
    dest_dir.mkdir(exist_ok=True)
    with Image.open(src) as img:
        image = img.convert("RGB")
        dest = dest_dir / f"{src.stem}_pixiv_clean.png"
        image.save(dest, "PNG")
        return CleanResult(output_path=dest, width=image.width, height=image.height)


def split_prompt_tokens(text: str) -> list[str]:
    if not text:
        return []
    tokens = []
    for part in PROMPT_TOKEN_SPLIT_RE.split(text):
        token = part.strip()
        if not token:
            continue
        if (
            token.startswith("(")
            and token.endswith(")")
            and "\\(" not in token
            and "\\)" not in token
            and token.count("(") == 1
            and token.count(")") == 1
        ):
            token = token[1:-1].strip()
        token = re.sub(r":[0-9.]+(?<!\\)\)?$", "", token).strip()
        token = re.sub(r"^-?[0-9.]*::", "", token).rstrip(":").strip()
        if token:
            tokens.append(token)
    return tokens


def extract_lora_tokens(text: str) -> list[str]:
    return [match.group(1).strip() for match in LORA_RE.finditer(text or "")]


def extract_filename_tokens(path: Path, filename_drop_tokens: list[str]) -> list[str]:
    parts = []
    for piece in FILENAME_SPLIT_RE.split(path.stem):
        token = piece.strip()
        if not token:
            continue
        if H_UINT_RE.match(token):
            continue
        if token in filename_drop_tokens:
            continue
        parts.append(token)
    return parts


def normalize_key(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("_", " ").strip().lower())


def _unescape_metadata_token(text: str) -> str:
    return (text or "").replace("\\(", "(").replace("\\)", ")").strip()


def _canonicalize_danbooru_like_token(text: str) -> str:
    token = _unescape_metadata_token(text)
    token = re.sub(r"\s+", " ", token).strip().lower()
    if not token:
        return ""
    if "(" in token or ")" in token:
        match = METADATA_ENTITY_PAREN_RE.match(token)
        if match:
            name = match.group("name").strip().rstrip("_").replace(" ", "_")
            scope = match.group("scope").strip().replace(" ", "_")
            if name and scope:
                return f"{name}_({scope})"
    return token.replace(" ", "_")


def _metadata_semantic_info(tag: str, alias_data: dict[str, Any]) -> dict[str, Any] | None:
    key = normalize_key(tag)
    if not key:
        return None
    mappings = alias_data.get("mappings", {})
    semantics = alias_data.get("semantics", {})
    mapped = mappings.get(key)
    semantic = mapped.get("semantic") if mapped else key if key in semantics else None
    if semantic not in semantics:
        return None
    return semantics[semantic]


def _looks_generic_metadata_tag(tag: str, alias_data: dict[str, Any]) -> bool:
    info = _metadata_semantic_info(tag, alias_data)
    if not info:
        return False
    return info.get("class") in {"theme", "feature", "meta", "rating", "identity", "relation"}


def _infer_metadata_entity_category(
    tag: str,
    alias_data: dict[str, Any],
    scope_hint: str = "",
) -> str | None:
    info = _metadata_semantic_info(tag, alias_data)
    if info:
        semantic_class = str(info.get("class", ""))
        if semantic_class == "character":
            return "character"
        if semantic_class == "franchise":
            return "copyright"
        if semantic_class in {"theme", "feature", "meta", "rating", "identity", "relation"}:
            return None
    if tag.endswith("_(series)"):
        return "copyright"
    if scope_hint:
        return "character"
    return None


def _candidate_builtin_variants(tag: str) -> list[str]:
    canonical = _canonicalize_danbooru_like_token(tag)
    if not canonical:
        return []
    variants = [canonical]
    underscored = canonical
    spaced = canonical.replace("_", " ")
    compact = canonical.replace("_", "").replace(" ", "")
    for variant in (underscored, spaced, compact):
        if variant and variant not in variants:
            variants.append(variant)
    return variants


def _has_fast_jp_mapping_signal(tag: str, builtin_jp_map: dict[str, str], jp_alias_cache: dict[str, Any]) -> bool:
    for candidate in _candidate_builtin_variants(tag):
        cached = jp_alias_cache.get(candidate)
        if isinstance(cached, str) and cached.strip():
            return True
        kl = candidate.lower()
        for k_in_map in (
            candidate, kl,
            kl.replace("_", ""),
            kl.replace("_", " "),
            kl.replace(" ", "_"),
        ):
            mapped = builtin_jp_map.get(k_in_map)
            if isinstance(mapped, str) and mapped.strip():
                return True
    return False


def _scope_hint_has_entity_signal(
    scope_hint: str,
    alias_data: dict[str, Any],
    builtin_jp_map: dict[str, str],
    jp_alias_cache: dict[str, Any],
) -> bool:
    info = _metadata_semantic_info(scope_hint, alias_data)
    if info:
        semantic_class = str(info.get("class", ""))
        if semantic_class in {"character", "franchise"}:
            return True
        if semantic_class in {"theme", "feature", "meta", "rating", "identity", "relation"}:
            return False
    return _has_fast_jp_mapping_signal(scope_hint, builtin_jp_map, jp_alias_cache)


def _resolve_metadata_entity_jp(
    tag: str,
    builtin_jp_map: dict[str, str],
    jp_alias_cache: dict[str, Any],
    pixiv_page: Any,
    live_jp_lookup: bool,
    wiki_strict_kana: bool,
) -> tuple[str | None, str | None]:
    for candidate in _candidate_builtin_variants(tag):
        jp_form = lookup_jp_alias(
            candidate,
            jp_alias_cache,
            page=pixiv_page,
            live=live_jp_lookup,
            wiki_strict_kana=wiki_strict_kana if live_jp_lookup else None,
            builtin_map=builtin_jp_map,
        )
        if jp_form:
            return candidate, jp_form
    return None, None


def extract_metadata_entity_groups(
    metadata_info: dict[str, Any],
    alias_data: dict[str, Any],
    builtin_jp_map: dict[str, str],
    jp_alias_cache: dict[str, Any],
    pixiv_page: Any,
    live_jp_lookup: bool,
) -> dict[str, Any]:
    metadata = metadata_info.get("metadata")
    positive_prompt = getattr(metadata, "positive_prompt", "") if metadata else ""
    lora_candidates = extract_lora_tokens(positive_prompt)
    text_candidates = list(split_prompt_tokens(LORA_RE.sub("", positive_prompt)))
    text_candidates.extend(lora_candidates)

    groups: dict[str, list[tuple[str, float]]] = {"character": [], "copyright": []}
    hits: list[dict[str, Any]] = []
    seen_pairs: set[tuple[str, str]] = set()
    seen_copy_norms: set[str] = set()

    def add_group(category: str, token: str, source: str, resolved_from: str, display: str) -> None:
        key = normalize_key(token)
        if not key:
            return
        pair = (category, key)
        if pair in seen_pairs:
            return
        seen_pairs.add(pair)
        score = 1.0 if category == "character" else 0.95
        groups.setdefault(category, []).append((token, score))
        hits.append({
            "category": category,
            "token": token,
            "source": source,
            "resolved_from": resolved_from,
            "jp": display,
        })
        if category == "copyright":
            seen_copy_norms.add(key)

    for raw in text_candidates:
        source = "lora" if raw in lora_candidates else "metadata"
        token = _canonicalize_danbooru_like_token(raw)
        if not token:
            continue
        if raw.startswith("@"):
            continue
        if _looks_generic_metadata_tag(token, alias_data):
            continue

        copyright_candidate = ""
        match = METADATA_ENTITY_PAREN_RE.match(_unescape_metadata_token(raw).lower())
        scope_hint = ""
        if match:
            scope_hint = match.group("scope").strip().lower()
            if scope_hint:
                copyright_candidate = scope_hint.replace(" ", "_")

        category = _infer_metadata_entity_category(token, alias_data, scope_hint)
        if category == "character" and scope_hint and not _scope_hint_has_entity_signal(
            scope_hint,
            alias_data,
            builtin_jp_map,
            jp_alias_cache,
        ):
            continue
        if category is None:
            continue

        resolved_from, jp_form = _resolve_metadata_entity_jp(
            token,
            builtin_jp_map,
            jp_alias_cache,
            pixiv_page,
            live_jp_lookup,
            wiki_strict_kana=False,
        )
        if not jp_form:
            continue
        add_group(category, token, source, resolved_from or token, jp_form)

        if category == "character" and copyright_candidate:
            resolved_copy_from, copy_jp_form = _resolve_metadata_entity_jp(
                copyright_candidate,
                builtin_jp_map,
                jp_alias_cache,
                pixiv_page,
                live_jp_lookup,
                wiki_strict_kana=False,
            )
            if copy_jp_form and normalize_key(copyright_candidate) not in seen_copy_norms:
                add_group("copyright", copyright_candidate, source, resolved_copy_from or copyright_candidate, copy_jp_form)

    return {"groups": groups, "hits": hits}


def fetch_pixiv_tag_count(tag: str) -> int | None:
    url = f"{PIXIV_BASE}/tags/{quote(tag, safe='')}/artworks"
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": PIXIV_BASE,
        "Accept-Language": "ja,en-US;q=0.8,en;q=0.7",
    }
    try:
        with httpx.Client(timeout=PIXIV_COUNT_TIMEOUT_SECONDS, follow_redirects=True, headers=headers) as client:
            resp = client.get(url)
            resp.raise_for_status()
            text = resp.text
    except Exception:
        return None

    for pattern in PIXIV_COUNT_PATTERNS:
        match = pattern.search(text)
        if match:
            try:
                return int(match.group(1).replace(",", ""))
            except ValueError:
                continue
    return None


def choose_semantic_winner(
    semantic: str,
    alias_data: dict[str, Any],
    popularity_data: dict[str, Any],
    live_lookup: bool,
    live_budget: dict[str, int] | None = None,
) -> tuple[str, dict[str, Any]]:
    info = alias_data["semantics"][semantic]
    candidates = list(info["candidates"])
    group = popularity_data.setdefault("groups", {}).setdefault(
        semantic,
        {"winner": info["default"], "counts": {candidate: 0 for candidate in candidates}, "updated_at": ""},
    )
    counts = dict(group.get("counts", {}))
    decision = {
        "semantic": semantic,
        "candidates": candidates,
        "winner": group.get("winner", info["default"]),
        "source": "cache" if any(counts.values()) else "default",
        "counts": counts,
    }
    if len(candidates) == 1:
        candidate = candidates[0]
        if live_lookup and not counts.get(candidate) and (live_budget is None or live_budget.get("remaining", 0) > 0):
            if live_budget is not None:
                live_budget["remaining"] = max(0, live_budget.get("remaining", 0) - 1)
            count = fetch_pixiv_tag_count(candidate)
            if count is not None:
                counts[candidate] = count
                group["counts"] = counts
                group["updated_at"] = datetime.now().isoformat(timespec="seconds")
                decision["source"] = "live"
        decision["counts"] = counts
        decision["winner"] = candidate
        decision["winner_count"] = int(counts.get(candidate, 0) or 0)
        if decision["source"] == "default":
            decision["source"] = "single"
        group["winner"] = candidate
        return candidate, decision

    if live_lookup:
        fresh = {}
        changed = False
        for candidate in candidates:
            existing = counts.get(candidate)
            if isinstance(existing, int) and existing > 0:
                fresh[candidate] = existing
                continue
            if live_budget is not None and live_budget.get("remaining", 0) <= 0:
                continue
            if live_budget is not None:
                live_budget["remaining"] = max(0, live_budget.get("remaining", 0) - 1)
            count = fetch_pixiv_tag_count(candidate)
            if count is not None:
                fresh[candidate] = count
                counts[candidate] = count
                changed = True
        if fresh:
            winner = max(fresh.items(), key=lambda item: item[1])[0]
            group["winner"] = winner
            group["counts"] = counts
            group["updated_at"] = datetime.now().isoformat(timespec="seconds")
            decision["winner"] = winner
            decision["counts"] = counts
            decision["winner_count"] = int(counts.get(winner, 0) or 0)
            decision["source"] = "live" if changed else "cache"
            return winner, decision

    winner = group.get("winner", info["default"]) or info["default"]
    group["winner"] = winner
    decision["winner"] = winner
    decision["counts"] = counts
    decision["winner_count"] = int(counts.get(winner, 0) or 0)
    return winner, decision


def infer_age_restriction(path: Path, age_rules: dict[str, Any]) -> str:
    haystacks = [str(path).lower(), str(path.parent).lower(), path.name.lower()]
    for rule in age_rules.get("rules", []):
        needle = str(rule.get("match", "")).strip().lower()
        if not needle:
            continue
        if any(needle in source for source in haystacks):
            return str(rule.get("age_restriction", "all_ages"))
    return str(age_rules.get("default", "all_ages"))


def candidate_rule_matches(candidate: str, needle: str) -> bool:
    return bool(re.search(rf"(?<![a-z0-9]){re.escape(needle)}(?![a-z0-9])", candidate))


def infer_candidate_age_restriction(candidates: list[str], age_rules: dict[str, Any]) -> str:
    normalized = set(normalize_key(candidate) for candidate in candidates)
    for rule in age_rules.get("candidate_rules", []):
        needle = normalize_key(str(rule.get("match", "")))
        if not needle:
            continue
        if needle in normalized:
            return str(rule.get("age_restriction", "all_ages"))
    return str(age_rules.get("default", "all_ages"))


def stronger_age_restriction(current: str, candidate: str) -> str:
    priority = {"all_ages": 0, "mild_sensitive": 1, "r18": 2, "r18g": 3}
    current = current or "all_ages"
    candidate = candidate or "all_ages"
    return candidate if priority.get(candidate, 0) > priority.get(current, 0) else current


def _priority_map(domain: str) -> dict[str, int]:
    if domain == "fanart":
        return {
            "franchise": 0,
            "character": 1,
            "relation": 2,
            "identity": 3,
            "rating": 4,
            "theme": 5,
            "feature": 6,
            "meta": 7,
        }
    return {
        "identity": 0,
        "character": 1,
        "franchise": 2,
        "relation": 3,
        "rating": 4,
        "theme": 5,
        "feature": 6,
        "meta": 7,
    }


def force_pixiv_age_restriction(payload: dict[str, Any], age_restriction: str) -> None:
    payload["age_restriction"] = age_restriction
    if age_restriction not in R18_AGE_RESTRICTIONS:
        return
    rating_tag = "R-18G" if age_restriction == "r18g" else "R-18"
    rating_translation = "R18G" if age_restriction == "r18g" else "R18"
    final_tags = payload.setdefault("final_tags", [])
    final_translations = payload.setdefault("final_tag_translations", [])
    if rating_tag in final_tags:
        return
    final_tags.insert(0, rating_tag)
    final_translations.insert(0, rating_translation)
    del final_tags[10:]
    del final_translations[10:]


def _domain_from_semantics(semantic_entries: list[dict[str, Any]]) -> str:
    has_fanart = any(item.get("domain") == "fanart" for item in semantic_entries)
    has_original = any(item.get("domain") == "original" for item in semantic_entries)
    if has_fanart:
        return "fanart"
    if has_original:
        return "original"
    return "original"


def normalize_semantic_tag(tag: str, alias_data: dict[str, Any]) -> dict[str, Any] | None:
    key = normalize_key(tag)
    if not key:
        return None
    mappings = alias_data.get("mappings", {})
    semantics = alias_data.get("semantics", {})
    mapped = mappings.get(key)
    semantic = mapped.get("semantic") if mapped else key if key in semantics else None
    if semantic not in semantics:
        return None
    info = semantics[semantic]
    return {
        "semantic": semantic,
        "display": info.get("default", tag),
        "class": info.get("class", "theme"),
        "domain": info.get("domain", "both"),
        "input_tag": tag,
    }


def infer_pixiv_domain_from_tags(tags: list[str], alias_data: dict[str, Any]) -> str:
    normalized = [normalize_semantic_tag(tag, alias_data) for tag in tags]
    normalized = [item for item in normalized if item is not None]
    return _domain_from_semantics(normalized) if normalized else "original"


def _tag_count_for_display(display: str, decision: dict[str, Any]) -> int:
    counts = decision.get("counts", {}) if isinstance(decision, dict) else {}
    try:
        return int(counts.get(display, decision.get("winner_count", 0)) or 0)
    except Exception:
        return 0


def direct_tag_count(
    semantic: str,
    display: str,
    popularity_data: dict[str, Any],
    live_lookup: bool,
    live_budget: dict[str, int] | None = None,
) -> tuple[int, dict[str, Any]]:
    groups = popularity_data.setdefault("groups", {})
    group = groups.setdefault(
        semantic,
        {"winner": display, "counts": {display: 0}, "updated_at": ""},
    )
    counts = dict(group.get("counts", {}))
    source = "cache" if int(counts.get(display, 0) or 0) > 0 else "default"
    if live_lookup and not int(counts.get(display, 0) or 0) and (live_budget is None or live_budget.get("remaining", 0) > 0):
        if live_budget is not None:
            live_budget["remaining"] = max(0, live_budget.get("remaining", 0) - 1)
        count = fetch_pixiv_tag_count(display)
        if count is not None:
            counts[display] = count
            group["counts"] = counts
            group["updated_at"] = datetime.now().isoformat(timespec="seconds")
            source = "live"
    count = int(counts.get(display, 0) or 0)
    group["winner"] = display
    decision = {
        "semantic": semantic,
        "candidates": [display],
        "winner": display,
        "source": source,
        "counts": counts,
        "winner_count": count,
    }
    return count, decision


TAGGER_SCORE_THRESHOLDS = {
    "character": 0.70,
    "copyright": 0.60,
    "general": 0.40,
}
GENERIC_VTUBER_TAG_KEYS = {"virtual youtuber", "vtuber"}

DANBOORU_BASE = "https://danbooru.donmai.us"
JP_CHAR_RE = re.compile(r"[぀-ゟ゠-ヿ一-鿿]")
KANA_RE = re.compile(r"[぀-ゟ゠-ヿ]")  # hiragana + katakana only — exclusive to Japanese
ZH_ALIAS_HINTS = ("镜头", "女仆", "虚化", "台灯")


def _looks_japanese(text: str) -> bool:
    return bool(text) and bool(JP_CHAR_RE.search(text))


def _has_kana(text: str) -> bool:
    """True only if the string contains hiragana or katakana (uniquely JP)."""
    return bool(text) and bool(KANA_RE.search(text))


def _valid_jp_alias(text: str, strict_kana: bool | None) -> bool:
    text = text.strip()
    if not _looks_japanese(text):
        return False
    return not any(hint in text for hint in ZH_ALIAS_HINTS)


def _fetch_danbooru_jp_alias(tag: str, strict_kana: bool = False) -> str | None:
    """Look up canonical Japanese form for a Danbooru tag via wiki page.

    `other_names` may contain Chinese (e.g. blue_hair → 蓝发). When
    `strict_kana=True`, only entries with hiragana or katakana are returned —
    pure-kanji results are skipped because they could be Chinese. Use strict
    mode for general tags; loose mode for character/copyright (where many
    valid JP names are kanji-only, e.g. 東方).
    """
    if not tag or not tag.strip():
        return None
    try:
        with httpx.Client(
            timeout=10,
            headers={"User-Agent": "civitai-post-splitter/1.0 (personal use)"},
        ) as client:
            url = f"{DANBOORU_BASE}/wiki_pages.json"
            resp = client.get(url, params={"search[title]": tag, "limit": 1})
            if resp.status_code != 200:
                return None
            data = resp.json()
            if not isinstance(data, list) or not data:
                return None
            other_names = data[0].get("other_names") or []
            kana_hits: list[str] = []
            kanji_hits: list[str] = []
            for name in other_names:
                if not isinstance(name, str) or not name.strip():
                    continue
                if _has_kana(name):
                    kana_hits.append(name.strip())
                elif _looks_japanese(name):
                    kanji_hits.append(name.strip())
            if kana_hits:
                return kana_hits[0]
            if not strict_kana and kanji_hits:
                return kanji_hits[0]
            return None
    except Exception as exc:
        log.warning(f"Danbooru wiki lookup 失败 tag={tag!r}: {type(exc).__name__}: {exc}")
        return None


_PURE_HIRAGANA_RE = re.compile(r"^[぀-ゟ]+$")
_JP_RUN_RE = re.compile(r"[぀-ゟ゠-ヿ一-鿿]+")
_QUOTED_JP_RE = re.compile(r"「([^」]+)」")


def _extract_canonical_from_pixpedia(body: dict, input_tag: str) -> str | None:
    """Extract pixiv-canonical JP form from /ajax/search/tags response.

    Only uses structured fields. abstract is intentionally NOT parsed because
    it's prose ("XXX is the English form of YYY") and grabbing arbitrary JP
    runs from it produces garbage like の複数形 / アルファベットでの.
    """
    pixpedia = body.get("pixpedia") if isinstance(body, dict) else None
    pixpedia = pixpedia if isinstance(pixpedia, dict) else {}
    input_lower = input_tag.strip().lower()

    # 1. parentTag — pixiv-canonical JP form (touhou → 東方, frieren → フリーレン)
    parent = pixpedia.get("parentTag")
    if isinstance(parent, str) and _looks_japanese(parent) and parent.strip():
        return parent.strip()

    # 2. siblingsTags — alternate canonical forms
    for sibling in pixpedia.get("siblingsTags") or []:
        if isinstance(sibling, str) and _looks_japanese(sibling) and sibling.strip().lower() != input_lower:
            return sibling.strip()

    # 3. body.tagTranslation[*].ja — translation field
    translations = body.get("tagTranslation") or {}
    if isinstance(translations, dict):
        for _key, info in translations.items():
            if isinstance(info, dict):
                v = info.get("ja")
                if isinstance(v, str) and _looks_japanese(v):
                    return v.strip()

    # 4. pixpedia.tag if differs from input AND is JP-looking
    pixp_tag = pixpedia.get("tag")
    if isinstance(pixp_tag, str) and _looks_japanese(pixp_tag) and pixp_tag.strip().lower() != input_lower:
        return pixp_tag.strip()

    return None


def _fetch_pixiv_tag_canonical_via_page(page, tag: str) -> str | None:
    """Ask Pixiv (via logged-in browser context) for the canonical tag form.

    Returns the form pixiv users actually use, parsed from /ajax/search/tags
    response (pixpedia.parentTag / siblingsTags / abstract). Returns None on miss.
    """
    if not tag or not tag.strip():
        return None
    js = """async (tag) => {
        try {
            const r = await fetch(`/ajax/search/tags/${encodeURIComponent(tag)}?lang=ja`, {credentials: 'include'});
            if (!r.ok) return {error: 'http_' + r.status};
            const data = await r.json();
            return data;
        } catch (e) { return {error: String(e)}; }
    }"""
    try:
        result = page.evaluate(js, tag)
    except Exception as exc:
        log.warning(f"Pixiv tag info evaluate 失败 tag={tag!r}: {type(exc).__name__}: {exc}")
        return None
    if not isinstance(result, dict) or result.get("error"):
        return None
    body = result.get("body") or {}
    if not isinstance(body, dict):
        return None
    return _extract_canonical_from_pixpedia(body, tag)


def lookup_jp_alias(
    tag: str,
    cache: dict[str, Any],
    page=None,
    live: bool = True,
    wiki_strict_kana: bool | None = None,
    builtin_map: dict[str, str] | None = None,
) -> str | None:
    """Resolve a Danbooru tag's Pixiv-canonical form.

    Lookup priority:
      1. cache (in-session + persisted via pixiv_jp_aliases.json)
      2. builtin_map — curated Danbooru→Pixiv-JP table (pixiv_general_jp.json)
      3. Pixiv ajax (via logged-in `page`) — for character/copyright authority
      4. Danbooru wiki — when `wiki_strict_kana` is not None (None = skip wiki).
         True = only accept hiragana/katakana entries (rejects Chinese kanji);
         False = also accept pure-kanji entries (needed for 東方 etc).
    """
    if not tag:
        return None
    key = tag.strip()
    if not key:
        return None
    # 1. Cache (only respect non-empty hits — empty entries shouldn't block
    # the builtin map, which may have grown since the cache was written).
    if key in cache:
        cached = cache[key]
        if isinstance(cached, str) and cached:
            if _valid_jp_alias(cached, wiki_strict_kana):
                return cached
            cache[key] = ""
            cache_seen_empty = False
        else:
            cache_seen_empty = True
    else:
        cache_seen_empty = False
    # 2. builtin map (try multiple normalizations: lower-case, no-underscore,
    # no-space — Danbooru tag-pair dataset stores `thighhighs`/`gardenofeden`
    # while WD14 emits `thigh_highs` etc.)
    if builtin_map:
        kl = key.lower()
        for k_in_map in (
            key, kl,
            kl.replace("_", ""),
            kl.replace("_", " "),
            kl.replace(" ", "_"),
        ):
            v = builtin_map.get(k_in_map)
            if isinstance(v, str) and v.strip() and _valid_jp_alias(v, wiki_strict_kana):
                cache[key] = v.strip()
                return v.strip()
    # If cache previously recorded "no canonical" we trust that and skip the
    # network lookups — builtin already had its chance above.
    if cache_seen_empty:
        return None
    if not live:
        return None
    found = None
    if page is not None:
        found = _fetch_pixiv_tag_canonical_via_page(page, key)
    if not found and wiki_strict_kana is not None:
        found = _fetch_danbooru_jp_alias(key, strict_kana=wiki_strict_kana)
    cache[key] = found if found else ""
    return found



def build_pixiv_payload(
    image_path: Path,
    metadata_info: dict[str, Any],
    alias_data: dict[str, Any],
    popularity_data: dict[str, Any],
    age_rules: dict[str, Any],
    extra_candidates: list[str] | None = None,
    extra_groups: dict[str, list[tuple[str, float]]] | None = None,
    jp_alias_cache: dict[str, Any] | None = None,
    general_jp_data: dict[str, Any] | None = None,
    pixiv_page: Any = None,
    live_lookup: bool = True,
    live_jp_lookup: bool = True,
    include_ai_art: bool = True,
) -> dict[str, Any]:
    metadata = metadata_info.get("metadata")
    positive_prompt = getattr(metadata, "positive_prompt", "") if metadata else ""
    prompt_without_lora = LORA_RE.sub("", positive_prompt)
    filename_drop_tokens = alias_data.get("filename_drop_tokens", [])
    ambiguous_tags = {normalize_key(item) for item in alias_data.get("ambiguous_tags", [])}
    drop_tags = {normalize_key(item) for item in alias_data.get("drop_tags", [])}
    extra_groups = extra_groups or {}

    # Build direct-pass-through index for tagger-output tags that should bypass
    # alias mapping. character/copyright are always direct (their semantic is
    # the tag itself, no curated japanese alias). general tags are direct only
    # when they survive the drop-list and length checks but have no alias entry.
    if jp_alias_cache is None:
        jp_alias_cache = {}
    general_jp_data = general_jp_data or {}
    # Build a chained lookup: user-tunable overrides win, then the bulk
    # 151k-entry Danbooru→JP dataset (loaded separately in cmd_upload and
    # injected here via general_jp_data["_danbooru_map"] for simplicity).
    user_overrides = general_jp_data.get("mappings") or {}
    bulk_danbooru = general_jp_data.get("_danbooru_map") or {}
    if user_overrides and bulk_danbooru:
        builtin_jp_map = dict(bulk_danbooru)
        builtin_jp_map.update(user_overrides)
    else:
        builtin_jp_map = user_overrides or bulk_danbooru

    metadata_entities = extract_metadata_entity_groups(
        metadata_info=metadata_info,
        alias_data=alias_data,
        builtin_jp_map=builtin_jp_map,
        jp_alias_cache=jp_alias_cache,
        pixiv_page=pixiv_page,
        live_jp_lookup=live_jp_lookup,
    )
    metadata_entity_groups = metadata_entities.get("groups", {})
    metadata_entity_hits = metadata_entities.get("hits", [])
    merged_extra_groups: dict[str, list[tuple[str, float]]] = {
        category: list(entries or []) for category, entries in extra_groups.items()
    }
    for category, entries in metadata_entity_groups.items():
        merged_extra_groups.setdefault(category, []).extend(entries or [])
    extra_groups = merged_extra_groups

    has_specific_character = any(
        normalize_key(entry[0] if isinstance(entry, (tuple, list)) and entry else entry)
        for entry in extra_groups.get("character", []) or []
        if not isinstance(entry, (tuple, list)) or len(entry) != 2 or float(entry[1]) >= TAGGER_SCORE_THRESHOLDS["character"]
    )

    # age check only uses prompt/filename — tagger tags excluded (false positive risk)
    age_candidates = []
    age_candidates.extend(extract_filename_tokens(image_path, filename_drop_tokens))
    age_candidates.extend(split_prompt_tokens(prompt_without_lora))
    age_candidates.extend(extract_lora_tokens(positive_prompt))

    raw_candidates = list(age_candidates)
    raw_candidates.extend(extra_candidates or [])
    for entries in extra_groups.values():
        for entry in entries or []:
            raw_candidates.append(str(entry[0] if isinstance(entry, (tuple, list)) and entry else entry))

    # normalized_key -> (display, semantic_class, domain_hint, score, source_category)
    direct_pass: dict[str, tuple[str, str, str, float, str]] = {}
    cat_meta = {
        "character": ("character", "fanart"),
        "copyright": ("franchise", "fanart"),
        "general": ("feature", "both"),
    }
    # Translate character/copyright/general via Pixiv ajax (with Danbooru wiki
    # fallback for character/copyright only — wiki is unreliable for generic
    # words like "shirt"/"panties"). General tags that have no pixiv canonical
    # stay in their original Danbooru English form.
    jp_lookup_categories = {"character", "copyright", "general"}
    for category in ("character", "copyright", "general"):
        cls, dom = cat_meta[category]
        threshold = TAGGER_SCORE_THRESHOLDS.get(category, 0.5)
        for entry in extra_groups.get(category, []) or []:
            # accept either (tag, score) tuple or bare string for back-compat
            if isinstance(entry, (tuple, list)) and len(entry) == 2:
                tag, score = entry[0], float(entry[1])
            else:
                tag, score = str(entry), 1.0
            if score < threshold:
                continue
            key = normalize_key(tag)
            if not key:
                continue
            if category == "general" and key in GENERIC_VTUBER_TAG_KEYS and not has_specific_character:
                continue
            # Default display: Danbooru form with underscore preserved (Pixiv
            # accepts blue_hair style and won't space-split it).
            display = tag.strip()
            if not display:
                continue
            # Translate priority: cache → builtin → pixiv ajax → wiki fallback.
            # general → strict (kana required, blocks Chinese kanji);
            # character/copyright → loose (kanji-only forms like 東方 OK).
            if category in jp_lookup_categories:
                jp_form = lookup_jp_alias(
                    tag,
                    jp_alias_cache,
                    page=pixiv_page,
                    live=live_jp_lookup,
                    wiki_strict_kana=(True if category == "general" else False) if live_jp_lookup else None,
                    builtin_map=builtin_jp_map,
                )
                if jp_form:
                    display = jp_form
            direct_pass.setdefault(key, (display, cls, dom, score, category))

    dedup_raw = []
    seen_raw = set()
    for candidate in raw_candidates:
        key = normalize_key(candidate)
        if not key or key in seen_raw:
            continue
        seen_raw.add(key)
        dedup_raw.append(candidate.strip())

    semantic_entries = []
    popularity_decisions = []
    rejected_tags = []
    seen_semantics = set()
    source_order: dict[str, int] = {}
    live_count_budget = {"remaining": PIXIV_COUNT_LIVE_BUDGET if live_lookup else 0}
    selling_count_budget = {"remaining": PIXIV_COUNT_SELLING_BUDGET if live_lookup else 0}
    mappings = alias_data.get("mappings", {})
    semantics = alias_data.get("semantics", {})

    for raw in dedup_raw:
        key = normalize_key(raw)
        if not key:
            continue
        if raw.startswith("@"):
            rejected_tags.append({"tag": raw, "reason": "style_marker"})
            continue
        if key in GENERIC_VTUBER_TAG_KEYS and not has_specific_character:
            rejected_tags.append({"tag": raw, "reason": "generic_vtuber_without_character"})
            continue
        mapped = mappings.get(key)
        source_info = direct_pass.get(key)
        source_category = source_info[4] if source_info else "raw"
        has_semantic_proof = bool(mapped or key in semantics or source_category in {"character", "copyright"})
        if key in ambiguous_tags and not has_semantic_proof:
            rejected_tags.append({"tag": raw, "reason": "ambiguous_without_semantic_proof"})
            continue
        if key in drop_tags and source_category not in {"character", "copyright"}:
            rejected_tags.append({"tag": raw, "reason": "drop_tag"})
            continue
        # Tagger tag bypass: character/copyright always direct (no curated jp alias);
        # general tags also bypass when alias has no entry, letting Pixiv's autocomplete
        # do the JP normalization at fill time.
        if source_info and key not in mappings and key not in semantics:
            display, cls, dom, score, _category = source_info
            semantic_id = f"{cls}:{key}"
            if semantic_id in seen_semantics:
                continue
            seen_semantics.add(semantic_id)
            count, decision = direct_tag_count(
                semantic_id,
                display,
                popularity_data,
                live_lookup=live_lookup,
                live_budget=live_count_budget,
            )
            decision["tagger_score"] = score
            popularity_decisions.append(decision)
            source_order.setdefault(semantic_id, len(source_order))
            semantic_entries.append({
                "semantic": semantic_id,
                "display": display,
                "zh": display,
                "class": cls,
                "domain": dom,
                "count": count,
                "source_order": source_order[semantic_id],
                "source_category": _category,
            })
            continue
        if not mapped:
            if key in semantics:
                mapped = {"semantic": key}
            else:
                if any(ch.isalpha() for ch in raw) and len(raw) > 40:
                    rejected_tags.append({"tag": raw, "reason": "too_long"})
                    continue
                rejected_tags.append({"tag": raw, "reason": "unmapped"})
                continue
        semantic = mapped.get("semantic")
        if semantic not in semantics:
            rejected_tags.append({"tag": raw, "reason": "missing_semantic"})
            continue
        bundle = [semantic, *(mapped.get("extras") or [])]
        for item_semantic in bundle:
            if item_semantic in seen_semantics:
                continue
            seen_semantics.add(item_semantic)
            source_order.setdefault(item_semantic, len(source_order))
            winner, decision = choose_semantic_winner(
                item_semantic,
                alias_data,
                popularity_data,
                live_lookup=live_lookup,
                live_budget=live_count_budget,
            )
            popularity_decisions.append(decision)
            info = semantics[item_semantic]
            semantic_entries.append(
                {
                    "semantic": item_semantic,
                    "display": winner,
                    "zh": info.get("zh", winner),
                    "class": info.get("class", "theme"),
                    "domain": info.get("domain", "both"),
                    "count": _tag_count_for_display(winner, decision),
                    "source_order": source_order[item_semantic],
                }
            )

    domain = _domain_from_semantics(semantic_entries)
    age_restriction = stronger_age_restriction(
        infer_age_restriction(image_path, age_rules),
        infer_candidate_age_restriction(age_candidates, age_rules),
    )

    if include_ai_art and not any(item["semantic"] == "ai_art" for item in semantic_entries):
        winner, decision = choose_semantic_winner("ai_art", alias_data, popularity_data, live_lookup=live_lookup, live_budget=live_count_budget)
        popularity_decisions.append(decision)
        info = semantics["ai_art"]
        semantic_entries.append(
            {
                "semantic": "ai_art",
                "display": winner,
                "zh": info.get("zh", winner),
                "class": info.get("class", "meta"),
                "domain": info.get("domain", "both"),
                "count": _tag_count_for_display(winner, decision),
                "source_order": -1,
            }
        )

    if age_restriction in {"r18", "r18g"}:
        rating_semantic = "r18g" if age_restriction == "r18g" else "r18"
        if not any(item["semantic"] == rating_semantic for item in semantic_entries):
            winner, decision = choose_semantic_winner(rating_semantic, alias_data, popularity_data, live_lookup=False)
            popularity_decisions.append(decision)
            info = semantics[rating_semantic]
            semantic_entries.append(
                {
                    "semantic": rating_semantic,
                    "display": winner,
                    "zh": info.get("zh", winner),
                    "class": info.get("class", "rating"),
                    "domain": info.get("domain", "both"),
                    "count": _tag_count_for_display(winner, decision),
                    "source_order": -2,
                }
            )

    required_semantics = {"ai_art", "r18", "r18g"}
    required_entries = [item for item in semantic_entries if item.get("semantic") in required_semantics]
    content_entries = [item for item in semantic_entries if item.get("semantic") not in required_semantics]

    def is_fanart_entity(item: dict[str, Any]) -> bool:
        if item.get("domain") != "fanart":
            return False
        return item.get("class") in {"franchise", "character", "identity"} or item.get("source_category") in {"copyright", "character"}

    def fanart_entity_key(item: dict[str, Any]) -> tuple[int, int, str, str]:
        source_category = item.get("source_category")
        cls = item.get("class")
        entity_rank = 0 if source_category == "copyright" or cls == "franchise" else 1
        return (entity_rank, int(item.get("source_order", 9999)), str(item.get("semantic", "")), item["display"])

    selling_points = general_jp_data.get("selling_points") or []
    tagger_keys: dict[str, float] = {}
    for entries in extra_groups.values():
        for entry in entries:
            if isinstance(entry, (tuple, list)) and len(entry) == 2:
                t, s = entry[0], float(entry[1])
            else:
                t, s = str(entry), 1.0
            k = normalize_key(t)
            if k:
                tagger_keys[k] = max(tagger_keys.get(k, 0.0), s)
    seen_selling = {item.get("display") for item in content_entries}
    for rule in selling_points:
        triggers = rule.get("trigger") or []
        threshold = float(rule.get("min_score", 0.5))
        sp_tag = rule.get("tag")
        if not sp_tag or sp_tag in seen_selling:
            continue
        if not any(tagger_keys.get(normalize_key(t), 0) >= threshold for t in triggers):
            continue
        semantic_id = f"selling:{normalize_key(sp_tag)}"
        count, decision = direct_tag_count(
            semantic_id,
            sp_tag,
            popularity_data,
            live_lookup=live_lookup,
            live_budget=selling_count_budget,
        )
        decision["source_rule"] = "selling_point"
        popularity_decisions.append(decision)
        content_entries.append(
            {
                "semantic": semantic_id,
                "display": sp_tag,
                "zh": sp_tag,
                "class": "theme",
                "domain": "both",
                "count": count,
                "source_order": len(source_order) + len(seen_selling),
                "source_rule": "selling_point",
            }
        )
        seen_selling.add(sp_tag)

    def heat_key(item: dict[str, Any]) -> tuple[int, int, int, str, str]:
        count = int(item.get("count", 0) or 0)
        has_heat = 0 if count > 0 else 1
        return (has_heat, -count, int(item.get("source_order", 9999)), str(item.get("semantic", "")), item["display"])

    protected_entity_entries = [item for item in content_entries if is_fanart_entity(item)]
    normal_content_entries = [item for item in content_entries if not is_fanart_entity(item)]

    # Safety net: fanart entities found → domain must be fanart
    if protected_entity_entries and domain != "fanart":
        domain = "fanart"

    protected_entity_entries.sort(key=fanart_entity_key)
    required_entries.sort(key=lambda item: int(item.get("source_order", 9999)))
    normal_content_entries.sort(key=heat_key)

    final_tags = []
    final_tag_translations = []
    entity_tags: list[str] = []
    entity_tags_zh: list[str] = []
    seen_display = set()
    _ENTITY_CLASSES = {"character", "franchise", "copyright", "identity"}

    def add_final(item: dict[str, Any]) -> None:
        display = item["display"]
        if display in seen_display:
            return
        seen_display.add(display)
        final_tags.append(display)
        final_tag_translations.append(item["zh"])
        if item.get("class") in _ENTITY_CLASSES:
            entity_tags.append(display)
            entity_tags_zh.append(item.get("zh") or display)

    for entries in (protected_entity_entries, required_entries, normal_content_entries):
        for item in entries:
            if len(final_tags) >= 10:
                break
            add_final(item)
    if general_jp_data.get("force_original", True) and domain == "original" and "オリジナル" not in seen_display:
        final_tags.insert(0, "オリジナル")
        final_tag_translations.insert(0, "オリジナル")
        seen_display.add("オリジナル")

    subject = next((item for item in [*protected_entity_entries, *normal_content_entries] if item["class"] in {"character", "identity", "franchise"}), None)
    theme = next((item for item in normal_content_entries if item["class"] in {"theme", "feature"}), None)
    subject_ja = subject["display"] if subject else "AIイラスト"
    subject_zh = subject["zh"] if subject else "AI插画"
    theme_ja = theme["display"] if theme and theme["display"] != subject_ja else ""
    theme_zh = theme["zh"] if theme and theme["zh"] != subject_zh else ""
    title_ja = "無題"
    title_zh = "无题"

    top_tags_ja = "、".join(final_tags[:4])
    top_tags_zh = "、".join(final_tag_translations[:4])
    caption_ja = ""
    caption_zh = ""

    return {
        "raw_candidates": dedup_raw,
        "metadata_entity_hits": metadata_entity_hits,
        "popularity_decisions": popularity_decisions,
        "rejected_tags": rejected_tags,
        "final_tags": final_tags,
        "final_tag_translations": final_tag_translations,
        "entity_tags": entity_tags,
        "entity_tags_zh": entity_tags_zh,
        "domain": domain,
        "title_ja": title_ja,
        "title_zh": title_zh,
        "caption_ja": caption_ja,
        "caption_zh": caption_zh,
        "age_restriction": age_restriction,
        "ai_generated": True,
    }


def append_validation_case(path: Path, validation_path: Path, manifest: dict[str, Any]) -> None:
    payload = load_json(validation_path, DEFAULT_VALIDATION_CASES)
    cases = payload.setdefault("cases", [])
    case = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "source_path": str(path),
        "domain": manifest.get("pixiv", {}).get("domain", ""),
        "raw_candidates": manifest.get("pixiv", {}).get("raw_candidates", []),
        "final_tags": manifest.get("pixiv", {}).get("final_tags", []),
        "title_ja": manifest.get("pixiv", {}).get("title_ja", ""),
        "title_zh": manifest.get("pixiv", {}).get("title_zh", ""),
    }
    cases.append(case)
    payload["cases"] = cases[-200:]
    save_json(validation_path, payload)


def create_manifest_path(manifest_dir: Path, src: Path) -> Path:
    return manifest_dir / f"{now_stamp()}_{safe_stem(src.stem)}.json"


def find_target_successes(manifest_dir: Path, source_path: Path) -> dict[str, str]:
    """Scan manifest_dir for prior successful posts of the given source image.

    Returns a {target: post_url} dict. The latest manifest (by filename
    timestamp) wins per target.
    """
    if not manifest_dir.exists():
        return {}
    suffix = f"_{safe_stem(source_path.stem)}.json"
    src_str = str(source_path)
    latest: dict[str, tuple[str, str]] = {}
    for path in manifest_dir.iterdir():
        if not path.is_file() or not path.name.endswith(suffix):
            continue
        try:
            with open(path, encoding="utf-8") as f:
                manifest = json.load(f)
        except Exception:
            continue
        if manifest.get("source_path") != src_str:
            continue
        if manifest.get("dry_run"):
            continue
        status_by = manifest.get("status_by_target") or {}
        for target, status in status_by.items():
            if status not in {"success", "maybe_posted"}:
                continue
            target_block = manifest.get(target) or {}
            url = target_block.get("post_url") or ""
            if not url:
                continue
            cur = latest.get(target)
            if cur is None or path.name > cur[0]:
                latest[target] = (path.name, url)
    return {t: url for t, (_, url) in latest.items()}


def write_manifest(path: Path, manifest: dict[str, Any]) -> None:
    save_json(path, manifest)


def create_rule_fit_manifest_path(manifest_dir: Path, illust_id: str) -> Path:
    return manifest_dir / f"{illust_id}.json"


def create_rule_fit_compare_path(manifest_dir: Path, illust_id: str) -> Path:
    return manifest_dir / f"{illust_id}.compare.json"


def create_rule_fit_report_path(report_dir: Path, stem: str = "summary") -> Path:
    return report_dir / f"{now_stamp()}_{safe_stem(stem)}.json"


def open_pixiv_browser(pw, profile_dir: Path | None = None):
    target_profile = profile_dir or PIXIV_PROFILE_DIR
    context = pw.chromium.launch_persistent_context(
        str(target_profile),
        channel="chrome",
        headless=False,
        args=[
            "--start-minimized",
            "--disable-sync",
            "--no-first-run",
        ],
        ignore_default_args=["--enable-automation", "--no-sandbox"],
    )
    page = context.pages[0] if context.pages else context.new_page()
    page.set_viewport_size({"width": 1920, "height": 1080})
    try:
        page.goto("https://www.pixiv.net/", wait_until="commit", timeout=15000)
    except Exception:
        pass
    return context, page


def extract_artwork_id(url: str) -> str | None:
    match = ARTWORK_ID_RE.search(url or "")
    return match.group(1) if match else None


def canonical_artwork_url(illust_id: str) -> str:
    return f"{PIXIV_BASE}/artworks/{illust_id}"


def _fetch_json_in_page(page, url: str) -> dict[str, Any] | None:
    try:
        return page.evaluate(
            """async (url) => {
                const resp = await fetch(url, { credentials: 'include' });
                const text = await resp.text();
                try {
                    return JSON.parse(text);
                } catch (error) {
                    return { error: true, message: text.slice(0, 500) };
                }
            }""",
            url,
        )
    except Exception:
        return None


def ensure_pixiv_logged_in(page, start_url: str = PIXIV_BASE) -> None:
    safe_goto(page, start_url, wait=6)
    if "login" in page.url or "accounts.pixiv.net" in page.url:
        print("未登录 Pixiv。请在浏览器里登录 Pixiv，然后按 Enter 继续...")
        input()
        safe_goto(page, start_url, wait=6)


def _collect_artwork_urls_from_page(page, max_items: int) -> list[str]:
    urls = []
    seen = set()
    for _ in range(4):
        try:
            batch = page.evaluate(
                """() => Array.from(document.querySelectorAll('a[href*="/artworks/"]'))
                    .map((node) => node.href)
                    .filter(Boolean)"""
            ) or []
        except Exception:
            batch = []
        for url in batch:
            illust_id = extract_artwork_id(url)
            if not illust_id or illust_id in seen:
                continue
            seen.add(illust_id)
            urls.append(canonical_artwork_url(illust_id))
            if len(urls) >= max_items:
                return urls
        try:
            page.mouse.wheel(0, 2400)
        except Exception:
            pass
        time.sleep(1.2)
    return urls


def collect_artwork_urls_from_source(page, source: dict[str, Any], max_items: int) -> list[str]:
    safe_goto(page, source["url"], wait=5)
    return _collect_artwork_urls_from_page(page, max_items=max_items)


def _context_cookies_dict(context) -> dict[str, str]:
    cookies = {}
    try:
        for item in context.cookies():
            name = item.get("name")
            value = item.get("value")
            if name:
                cookies[name] = value or ""
    except Exception:
        pass
    return cookies


def fetch_pixiv_illust_data(page, illust_id: str) -> dict[str, Any] | None:
    detail_payload = _fetch_json_in_page(page, f"{PIXIV_BASE}/ajax/illust/{illust_id}")
    if not detail_payload or detail_payload.get("error"):
        return None
    pages_payload = _fetch_json_in_page(page, f"{PIXIV_BASE}/ajax/illust/{illust_id}/pages")
    if not pages_payload or pages_payload.get("error"):
        return None
    body = detail_payload.get("body") or {}
    pages = pages_payload.get("body") or []
    first_page = pages[0] if pages else {}
    urls = first_page.get("urls") or {}
    tags = [item.get("tag", "") for item in ((body.get("tags") or {}).get("tags") or []) if item.get("tag")]
    age = "r18g" if int(body.get("xRestrict", 0) or 0) >= 2 else "r18" if int(body.get("xRestrict", 0) or 0) == 1 else "all_ages"
    metrics = {
        "bookmark_count": int(body.get("bookmarkCount") or 0),
        "like_count": int(body.get("likeCount") or 0),
        "view_count": int(body.get("viewCount") or 0),
        "comment_count": int(body.get("commentCount") or 0),
    }
    return {
        "illust_id": illust_id,
        "work_url": canonical_artwork_url(illust_id),
        "title": body.get("title", ""),
        "author": body.get("userName", ""),
        "author_id": str(body.get("userId", "") or ""),
        "pixiv_tags": tags,
        "page_count": int(body.get("pageCount") or len(pages) or 1),
        "visible_age_restriction": age,
        "metrics": metrics,
        "image_urls": urls,
        "original_image_url": urls.get("original") or urls.get("regular") or body.get("urls", {}).get("original") or "",
        "ai_type": int(body.get("aiType") or 0),
        "description": body.get("description", ""),
    }


def review_traffic_metrics(
    metrics: dict[str, int],
    min_bookmarks: int,
    min_likes: int,
    min_views: int,
    min_score: float,
) -> dict[str, Any]:
    bookmark_count = int(metrics.get("bookmark_count", 0) or 0)
    like_count = int(metrics.get("like_count", 0) or 0)
    view_count = int(metrics.get("view_count", 0) or 0)
    score = bookmark_count * 12 + like_count * 3 + (view_count / 40.0)
    reasons = []
    if bookmark_count >= min_bookmarks:
        reasons.append("bookmark_count")
    if like_count >= min_likes:
        reasons.append("like_count")
    if view_count >= min_views:
        reasons.append("view_count")
    if score >= min_score:
        reasons.append("traffic_score")
    return {
        "bookmark_count": bookmark_count,
        "like_count": like_count,
        "view_count": view_count,
        "traffic_score": round(score, 2),
        "engagement_ratio": round((bookmark_count / view_count), 4) if view_count else 0.0,
        "passes": bool(reasons),
        "passed_by": reasons,
        "thresholds": {
            "min_bookmarks": min_bookmarks,
            "min_likes": min_likes,
            "min_views": min_views,
            "min_score": min_score,
        },
    }


def build_sample_manifest_from_illust(
    illust: dict[str, Any],
    source: dict[str, Any],
    alias_data: dict[str, Any],
    traffic_review: dict[str, Any],
) -> dict[str, Any]:
    tags = illust.get("pixiv_tags", [])
    domain = infer_pixiv_domain_from_tags(tags, alias_data)
    return {
        "collected_at": datetime.now().isoformat(timespec="seconds"),
        "illust_id": illust.get("illust_id", ""),
        "work_url": illust.get("work_url", ""),
        "title": illust.get("title", ""),
        "author": illust.get("author", ""),
        "author_id": illust.get("author_id", ""),
        "pixiv_tags": tags,
        "visible_age_restriction": illust.get("visible_age_restriction", "all_ages"),
        "source": {
            "name": source.get("name", ""),
            "kind": source.get("kind", ""),
            "url": source.get("url", ""),
            "tag": source.get("tag", ""),
            "domain_hint": source.get("domain_hint", ""),
            "age_hint": source.get("age_hint", ""),
        },
        "page_count": illust.get("page_count", 1),
        "original_image_url": illust.get("original_image_url", ""),
        "image_urls": illust.get("image_urls", {}),
        "metrics": illust.get("metrics", {}),
        "traffic_review": traffic_review,
        "domain": domain,
        "ai_generated": bool(int(illust.get("ai_type", 0) or 0) > 0),
        "description": illust.get("description", ""),
        "sample_image_path": "",
        "selected_image_url": "",
        "selected_image_variant": "",
        "image_download_attempts": [],
        "image_download_status": "pending",
        "image_download_error": "",
    }


def rule_fit_sample_score(item: dict[str, Any]) -> tuple[float, int, int]:
    return (
        float(item.get("traffic_review", {}).get("traffic_score", 0.0) or 0.0),
        int(item.get("metrics", {}).get("bookmark_count", 0) or 0),
        int(item.get("metrics", {}).get("like_count", 0) or 0),
    )


def is_rule_fit_image_ready(item: dict[str, Any]) -> bool:
    sample_image_path = Path(item.get("sample_image_path", "")) if item.get("sample_image_path") else None
    return bool(
        item.get("image_download_status") in {"", "success"}
        and sample_image_path
        and sample_image_path.exists()
    )


def rule_fit_distribution_counts(items: list[dict[str, Any]]) -> dict[str, int]:
    effective = [item for item in items if is_rule_fit_image_ready(item)]
    return {
        "effective_count": len(effective),
        "original_count": sum(1 for item in effective if item.get("domain") == "original"),
        "fanart_count": sum(1 for item in effective if item.get("domain") == "fanart"),
        "r18_count": sum(1 for item in effective if item.get("visible_age_restriction") in R18_AGE_RESTRICTIONS),
        "all_ages_count": sum(1 for item in effective if item.get("visible_age_restriction") not in R18_AGE_RESTRICTIONS),
    }


def rule_fit_constraints_satisfied(
    items: list[dict[str, Any]],
    target_count: int,
    min_original: int,
    min_fanart: int,
    min_r18: int,
) -> bool:
    counts = rule_fit_distribution_counts(items)
    return (
        counts["effective_count"] >= target_count
        and counts["original_count"] >= min_original
        and counts["fanart_count"] >= min_fanart
        and counts["r18_count"] >= min_r18
    )


def choose_next_rule_fit_candidate(
    pending: list[dict[str, Any]],
    accepted: list[dict[str, Any]],
    target_count: int,
    min_original: int,
    min_fanart: int,
    min_r18: int,
) -> dict[str, Any] | None:
    if not pending:
        return None
    counts = rule_fit_distribution_counts(accepted)

    def bucket_priority(item: dict[str, Any]) -> int:
        score = 0
        if counts["original_count"] < min_original and item.get("domain") == "original":
            score += 8
        if counts["fanart_count"] < min_fanart and item.get("domain") == "fanart":
            score += 8
        if counts["r18_count"] < min_r18 and item.get("visible_age_restriction") in R18_AGE_RESTRICTIONS:
            score += 8
        if counts["effective_count"] < target_count:
            score += 1
        return score

    best = max(
        pending,
        key=lambda item: (
            bucket_priority(item),
            *rule_fit_sample_score(item),
        ),
    )
    return best


def download_pixiv_original_image(context, image_url: str, referer_url: str, dest_path: Path) -> None:
    cookies = _context_cookies_dict(context)
    cookie_header = "; ".join(f"{name}={value}" for name, value in cookies.items())
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": referer_url,
        "Accept-Language": "ja,en-US;q=0.8,en;q=0.7",
    }
    try:
        with httpx.Client(timeout=90, follow_redirects=True, headers=headers, cookies=cookies) as client:
            resp = client.get(image_url)
            resp.raise_for_status()
            dest_path.write_bytes(resp.content)
            return
    except Exception:
        pass

    try:
        page = context.new_page()
        try:
            page.set_extra_http_headers({"Referer": referer_url})
            response = page.goto(image_url, wait_until="commit", timeout=90000)
            if response is None:
                raise RuntimeError("browser_download_missing_response")
            dest_path.write_bytes(response.body())
            return
        finally:
            page.close()
    except Exception:
        pass

    try:
        page = context.new_page()
        try:
            page.goto(referer_url, wait_until="domcontentloaded", timeout=30000)
            encoded = page.evaluate(
                """async ({ url, referer }) => {
                    const resp = await fetch(url, {
                        credentials: 'include',
                        headers: { Referer: referer },
                    });
                    if (!resp.ok) {
                        throw new Error(`fetch_failed_${resp.status}`);
                    }
                    const buffer = await resp.arrayBuffer();
                    const bytes = new Uint8Array(buffer);
                    const chunk = 0x8000;
                    const parts = [];
                    for (let i = 0; i < bytes.length; i += chunk) {
                        parts.push(String.fromCharCode(...bytes.subarray(i, i + chunk)));
                    }
                    return btoa(parts.join(''));
                }""",
                {"url": image_url, "referer": referer_url},
            )
            dest_path.write_bytes(base64.b64decode(encoded))
            return
        finally:
            page.close()
    except Exception:
        pass

    command = [
        "curl.exe",
        "-L",
        "-A",
        headers["User-Agent"],
        "-e",
        referer_url,
        "-H",
        f"Cookie: {cookie_header}",
        "-o",
        str(dest_path),
        image_url,
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=180)
    if result.returncode != 0 or not dest_path.exists() or dest_path.stat().st_size == 0:
        raise RuntimeError(f"pixiv_image_download_failed: {result.stderr.strip() or result.stdout.strip()}")


def build_pixiv_image_download_candidates(image_urls: dict[str, Any]) -> list[dict[str, str]]:
    urls = image_urls or {}
    candidates: list[dict[str, str]] = []
    mapping = [
        ("original", "original"),
        ("fallback_regular", "regular"),
        ("fallback_large", "large"),
    ]
    for variant, key in mapping:
        url = str(urls.get(key, "") or "").strip()
        if url:
            candidates.append({"variant": variant, "url": url})
    # Some pages only expose regular; keep the named priority stable without inventing URLs.
    return candidates


def _dedupe_download_candidates(candidates: list[dict[str, str]]) -> list[dict[str, str]]:
    deduped = []
    seen = set()
    for item in candidates:
        key = item.get("url", "")
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def download_pixiv_image_with_fallback(
    context,
    image_urls: dict[str, Any],
    referer_url: str,
    dest_base: Path,
) -> dict[str, Any]:
    attempts = []
    last_error = ""
    candidates = _dedupe_download_candidates(build_pixiv_image_download_candidates(image_urls))

    for candidate in candidates:
        variant = candidate["variant"]
        url = candidate["url"]
        suffix = _sample_suffix_from_url(url)
        dest_path = dest_base.with_suffix(suffix)
        attempt = {
            "variant": variant,
            "url": url,
            "path": str(dest_path),
            "status": "pending",
            "error": "",
        }
        try:
            download_pixiv_original_image(context, url, referer_url, dest_path)
            attempt["status"] = "success"
            attempts.append(attempt)
            return {
                "status": "success",
                "selected_image_url": url,
                "selected_image_variant": variant,
                "sample_image_path": str(dest_path),
                "image_download_attempts": attempts,
                "image_download_error": "",
            }
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            attempt["status"] = "failed"
            attempt["error"] = last_error
            attempts.append(attempt)

    return {
        "status": "failed",
        "selected_image_url": "",
        "selected_image_variant": "",
        "sample_image_path": "",
        "image_download_attempts": attempts,
        "image_download_error": last_error or "no_download_candidate",
    }


def _sample_suffix_from_url(url: str) -> str:
    path = urlparse(url or "").path
    suffix = Path(path).suffix.lower()
    return suffix if suffix in {".png", ".jpg", ".jpeg", ".webp"} else ".jpg"


def collect_rule_fit_sample_manifests(
    context,
    page,
    sample_dir: Path,
    manifest_dir: Path,
    alias_data: dict[str, Any],
    target_count: int,
    per_source_limit: int,
    min_bookmarks: int,
    min_likes: int,
    min_views: int,
    min_score: float,
    min_original: int,
    min_fanart: int,
    min_r18: int,
) -> dict[str, Any]:
    ensure_pixiv_logged_in(page, PIXIV_BASE)
    existing_manifests = sorted(
        path for path in manifest_dir.glob("*.json")
        if not path.name.endswith(".compare.json")
    )
    existing_payloads = [load_json(path, {}) for path in existing_manifests]
    accepted = [item for item in existing_payloads if is_rule_fit_image_ready(item)]
    existing_success_ids = {str(item.get("illust_id", "")) for item in accepted if item.get("illust_id")}
    seen_ids = set(existing_success_ids)
    candidates: list[dict[str, Any]] = []
    reviewed_sources = []

    for source in DEFAULT_RULE_FIT_SOURCES:
        urls = collect_artwork_urls_from_source(page, source, max_items=per_source_limit)
        reviewed_sources.append({"source": source["name"], "candidate_urls": len(urls)})
        for url in urls:
            illust_id = extract_artwork_id(url)
            if not illust_id or illust_id in seen_ids:
                continue
            seen_ids.add(illust_id)
            illust = fetch_pixiv_illust_data(page, illust_id)
            if not illust:
                continue
            traffic_review = review_traffic_metrics(
                illust.get("metrics", {}),
                min_bookmarks=min_bookmarks,
                min_likes=min_likes,
                min_views=min_views,
                min_score=min_score,
            )
            manifest = build_sample_manifest_from_illust(illust, source, alias_data, traffic_review)
            candidates.append(manifest)

    pending = [item for item in candidates if item.get("traffic_review", {}).get("passes")]
    processed = []

    while pending and not rule_fit_constraints_satisfied(
        accepted,
        target_count=target_count,
        min_original=min_original,
        min_fanart=min_fanart,
        min_r18=min_r18,
    ):
        manifest = choose_next_rule_fit_candidate(
            pending,
            accepted,
            target_count=target_count,
            min_original=min_original,
            min_fanart=min_fanart,
            min_r18=min_r18,
        )
        if manifest is None:
            break
        pending.remove(manifest)
        illust_id = str(manifest.get("illust_id", ""))
        safe_title = safe_stem(manifest.get("title", ""))
        base_path = sample_dir / f"{illust_id}_{safe_title}"
        download_result = download_pixiv_image_with_fallback(
            context=context,
            image_urls=manifest.get("image_urls", {}),
            referer_url=manifest["work_url"],
            dest_base=base_path,
        )
        manifest["sample_image_path"] = download_result["sample_image_path"]
        manifest["selected_image_url"] = download_result["selected_image_url"]
        manifest["selected_image_variant"] = download_result["selected_image_variant"]
        manifest["image_download_attempts"] = download_result["image_download_attempts"]
        manifest["image_download_status"] = download_result["status"]
        manifest["image_download_error"] = download_result["image_download_error"]
        write_manifest(create_rule_fit_manifest_path(manifest_dir, illust_id), manifest)
        processed.append(manifest)
        if is_rule_fit_image_ready(manifest):
            accepted.append(manifest)

    final_counts = rule_fit_distribution_counts(accepted)
    stats = {
        "requested_count": target_count,
        "existing_success_count": len(existing_success_ids),
        "candidate_count": len(candidates),
        "processed_count": len(processed),
        "downloaded_count": sum(1 for item in processed if item.get("image_download_status") == "success"),
        "download_failed_count": sum(1 for item in processed if item.get("image_download_status") == "failed"),
        "effective_count": final_counts["effective_count"],
        "original_count": final_counts["original_count"],
        "fanart_count": final_counts["fanart_count"],
        "r18_count": final_counts["r18_count"],
        "all_ages_count": final_counts["all_ages_count"],
        "constraints_satisfied": rule_fit_constraints_satisfied(
            accepted,
            target_count=target_count,
            min_original=min_original,
            min_fanart=min_fanart,
            min_r18=min_r18,
        ),
        "sources": reviewed_sources,
    }
    return {"selected": processed, "accepted": accepted, "stats": stats}


def classify_compare_differences(
    pixiv_tags: list[str],
    local_tags: list[str],
    alias_data: dict[str, Any],
) -> dict[str, Any]:
    pixiv_entries = [normalize_semantic_tag(tag, alias_data) for tag in pixiv_tags]
    local_entries = [normalize_semantic_tag(tag, alias_data) for tag in local_tags]
    pixiv_entries = [item for item in pixiv_entries if item is not None]
    local_entries = [item for item in local_entries if item is not None]

    pixiv_by_semantic = {item["semantic"]: item for item in pixiv_entries}
    local_by_semantic = {item["semantic"]: item for item in local_entries}

    missing = [pixiv_by_semantic[key]["input_tag"] for key in pixiv_by_semantic.keys() - local_by_semantic.keys()]
    extra = [local_by_semantic[key]["input_tag"] for key in local_by_semantic.keys() - pixiv_by_semantic.keys()]

    synonym_mismatch = []
    for semantic in pixiv_by_semantic.keys() & local_by_semantic.keys():
        if normalize_key(pixiv_by_semantic[semantic]["input_tag"]) != normalize_key(local_by_semantic[semantic]["input_tag"]):
            synonym_mismatch.append(
                {
                    "semantic": semantic,
                    "pixiv": pixiv_by_semantic[semantic]["input_tag"],
                    "local": local_by_semantic[semantic]["input_tag"],
                }
            )

    pixiv_order = {normalize_key(tag): idx for idx, tag in enumerate(pixiv_tags)}
    local_order = {normalize_key(tag): idx for idx, tag in enumerate(local_tags)}
    ordering_mismatch = []
    shared = [tag for tag in pixiv_tags if normalize_key(tag) in local_order]
    for tag in shared:
        delta = abs(pixiv_order[normalize_key(tag)] - local_order[normalize_key(tag)])
        if delta >= 2:
            ordering_mismatch.append({"tag": tag, "pixiv_index": pixiv_order[normalize_key(tag)], "local_index": local_order[normalize_key(tag)]})

    category_mismatch = []
    for tag in extra:
        entry = normalize_semantic_tag(tag, alias_data)
        if entry and entry["class"] == "theme":
            category_mismatch.append({"tag": tag, "reason": "background_or_theme_overweighted"})

    return {
        "missing": missing,
        "extra": extra,
        "synonym_mismatch": synonym_mismatch,
        "ordering_mismatch": ordering_mismatch,
        "category_mismatch": category_mismatch,
    }


def compare_rule_fit_samples(
    manifest_dir: Path,
    alias_data: dict[str, Any],
    popularity_data: dict[str, Any],
    age_rules: dict[str, Any],
    metadata_bridge: HainTagBridge,
    tagger_bridge: HainTagTaggerBridge | None = None,
    jp_alias_cache: dict[str, Any] | None = None,
    general_jp_data: dict[str, Any] | None = None,
    live_lookup: bool = True,
) -> dict[str, Any]:
    if jp_alias_cache is None:
        jp_alias_cache = {}
    if general_jp_data is None:
        general_jp_data = {}
    sample_manifests = sorted(
        path for path in manifest_dir.glob("*.json")
        if not path.name.endswith(".compare.json")
    )
    results = []
    compared_count = 0
    skipped_image_missing_count = 0
    for manifest_path in sample_manifests:
        sample_manifest = load_json(manifest_path, {})
        image_path_value = sample_manifest.get("sample_image_path", "")
        image_path = Path(image_path_value) if image_path_value else None
        image_ready = bool(image_path and image_path.exists())
        if image_ready:
            metadata_info = metadata_bridge.read_metadata(image_path)
            tagger_result = tagger_bridge.predict_tags(image_path) if tagger_bridge is not None else {"available": False, "status": "disabled", "flat_tags": [], "groups": {}}
            payload = build_pixiv_payload(
                image_path=image_path,
                metadata_info=metadata_info,
                alias_data=alias_data,
                popularity_data=popularity_data,
                age_rules=age_rules,
                extra_candidates=tagger_result.get("flat_tags", []),
                extra_groups=tagger_result.get("groups", {}),
                jp_alias_cache=jp_alias_cache,
                general_jp_data=general_jp_data,
                live_lookup=live_lookup,
                live_jp_lookup=live_lookup,
            )
            diffs = classify_compare_differences(sample_manifest.get("pixiv_tags", []), payload.get("final_tags", []), alias_data)
            compare_stage = "full_compare"
            compared_count += 1
        else:
            tagger_result = {"available": False, "status": "image_missing", "flat_tags": [], "groups": {}, "details": []}
            payload = {
                "raw_candidates": [],
                "final_tags": [],
                "rejected_tags": [],
                "popularity_decisions": [],
                "title_ja": "",
                "title_zh": "",
                "caption_ja": "",
                "caption_zh": "",
            }
            diffs = {
                "missing": [],
                "extra": [],
                "synonym_mismatch": [],
                "ordering_mismatch": [],
                "category_mismatch": [],
            }
            compare_stage = "image_missing"
            skipped_image_missing_count += 1
        compare_payload = {
            "compared_at": datetime.now().isoformat(timespec="seconds"),
            "compare_stage": compare_stage,
            "illust_id": sample_manifest.get("illust_id", ""),
            "sample_image_path": str(image_path) if image_path else "",
            "work_url": sample_manifest.get("work_url", ""),
            "domain": sample_manifest.get("domain", ""),
            "visible_age_restriction": sample_manifest.get("visible_age_restriction", "all_ages"),
            "traffic_review": sample_manifest.get("traffic_review", {}),
            "pixiv_tags": sample_manifest.get("pixiv_tags", []),
            "selected_image_variant": sample_manifest.get("selected_image_variant", ""),
            "image_download_status": sample_manifest.get("image_download_status", ""),
            "image_download_error": sample_manifest.get("image_download_error", ""),
            "local": {
                "raw_candidates": payload.get("raw_candidates", []),
                "metadata_entity_hits": payload.get("metadata_entity_hits", []),
                "final_tags": payload.get("final_tags", []),
                "rejected_tags": payload.get("rejected_tags", []),
                "popularity_decisions": payload.get("popularity_decisions", []),
                "title_ja": payload.get("title_ja", ""),
                "title_zh": payload.get("title_zh", ""),
                "caption_ja": payload.get("caption_ja", ""),
                "caption_zh": payload.get("caption_zh", ""),
            },
            "tagger": {
                "status": tagger_result.get("status", "disabled"),
                "available": tagger_result.get("available", False),
                "top_tags": tagger_result.get("flat_tags", [])[:30],
            },
            "diffs": diffs,
        }
        write_manifest(create_rule_fit_compare_path(manifest_dir, str(sample_manifest.get("illust_id", ""))), compare_payload)
        results.append(compare_payload)
    return {
        "results": results,
        "count": len(results),
        "compared_count": compared_count,
        "skipped_image_missing_count": skipped_image_missing_count,
    }


def summarize_rule_fit_report(compare_results: list[dict[str, Any]]) -> dict[str, Any]:
    missing_counter = Counter()
    extra_counter = Counter()
    synonym_counter = Counter()
    domain_buckets = {"original": [], "fanart": []}
    age_buckets = {"all_ages": [], "r18": []}
    stage_counter = Counter()
    tagger_status_counter = Counter()

    for item in compare_results:
        stage_counter.update([item.get("compare_stage", "unknown")])
        tagger_status_counter.update([item.get("tagger", {}).get("status", "unknown")])
        if item.get("compare_stage") != "full_compare":
            continue
        diffs = item.get("diffs", {})
        missing_counter.update(diffs.get("missing", []))
        extra_counter.update(diffs.get("extra", []))
        for pair in diffs.get("synonym_mismatch", []):
            synonym_counter.update([f"{pair.get('local', '')} -> {pair.get('pixiv', '')}"])
        domain = item.get("domain", "original")
        age_bucket = "r18" if item.get("visible_age_restriction") in R18_AGE_RESTRICTIONS else "all_ages"
        domain_buckets.setdefault(domain, []).append(item)
        age_buckets.setdefault(age_bucket, []).append(item)

    def bucket_stats(items: list[dict[str, Any]]) -> dict[str, Any]:
        total = len(items)
        if total == 0:
            return {"count": 0, "avg_missing": 0.0, "avg_extra": 0.0, "avg_synonym": 0.0}
        return {
            "count": total,
            "avg_missing": round(sum(len(item.get("diffs", {}).get("missing", [])) for item in items) / total, 2),
            "avg_extra": round(sum(len(item.get("diffs", {}).get("extra", [])) for item in items) / total, 2),
            "avg_synonym": round(sum(len(item.get("diffs", {}).get("synonym_mismatch", [])) for item in items) / total, 2),
        }

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "sample_count": len(compare_results),
        "stage_counts": dict(stage_counter),
        "tagger_status_counts": dict(tagger_status_counter),
        "top_missing": [{"tag": tag, "count": count} for tag, count in missing_counter.most_common(20)],
        "top_extra": [{"tag": tag, "count": count} for tag, count in extra_counter.most_common(20)],
        "top_synonym_mismatch": [{"pair": pair, "count": count} for pair, count in synonym_counter.most_common(10)],
        "domain_patterns": {key: bucket_stats(value) for key, value in domain_buckets.items()},
        "age_patterns": {key: bucket_stats(value) for key, value in age_buckets.items()},
    }


def safe_goto(page, url: str, wait: float = 5.0) -> None:
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=20000)
    except Exception as exc:
        log.warning(f"safe_goto({url}) 失败: {type(exc).__name__}: {exc}")
    time.sleep(wait)


def _wait_for_file_input(page, timeout: float = 30.0) -> bool:
    """Poll for file input up to timeout seconds. Returns True if found."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _first_visible_locator(page, PIXIV_SELECTORS["file_input"]) is not None:
            return True
        time.sleep(1)
    return False


def ensure_on_pixiv_upload_page(page) -> None:
    if "upload.php" in page.url:
        if _wait_for_file_input(page, timeout=5.0):
            return
    safe_goto(page, PIXIV_UPLOAD_URL, wait=4)

    # If redirected to login page, ask user to log in first.
    if "login" in page.url or "accounts.pixiv.net" in page.url:
        print("\n[!] Pixiv 未登录，请在浏览器里完成登录，然后按 Enter 继续...")
        input()
        safe_goto(page, PIXIV_UPLOAD_URL, wait=6)

    # Wait up to 30 s for the React SPA to render the file input.
    if _wait_for_file_input(page, timeout=30.0):
        return

    # If still not found, fall back to manual intervention.
    print(
        f"\n[!] 没找到上传表单（当前 URL: {page.url}）。"
        "\n  请在浏览器里导航到 https://www.pixiv.net/upload.php"
        "\n  能看到拖拽上传区域后，回到这里按 Enter 继续..."
    )
    input()
    if not _wait_for_file_input(page, timeout=15.0):
        print("[!] 仍未检测到上传表单，强行继续（可能会失败）...")


_ALERT_WAV = Path(__file__).parent.parent / "猫猫怕痛惹 - 许巍-蓝莲哈.wav"


def _alert_captcha(page):
    """Notification chime + bring browser to front so user notices the captcha.

    Returns a stop() callable that silences the sound when the captcha is resolved.
    """
    try:
        page.bring_to_front()
    except Exception:
        pass
    try:
        import winsound
        wav = str(_ALERT_WAV) if _ALERT_WAV.exists() else r"C:\Windows\Media\chimes.wav"
        if os.path.isfile(wav):
            winsound.PlaySound(wav, winsound.SND_FILENAME | winsound.SND_ASYNC)

            def stop():
                try:
                    winsound.PlaySound(None, winsound.SND_PURGE)
                except Exception:
                    pass

            return stop
        else:
            winsound.MessageBeep(winsound.MB_ICONASTERISK)
    except Exception:
        try:
            sys.stdout.write("\a")
            sys.stdout.flush()
        except Exception:
            pass
    return lambda: None


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


def _jsleep(base: float, jitter: float = 0.4, cancel_event=None) -> None:
    """Sleep base seconds plus uniform random jitter (±jitter*base) to humanize timing.

    Default jitter=0.4 means actual sleep ranges over [0.6*base, 1.4*base].
    """
    delta = random.uniform(-jitter, jitter) * base
    _sleep_with_cancel(max(0.05, base + delta), cancel_event)


def _typing_delay() -> int:
    """Per-character typing delay in ms, randomized 25-75."""
    return random.randint(25, 75)


def _human_move_and_click(page, locator, *, cancel_event=None) -> None:
    """Move mouse along a bezier curve to the element, then click."""
    if cancel_event is not None and cancel_event.is_set():
        raise InterruptedError("task canceled")
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


def _first_visible_locator(page, selectors: list[str]):
    for selector in selectors:
        locator = page.locator(selector)
        try:
            if locator.count() > 0:
                return locator.first
        except Exception:
            continue
    return None


def _click_first(page, selectors: list[str], cancel_event=None) -> bool:
    locator = _first_visible_locator(page, selectors)
    if locator is None:
        return False
    try:
        _human_move_and_click(page, locator, cancel_event=cancel_event)
        return True
    except Exception:
        return False


def _capture_failure(page, log_dir: Path | None, step_name: str) -> str:
    if log_dir is None:
        return ""
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe = re.sub(r"[^A-Za-z0-9_-]+", "_", step_name)
        png = log_dir / f"pixiv_failure_{ts}_{safe}.png"
        html = log_dir / f"pixiv_failure_{ts}_{safe}.html"
        try:
            page.screenshot(path=str(png), full_page=True)
        except Exception as exc:
            log.warning(f"截图失败: {type(exc).__name__}: {exc}")
            png = None
        try:
            html.write_text(page.content(), encoding="utf-8")
        except Exception as exc:
            log.warning(f"dump HTML 失败: {type(exc).__name__}: {exc}")
            html = None
        bits = []
        if png:
            bits.append(f"png={png.name}")
        if html:
            bits.append(f"html={html.name}")
        return " ".join(bits)
    except Exception as exc:
        log.warning(f"_capture_failure 异常: {type(exc).__name__}: {exc}")
        return ""


def _fill_if_found(page, name: str, selectors: list[str], value: str, cancel_event=None) -> PixivStep:
    locator = _first_visible_locator(page, selectors)
    if locator is None:
        return PixivStep(name, False, "selector_miss", f"none of: {selectors}")
    try:
        _human_move_and_click(page, locator, cancel_event=cancel_event)
        locator.fill(value)
        return PixivStep(name, True)
    except Exception as exc1:
        try:
            locator.evaluate(
                "(el, value) => { el.value = value; el.dispatchEvent(new Event('input', {bubbles:true})); el.dispatchEvent(new Event('change', {bubbles:true})); }",
                value,
            )
            return PixivStep(name, True, detail="js_fallback")
        except Exception as exc2:
            return PixivStep(
                name, False, "fill_failed",
                f"click_fill: {type(exc1).__name__}: {exc1} | js: {type(exc2).__name__}: {exc2}",
            )


def _click_radio(page, name: str, selectors_per_choice: dict[str, list[str]], choice: str) -> PixivStep:
    text_options = selectors_per_choice.get(choice)
    if not text_options:
        return PixivStep(name, False, "selector_miss", f"unknown choice: {choice}")
    for text in text_options:
        if _click_first(
            page,
            [
                f'label:has-text("{text}")',
                f'button:has-text("{text}")',
                f'[role="radio"]:has-text("{text}")',
                f'text="{text}"',
            ],
        ):
            return PixivStep(name, True, detail=f"choice={choice}, matched={text}")
    return PixivStep(
        name, False, "selector_miss",
        f"choice={choice}, none matched: {text_options}",
    )


def _read_checked_state(locator) -> bool | None:
    try:
        return bool(locator.is_checked())
    except Exception:
        pass
    try:
        aria = locator.get_attribute("aria-checked")
        if aria is None:
            return None
        return aria == "true"
    except Exception:
        return None


def _set_checkbox_by_text(page, name: str, texts: list[str], desired: bool, cancel_event=None) -> PixivStep:
    last_detail = ""
    for text in texts:
        locator = _first_visible_locator(
            page,
            [
                f'label:has-text("{text}") input[type="checkbox"]',
                f'[role="checkbox"]:has-text("{text}")',
            ],
        )
        if locator is None:
            last_detail = f"selector_miss for text='{text}'"
            continue
        current = _read_checked_state(locator)
        if current is None:
            last_detail = f"text='{text}' but cannot read checked state"
            continue
        if current == desired:
            return PixivStep(name, True, detail=f"already={current}, text='{text}'")
        try:
            _human_move_and_click(page, locator, cancel_event=cancel_event)
        except Exception as exc:
            last_detail = f"click failed for '{text}': {type(exc).__name__}: {exc}"
            continue
        _jsleep(0.4, cancel_event=cancel_event)
        final = _read_checked_state(locator)
        if final == desired:
            return PixivStep(name, True, detail=f"toggled to {desired}, text='{text}'")
        last_detail = f"after click, state={final} but desired={desired} (text='{text}')"
    return PixivStep(name, False, "verify_failed", last_detail or f"no candidate matched: {texts}")


def _read_tag_count(page) -> int:
    """Read current tag count from Pixiv's 'N/10' counter near the tag input."""
    try:
        for sel in ('input[placeholder="标签"]', 'input[placeholder="タグ"]', 'input[placeholder="Tags"]'):
            loc = page.locator(sel)
            if loc.count() > 0:
                container = loc.locator("xpath=ancestor::label")
                text = container.inner_text()
                m = re.search(r"(\d+)\s*/\s*10", text)
                if m:
                    return int(m.group(1))
    except Exception:
        pass
    return 0


def _fill_tag_input(page, name: str, selectors: list[str], tags: list[str], cancel_event=None) -> PixivStep:
    tags = tags[:10]
    failed: list[str] = []
    not_committed: list[str] = []
    autocomplete_used = 0
    raw_used = 0
    last_exc: str = ""
    listbox_selectors = PIXIV_SELECTORS.get("tag_autocomplete_listbox", [])
    autocomplete_debug_dumps = 0  # cap per call
    for tag_index, tag in enumerate(tags):
        current_count = _read_tag_count(page)
        if current_count >= 10:
            log.info(f"    tag count already {current_count}/10, stopping")
            break
        # Re-locate each iteration: pixiv tag input may briefly hide/swap during chip insert
        locator = _first_visible_locator(page, selectors)
        if locator is None:
            failed.append(f"{tag}(selector_miss)")
            last_exc = f"none of: {selectors}"
            continue
        try:
            _human_move_and_click(page, locator, cancel_event=cancel_event)
            _jsleep(0.3, cancel_event=cancel_event)
            locator.fill(tag)
            locator.dispatch_event("input")
            _jsleep(1.2, cancel_event=cancel_event)
            listbox = _first_visible_locator(page, listbox_selectors) if listbox_selectors else None
            if listbox is None and autocomplete_debug_dumps < 3:
                # No listbox detected — dump page HTML so we can see what real
                # autocomplete DOM looks like. Cap at 3 dumps per fill_tags call
                # to avoid runaway disk usage.
                try:
                    log_dir = Path(__file__).parent / "logs"
                    log_dir.mkdir(parents=True, exist_ok=True)
                    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                    safe_tag = re.sub(r"[^A-Za-z0-9_-]+", "_", tag)[:24]
                    html_path = log_dir / f"pixiv_autocomplete_probe_{ts}_{tag_index}_{safe_tag}.html"
                    html_path.write_text(page.content(), encoding="utf-8")
                    log.info(f"    autocomplete listbox not found at tag[{tag_index}]={tag!r}; DOM dumped: {html_path.name}")
                except Exception as exc:
                    log.warning(f"    autocomplete DOM dump 失败: {exc}")
                autocomplete_debug_dumps += 1
            if listbox is not None:
                clicked = False
                exact_option = None
                try:
                    options = page.locator('[data-tag][data-type="front_matching"]')
                    for option_index in range(options.count()):
                        option = options.nth(option_index)
                        data_tag = (option.get_attribute("data-tag") or "").strip()
                        if data_tag == tag:
                            exact_option = option
                            break
                except Exception as exc:
                    log.warning(f"    autocomplete exact-match lookup 失败 tag={tag!r}: {type(exc).__name__}: {exc}")
                if exact_option is not None:
                    try:
                        _human_move_and_click(page, exact_option, cancel_event=cancel_event)
                        clicked = True
                    except Exception as exc:
                        log.warning(f"    autocomplete exact-option click 失败 tag={tag!r}: {type(exc).__name__}: {exc}")
                if clicked:
                    autocomplete_used += 1
                else:
                    page.keyboard.press(" ")
                    raw_used += 1
            else:
                page.keyboard.press(" ")
                raw_used += 1
            _jsleep(0.6, cancel_event=cancel_event)
            try:
                value_after = locator.input_value()
            except Exception:
                value_after = None
            if value_after:
                for commit_key in (" ", "Enter", "Tab"):
                    try:
                        locator.press(commit_key)
                        _jsleep(0.4, cancel_event=cancel_event)
                        value_after = locator.input_value()
                    except Exception:
                        pass
                    if not value_after:
                        break
            if value_after:
                not_committed.append(f"{tag}(remained:{value_after!r})")
        except InterruptedError:
            raise
        except Exception as exc:
            failed.append(f"{tag}({type(exc).__name__})")
            last_exc = f"{type(exc).__name__}: {exc}"
    detail = f"{len(tags)} tags (autocomplete={autocomplete_used}, raw={raw_used})"
    if failed or not_committed:
        return PixivStep(
            name, False, "fill_failed",
            f"{detail} | failed: {failed} | not_committed: {not_committed} | last: {last_exc}",
        )
    return PixivStep(name, True, detail=detail)


def _set_radio_by_attr(page, name: str, attr_name: str, attr_value: str, cancel_event=None) -> PixivStep:
    selector = f'input[name="{attr_name}"][value="{attr_value}"]'
    locator = _first_visible_locator(page, [selector])
    if locator is None:
        return PixivStep(name, False, "selector_miss", selector)
    try:
        # charcoal radio: hidden input inside <label>. Click the label, not the input.
        locator.evaluate("""el => {
            const label = el.closest('label') || el.parentElement;
            if (label && label.tagName === 'LABEL') { label.click(); return; }
            el.checked = true;
            el.dispatchEvent(new Event('change', {bubbles: true}));
            el.dispatchEvent(new Event('input', {bubbles: true}));
        }""")
        _jsleep(0.3, cancel_event=cancel_event)
        try:
            checked = bool(locator.is_checked())
        except Exception:
            checked = None
        if checked is False:
            return PixivStep(name, False, "verify_failed", f"{selector} not checked after click")
        return PixivStep(name, True, detail=f"name={attr_name}, value={attr_value}")
    except InterruptedError:
        raise
    except Exception as exc:
        return PixivStep(name, False, "exception", f"{type(exc).__name__}: {exc}")


def _accept_safety_check(page) -> tuple:
    """Detect Pixiv's 安全検査 section and alert if reCAPTCHA is present.

    Returns (PixivStep, stop_fn). stop_fn silences the alert; no-op if no alert played.
    """
    stop_fn = lambda: None
    actions: list[str] = []
    try:
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        _jsleep(0.5, jitter=0.0)

        section_present = any(
            page.locator(f'text="{h}"').count() > 0
            for h in ["安全検査", "安全检查", "Security Check"]
        )
        if not section_present:
            return PixivStep("safety_check", True, detail="not_present"), stop_fn

        actions.append("detected")

        # reCAPTCHA inside the safety check section → alert user to complete it manually.
        recaptcha_selectors = [
            'iframe[src*="recaptcha"]',
            'iframe[src*="google.com/recaptcha"]',
            '.g-recaptcha',
            '[data-sitekey]',
        ]
        if any(page.locator(sel).count() > 0 for sel in recaptcha_selectors):
            log.warning("    pixiv: 安全检查区域出现 reCAPTCHA，请在浏览器里完成验证")
            stop_fn = _alert_captcha(page)
            actions.append("recaptcha_alert")

        return PixivStep("safety_check", True, detail=", ".join(actions)), stop_fn
    except InterruptedError:
        raise
    except Exception as exc:
        return PixivStep("safety_check", True, detail=f"skipped: {exc}"), stop_fn


def _set_checkbox_by_attr(page, name: str, attr_name: str, desired: bool, cancel_event=None) -> PixivStep:
    selector = f'input[name="{attr_name}"][type="checkbox"]'
    locator = _first_visible_locator(page, [selector])
    if locator is None:
        # try without type filter
        locator = _first_visible_locator(page, [f'input[name="{attr_name}"]'])
        if locator is None:
            return PixivStep(name, False, "selector_miss", selector)
    current = _read_checked_state(locator)
    if current is None:
        return PixivStep(name, False, "verify_failed", f"{selector} cannot read state")
    if current == desired:
        return PixivStep(name, True, detail=f"already={current}")
    try:
        _human_move_and_click(page, locator, cancel_event=cancel_event)
    except Exception as exc:
        return PixivStep(name, False, "exception", f"{type(exc).__name__}: {exc}")
    _jsleep(0.4, cancel_event=cancel_event)
    final = _read_checked_state(locator)
    if final == desired:
        return PixivStep(name, True, detail=f"toggled to {desired}")
    return PixivStep(name, False, "verify_failed", f"after click state={final}")


def _record_step(steps: list[PixivStep], page, log_dir: Path | None, step: PixivStep, cancel_event=None) -> PixivStep:
    steps.append(step)
    if step.ok:
        log.info(f"    pixiv: {step.name} ✓ {step.detail}".rstrip())
    else:
        capture = _capture_failure(page, log_dir, step.name)
        if capture:
            step.detail = (step.detail + f" | {capture}").strip(" |")
        log.error(f"    pixiv: {step.name} ✗ [{step.reason}] {step.detail}")
    # Small random pause after every recorded operation — reduces fingerprint
    # similarity across requests and lowers risk of triggering Pixiv's bot check.
    time.sleep(random.uniform(0.1, 0.5))
    return step


def create_pixiv_post(
    page,
    payload: dict[str, Any],
    image_path: Path,
    delay: float,
    log_dir: Path | None = None,
    cancel_event=None,
) -> tuple[str | None, list[PixivStep]]:
    steps: list[PixivStep] = []

    def record(step: PixivStep) -> PixivStep:
        return _record_step(steps, page, log_dir, step, cancel_event=cancel_event)

    try:
        _raise_if_canceled(cancel_event)
        ensure_on_pixiv_upload_page(page)
        record(PixivStep("ensure_upload_page", True))
    except InterruptedError:
        raise
    except Exception as exc:
        record(PixivStep("ensure_upload_page", False, "exception", f"{type(exc).__name__}: {exc}"))
        return None, steps

    file_input = _first_visible_locator(page, PIXIV_SELECTORS["file_input"])
    if file_input is None:
        record(PixivStep("select_file", False, "selector_miss", str(PIXIV_SELECTORS["file_input"])))
        return None, steps
    try:
        file_input.set_input_files(str(image_path))
        record(PixivStep("select_file", True, detail=image_path.name))
    except Exception as exc:
        record(PixivStep("select_file", False, "exception", f"{type(exc).__name__}: {exc}"))
        return None, steps

    _jsleep(4.0, cancel_event=cancel_event)

    record(_fill_if_found(
        page, "fill_title", PIXIV_SELECTORS["title"],
        payload["title_ja"],
    ))
    _jsleep(0.5, cancel_event=cancel_event)
    caption_text = "\n".join(s for s in (payload.get("caption_ja", ""), payload.get("caption_zh", "")) if s).strip()
    if caption_text:
        record(_fill_if_found(page, "fill_caption", PIXIV_SELECTORS["caption"], caption_text))
    else:
        record(PixivStep("fill_caption", True, detail="empty (skipped)"))
    _jsleep(0.4, cancel_event=cancel_event)
    record(_fill_tag_input(page, "fill_tags", PIXIV_SELECTORS["tag_input"], payload["final_tags"], cancel_event=cancel_event))
    _jsleep(0.6, cancel_event=cancel_event)

    # Age restriction: prefer name=value attr, fallback to text
    age = payload["age_restriction"]
    age_attr = PIXIV_SELECTORS["age_radio_attr"]
    age_value = age_attr["values"].get(age)
    if age_value is not None:
        step_age = _set_radio_by_attr(page, "age_restriction", age_attr["name"], age_value, cancel_event=cancel_event)
    else:
        step_age = PixivStep("age_restriction", False, "selector_miss", f"unknown age={age}")
    if not step_age.ok:
        step_age = _click_radio(page, "age_restriction", PIXIV_SELECTORS["age_radio_text"], age)
    record(step_age)

    # AI flag: radio name=ai_type (NOT a checkbox)
    ai_attr = PIXIV_SELECTORS["ai_radio_attr"]
    record(_set_radio_by_attr(page, "ai_flag", ai_attr["name"], ai_attr["values"][True], cancel_event=cancel_event))

    # Sexual content radio. Pixiv only shows this in all_ages mode (R-18
    # implies sexual=true and the field is removed). Probe for presence first.
    sex_attr = PIXIV_SELECTORS["sexual_radio_attr"]
    sexual_present = _first_visible_locator(page, [f'input[name="{sex_attr["name"]}"]']) is not None
    if sexual_present:
        has_sexual = age in {"r18", "r18g"}
        record(_set_radio_by_attr(page, "sexual_flag", sex_attr["name"], sex_attr["values"][has_sexual], cancel_event=cancel_event))
    else:
        record(PixivStep("sexual_flag", True, detail="field absent (R-18 implicit)"))

    # Original/fanart toggle (D item): checkbox name=original
    domain = payload.get("domain", "original")
    record(_set_checkbox_by_attr(
        page, "original_flag", PIXIV_SELECTORS["original_checkbox_attr"],
        domain == "original",
        cancel_event=cancel_event,
    ))
    _raise_if_canceled(cancel_event)

    # Privacy: prefer name=value attr, fallback to text
    privacy = payload.get("privacy", "public")
    priv_attr = PIXIV_SELECTORS["privacy_radio_attr"]
    priv_value = priv_attr["values"].get(privacy)
    if priv_value is not None:
        step_priv = _set_radio_by_attr(page, "privacy", priv_attr["name"], priv_value, cancel_event=cancel_event)
    else:
        step_priv = PixivStep("privacy", False, "selector_miss", f"unknown privacy={privacy}")
    if not step_priv.ok:
        step_priv = _click_radio(page, "privacy", PIXIV_SELECTORS["privacy_radio_text"], privacy)
    record(step_priv)

    # Allow tag edits checkbox
    record(_set_checkbox_by_attr(
        page, "allow_tag_edits", PIXIV_SELECTORS["allow_tag_edit_checkbox_attr"],
        bool(payload.get("allow_tag_edits", False)),
        cancel_event=cancel_event,
    ))

    # Safety check (安全検査) — new Pixiv required section; always ok, won't abort on miss
    safety_step, stop_alert = _accept_safety_check(page)
    record(safety_step)

    if any(not s.ok for s in steps):
        log.error("    pixiv: 字段填写有失败步骤，放弃 publish")
        return None, steps

    publish_locator = _first_visible_locator(page, PIXIV_SELECTORS["publish_button"])
    if publish_locator is None:
        record(PixivStep("locate_publish", False, "selector_miss", str(PIXIV_SELECTORS["publish_button"])))
        return None, steps
    record(PixivStep("locate_publish", True))

    enabled = False
    for _ in range(60):
        _raise_if_canceled(cancel_event)
        try:
            if publish_locator.is_enabled():
                enabled = True
                break
        except Exception:
            pass
        _sleep_with_cancel(2, cancel_event)
    if not enabled:
        record(PixivStep("publish_enable", False, "verify_failed", "publish 按钮 120 秒内未启用"))
        stop_alert()
        return None, steps
    stop_alert()
    record(PixivStep("publish_enable", True))

    try:
        _human_move_and_click(page, publish_locator, cancel_event=cancel_event)
        record(PixivStep("publish_click", True))
    except Exception as exc:
        record(PixivStep("publish_click", False, "exception", f"{type(exc).__name__}: {exc}"))
        return None, steps

    # Success signals (any of):
    #   - URL contains /artworks/<digits>
    #   - URL no longer contains upload.php / illustration/create
    #   - file input gone (form unmounted) — page transitioned away from upload form
    artwork_re = re.compile(r"/artworks/\d+")
    # Only match actual hCaptcha iframes — the broad div:has-text("安全检查")
    # was firing false positives on Pixiv's footer/disclaimer text even on the
    # success page. Require being still on the upload/create page too.
    captcha_selectors = [
        'iframe[src*="hcaptcha"]',
        'iframe[src*="newcaptcha"]',
        'iframe[title*="hCaptcha"]',
        'iframe[title*="captcha" i]',
        'iframe[src*="recaptcha"]',
        'iframe[src*="google.com/recaptcha"]',
        '.g-recaptcha',
        '[data-sitekey]',
    ]
    captcha_detected = False
    captcha_grace = time.monotonic() + 6  # give 6 s for normal redirect before captcha detection
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        time.sleep(1.5)
        try:
            url = page.url
        except Exception as exc:
            record(PixivStep("redirect", False, "exception", f"page closed: {exc}"))
            return None, steps
        if artwork_re.search(url):
            if cancel_event is None or not cancel_event.is_set():
                time.sleep(delay)
            record(PixivStep("redirect", True, detail=f"artwork url={url}"))
            stop_alert()
            return url, steps
        upload_in_url = "upload.php" in url or "illustration/create" in url
        if not upload_in_url:
            # Left the upload page → success (probably to user mypage / artwork list)
            if cancel_event is None or not cancel_event.is_set():
                time.sleep(delay)
            record(PixivStep("redirect", True, detail=f"left upload page url={url}"))
            stop_alert()
            return url, steps
        # Form gone? (file input no longer in DOM) — success indicator.
        # Check BEFORE captcha to avoid false positives during the success modal.
        try:
            if _first_visible_locator(page, PIXIV_SELECTORS["file_input"]) is None:
                if cancel_event is None or not cancel_event.is_set():
                    time.sleep(delay)
                record(PixivStep("redirect", True, detail=f"form unmounted url={url}"))
                stop_alert()
                return url, steps
        except Exception:
            pass
        # Success modal text ("作品投稿成功") — appears while URL is still the upload page.
        try:
            if page.locator('text=作品投稿成功').count() > 0:
                if cancel_event is None or not cancel_event.is_set():
                    time.sleep(delay)
                record(PixivStep("redirect", True, detail=f"success modal text detected url={url}"))
                stop_alert()
                return url, steps
        except Exception:
            pass
        # Captcha detection: only after grace period so normal redirects finish first.
        if not captcha_detected and upload_in_url and time.monotonic() > captcha_grace:
            if _first_visible_locator(page, captcha_selectors) is not None:
                captcha_detected = True
                log.warning("    pixiv: 触发人机验证！在浏览器里完成验证 → 点'投稿'，脚本等你 5 分钟")
                deadline = time.monotonic() + 300
                stop_alert = _alert_captcha(page)
    timeout_msg = (
        "5 分钟内未检测到跳转/表单卸载（人机验证未完成？）"
        if captcha_detected
        else "60 秒内未检测到跳转/表单卸载"
    )
    record(PixivStep("redirect", False, "verify_failed", timeout_msg))
    return None, steps
