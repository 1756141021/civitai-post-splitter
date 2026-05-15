from __future__ import annotations

import base64
import json
import mimetypes
import re
from copy import deepcopy
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import httpx

from .llm_platforms import (
    DEFAULT_PLATFORM_ID,
    PLATFORM_SPECS,
    all_field_keys,
    empty_sample_fields,
    get_merged_spec,
    get_platform_spec,
    list_platform_ids,
    normalize_platform_id,
    normalize_platform_ids,
    required_field_keys,
)

POLITICAL_RE = re.compile(
    r"(政治|国家政治|政府|政党|意识形态|战争|领土|主权|外交|革命|选举|民主党|共和党|共产党|"
    r"politic|government|party|ideology|war|territor|sovereign|diploma|election|"
    r"democrat|republican|communist|国家|国旗|国境|紛争|戦争|政府|政党)",
    re.IGNORECASE,
)

MAX_FEW_SHOT_SAMPLES = 4


def default_llm_reverse_config() -> dict[str, Any]:
    return {
        "enabled": False,
        "provider": "openai_compatible",
        "base_url": "",
        "api_key": "",
        "model": "",
        "timeout_seconds": 45,
        "default_persona_id": "pixiv_soft",
        "default_account_id": "pixiv_main",
        "default_content_mode": "sfw",
        "personas": [
            {
                "id": "pixiv_soft",
                "label": "Pixiv 软系",
                "platform": "pixiv",
                "default_content_mode": "sfw",
                "voice": "短诗体标题，轻描淡写的简介。语气克制，避免感叹号堆叠。",
                "sfw_prompt": "Write clean Pixiv-friendly copy for this illustration.",
                "nsfw_prompt": "Write direct adult-oriented Pixiv copy for this illustration when the platform allows it.",
                "extra_prompt": "Do not discuss politics, countries, governments, parties, ideology, war, territorial disputes, or real-world national issues.",
                "avoid": ["politics", "national politics", "state or government commentary"],
                "samples": [],
            }
        ],
        "accounts": [
            {
                "id": "pixiv_main",
                "label": "Pixiv main",
                "platform": "pixiv",
                "persona_id": "pixiv_soft",
                "default_content_mode": "sfw",
                "allowed_content_modes": ["sfw", "nsfw"],
                "notes": "默认账号（后端回退用，UI 不再暴露）。",
            }
        ],
    }


def normalize_llm_reverse_config(config: dict[str, Any] | None) -> dict[str, Any]:
    merged = default_llm_reverse_config()
    if isinstance(config, dict):
        for key, value in config.items():
            if key in {"personas", "accounts"}:
                if isinstance(value, list) and value:
                    merged[key] = value
            elif key in {"has_api_key", "api_key_masked"}:
                continue  # mask 输出字段，不持久化
            else:
                merged[key] = value
    merged["personas"] = [_migrate_persona(p) for p in merged.get("personas", []) if isinstance(p, dict)]
    merged["accounts"] = [_clean_account(a) for a in merged.get("accounts", []) if isinstance(a, dict)]
    merged["default_content_mode"] = _normalize_content_mode(merged.get("default_content_mode", "sfw"))
    return merged


