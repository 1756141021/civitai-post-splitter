"""
PixAI Tagger v0.9 bridge — ONNX inference via deepghs/pixai-tagger-v0.9-onnx.

Preprocessing differs from WD14/CL (CLIP-style [-1,1] normalization, BCHW layout).
Returns the same dict schema as StandaloneTaggerBridge for zero-impact integration.
"""
from __future__ import annotations

import csv
import json
import os
from pathlib import Path
from typing import Any


_CAT_ID_TO_NAME = {0: "general", 4: "character"}
_DEFAULT_THRESHOLDS = {"general": 0.3, "character": 0.85}
_DEFAULT_INPUT_SIZE = 448
_DEFAULT_MEAN = [0.5, 0.5, 0.5]
_DEFAULT_STD = [0.5, 0.5, 0.5]


class PixAITaggerBridge:
    """
    PixAI tagger v0.9 ONNX bridge.
    model_dir must contain model.onnx and selected_tags.csv.
    Optionally reads preprocess.json and thresholds.csv for config.
    """

    def __init__(self, model_dir: Path) -> None:
        self._dir = model_dir
        self._session = None
        self._tags: list[dict] | None = None
        self._thresholds: dict[str, float] = dict(_DEFAULT_THRESHOLDS)
        self._input_size: int = _DEFAULT_INPUT_SIZE
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
            outputs = self._session.run(["prediction"], {inp: arr})
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

        if not model_path.exists():
            self._status = "model_not_found"
            return False
        if not tags_path.exists():
            self._status = "mapping_not_found"
            return False

        self._load_preprocess_config()
        self._load_thresholds()

        try:
            import onnxruntime as ort
            self._session = ort.InferenceSession(str(model_path), providers=["CPUExecutionProvider"])
        except ImportError:
            self._status = "onnxruntime_not_installed"
            return False
        except Exception as exc:
            self._status = f"model_load_error:{exc}"
            return False

        try:
            self._tags = self._load_tags(tags_path)
        except Exception as exc:
            self._session = None
            self._status = f"mapping_load_error:{exc}"
            return False

        self._status = "ok"
        return True

    def _load_preprocess_config(self) -> None:
        cfg_path = self._dir / "preprocess.json"
        if not cfg_path.exists():
            return
        try:
            stages = json.loads(cfg_path.read_text(encoding="utf-8")).get("stages", [])
            for stage in stages:
                t = stage.get("type", "")
                if t == "resize":
                    size = stage.get("size")
                    if isinstance(size, list) and len(size) == 2:
                        self._input_size = size[0]
                elif t == "normalize":
                    mean = stage.get("mean")
                    std = stage.get("std")
                    if isinstance(mean, list) and len(mean) == 3:
                        self._mean = mean
                    if isinstance(std, list) and len(std) == 3:
                        self._std = std
        except Exception:
            pass

    def _load_thresholds(self) -> None:
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
        Reads selected_tags.csv. DeepGHS format:
          tag_id,name,category,category_name
          0,1girl,0,general
        category field is an integer matching categories.json.
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
                    # fallback: string category name
                    cat = cat_raw.lower() if cat_raw else ""
                tags.append({"name": name, "category": cat})
        return tags

    def _preprocess(self, path: Path):
        import numpy as np
        from PIL import Image

        img = Image.open(str(path)).convert("RGBA")
        bg = Image.new("RGBA", img.size, (255, 255, 255, 255))
        bg.paste(img, mask=img.split()[3])
        img = bg.convert("RGB")

        # PixAI: resize directly (no square-pad), bilinear
        img = img.resize((self._input_size, self._input_size), Image.BILINEAR)

        arr = np.array(img, dtype=np.float32) / 255.0  # HWC [0,1]
        mean = np.array(self._mean, dtype=np.float32)
        std = np.array(self._std, dtype=np.float32)
        arr = (arr - mean) / std  # HWC [-1,1]

        arr = arr.transpose(2, 0, 1)  # HWC → CHW
        return arr[None]  # BCHW

    def _decode(self, scores) -> dict[str, Any]:
        if self._tags is None:
            return {"available": False, "status": "mapping_missing", "flat_tags": [], "groups": {}, "details": [], "rating_scores": {}}

        groups: dict[str, list] = {"general": [], "character": [], "copyright": []}
        flat: list[dict] = []

        for i, score in enumerate(scores):
            if i >= len(self._tags):
                break
            info = self._tags[i]
            cat = info.get("category", "")
            if not cat or cat not in self._thresholds:
                continue
            prob = float(score)
            threshold = self._thresholds[cat]
            if prob < threshold:
                continue
            name = info.get("name", "")
            if not name:
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
            "rating_scores": {},
        }
