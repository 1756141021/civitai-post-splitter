"""Pixiv R-18 auto-censor: detect explicit regions with YOLOv8 and apply mosaic.

References Wenaka2004/auto-censor (github) for the model class layout and
mosaic algorithm. Model file is downloaded separately from
https://civitai.com/models/1736285?modelVersionId=1965032 and placed at
the path passed in (default: <script_dir>/models/auto_censor.pt).

Class indices in the trained model:
  0 = anus
  1 = cum
  2 = dick
  3 = breasts
  4 = pussy

Pixiv R-18 mandates mosaic on exposed genitalia (dick / pussy / anus) and
body fluids (cum). Breasts are usually allowed under R-18 without mosaic.
Default enabled set therefore: {0, 1, 2, 4}.
"""
from __future__ import annotations

import logging
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger("civitai_splitter")

CENSOR_CLASS_NAMES = {0: "anus", 1: "cum", 2: "dick", 3: "tits", 4: "vagina"}
CENSOR_CLASS_BY_NAME = {v: k for k, v in CENSOR_CLASS_NAMES.items()}
# Aliases so users can pass either naming
CENSOR_CLASS_BY_NAME.update({"breasts": 3, "pussy": 4})
DEFAULT_CENSOR_CLASSES = frozenset({0, 1, 2, 4})  # anus, cum, dick, vagina (no tits)


@dataclass
class CensorResult:
    status: str  # "ok" | "disabled" | "model_missing" | "ultralytics_missing" | "load_error" | "infer_error" | "io_error"
    applied: bool = False
    detections: list[dict[str, Any]] = field(default_factory=list)
    output_path: Path | None = None
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "applied": self.applied,
            "detections": self.detections,
            "output_path": str(self.output_path) if self.output_path else "",
            "detail": self.detail,
        }


def parse_class_set(spec: str | None) -> frozenset[int]:
    """Parse a CLI string like 'dick,pussy,cum' into a class-id set.

    Accepts class names (case insensitive) and/or numeric ids; comma-separated.
    Returns DEFAULT_CENSOR_CLASSES on empty/None input.
    """
    if not spec or not spec.strip():
        return DEFAULT_CENSOR_CLASSES
    out: set[int] = set()
    for raw in spec.split(","):
        token = raw.strip().lower()
        if not token:
            continue
        if token.isdigit():
            cls = int(token)
            if 0 <= cls <= 4:
                out.add(cls)
                continue
        if token in CENSOR_CLASS_BY_NAME:
            out.add(CENSOR_CLASS_BY_NAME[token])
            continue
        log.warning(f"unknown censor class token: {raw!r} (ignored)")
    return frozenset(out) if out else DEFAULT_CENSOR_CLASSES