def _migrate_persona(persona: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    out["id"] = str(persona.get("id") or "").strip() or _gen_persona_id()
    out["label"] = str(persona.get("label") or out["id"]).strip()
    out["platform"] = normalize_platform_ids(persona.get("platform"))
    out["default_content_mode"] = _normalize_content_mode(persona.get("default_content_mode", "sfw"))
    voice = str(persona.get("voice") or "").strip()
    if not voice:
        # Migrate legacy title_style + caption_style into a single voice description.
        ts = str(persona.get("title_style") or "").strip()
        cs = str(persona.get("caption_style") or "").strip()
        voice = " / ".join(part for part in (ts, cs) if part)
    out["voice"] = voice
    out["sfw_prompt"] = str(persona.get("sfw_prompt") or "").strip()
    out["nsfw_prompt"] = str(persona.get("nsfw_prompt") or "").strip()
    out["extra_prompt"] = str(persona.get("extra_prompt") or "").strip()
    avoid_raw = persona.get("avoid") or []
    out["avoid"] = [str(item).strip() for item in avoid_raw if str(item).strip()] if isinstance(avoid_raw, list) else []
    samples_raw = persona.get("samples") or []
    out["samples"] = [_clean_sample(s, out["platform"]) for s in samples_raw if isinstance(s, dict)] if isinstance(samples_raw, list) else []
    return out


def _clean_sample(sample: dict[str, Any], platform_id: str) -> dict[str, Any]:
    fields_raw = sample.get("fields") or {}
    fields: dict[str, Any] = {}
    if isinstance(fields_raw, dict):
        # Preserve any keys (even from other platforms), so switching platforms
        # back doesn't lose data. Validation will warn on unknown keys.
        for key, value in fields_raw.items():
            if isinstance(value, list):
                fields[str(key)] = [str(v).strip() for v in value if str(v).strip()]
            else:
                fields[str(key)] = str(value or "").strip()
    return {
        "mode": _normalize_content_mode(sample.get("mode", "sfw")),
        "note": str(sample.get("note") or "").strip(),
        "fields": fields,
    }


_NSFW_TIER = {"all_ages": 0, "sfw": 0, "r18": 1, "r18g": 2}


def _normalize_max_nsfw_level(value: Any) -> str:
    raw = str(value or "").strip().lower()
    if raw in {"r18g", "r-18g", "r18_g"}:
        return "r18g"
    if raw in {"r18", "r-18"}:
        return "r18"
    return "sfw"


def _clean_account(account: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(account.get("id") or "").strip(),
        "label": str(account.get("label") or "").strip(),
        "platform": normalize_platform_id(account.get("platform")),
        "persona_id": str(account.get("persona_id") or "").strip(),
        "default_content_mode": _normalize_content_mode(account.get("default_content_mode", "sfw")),
        "allowed_content_modes": _normalize_allowed_modes(account.get("allowed_content_modes")),
        "max_nsfw_level": _normalize_max_nsfw_level(account.get("max_nsfw_level", "sfw")),
        "notes": str(account.get("notes") or ""),
    }


def account_can_handle_age(account: dict[str, Any] | None, image_age: str) -> bool:
    """Check if an account's max_nsfw_level can ingest an image at image_age.

    Tiers: all_ages/sfw = 0, r18 = 1, r18g = 2. Account must have a tier >=
    the image's tier. Used to skip LLM reverse when a SFW-only provider
    would otherwise be asked to look at NSFW content (usually refuses or hallucinates).
    """
    if not account:
        return True
    image_tier = _NSFW_TIER.get(image_age, 0)
    account_tier = _NSFW_TIER.get(account.get("max_nsfw_level", "sfw"), 0)
    return account_tier >= image_tier


def resolve_account(
    config: dict[str, Any] | None,
    account_id: str = "",
) -> dict[str, Any]:
    """Resolve which account dict to use, falling back to default_account_id
    then to the first account. Returns empty dict if no accounts configured.
    """
    cfg = normalize_llm_reverse_config(config)
    accounts = {str(item.get("id", "")): item for item in cfg.get("accounts", []) if item.get("id")}
    return (
        accounts.get(account_id)
        or accounts.get(str(cfg.get("default_account_id", "")))
        or next(iter(accounts.values()), {})
    )


def mask_llm_config(config: dict[str, Any] | None) -> dict[str, Any]:
    masked = deepcopy(normalize_llm_reverse_config(config))
    api_key = str(masked.get("api_key", ""))
    masked["has_api_key"] = bool(api_key)
    masked["api_key_masked"] = _mask_secret(api_key)
    masked["api_key"] = ""
    return masked


def validate_llm_reverse_config(config: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    cfg = normalize_llm_reverse_config(config)
    personas = cfg.get("personas", [])
    accounts = cfg.get("accounts", [])
    persona_ids = _unique_ids(personas, "persona", errors)
    account_ids = _unique_ids(accounts, "account", errors)
    valid_platforms = set(list_platform_ids())
    for persona in personas:
        platforms = persona.get("platform") if isinstance(persona.get("platform"), list) else [persona.get("platform")]
        for plat in platforms:
            if plat not in valid_platforms:
                errors.append(f"persona {persona.get('id', '')} unknown platform: {plat}")
        for sample in persona.get("samples", []):
            if sample.get("mode") not in {"sfw", "nsfw"}:
                errors.append(f"persona {persona.get('id', '')} sample mode invalid")
    if cfg.get("default_persona_id") and cfg["default_persona_id"] not in persona_ids:
        errors.append("default_persona_id not found")
    if cfg.get("default_account_id") and cfg["default_account_id"] not in account_ids:
        errors.append("default_account_id not found")
    for account in accounts:
        persona_id = account.get("persona_id")
        if persona_id and persona_id not in persona_ids:
            errors.append(f"account {account.get('id', '')} references missing persona {persona_id}")
        modes = _normalize_allowed_modes(account.get("allowed_content_modes"))
        default_mode = _normalize_content_mode(account.get("default_content_mode", cfg.get("default_content_mode", "sfw")))
        if default_mode not in modes:
            errors.append(f"account {account.get('id', '')} default_content_mode not allowed")
    if cfg.get("enabled"):
        provider = str(cfg.get("provider") or "openai_compatible").strip().lower()
        # anthropic/gemini allow empty base_url (each has its own official endpoint fallback)
        required_keys = ("api_key", "model") if provider in ("anthropic", "google_gemini") else ("base_url", "api_key", "model")
        for key in required_keys:
            if not str(cfg.get(key, "")).strip():
                errors.append(f"{key} is required when enabled")
    return errors


def resolve_persona(
    config: dict[str, Any] | None,
    persona_id: str = "",
    content_mode: str = "",
) -> tuple[dict[str, Any], str]:
    """Resolve which persona and content mode to use.

    Falls back to default_persona_id then to the first persona. Mode falls
    back to persona.default_content_mode then config.default_content_mode.
    """
    cfg = normalize_llm_reverse_config(config)
    personas = {str(item.get("id", "")): item for item in cfg.get("personas", []) if item.get("id")}
    persona = (
        personas.get(persona_id)
        or personas.get(str(cfg.get("default_persona_id", "")))
        or next(iter(personas.values()), {})
    )
    mode = _normalize_content_mode(
        content_mode
        or persona.get("default_content_mode", "")
        or cfg.get("default_content_mode", "sfw")
    )
    return persona, mode


def infer_image_copy(
    image_path: Path | None = None,
    image_url: str | None = None,
    config: dict[str, Any] | None = None,
    persona_id: str = "",
    account_id: str = "",  # accepted for backward compat; ignored now
    content_mode: str = "",
    cancel_event=None,
) -> dict[str, Any]:
    cfg = normalize_llm_reverse_config(config)
    persona, mode = resolve_persona(cfg, persona_id, content_mode)
    platforms = persona.get("platform", DEFAULT_PLATFORM_ID)
    if not isinstance(platforms, list):
        platforms = [platforms]
    spec = get_merged_spec(platforms)
    result = _base_result(cfg, persona, mode, spec)
    if not cfg.get("enabled"):
        result["status"] = "disabled"
        result["error"] = "llm reverse disabled"
        return result
    provider = str(cfg.get("provider") or "openai_compatible").strip().lower()
    _required = ("api_key", "model") if provider == "anthropic" else ("base_url", "api_key", "model")
    missing = [key for key in _required if not str(cfg.get(key, "")).strip()]
    if missing:
        result["status"] = "failed"
        result["error"] = f"missing config: {', '.join(missing)}"
        return result
    if not image_path and not image_url:
        result["status"] = "failed"
        result["error"] = "image_path or image_url required"
        return result
    _raise_if_canceled(cancel_event)
    try:
        image_ref = image_url or _image_to_data_url(Path(image_path))
        timeout = float(cfg.get("timeout_seconds", 45) or 45)

        if provider == "google_gemini":
            payload, endpoint, headers = _build_gemini_request(cfg, persona, mode, spec, image_ref)
        elif provider == "anthropic":
            payload, endpoint, headers = _build_anthropic_request(cfg, persona, mode, spec, image_ref)
        else:
            payload = _build_request_payload(cfg, persona, mode, spec, image_ref)
            endpoint = _chat_completions_url(str(cfg.get("base_url", "")))
            headers = {"Authorization": f"Bearer {cfg['api_key']}", "Content-Type": "application/json"}

        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            response = client.post(endpoint, headers=headers, json=payload)
            response.raise_for_status()
            data = response.json()
        _raise_if_canceled(cancel_event)

        if provider == "google_gemini":
            content = _extract_gemini_content(data)
        elif provider == "anthropic":
            content = _extract_anthropic_content(data)
        else:
            content = _extract_message_content(data)
        parsed = _parse_json_object(content)
        normalized = _normalize_output(parsed, spec)
        combined = "\n".join(_stringify_for_check(v) for v in normalized.values())
        if _has_political_content(combined):
            result["status"] = "political_blocked"
            result["error"] = "political content detected"
            return result
        result["fields"] = normalized
        # Backward-compat top-level keys for pixiv consumers.
        for key in ("title_ja", "title_zh", "caption_ja", "caption_zh", "description"):
            if key in normalized:
                result[key] = normalized[key]
        if "keywords" in normalized:
            result["keywords"] = normalized["keywords"]
        result["status"] = "ok"
        return result
    except InterruptedError:
        raise
    except Exception as exc:
        result["status"] = "failed"
        result["error"] = _scrub_error(str(exc), str(cfg.get("api_key", "")))
        return result


def apply_llm_result_to_pixiv_payload(payload: dict[str, Any], result: dict[str, Any]) -> None:
    if result.get("status") != "ok":
        return
    fields = result.get("fields") or {k: result.get(k) for k in ("title_ja", "title_zh", "caption_ja", "caption_zh")}
    for key in ("title_ja", "title_zh", "caption_ja", "caption_zh"):
        value = str(fields.get(key, "") or "").strip()
        if value:
            payload[key] = value


def empty_copy_block() -> dict[str, Any]:
    """Empty `manifest.copy` block — the universal cross-platform copy area.

    Fields:
      - title.{ja,en,zh}   localized title strings
      - caption.{ja,en,zh} localized caption/body strings
      - llm_reverse        status/persona/account/platform metadata
    """
    return {
        "title": {"ja": "", "en": "", "zh": ""},
        "caption": {"ja": "", "en": "", "zh": ""},
        "xhs": {"title": "", "body": "", "tags": []},
        "llm_reverse": {
            "status": "",
            "persona_id": "",
            "account_id": "",
            "platform": "",
            "content_mode": "",
            "error": "",
        },
    }


def apply_llm_result_to_copy_block(
    copy: dict[str, Any],
    result: dict[str, Any],
    platform: str = "",
    account_id: str = "",
) -> None:
    """In-place merge of LLM reverse result into a copy block (see empty_copy_block).

    Use this when you already have a copy block to populate (e.g. before the
    full manifest is assembled). For convenience, `apply_llm_result_to_manifest_copy`
    wraps this for the manifest case.
    """
    plat = platform or result.get("platform", "") or ""
    platform_ids: list[str]
    if isinstance(plat, list):
        platform_ids = plat
    else:
        platform_ids = [str(plat).strip().lower()] if plat else []

    copy["llm_reverse"] = {
        "status": str(result.get("status", "") or ""),
        "persona_id": str(result.get("persona_id", "") or ""),
        "account_id": str(account_id or ""),
        "platform": ",".join(platform_ids) if platform_ids else "",
        "content_mode": str(result.get("content_mode", "") or ""),
        "error": str(result.get("error", "") or ""),
    }
    if result.get("status") != "ok":
        return

    fields = result.get("fields") or {}

    def _set(bucket: str, lang: str, value: Any) -> None:
        v = str(value or "").strip()
        if v:
            copy[bucket][lang] = v

    for pid in platform_ids:
        if pid == "pixiv":
            _set("title",   "ja", fields.get("title_ja"))
            _set("title",   "zh", fields.get("title_zh"))
            _set("caption", "ja", fields.get("caption_ja"))
            _set("caption", "zh", fields.get("caption_zh"))
        elif pid == "x":
            _set("caption", "en", fields.get("tweet"))
        elif pid == "xhs":
            xhs_block = copy.setdefault("xhs", {"title": "", "body": "", "tags": []})
            xhs_title = str(fields.get("xhs_title") or "").strip()
            xhs_body = str(fields.get("xhs_body") or "").strip()
            xhs_tags_raw = fields.get("xhs_tags") or []
            xhs_tags = [str(t).strip() for t in xhs_tags_raw if str(t).strip()] if isinstance(xhs_tags_raw, list) else []
            if xhs_title:
                xhs_block["title"] = xhs_title
            if xhs_body:
                xhs_block["body"] = xhs_body
            if xhs_tags:
                xhs_block["tags"] = xhs_tags
        else:
            for k in ("title_ja", "title_zh", "title_en"):
                if k in fields:
                    _set("title", k.rsplit("_", 1)[-1], fields[k])
            for k in ("caption_ja", "caption_zh", "caption_en"):
                if k in fields:
                    _set("caption", k.rsplit("_", 1)[-1], fields[k])


def apply_llm_result_to_manifest_copy(
    manifest: dict[str, Any],
    result: dict[str, Any],
    platform: str = "",
    account_id: str = "",
) -> None:
    """Project a LLM reverse result onto the universal `manifest.copy` area."""
    copy = manifest.setdefault("copy", empty_copy_block())
    apply_llm_result_to_copy_block(copy, result, platform, account_id)


def _base_result(cfg: dict[str, Any], persona: dict[str, Any], mode: str, spec: dict[str, Any]) -> dict[str, Any]:
    plat = persona.get("platform", DEFAULT_PLATFORM_ID)
    if not isinstance(plat, list):
        plat = [plat]
    return {
        "enabled": bool(cfg.get("enabled")),
        "status": "disabled",
        "provider": str(cfg.get("provider", "openai_compatible")),
        "model": str(cfg.get("model", "")),
        "persona_id": str(persona.get("id", "")),
        "platform": plat,
        "platform_label": str(spec.get("label", "")),
        "content_mode": mode,
        "fields": {},
        "error": "",
    }


def _build_request_payload(
    cfg: dict[str, Any],
    persona: dict[str, Any],
    mode: str,
    spec: dict[str, Any],
    image_ref: str,
) -> dict[str, Any]:
    fields = spec.get("fields") or []
    extra_fields = spec.get("extra_fields") or []
    required_keys = [str(f.get("key")) for f in fields if f.get("key")]
    extra_keys = [str(f.get("key")) for f in extra_fields if f.get("key")]
    field_lines = [_describe_field(f) for f in fields]
    extra_lines = [_describe_field(f) for f in extra_fields]

    mode_prompt = str(persona.get("nsfw_prompt" if mode == "nsfw" else "sfw_prompt", ""))
    voice = str(persona.get("voice", "")).strip()
    extra_prompt = str(persona.get("extra_prompt", "")).strip()
    avoid = persona.get("avoid") or []
    avoid_line = ", ".join(str(item) for item in avoid) if avoid else ""

    parts: list[str] = []
    parts.append(spec.get("prompt_intro", "Analyze the image."))
    parts.append(
        "Return only one JSON object. Required keys: "
        + ", ".join(required_keys)
        + (f". Optional keys: {', '.join(extra_keys)}" if extra_keys else "")
        + "."
    )
    parts.append("Field rules:")
    parts.extend(f"  - {line}" for line in field_lines + extra_lines)
    parts.append(f"content_mode: {mode}")
    if voice:
        parts.append(f"voice / style: {voice}")
    if mode_prompt:
        parts.append(f"mode instruction: {mode_prompt}")
    if extra_prompt:
        parts.append(f"extra persona instruction: {extra_prompt}")
    if avoid_line:
        parts.append(f"avoid topics: {avoid_line}")
    if spec.get("policy_notes"):
        parts.append(f"platform policy: {spec['policy_notes']}")
    parts.append(
        "Never discuss politics, countries, governments, parties, ideology, war, "
        "territorial disputes, real-world national issues, or state affairs."
    )
    parts.append("Do not identify real people. Do not invent copyrighted character names unless visually obvious.")

    samples_block = _render_samples_block(persona, mode, spec)
    if samples_block:
        parts.append("")
        parts.append("Example outputs to imitate (style, tone, length). Match this voice closely:")
        parts.append(samples_block)

    prompt = "\n".join(parts).strip()

    return {
        "model": str(cfg.get("model", "")),
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": image_ref}},
                ],
            }
        ],
        "temperature": 0.7,
        "response_format": {"type": "json_object"},
    }


