"""
PixAI Tagger v0.9 / v1.0 bridge — ONNX inference.

Supports both:
  - PixAI Tagger v1.0 (1008x1008 rescale_pad, 30,877 tags: general/character/copyright/style/meta/rating)
  - PixAI Tagger v0.9 (448x448 resize, 13,461 tags: general/character)
Returns the same dict schema as StandaloneTaggerBridge for zero-impact integration.
"""
from __future__ import annotations

import csv
import json
import os
from pathlib import Path
from typing import Any


_CAT_ID_TO_NAME = {
    0: "general",
    1: "style",
    3: "copyright",
    4: "character",
    5: "meta",
    9: "rating",
}
_RATING_TAG_MAP = {
    "rating:g": "general",
    "rating:s": "sensitive",
    "rating:q": "questionable",
    "rating:e": "explicit",
    "general": "general",
    "sensitive": "sensitive",
    "questionable": "questionable",
    "explicit": "explicit",
}
_DEFAULT_THRESHOLDS = {
    "general": 0.3,
    "character": 0.85,
    "copyright": 0.24,
}
_DEFAULT_INPUT_SIZE = 448
_DEFAULT_MEAN = [0.5, 0.5, 0.5]
_DEFAULT_STD = [0.5, 0.5, 0.5]


class PixAITaggerBridge:
    """
    PixAI tagger v0.9 / v1.0 ONNX bridge.
    model_dir must contain model.onnx and selected_tags.csv (or config.json).
    Optionally reads preprocess.json and thresholds.csv for config.
    """

    def __init__(self, model_dir: Path) -> None:
        self._dir = Path(model_dir)
        self._session = None
        self._tags: list[dict] | None = None
        self._thresholds: dict[str, float] = dict(_DEFAULT_THRESHOLDS)
        self._input_size: int = _DEFAULT_INPUT_SIZE
        self._rescale_pad: bool = False
        self._pad_value: int = 0
        self._mean: list[float] = list(_DEFAULT_MEAN)
        self._std: list[float] = list(_DEFAULT_STD)
        self._status = "uninitialized"

    # ------------------------------------------------------------------
    # public

    @property
    def available_on_disk(self) -> bool:
        return (self._dir / "model.onnx").exists()

    def predict_tags(self, path: Path) -> dict[str, Any]:
        if not self._ensure_loaded():
            return {"available": False, "status": self._status, "flat_tags": [], "groups": {}, "details": [], "rating_scores": {}}
        try:
            arr = self._preprocess(path)
            inp = self._session.get_inputs()[0].name
            out_name = self._session.get_outputs()[0].name
            outputs = self._session.run([out_name], {inp: arr})
            scores = outputs[0][0]
            return self._decode(scores)
        except Exception as exc:
            return {"available": True, "status": "error", "flat_tags": [], "groups": {}, "details": [f"{type(exc).__name__}: {exc}"], "rating_scores": {}}

    # ------------------------------------------------------------------
    # internal

    def _ensure_loaded(self) -> bool:
        if self._session is not None:
            return True

        model_path = self._dir / "model.onnx"
        tags_path = self._dir / "selected_tags.csv"
        cfg_path = self._dir / "config.json"

        if not model_path.exists():
            self._status = "model_not_found"
            return False
        if not tags_path.exists() and not cfg_path.exists():
            self._status = "mapping_not_found"
            return False

        self._load_preprocess_config()
        self._load_thresholds()

        try:
            import onnxruntime as ort
            so = ort.SessionOptions()
            so.log_severity_level = 3
            self._session = ort.InferenceSession(str(model_path), sess_options=so, providers=["CPUExecutionProvider"])
        except ImportError:
            self._status = "onnxruntime_not_installed"
            return False
        except Exception as exc:
            self._status = f"model_load_error:{exc}"
            return False

        try:
            if tags_path.exists():
                self._tags = self._load_tags(tags_path)
            else:
                self._tags = self._load_tags_from_config(cfg_path)
        except Exception as exc:
            self._session = None
            self._status = f"mapping_load_error:{exc}"
            return False

        self._status = "ok"
        return True

    def _load_preprocess_config(self) -> None:
        cfg_path = self._dir / "preprocess.json"
        if cfg_path.exists():
            try:
                stages = json.loads(cfg_path.read_text(encoding="utf-8")).get("stages", [])
                for stage in stages:
                    t = stage.get("type", "")
                    if t == "resize":
                        size = stage.get("size")
                        if isinstance(size, list) and len(size) == 2:
                            self._input_size = int(size[0])
                            self._rescale_pad = False
                        elif isinstance(size, int):
                            self._input_size = int(size)
                    elif t == "rescale_pad":
                        size = stage.get("size")
                        if isinstance(size, int):
                            self._input_size = int(size)
                        elif isinstance(size, list) and size:
                            self._input_size = int(size[0])
                        self._rescale_pad = True
                        self._pad_value = int(stage.get("pad_value", 0))
                    elif t == "normalize":
                        mean = stage.get("mean")
                        std = stage.get("std")
                        if isinstance(mean, list) and len(mean) == 3:
                            self._mean = mean
                        if isinstance(std, list) and len(std) == 3:
                            self._std = std
                return
            except Exception:
                pass

        hf_cfg_path = self._dir / "config.json"
        if hf_cfg_path.exists():
            try:
                data = json.loads(hf_cfg_path.read_text(encoding="utf-8"))
                img_size = data.get("img_size")
                if isinstance(img_size, int) and img_size > 0:
                    self._input_size = img_size
                    if img_size >= 672:
                        self._rescale_pad = True
            except Exception:
                pass

    def _load_thresholds(self) -> None:
        hf_cfg_path = self._dir / "config.json"
        if hf_cfg_path.exists():
            try:
                data = json.loads(hf_cfg_path.read_text(encoding="utf-8"))
                best_thr = data.get("category_best_threshold")
                if isinstance(best_thr, dict):
                    for k, v in best_thr.items():
                        try:
                            self._thresholds[str(k).strip().lower()] = float(v)
                        except (TypeError, ValueError):
                            pass
            except Exception:
                pass

        thr_path = self._dir / "thresholds.csv"
        if not thr_path.exists():
            return
        try:
            with open(thr_path, newline="", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    name = (row.get("name") or "").strip().lower()
                    thr = row.get("threshold") or ""
                    if name and thr:
                        try:
                            self._thresholds[name] = float(thr)
                        except ValueError:
                            pass
        except Exception:
            pass

    @staticmethod
    def _load_tags(path: Path) -> list[dict]:
        """
        Reads selected_tags.csv. DeepGHS / v1.0 format:
          tag_id,name,category,category_name (or count)
        """
        tags = []
        with open(path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                name = (row.get("name") or row.get("tag") or "").strip()
                cat_raw = row.get("category") or row.get("category_id") or ""
                try:
                    cat_id = int(cat_raw)
                    cat = _CAT_ID_TO_NAME.get(cat_id, "")
                except (ValueError, TypeError):
                    cat = cat_raw.lower() if cat_raw else ""
                tags.append({"name": name, "category": cat})
        return tags

    @staticmethod
    def _load_tags_from_config(cfg_path: Path) -> list[dict]:
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
        names = data.get("tags") or []
        splits = data.get("tags_split") or []
        tags: list[dict] = []
        idx = 0
        for split_entry in splits:
            if isinstance(split_entry, list) and len(split_entry) == 2:
                cat_name, count = str(split_entry[0]).lower(), int(split_entry[1])
                for name in names[idx : idx + count]:
                    tags.append({"name": str(name), "category": cat_name})
                idx += count
        return tags

    def _preprocess(self, path: Path):
        import numpy as np
        from PIL import Image

        img = Image.open(str(path)).convert("RGBA")
        bg = Image.new("RGBA", img.size, (255, 255, 255, 255))
        bg.paste(img, mask=img.split()[3])
        img = bg.convert("RGB")

        size = self._input_size
        if self._rescale_pad:
            w, h = img.size
            if w != size or h != size:
                scale = min(size / h, size / w)
                new_w, new_h = max(1, int(round(w * scale))), max(1, int(round(h * scale)))
                img = img.resize((new_w, new_h), Image.BICUBIC)
                canvas = Image.new("RGB", (size, size), (self._pad_value, self._pad_value, self._pad_value))
                canvas.paste(img, ((size - new_w) // 2, (size - new_h) // 2))
                img = canvas
        else:
            img = img.resize((size, size), Image.BILINEAR)

        arr = np.array(img, dtype=np.float32) / 255.0  # HWC [0,1]
        mean = np.array(self._mean, dtype=np.float32)
        std = np.array(self._std, dtype=np.float32)
        arr = (arr - mean) / std  # HWC [-1,1]

        arr = arr.transpose(2, 0, 1)  # HWC -> CHW
        return arr[None]  # BCHW

    def _decode(self, scores) -> dict[str, Any]:
        if self._tags is None:
            return {"available": False, "status": "mapping_missing", "flat_tags": [], "groups": {}, "details": [], "rating_scores": {}}

        groups: dict[str, list] = {"general": [], "character": [], "copyright": []}
        flat: list[dict] = []
        rating_scores: dict[str, float] = {}

        for i, score in enumerate(scores):
            if i >= len(self._tags):
                break
            info = self._tags[i]
            cat = info.get("category", "")
            name = info.get("name", "")
            if not name:
                continue
            prob = float(score)
            if cat == "rating":
                r_key = _RATING_TAG_MAP.get(name.lower(), name.lower())
                rating_scores[r_key] = round(prob, 4)
                continue
            if cat not in groups or cat not in self._thresholds:
                continue
            threshold = self._thresholds[cat]
            if prob < threshold:
                continue
            groups[cat].append((name, round(prob, 4)))
            flat.append({"tag": name, "score": round(prob, 4), "category": cat})

        flat.sort(key=lambda x: x["score"], reverse=True)
        for cat in groups:
            groups[cat].sort(key=lambda x: x[1], reverse=True)

        return {
            "available": True,
            "status": "ok",
            "tagger_type": "pixai",
            "flat_tags": [x["tag"] for x in flat],
            "groups": groups,
            "details": [],
            "scored_tags": flat,
            "rating_scores": rating_scores,
        }

def ensure_v10_tags_csv(model_dir: Path) -> None:
    """Ensure selected_tags.csv and preprocess.json exist for nocturna/pixai-tagger-v1.0-onnx."""
    import csv as _csv
    import json as _json
    cfg_p = model_dir / "config.json"
    csv_p = model_dir / "selected_tags.csv"
    prep_p = model_dir / "preprocess.json"
    if cfg_p.exists() and not csv_p.exists():
        cfg = _json.loads(cfg_p.read_text(encoding="utf-8"))
        tag_map = cfg.get("tag_map") or {}
        cat_ranges = cfg.get("category_ranges") or {}
        cat_to_id = {"general": 0, "artist": 1, "style": 2, "copyright": 3, "character": 4, "meta": 5, "rating": 9}
        idx_to_cat = {}
        for cat_name, (start, end) in cat_ranges.items():
            cid = cat_to_id.get(cat_name, 0)
            for idx in range(int(start), int(end)):
                idx_to_cat[idx] = cid
        sorted_tags = sorted(tag_map.items(), key=lambda kv: int(kv[1]))
        with csv_p.open("w", encoding="utf-8", newline="") as f:
            w = _csv.writer(f)
            w.writerow(["tag_id", "name", "category", "count", "ips"])
            for name, idx in sorted_tags:
                idx_int = int(idx)
                w.writerow([idx_int, name, idx_to_cat.get(idx_int, 0), 0, "[]"])
    if not prep_p.exists():
        prep_p.write_text(_json.dumps({
            "image_size": 1008,
            "resize_mode": "rescale_pad",
            "pad_color": [255, 255, 255],
            "mean": [0.5, 0.5, 0.5],
            "std": [0.5, 0.5, 0.5],
            "channel_order": "RGB"
        }, indent=2), encoding="utf-8")