class CensorEngine:
    """Lazy-loaded YOLOv8 censor. detect_and_censor is a no-op if model/deps missing."""

    def __init__(
        self,
        model_path: Path | str | None,
        conf_threshold: float = 0.55,
        mode: str = "mosaic",
        bar_count: int = 4,
        box_expand: dict[int, float] | None = None,
        box_expand_default: float = 0.0,
        class_thresholds: dict[int, float] | None = None,
    ):
        """
        mode:
          - "mosaic" — fine pixelation (传统码，块小且边缘平滑)
          - "blur"   — pure gaussian blur on bbox
          - "bar"    — N 条横向黑 bar 堆叠（日式条码）
        bar_count: number of horizontal bars per region (default 4).
        box_expand: per-class bbox expansion ratio (e.g. {4: 0.22} = vagina expands 22% each side).
        box_expand_default: fallback expansion ratio for classes not in box_expand.
        class_thresholds: per-class confidence overrides (e.g. {4: 0.30} = vagina at 0.30).
        """
        self._model_path = Path(model_path) if model_path else None
        self._conf = conf_threshold
        self._model: Any = None
        self._cv2: Any = None
        self._status = "uninitialized"
        self._mode = mode if mode in {"mosaic", "blur", "bar", "heart"} else "mosaic"
        self._bar_count = max(1, int(bar_count))
        self._heart_template: Any = None
        self._box_expand = box_expand or {}
        self._box_expand_default = box_expand_default
        self._class_thresholds = class_thresholds or {}

    @property
    def status(self) -> str:
        return self._status

    def is_available(self) -> bool:
        return self._ensure_loaded() is not None

    def _ensure_loaded(self) -> Any:
        if self._model is not None:
            return self._model
        if self._status not in {"uninitialized"}:
            # already attempted, don't retry on every image
            return None
        if self._model_path is None or not self._model_path.exists():
            self._status = "model_missing"
            log.warning(
                f"censor 模型文件不存在: {self._model_path}（跳过自动打码）"
            )
            return None
        try:
            import cv2  # noqa: F401
            self._cv2 = cv2
        except ImportError:
            self._status = "cv2_missing"
            log.warning("censor 需要 opencv-python，未安装；跳过自动打码")
            return None
        try:
            from ultralytics import YOLO
        except ImportError:
            self._status = "ultralytics_missing"
            log.warning("censor 需要 ultralytics，未安装；跳过自动打码")
            return None
        try:
            self._model = YOLO(str(self._model_path))
            self._status = "ok"
            log.info(f"censor 模型加载成功: {self._model_path.name}")
            return self._model
        except Exception as exc:
            self._status = "load_error"
            log.warning(f"censor 模型加载失败: {type(exc).__name__}: {exc}")
            return None

    def _load_heart(self):
        """Lazy-load heart_base.png as BGRA numpy array at original resolution."""
        if self._heart_template is not None:
            return self._heart_template
        heart_path = Path(__file__).parent / "heart_base.png"
        if not heart_path.exists():
            log.warning(f"heart 模式需要 {heart_path}，文件不存在；回退到 mosaic")
            return None
        import numpy as np
        cv2 = self._cv2
        with open(heart_path, "rb") as f:
            raw = f.read()
        arr = np.frombuffer(raw, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_UNCHANGED)
        if img is None or img.ndim < 3:
            return None
        if img.shape[2] == 3:
            alpha = np.full((*img.shape[:2], 1), 255, dtype=np.uint8)
            img = np.concatenate([img, alpha], axis=2)
        self._heart_template = img
        return img

    def _paste_one_heart(self, img, cx, cy, scale, angle, np):
        """Paste a single heart at (cx,cy) with given scale and rotation angle."""
        cv2 = self._cv2
        template = self._heart_template
        th, tw = template.shape[:2]
        sw = max(1, int(tw * scale))
        sh = max(1, int(th * scale))
        resized = cv2.resize(template, (sw, sh),
                             interpolation=cv2.INTER_AREA if scale < 1.0 else cv2.INTER_LINEAR)
        M = cv2.getRotationMatrix2D((sw / 2, sh / 2), angle, 1.0)
        cos_a, sin_a = abs(M[0, 0]), abs(M[0, 1])
        nw = int(sh * sin_a + sw * cos_a)
        nh = int(sh * cos_a + sw * sin_a)
        M[0, 2] += (nw - sw) / 2
        M[1, 2] += (nh - sh) / 2
        rotated = cv2.warpAffine(resized, M, (nw, nh),
                                 flags=cv2.INTER_LINEAR, borderValue=(0, 0, 0, 0))
        h_img, w_img = img.shape[:2]
        px1 = cx - nw // 2
        py1 = cy - nh // 2
        sx1 = max(0, -px1)
        sy1 = max(0, -py1)
        sx2 = nw - max(0, px1 + nw - w_img)
        sy2 = nh - max(0, py1 + nh - h_img)
        dx1 = max(0, px1)
        dy1 = max(0, py1)
        dx2 = min(w_img, px1 + nw)
        dy2 = min(h_img, py1 + nh)
        if dx2 <= dx1 or dy2 <= dy1 or sx2 <= sx1 or sy2 <= sy1:
            return
        crop = rotated[sy1:sy2, sx1:sx2]
        alpha = crop[:, :, 3:4].astype(np.float32) / 255.0
        bgr = crop[:, :, :3].astype(np.float32)
        roi = img[dy1:dy2, dx1:dx2].astype(np.float32)
        img[dy1:dy2, dx1:dx2] = (roi * (1 - alpha) + bgr * alpha).astype(np.uint8)

    def _stamp_heart(self, img, x1, y1, x2, y2, rng):
        """Scatter many small hearts over the bbox region."""
        import numpy as np
        import math
        template = self._load_heart()
        if template is None:
            return False
        th, tw = template.shape[:2]
        bw, bh = x2 - x1, y2 - y1
        bbox_area = bw * bh
        heart_area = tw * th
        if bbox_area < heart_area * 0.5:
            scale = min(bw / tw, bh / th)
            self._paste_one_heart(img, (x1 + x2) // 2, (y1 + y2) // 2,
                                  scale, rng.uniform(-25, 25), np)
            return True
        count = max(3, math.ceil(bbox_area / heart_area * 1.8))
        for _ in range(count):
            cx = rng.randint(x1, x2)
            cy = rng.randint(y1, y2)
            scale = rng.uniform(0.8, 1.2)
            angle = rng.uniform(-25, 25)
            self._paste_one_heart(img, cx, cy, scale, angle, np)
        return True

    def detect_and_censor(
        self,
        image_path: Path,
        output_path: Path | None = None,
        enabled_classes: frozenset[int] | set[int] | None = None,
        secondary_detector: "DeepghsDetector | None" = None,
    ) -> CensorResult:
        """Run detection on image_path, apply mosaic to enabled classes, write result.

        If output_path is None, overwrites image_path. Returns CensorResult with
        applied=False if no enabled-class detections were found (image untouched).
        """
        model = self._ensure_loaded()
        if model is None:
            return CensorResult(status=self._status, applied=False, detail=f"engine status={self._status}")

        if enabled_classes is None:
            enabled_classes = DEFAULT_CENSOR_CLASSES
        cv2 = self._cv2

        try:
            img = cv2.imread(str(image_path))
            if img is None:
                # cv2.imread fails on non-ASCII paths on Windows; fall back to numpy buffer
                import numpy as np
                with open(image_path, "rb") as f:
                    raw = f.read()
                arr = np.frombuffer(raw, dtype=np.uint8)
                img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            if img is None:
                return CensorResult(status="io_error", applied=False, detail=f"cannot read {image_path}")
        except Exception as exc:
            return CensorResult(status="io_error", applied=False, detail=f"{type(exc).__name__}: {exc}")

        effective_conf = self._conf
        if self._class_thresholds:
            effective_conf = min(self._conf, *self._class_thresholds.values())
        try:
            results = model.predict(img, conf=effective_conf, verbose=False)
        except Exception as exc:
            return CensorResult(status="infer_error", applied=False, detail=f"{type(exc).__name__}: {exc}")

        h, w = img.shape[:2]
        pixiv_min = max(4, max(w, h) // 150)
        block_size = max(8, min(w, h) // 140, pixiv_min)
        all_dets: list[dict[str, Any]] = []
        applied_count = 0
        for r in results:
            boxes = getattr(r, "boxes", None)
            if boxes is None:
                continue
            for b in boxes:
                try:
                    cls = int(b.cls[0].cpu().numpy())
                    conf = float(b.conf[0].cpu().numpy())
                    x1, y1, x2, y2 = b.xyxy[0].cpu().numpy().tolist()
                except Exception:
                    continue
                x1, y1, x2, y2 = int(x1), int(y1), int(x2), int(y2)
                x1 = max(0, min(w - 1, x1))
                x2 = max(0, min(w, x2))
                y1 = max(0, min(h - 1, y1))
                y2 = max(0, min(h, y2))
                cls_threshold = self._class_thresholds.get(cls, self._conf)
                should_apply = cls in enabled_classes and conf >= cls_threshold and x2 > x1 and y2 > y1
                if should_apply:
                    expand_ratio = self._box_expand.get(cls, self._box_expand_default)
                    if expand_ratio > 0:
                        bw, bh = x2 - x1, y2 - y1
                        ex, ey = int(bw * expand_ratio), int(bh * expand_ratio)
                        x1 = max(0, x1 - ex)
                        y1 = max(0, y1 - ey)
                        x2 = min(w, x2 + ex)
                        y2 = min(h, y2 + ey)
                det = {
                    "class": cls,
                    "name": CENSOR_CLASS_NAMES.get(cls, str(cls)),
                    "confidence": round(conf, 3),
                    "bbox": [x1, y1, x2, y2],
                    "applied": should_apply,
                }
                all_dets.append(det)
                if not det["applied"]:
                    continue
                if self._mode == "heart":
                    rng = random.Random(hash((x1, y1, x2, y2)))
                    if self._stamp_heart(img, x1, y1, x2, y2, rng):
                        applied_count += 1
                    else:
                        det["applied"] = False
                elif self._mode == "bar":
                    bbox_h = y2 - y1
                    n = self._bar_count
                    # Each "slot" gets a bar in its top half + a gap in bottom half.
                    # Bars occupy roughly 50% of bbox height total.
                    slot = bbox_h / n
                    bar_h = max(6, int(slot * 0.5))
                    drew = 0
                    for i in range(n):
                        cy = y1 + int((i + 0.5) * slot)
                        by1 = max(0, cy - bar_h // 2)
                        by2 = min(h, by1 + bar_h)
                        if by2 > by1 and x2 > x1:
                            img[by1:by2, x1:x2] = 0  # BGR black
                            drew += 1
                    if drew:
                        applied_count += 1
                    else:
                        det["applied"] = False
                elif self._mode == "blur":
                    roi = img[y1:y2, x1:x2]
                    if roi.size == 0:
                        det["applied"] = False
                        continue
                    rh, rw = roi.shape[:2]
                    k = max(15, (min(rh, rw) // 4) | 1)  # odd kernel
                    img[y1:y2, x1:x2] = cv2.GaussianBlur(roi, (k, k), sigmaX=k / 2)
                    applied_count += 1
                else:  # mosaic
                    roi = img[y1:y2, x1:x2]
                    if roi.size == 0:
                        det["applied"] = False
                        continue
                    rh, rw = roi.shape[:2]
                    small_w = max(1, rw // block_size)
                    small_h = max(1, rh // block_size)
                    small = cv2.resize(roi, (small_w, small_h), interpolation=cv2.INTER_LINEAR)
                    pixelated = cv2.resize(small, (rw, rh), interpolation=cv2.INTER_NEAREST)
                    # Light smoothing to soften block edges without losing the
                    # mosaic structure (kernel ~25% of block_size, odd).
                    smooth_k = max(3, (block_size // 4) | 1)
                    img[y1:y2, x1:x2] = cv2.GaussianBlur(pixelated, (smooth_k, smooth_k), 0)
                    applied_count += 1

        # Secondary detector: merge additional detections from deepghs
        if secondary_detector is not None and secondary_detector.is_available():
            try:
                secondary_dets = secondary_detector.detect(image_path)
                secondary_dets = [d for d in secondary_dets if d["class"] in enabled_classes]
                if secondary_dets:
                    new_dets = merge_detections([], secondary_dets, iou_threshold=0.5)
                    for sd in new_dets:
                        already_covered = any(
                            d["class"] == sd["class"]
                            and d["applied"]
                            and _iou(d["bbox"], sd["bbox"]) > 0.3
                            for d in all_dets
                        )
                        if already_covered:
                            sd["applied"] = False
                            sd["source"] = "deepghs_dup"
                            all_dets.append(sd)
                            continue
                        cls = sd["class"]
                        expand_ratio = self._box_expand.get(cls, self._box_expand_default)
                        x1, y1, x2, y2 = sd["bbox"]
                        if expand_ratio > 0:
                            bw, bh = x2 - x1, y2 - y1
                            ex, ey = int(bw * expand_ratio), int(bh * expand_ratio)
                            x1 = max(0, x1 - ex)
                            y1 = max(0, y1 - ey)
                            x2 = min(w, x2 + ex)
                            y2 = min(h, y2 + ey)
                            sd["bbox"] = [x1, y1, x2, y2]
                        if x2 > x1 and y2 > y1:
                            if self._mode == "heart":
                                rng = random.Random(hash((x1, y1, x2, y2)))
                                if self._stamp_heart(img, x1, y1, x2, y2, rng):
                                    applied_count += 1
                                else:
                                    sd["applied"] = False
                            elif self._mode == "bar":
                                bbox_h = y2 - y1
                                n = self._bar_count
                                slot = bbox_h / n
                                bar_h = max(6, int(slot * 0.5))
                                for i in range(n):
                                    cy = y1 + int((i + 0.5) * slot)
                                    by1 = max(0, cy - bar_h // 2)
                                    by2 = min(h, by1 + bar_h)
                                    if by2 > by1 and x2 > x1:
                                        img[by1:by2, x1:x2] = 0
                                applied_count += 1
                            elif self._mode == "blur":
                                roi = img[y1:y2, x1:x2]
                                if roi.size > 0:
                                    rh, rw = roi.shape[:2]
                                    k = max(15, (min(rh, rw) // 4) | 1)
                                    img[y1:y2, x1:x2] = cv2.GaussianBlur(roi, (k, k), sigmaX=k / 2)
                                    applied_count += 1
                                else:
                                    sd["applied"] = False
                            else:  # mosaic
                                roi = img[y1:y2, x1:x2]
                                if roi.size > 0:
                                    rh, rw = roi.shape[:2]
                                    small_w = max(1, rw // block_size)
                                    small_h = max(1, rh // block_size)
                                    small = cv2.resize(roi, (small_w, small_h), interpolation=cv2.INTER_LINEAR)
                                    pixelated = cv2.resize(small, (rw, rh), interpolation=cv2.INTER_NEAREST)
                                    smooth_k = max(3, (block_size // 4) | 1)
                                    img[y1:y2, x1:x2] = cv2.GaussianBlur(pixelated, (smooth_k, smooth_k), 0)
                                    applied_count += 1
                                else:
                                    sd["applied"] = False
                        else:
                            sd["applied"] = False
                        all_dets.append(sd)
            except Exception as exc:
                log.warning(f"辅助检测合并异常: {type(exc).__name__}: {exc}")

        if applied_count == 0:
            return CensorResult(
                status="ok",
                applied=False,
                detections=all_dets,
                output_path=image_path,
                detail=f"no enabled-class detections (total dets: {len(all_dets)}, block_size={block_size})",
            )

        out = Path(output_path) if output_path else image_path
        try:
            ok, buf = cv2.imencode(out.suffix or ".png", img)
            if not ok:
                return CensorResult(status="io_error", applied=False, detections=all_dets,
                                    detail="cv2.imencode returned False")
            with open(out, "wb") as f:
                f.write(buf.tobytes())
        except Exception as exc:
            return CensorResult(status="io_error", applied=False, detections=all_dets,
                                detail=f"write failed: {type(exc).__name__}: {exc}")

        return CensorResult(
            status="ok",
            applied=True,
            detections=all_dets,
            output_path=out,
            detail=f"applied {self._mode} to {applied_count} regions",
        )


# ---------------------------------------------------------------------------
#  Secondary detector: deepghs/imgutils anime censor detection
# ---------------------------------------------------------------------------

_DEEPGHS_LABEL_MAP = {
    "pussy": 4,     # vagina
    "penis": 2,     # dick
    "nipple_f": 3,  # tits/breasts
}


def _iou(a: list[int], b: list[int]) -> float:
    """Intersection-over-union for two [x1,y1,x2,y2] boxes."""
    ix1 = max(a[0], b[0])
    iy1 = max(a[1], b[1])
    ix2 = min(a[2], b[2])
    iy2 = min(a[3], b[3])
    inter = max(0, ix2 - ix1) * max(0, iy2 - iy1)
    if inter == 0:
        return 0.0
    area_a = (a[2] - a[0]) * (a[3] - a[1])
    area_b = (b[2] - b[0]) * (b[3] - b[1])
    return inter / (area_a + area_b - inter)


def _union_bbox(a: list[int], b: list[int]) -> list[int]:
    return [min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])]


def merge_detections(
    primary: list[dict],
    secondary: list[dict],
    iou_threshold: float = 0.5,
) -> list[dict]:
    """Merge secondary detections into primary. Same-class overlaps get union bbox."""
    merged = [dict(d) for d in primary]
    for s in secondary:
        overlaps = False
        for i, p in enumerate(merged):
            if p["class"] == s["class"] and _iou(p["bbox"], s["bbox"]) > iou_threshold:
                merged[i]["bbox"] = _union_bbox(p["bbox"], s["bbox"])
                merged[i]["source"] = "merged"
                overlaps = True
                break
        if not overlaps:
            merged.append(dict(s))
    return merged


class DeepghsDetector:
    """Optional secondary detector using deepghs/imgutils anime censor detection."""

    def __init__(
        self,
        model_name: str | None = None,
        conf: float = 0.25,
        iou: float = 0.7,
        level: str = "s",
    ):
        self._model_name = model_name
        self._conf = conf
        self._iou = iou
        self._level = level
        self._available: bool | None = None

    def is_available(self) -> bool:
        if self._available is not None:
            return self._available
        try:
            from imgutils.detect import detect_censors  # noqa: F401
            self._available = True
        except ImportError:
            self._available = False
            log.warning("deepghs 辅助检测不可用（需 pip install dghs-imgutils）")
        return self._available

    def detect(self, image_path: Path) -> list[dict]:
        """Run detection, return results in same format as YOLO detections."""
        if not self.is_available():
            return []
        import os
        old_offline = os.environ.get("HF_HUB_OFFLINE")
        os.environ["HF_HUB_OFFLINE"] = "1"
        try:
            from imgutils.detect import detect_censors
            kwargs = {"conf_threshold": self._conf, "iou_threshold": self._iou, "level": self._level}
            if self._model_name:
                kwargs["model_name"] = self._model_name
            try:
                results = detect_censors(str(image_path), **kwargs)
            except Exception:
                if old_offline is not None:
                    os.environ["HF_HUB_OFFLINE"] = old_offline
                else:
                    os.environ.pop("HF_HUB_OFFLINE", None)
                results = detect_censors(str(image_path), **kwargs)
        except Exception as exc:
            log.warning(f"deepghs 检测失败: {type(exc).__name__}: {exc}")
            return []
        finally:
            if old_offline is not None:
                os.environ["HF_HUB_OFFLINE"] = old_offline
            else:
                os.environ.pop("HF_HUB_OFFLINE", None)

        dets = []
        for bbox, label, conf in results:
            cls = _DEEPGHS_LABEL_MAP.get(label)
            if cls is None:
                continue
            x1, y1, x2, y2 = bbox
            dets.append({
                "class": cls,
                "name": CENSOR_CLASS_NAMES.get(cls, str(cls)),
                "confidence": round(conf, 3),
                "bbox": [int(x1), int(y1), int(x2), int(y2)],
                "applied": True,
                "source": "deepghs",
            })
        return dets