def _describe_field(field: dict[str, Any]) -> str:
    key = field.get("key", "")
    kind = field.get("kind", "text")
    if kind == "tags":
        max_count = field.get("max_count", 10)
        per_max = field.get("max", 50)
        return f"{key}: list of strings, up to {max_count} items, each within {per_max} chars"
    limit = field.get("max", 200)
    shape = "single line" if kind == "text" else "1-3 short lines"
    return f"{key}: string, {shape}, within {limit} chars"


def _render_samples_block(persona: dict[str, Any], mode: str, spec: dict[str, Any]) -> str:
    samples = [s for s in (persona.get("samples") or []) if isinstance(s, dict)]
    matched = [s for s in samples if s.get("mode") == mode]
    if not matched:
        return ""
    _plat = persona.get("platform", DEFAULT_PLATFORM_ID)
    if isinstance(_plat, list):
        valid_keys = set(k for pid in _plat for k in all_field_keys(pid))
    else:
        valid_keys = set(all_field_keys(_plat))
    rendered: list[str] = []
    for idx, sample in enumerate(matched[:MAX_FEW_SHOT_SAMPLES], start=1):
        fields = sample.get("fields") or {}
        clean = {k: v for k, v in fields.items() if k in valid_keys and v not in (None, "", [])}
        if not clean:
            continue
        note = str(sample.get("note", "")).strip()
        header = f"Example {idx}" + (f" ({note})" if note else "")
        body = json.dumps(clean, ensure_ascii=False, indent=2)
        rendered.append(f"{header}:\n{body}")
    return "\n\n".join(rendered)


