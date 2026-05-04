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
        conf_threshold: float = 0.45,
        mode: str = "mosaic",
        bar_count: int = 4,
    ):
        """
        mode:
          - "mosaic" — fine pixelation (传统码，块小且边缘平滑)
          - "blur"   — pure gaussian blur on bbox
          - "bar"    — N 条横向黑 bar 堆叠（日式条码）
        bar_count: number of horizontal bars per region (default 4).
        """
        self._model_path = Path(model_path) if model_path else None
        self._conf = conf_threshold
        self._model: Any = None
        self._cv2: Any = None
        self._status = "uninitialized"
        self._mode = mode if mode in {"mosaic", "blur", "bar"} else "mosaic"
        self._bar_count = max(1, int(bar_count))

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

    def detect_and_censor(
        self,
        image_path: Path,
        output_path: Path | None = None,
        enabled_classes: frozenset[int] | set[int] | None = None,
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

        try:
            results = model.predict(img, conf=self._conf, verbose=False)
        except Exception as exc:
            return CensorResult(status="infer_error", applied=False, detail=f"{type(exc).__name__}: {exc}")

        h, w = img.shape[:2]
        # Fine mosaic — small blocks for higher visual fidelity, then a tiny
        # Gaussian smooth pass kills the harsh staircase edges.
        block_size = max(8, min(w, h) // 140)
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
                det = {
                    "class": cls,
                    "name": CENSOR_CLASS_NAMES.get(cls, str(cls)),
                    "confidence": round(conf, 3),
                    "bbox": [x1, y1, x2, y2],
                    "applied": cls in enabled_classes and x2 > x1 and y2 > y1,
                }
                all_dets.append(det)
                if not det["applied"]:
                    continue
                if self._mode == "bar":
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
