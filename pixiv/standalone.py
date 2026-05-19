"""Standalone metadata reader and WD14 tagger — no haintag dependency.

Drop-in replacements for HainTagBridge / HainTagTaggerBridge when haintag
is not installed. Interfaces are identical so callers don't need to branch.
"""
from __future__ import annotations

import csv
import gzip
import json
import os
import re
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------

class _Meta:
    """Minimal metadata object compatible with HainTagBridge output."""
    def __init__(self, positive_prompt="", negative_prompt="", parameters=None, raw_chunks=None):
        self.positive_prompt = positive_prompt
        self.negative_prompt = negative_prompt
        self.parameters = parameters or {}
        self.raw_chunks = raw_chunks or {}
        self.generator = None
        self.has_content = bool(positive_prompt or negative_prompt or parameters)


class StandaloneMetadataReader:
    """Read PNG metadata (A1111 / ComfyUI) and verify clean copies without haintag."""

    def read_metadata(self, path: Path) -> dict[str, Any]:
        """Return same schema as HainTagBridge.read_metadata()."""
        try:
            chunks = self._read_chunks(path)
        except Exception:
            chunks = {}

        if "Comment" not in chunks:
            try:
                stealth = self._read_stealth_pnginfo(path)
                if stealth:
                    chunks.update(stealth)
            except Exception:
                pass

        if not chunks:
            return {"available": True, "status": "clean", "detected_types": [], "details": [], "metadata": None}

        positive = negative = ""
        parameters: dict = {}
        detected: list[str] = []
        details: list[str] = []

        if "parameters" in chunks:
            detected += ["a1111", "parameters"]
            positive, negative, parameters = self._parse_a1111(chunks["parameters"])

        if "workflow" in chunks or "prompt" in chunks:
            detected.append("workflow")
            for key in ("prompt", "workflow"):
                if key in chunks and not positive:
                    positive = self._extract_comfy_prompt(chunks[key])

        if not positive and "Comment" in chunks:
            try:
                comment_data = json.loads(chunks["Comment"])
                nai_data = self._extract_nai_data(comment_data)
                if nai_data is not None:
                    detected.append("nai")
                    positive = str(nai_data.get("prompt", ""))
                    negative = str(nai_data.get("uc", ""))
                    for nai_key in ("steps", "sampler", "seed", "strength", "noise",
                                    "scale", "uncond_scale", "cfg_rescale", "sm", "sm_dyn",
                                    "dynamic_thresholding", "noise_schedule", "version"):
                        if nai_key in nai_data:
                            parameters[nai_key] = str(nai_data[nai_key])
                    if chunks.get("Software", "").startswith("NovelAI"):
                        detected.append("novelai_software")
            except (json.JSONDecodeError, ValueError, TypeError):
                pass

        if positive:
            details.append("positive_prompt")
        if negative:
            details.append("negative_prompt")
        if parameters:
            details.append("parameters")
        if chunks:
            details.append("raw_text_chunks")

        has_content = bool(positive or negative or parameters)
        meta_obj = _Meta(positive, negative, parameters, chunks) if (has_content or chunks) else None

        return {
            "available": True,
            "status": "failed" if (has_content or chunks) else "clean",
            "detected_types": sorted(set(detected)),
            "details": details,
            "metadata": meta_obj,
        }

    # ------------------------------------------------------------------

    @staticmethod
    def _extract_nai_data(comment_data: Any) -> dict | None:
        """Extract NAI generation params from Comment JSON, handling both old and V4.5 formats."""
        if not isinstance(comment_data, dict):
            return None
        if "prompt" in comment_data and "uc" in comment_data:
            return comment_data
        if "Comment" in comment_data:
            try:
                inner = comment_data["Comment"]
                if isinstance(inner, str):
                    inner = json.loads(inner)
                if isinstance(inner, dict) and "prompt" in inner:
                    return inner
            except (json.JSONDecodeError, ValueError, TypeError):
                pass
        return None

    @staticmethod
    def _read_chunks(path: Path) -> dict[str, str]:
        from PIL import Image
        img = Image.open(str(path))
        result: dict[str, str] = {}
        for key, val in (img.info or {}).items():
            if isinstance(val, str):
                result[str(key)] = val
            elif isinstance(val, bytes):
                result[str(key)] = val.decode("utf-8", errors="replace")
        return result

    @staticmethod
    def _read_stealth_pnginfo(path: Path) -> dict[str, str]:
        """Extract NAI stealth pnginfo from alpha channel LSB."""
        import numpy as np
        from PIL import Image

        img = Image.open(str(path))
        if img.mode != "RGBA":
            return {}

        alpha = np.array(img)[:, :, 3].T.flatten()
        bits = alpha & 1
        n_bytes = len(bits) // 8
        if n_bytes < 20:
            return {}
        byte_arr = np.packbits(bits[:n_bytes * 8])
        raw = bytes(byte_arr)

        magic = b"stealth_pngcomp"
        if raw[:15] != magic:
            return {}

        data_len = int.from_bytes(raw[15:19], "big")
        byte_len = (data_len + 7) // 8
        start = 19
        if start + byte_len > len(raw):
            return {}

        try:
            decompressed = gzip.decompress(raw[start:start + byte_len])
            payload = json.loads(decompressed.decode("utf-8"))
        except Exception:
            return {}

        if not isinstance(payload, dict):
            return {}

        result: dict[str, str] = {}
        for k, v in payload.items():
            if isinstance(v, str):
                result[k] = v
            elif isinstance(v, (dict, list)):
                result[k] = json.dumps(v)
        return result

    @staticmethod
    def _parse_a1111(text: str) -> tuple[str, str, dict]:
        positive_lines: list[str] = []
        negative_lines: list[str] = []
        parameters: dict[str, str] = {}
        state = "positive"
        for line in text.split("\n"):
            stripped = line.strip()
            if stripped.startswith("Negative prompt:"):
                state = "negative"
                negative_lines.append(stripped[len("Negative prompt:"):].strip())
            elif state in ("positive", "negative") and re.match(
                r"^(Steps|Sampler|CFG scale|Seed|Size|Model hash|Model|Clip skip|Version):", stripped
            ):
                state = "params"
                for part in stripped.split(","):
                    kv = part.split(":", 1)
                    if len(kv) == 2:
                        parameters[kv[0].strip()] = kv[1].strip()
            elif state == "params":
                for part in stripped.split(","):
                    kv = part.split(":", 1)
                    if len(kv) == 2:
                        parameters[kv[0].strip()] = kv[1].strip()
            elif state == "positive":
                positive_lines.append(line)
            elif state == "negative":
                negative_lines.append(line)
        return "\n".join(positive_lines).strip(), "\n".join(negative_lines).strip(), parameters

    @staticmethod
    def _extract_comfy_prompt(json_str: str) -> str:
        try:
            data = json.loads(json_str)
            texts: list[str] = []

            def walk(obj: Any, depth: int = 0) -> None:
                if depth > 12:
                    return
                if isinstance(obj, dict):
                    cls = str(obj.get("class_type", ""))
                    if "CLIPTextEncode" in cls or "Text" in cls:
                        inp = obj.get("inputs") or {}
                        t = inp.get("text") or inp.get("Text") or inp.get("positive")
                        if isinstance(t, str) and len(t) > 5:
                            texts.append(t)
                    for v in obj.values():
                        walk(v, depth + 1)
                elif isinstance(obj, list):
                    for item in obj:
                        walk(item, depth + 1)

            walk(data)
            return texts[0] if texts else ""
        except Exception:
            return ""