def _chat_completions_url(base_url: str) -> str:
    base = base_url.strip()
    if base.lower().endswith("/chat/completions"):
        return base
    if not base.endswith("/"):
        base += "/"
    if base.lower().endswith("v1/"):
        return urljoin(base, "chat/completions")
    return urljoin(base, "v1/chat/completions")


def _parse_data_url(data_url: str) -> tuple[str, str]:
    """Return (mime_type, base64_data) from a data URL."""
    if not data_url.startswith("data:"):
        raise ValueError("not a data URL")
    header, data = data_url.split(",", 1)
    mime = header.split(";")[0][5:]  # strip "data:"
    return mime or "image/jpeg", data


def _build_gemini_request(
    cfg: dict[str, Any],
    persona: dict[str, Any],
    mode: str,
    spec: dict[str, Any],
    image_ref: str,
) -> tuple[dict[str, Any], str, dict[str, str]]:
    """Return (payload, endpoint, headers) for Gemini native API."""
    oai_payload = _build_request_payload(cfg, persona, mode, spec, image_ref)
    content_parts = oai_payload["messages"][0]["content"]
    prompt_text = next((p["text"] for p in content_parts if p.get("type") == "text"), "")

    parts: list[dict[str, Any]] = [{"text": prompt_text}]
    if image_ref.startswith("data:"):
        mime_type, b64_data = _parse_data_url(image_ref)
        parts.append({"inline_data": {"mime_type": mime_type, "data": b64_data}})
    else:
        parts.append({"file_data": {"file_uri": image_ref, "mime_type": "image/jpeg"}})

    model = str(cfg.get("model", "gemini-2.5-flash"))
    base = str(cfg.get("base_url", "")).rstrip("/") or "https://generativelanguage.googleapis.com"
    endpoint = f"{base}/v1beta/models/{model}:generateContent"
    headers = {"x-goog-api-key": str(cfg["api_key"]), "Content-Type": "application/json"}
    payload = {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {"temperature": 0.7, "responseMimeType": "application/json"},
    }
    return payload, endpoint, headers


