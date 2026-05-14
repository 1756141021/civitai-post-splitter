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

POLITICAL_RE = re.compile(
    r"(政治|国家政治|政府|政党|意识形态|战争|领土|主权|外交|革命|选举|民主党|共和党|共产党|"
    r"politic|government|party|ideology|war|territor|sovereign|diploma|election|"
    r"democrat|republican|communist|国家|国旗|国境|紛争|戦争|政府|政党)",
    re.IGNORECASE,
)


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
                "label": "Pixiv Soft",
                "language": "ja_zh",
                "default_content_mode": "sfw",
                "title_style": "short_poetic",
                "caption_style": "light_descriptive",
                "sfw_prompt": "Write clean Pixiv-friendly copy for this illustration.",
                "nsfw_prompt": "Write direct adult-oriented Pixiv copy for this illustration when the account/platform allows it.",
                "avoid": ["politics", "national politics", "state or government commentary"],
                "extra_prompt": "Do not discuss politics, countries, governments, parties, ideology, war, territorial disputes, or real-world national issues.",
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
                "notes": "General Pixiv account.",
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
            else:
                merged[key] = value
    return merged


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
        for key in ("base_url", "api_key", "model"):
            if not str(cfg.get(key, "")).strip():
                errors.append(f"{key} is required when enabled")
    return errors


def resolve_persona_account(
    config: dict[str, Any] | None,
    persona_id: str = "",
    account_id: str = "",
    content_mode: str = "",
) -> tuple[dict[str, Any], dict[str, Any], str]:
    cfg = normalize_llm_reverse_config(config)
    personas = {str(item.get("id", "")): item for item in cfg.get("personas", []) if item.get("id")}
    accounts = {str(item.get("id", "")): item for item in cfg.get("accounts", []) if item.get("id")}
    account = accounts.get(account_id) or accounts.get(str(cfg.get("default_account_id", ""))) or {}
    persona_key = persona_id or str(account.get("persona_id", "")) or str(cfg.get("default_persona_id", ""))
    persona = personas.get(persona_key) or next(iter(personas.values()), {})
    if not account:
        account = next(iter(accounts.values()), {})
    mode = _normalize_content_mode(
        content_mode
        or account.get("default_content_mode", "")
        or persona.get("default_content_mode", "")
        or cfg.get("default_content_mode", "sfw")
    )
    allowed = _normalize_allowed_modes(account.get("allowed_content_modes", ["sfw", "nsfw"]))
    if mode not in allowed:
        mode = allowed[0] if allowed else "sfw"
    return persona, account, mode


def infer_image_copy(
    image_path: Path | None = None,
    image_url: str | None = None,
    config: dict[str, Any] | None = None,
    persona_id: str = "",
    account_id: str = "",
    content_mode: str = "",
    cancel_event=None,
) -> dict[str, Any]:
    cfg = normalize_llm_reverse_config(config)
    persona, account, mode = resolve_persona_account(cfg, persona_id, account_id, content_mode)
    result = _base_result(cfg, persona, account, mode)
    if not cfg.get("enabled"):
        result["status"] = "disabled"
        result["error"] = "llm reverse disabled"
        return result
    missing = [key for key in ("base_url", "api_key", "model") if not str(cfg.get(key, "")).strip()]
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
        payload = _build_request_payload(cfg, persona, account, mode, image_ref)
        endpoint = _chat_completions_url(str(cfg.get("base_url", "")))
        timeout = float(cfg.get("timeout_seconds", 45) or 45)
        headers = {"Authorization": f"Bearer {cfg['api_key']}", "Content-Type": "application/json"}
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            response = client.post(endpoint, headers=headers, json=payload)
            response.raise_for_status()
            data = response.json()
        _raise_if_canceled(cancel_event)
        content = _extract_message_content(data)
        parsed = _parse_json_object(content)
        normalized = _normalize_output(parsed)
        combined = "\n".join(str(normalized.get(key, "")) for key in ("title_ja", "title_zh", "caption_ja", "caption_zh", "description"))
        if _has_political_content(combined):
            result["status"] = "political_blocked"
            result["error"] = "political content detected"
            return result
        result.update(normalized)
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
    for key in ("title_ja", "title_zh", "caption_ja", "caption_zh"):
        value = str(result.get(key, "")).strip()
        if value:
            payload[key] = value


def _base_result(cfg: dict[str, Any], persona: dict[str, Any], account: dict[str, Any], mode: str) -> dict[str, Any]:
    return {
        "enabled": bool(cfg.get("enabled")),
        "status": "disabled",
        "provider": str(cfg.get("provider", "openai_compatible")),
        "model": str(cfg.get("model", "")),
        "persona_id": str(persona.get("id", "")),
        "account_id": str(account.get("id", "")),
        "content_mode": mode,
        "title_ja": "",
        "title_zh": "",
        "caption_ja": "",
        "caption_zh": "",
        "description": "",
        "keywords": [],
        "error": "",
    }


def _build_request_payload(
    cfg: dict[str, Any],
    persona: dict[str, Any],
    account: dict[str, Any],
    mode: str,
    image_ref: str,
) -> dict[str, Any]:
    mode_prompt = str(persona.get("nsfw_prompt" if mode == "nsfw" else "sfw_prompt", ""))
    prompt = f"""
Analyze the image and write Pixiv post copy.
Return only one JSON object with keys: title_ja, title_zh, caption_ja, caption_zh, description, keywords.
content_mode: {mode}
platform: {account.get('platform', 'pixiv')}
language preference: {persona.get('language', 'ja_zh')}
title style: {persona.get('title_style', '')}
caption style: {persona.get('caption_style', '')}
mode instruction: {mode_prompt}
extra persona instruction: {persona.get('extra_prompt', '')}
Never discuss politics, countries, governments, parties, ideology, war, territorial disputes, real-world national issues, or state affairs.
Do not identify real people. Do not invent copyrighted character names unless the visual evidence is obvious.
Keep title_ja and title_zh within 30 characters each. Keep captions to 1-3 short lines.
""".strip()
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


def _chat_completions_url(base_url: str) -> str:
    base = base_url.strip()
    if base.lower().endswith("/chat/completions"):
        return base
    if not base.endswith("/"):
        base += "/"
    if base.lower().endswith("v1/"):
        return urljoin(base, "chat/completions")
    return urljoin(base, "v1/chat/completions")


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


def _normalize_output(data: dict[str, Any]) -> dict[str, Any]:
    out = {
        "title_ja": _clean_text(data.get("title_ja", ""), 60),
        "title_zh": _clean_text(data.get("title_zh", ""), 60),
        "caption_ja": _clean_text(data.get("caption_ja", ""), 500),
        "caption_zh": _clean_text(data.get("caption_zh", ""), 500),
        "description": _clean_text(data.get("description", ""), 1000),
        "keywords": [],
    }
    keywords = data.get("keywords", [])
    if isinstance(keywords, list):
        out["keywords"] = [_clean_text(item, 50) for item in keywords[:20] if _clean_text(item, 50)]
    if not any(out.get(key) for key in ("title_ja", "title_zh", "caption_ja", "caption_zh")):
        raise ValueError("empty title and caption")
    return out


def _clean_text(value: Any, limit: int) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    text = re.sub(r"[ \t]+", " ", text)
    return text[:limit].strip()


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