# ---------------------------------------------------------------------------
# Metadata sanitization (standalone, no haintag)
# ---------------------------------------------------------------------------

def sanitize_and_verify(src: Path, dest_dir: Path):
    """
    Strip ALL PNG text chunks via PIL re-save, then verify the output is clean.
    Returns a dict compatible with CleanResult + metadata_check.

    Keys: output_path, width, height, status ("clean" / "failed"), detected_types, details
    """
    from PIL import Image

    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{src.stem}_pixiv_clean.png"

    with Image.open(src) as img:
        clean = img.convert("RGB")
        clean.save(dest, "PNG")
        w, h = clean.width, clean.height

    # Verify
    reader = StandaloneMetadataReader()
    check = reader.read_metadata(dest)
    return {
        "output_path": dest,
        "width": w,
        "height": h,
        "status": check["status"],
        "detected_types": check["detected_types"],
        "details": check["details"],
    }


# ---------------------------------------------------------------------------
# WD14 tagger (onnxruntime)
# ---------------------------------------------------------------------------

_CATEGORY_MAP = {
    "Rating": "rating",
    "General": "general",
    "Artist": "artist",
    "Copyright": "copyright",
    "Character": "character",
}
_SKIP_CATEGORIES = {"rating", "artist"}
_THRESHOLDS = {"general": 0.58, "character": 0.70, "copyright": 0.5}
_DEFAULT_THRESHOLD = 0.5