def _extract_gemini_content(data: dict[str, Any]) -> str:
    candidates = data.get("candidates") or []
    if not candidates:
        raise ValueError("empty candidates in Gemini response")
    content = candidates[0].get("content") or {}
    parts = content.get("parts") or []
    texts = [p.get("text", "") for p in parts if p.get("text")]
    if not texts:
        raise ValueError("no text in Gemini candidate parts")
    return "\n".join(texts)


def _build_anthropic_request(
    cfg: dict[str, Any],
    persona: dict[str, Any],
    mode: str,
    spec: dict[str, Any],
    image_ref: str,
) -> tuple[dict[str, Any], str, dict[str, str]]:
    """Return (payload, endpoint, headers) for Anthropic Messages API."""
    oai_payload = _build_request_payload(cfg, persona, mode, spec, image_ref)
    content_parts = oai_payload["messages"][0]["content"]
    prompt_text = next((p["text"] for p in content_parts if p.get("type") == "text"), "")

    anthropic_content: list[dict[str, Any]] = []
    if image_ref.startswith("data:"):
        mime_type, b64_data = _parse_data_url(image_ref)
        anthropic_content.append(
            {"type": "image", "source": {"type": "base64", "media_type": mime_type, "data": b64_data}}
        )
    else:
        anthropic_content.append(
            {"type": "image", "source": {"type": "url", "url": image_ref}}
        )
    anthropic_content.append({"type": "text", "text": prompt_text})

    base = str(cfg.get("base_url") or "").rstrip("/")
    endpoint = f"{base}/v1/messages" if base else "https://api.anthropic.com/v1/messages"
    headers = {
        "x-api-key": str(cfg["api_key"]),
        "Content-Type": "application/json",
        "anthropic-version": "2023-06-01",
    }
    payload = {
        "model": str(cfg.get("model", "")),
        "max_tokens": 2048,
        "messages": [{"role": "user", "content": anthropic_content}],
    }
    return payload, endpoint, headers


def _extract_anthropic_content(data: dict[str, Any]) -> str:
    content = data.get("content") or []
    texts = [block.get("text", "") for block in content if block.get("type") == "text"]
    if not texts:
        raise ValueError("no text content in Anthropic response")
    return "\n".join(texts)


def _image_to_data_url(path: Path) -> str:
    mime = mimetypes.guess_type(str(path))[0] or "image/png"
    data = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{data}"


def _extract_message_content(data: dict[str, Any]) -> str:
    choices = data.get("choices") or []
    if not choices:
        raise ValueError("empty choices")
    message = choices[0].get("message") or {}
    content = message.get("content", "")
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("content") or ""))
            else:
                parts.append(str(item))
        return "\n".join(parts)
    return str(content)


def _parse_json_object(text: str) -> dict[str, Any]:
    raw = text.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
    raw = re.sub(r"\s*```$", "", raw)
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        start = raw.find("{")
        end = raw.rfind("}")
        if start < 0 or end <= start:
            raise
        parsed = json.loads(raw[start : end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("model output is not a JSON object")
    return parsed


def _normalize_output(data: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    has_required = False
    for field in spec.get("fields") or []:
        key = str(field.get("key", ""))
        if not key:
            continue
        value = _coerce_field_value(data.get(key), field)
        out[key] = value
        if value not in (None, "", []):
            has_required = True
    for field in spec.get("extra_fields") or []:
        key = str(field.get("key", ""))
        if not key or key not in data:
            continue
        out[key] = _coerce_field_value(data.get(key), field)
    if not has_required:
        raise ValueError(f"empty required fields for platform {spec.get('label', '')}")
    return out


def _coerce_field_value(value: Any, field: dict[str, Any]) -> Any:
    kind = field.get("kind", "text")
    if kind == "tags":
        max_count = int(field.get("max_count", 10))
        per_max = int(field.get("max", 50))
        items = value if isinstance(value, list) else []
        return [_clean_text(item, per_max) for item in items[:max_count] if _clean_text(item, per_max)]
    limit = int(field.get("max", 200))
    return _clean_text(value, limit)


def _clean_text(value: Any, limit: int) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    text = re.sub(r"[ \t]+", " ", text)
    return text[:limit].strip()


def _stringify_for_check(value: Any) -> str:
    if isinstance(value, list):
        return " ".join(str(v) for v in value)
    return str(value or "")


def _has_political_content(text: str) -> bool:
    return bool(POLITICAL_RE.search(text or ""))


def _normalize_content_mode(value: Any) -> str:
    mode = str(value or "sfw").strip().lower()
    return mode if mode in {"sfw", "nsfw"} else "sfw"


def _normalize_allowed_modes(value: Any) -> list[str]:
    if not isinstance(value, list):
        return ["sfw", "nsfw"]
    modes = []
    for item in value:
        mode = _normalize_content_mode(item)
        if mode not in modes:
            modes.append(mode)
    return modes or ["sfw"]


def _unique_ids(items: list[Any], label: str, errors: list[str]) -> set[str]:
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            errors.append(f"invalid {label} entry")
            continue
        ident = str(item.get("id", "")).strip()
        if not ident:
            errors.append(f"{label} id required")
        elif ident in seen:
            errors.append(f"duplicate {label} id: {ident}")
        else:
            seen.add(ident)
    return seen


def _mask_secret(secret: str) -> str:
    if len(secret) > 4:
        return "*" * (len(secret) - 4) + secret[-4:]
    return "*" * len(secret)


def _scrub_error(message: str, api_key: str) -> str:
    text = message or ""
    if api_key:
        text = text.replace(api_key, "***")
    return text[:500]


def _raise_if_canceled(cancel_event) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise InterruptedError("task canceled")


def _gen_persona_id() -> str:
    import secrets
    import time

    return f"persona_{int(time.time() * 1000)}_{secrets.token_hex(3)}"