class StandaloneTaggerBridge:
    """
    WD14 ONNX tagger using onnxruntime — no haintag required.
    Reads model_dir from %APPDATA%\\HainTag\\settings.json (same key as HainTagTaggerBridge).
    Compatible return schema with HainTagTaggerBridge.predict_tags().
    """

    _INPUT_SIZE = 448  # overridden after model load if shape differs

    def __init__(self) -> None:
        self._session = None
        self._tags: list[dict] | None = None
        self._status = "uninitialized"
        self._channel_first = False
        self._model_dir = self._load_model_dir()

    # ------------------------------------------------------------------
    # public

    def predict_tags(self, path: Path) -> dict[str, Any]:
        if not self._ensure_loaded():
            return {"available": False, "status": self._status, "flat_tags": [], "groups": {}, "details": [], "rating_scores": {}}
        try:
            arr = self._preprocess(path)
            inp = self._session.get_inputs()[0].name
            scores = self._session.run(None, {inp: arr})[0][0]
            return self._decode(scores)
        except Exception as exc:
            return {"available": True, "status": "error", "flat_tags": [], "groups": {}, "details": [f"{type(exc).__name__}: {exc}"], "rating_scores": {}}

    # ------------------------------------------------------------------
    # internal

    @staticmethod
    def _load_model_dir() -> str:
        appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        cfg = Path(appdata) / "HainTag" / "settings.json"
        if cfg.exists():
            try:
                payload = json.loads(cfg.read_text(encoding="utf-8"))
                s = payload.get("settings", payload) if isinstance(payload, dict) else {}
                return s.get("tagger_model_dir") or ""
            except Exception:
                pass
        return ""

    def _ensure_loaded(self) -> bool:
        if self._session is not None:
            return True
        if not self._model_dir:
            self._status = "model_dir_not_configured"
            return False

        model_path = mapping_path = None
        try:
            for fname in sorted(os.listdir(self._model_dir)):
                fl = fname.lower()
                if fl.endswith(".onnx") and not model_path:
                    model_path = os.path.join(self._model_dir, fname)
                elif not mapping_path:
                    if fl.endswith(".json") and any(x in fl for x in ("tag", "mapping", "label")):
                        mapping_path = os.path.join(self._model_dir, fname)
                    elif fl.endswith(".csv") and any(x in fl for x in ("tag", "label")):
                        mapping_path = os.path.join(self._model_dir, fname)
        except Exception as exc:
            self._status = f"scan_error:{exc}"
            return False

        if not model_path:
            self._status = "model_not_found"
            return False
        if not mapping_path:
            self._status = "mapping_not_found"
            return False

        try:
            import onnxruntime as ort
            self._session = ort.InferenceSession(model_path, providers=["CPUExecutionProvider"])
        except ImportError:
            self._status = "onnxruntime_not_installed"
            return False
        except Exception as exc:
            self._status = f"model_load_error:{exc}"
            return False

        # Detect input layout and size from model shape
        try:
            shape = self._session.get_inputs()[0].shape  # [B, H, W, C] or [B, C, H, W]
            if len(shape) == 4 and isinstance(shape[1], int):
                if shape[1] == 3:
                    self._channel_first = True
                    if isinstance(shape[2], int) and shape[2] > 3:
                        self._INPUT_SIZE = shape[2]
                elif shape[1] > 3:
                    self._INPUT_SIZE = shape[1]
        except Exception:
            pass

        try:
            self._tags = self._load_mapping(mapping_path)
        except Exception as exc:
            self._session = None
            self._status = f"mapping_load_error:{exc}"
            return False

        self._status = "ok"
        return True

    @staticmethod
    def _load_mapping(path: str) -> list[dict]:
        if path.lower().endswith(".csv"):
            tags = []
            with open(path, newline="", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    tags.append({
                        "name": row.get("name") or row.get("tag") or "",
                        "category": row.get("category") or row.get("category_id") or "General",
                    })
            return tags

        with open(path, encoding="utf-8") as f:
            data = json.load(f)

        # Format: {"0": {"tag": "1girl", "category": "General"}, ...}
        if isinstance(data, dict):
            max_idx = -1
            for k in data:
                if k.isdigit():
                    max_idx = max(max_idx, int(k))
            if max_idx >= 0:
                result = [{"name": "", "category": "General"}] * (max_idx + 1)
                for k, v in data.items():
                    if k.isdigit():
                        if isinstance(v, dict):
                            result[int(k)] = {
                                "name": v.get("tag") or v.get("name") or "",
                                "category": v.get("category") or "General",
                            }
                        elif isinstance(v, str):
                            result[int(k)] = {"name": v, "category": "General"}
                return result

        # Format: [{"name": ..., "category": ...}, ...]
        if isinstance(data, list):
            result = []
            for item in data:
                if isinstance(item, dict):
                    result.append({"name": item.get("name") or item.get("tag") or "", "category": item.get("category") or "General"})
                elif isinstance(item, str):
                    result.append({"name": item, "category": "General"})
            return result

        return []

    def _preprocess(self, path: Path):
        import numpy as np
        from PIL import Image

        img = Image.open(str(path)).convert("RGBA")
        bg = Image.new("RGBA", img.size, (255, 255, 255, 255))
        bg.paste(img, mask=img.split()[3])
        img = bg.convert("RGB")

        # Pad to square
        size = max(img.width, img.height)
        padded = Image.new("RGB", (size, size), (255, 255, 255))
        padded.paste(img, ((size - img.width) // 2, (size - img.height) // 2))

        resized = padded.resize((self._INPUT_SIZE, self._INPUT_SIZE), Image.BICUBIC)
        import numpy as np
        arr = np.array(resized, dtype=np.float32) / 255.0
        arr = arr[:, :, ::-1]  # RGB → BGR
        if self._channel_first:
            arr = arr.transpose(2, 0, 1)  # HWC → CHW
            mean = np.array([0.5, 0.5, 0.5], dtype=np.float32).reshape(3, 1, 1)
            std = np.array([0.5, 0.5, 0.5], dtype=np.float32).reshape(3, 1, 1)
            arr = (arr - mean) / std
        return arr[None]  # add batch dim

    @staticmethod
    def _sigmoid(x: float) -> float:
        import math
        return 1.0 / (1.0 + math.exp(-x)) if x >= -500 else 0.0

    def _decode(self, scores) -> dict[str, Any]:
        if self._tags is None:
            return {"available": False, "status": "mapping_missing", "flat_tags": [], "groups": {}, "details": [], "rating_scores": {}}

        groups: dict[str, list] = {}
        flat: list[dict] = []
        rating_scores: dict[str, float] = {}

        for i, score in enumerate(scores):
            if i >= len(self._tags):
                break
            info = self._tags[i]
            cat_raw = str(info.get("category", "General"))
            cat = _CATEGORY_MAP.get(cat_raw, cat_raw.lower())
            if cat == "rating":
                name = info.get("name", "")
                if name:
                    rating_scores[name] = round(self._sigmoid(float(score)), 4)
                continue
            if cat in _SKIP_CATEGORIES:
                continue
            prob = self._sigmoid(float(score))
            threshold = _THRESHOLDS.get(cat, _DEFAULT_THRESHOLD)
            if prob < threshold:
                continue
            name = info.get("name", "")
            if not name:
                continue
            groups.setdefault(cat, []).append((name, round(prob, 4)))
            flat.append({"tag": name, "score": round(prob, 4), "category": cat})

        flat.sort(key=lambda x: x["score"], reverse=True)
        for cat in groups:
            groups[cat].sort(key=lambda x: x[1], reverse=True)

        return {
            "available": True,
            "status": "ok",
            "flat_tags": [x["tag"] for x in flat],
            "groups": groups,
            "details": [],
            "scored_tags": flat,
            "rating_scores": rating_scores,
        }
