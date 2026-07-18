from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any, Protocol

from PIL import Image, ImageColor, ImageDraw, ImageFont

CONFIG_FILENAME = "watermark.json"
FONTS_DIRNAME = "watermark_fonts"
MAX_FONT_UPLOAD_BYTES = 20 * 1024 * 1024
MAX_FONT_STORAGE_BYTES = 100 * 1024 * 1024
MAX_FONT_FACES = 64
MAX_TEXT_LENGTH = 512


class WatermarkError(ValueError):
    pass


@dataclass(frozen=True)
class FontFace:
    index: int
    family: str
    style: str

    def to_dict(self) -> dict[str, Any]:
        return {"index": self.index, "family": self.family, "style": self.style}


@dataclass(frozen=True)
class FontFile:
    file_name: str
    format_id: str
    faces: tuple[FontFace, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "file_name": self.file_name,
            "format": self.format_id,
            "faces": [face.to_dict() for face in self.faces],
        }


@dataclass(frozen=True)
class FontSelection:
    file_name: str = ""
    face_index: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {"file_name": self.file_name, "face_index": self.face_index}


@dataclass(frozen=True)
class TextWatermarkSpec:
    version: int = 1
    renderer: str = "text"
    enabled: bool = False
    text: str = ""
    font: FontSelection = FontSelection()
    position: str = "bottom_right"
    font_size_ratio: float = 0.045
    opacity: float = 0.72
    color: str = "#FFFFFF"
    stroke_color: str = "#000000"
    margin_ratio: float = 0.025

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "renderer": self.renderer,
            "enabled": self.enabled,
            "text": self.text,
            "font": self.font.to_dict(),
            "style": {
                "position": self.position,
                "font_size_ratio": self.font_size_ratio,
                "opacity": self.opacity,
                "color": self.color,
                "stroke_color": self.stroke_color,
                "margin_ratio": self.margin_ratio,
            },
        }


@dataclass(frozen=True)
class WatermarkRenderResult:
    renderer: str
    applied: bool
    output_path: str
    font_file: str
    font_face_index: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "renderer": self.renderer,
            "applied": self.applied,
            "output_path": self.output_path,
            "font": {
                "file_name": self.font_file,
                "face_index": self.font_face_index,
            },
        }


class FontFormatHandler(Protocol):
    format_id: str
    suffixes: tuple[str, ...]

    def inspect(self, data: bytes) -> tuple[FontFace, ...]:
        ...

    def load(self, data: bytes, size: int, face_index: int):
        ...


class PillowFontFormatHandler:
    format_id = "truetype"
    suffixes = (".ttf", ".otf", ".ttc", ".otc")

    def load(self, data: bytes, size: int, face_index: int):
        try:
            return ImageFont.truetype(BytesIO(data), size=size, index=face_index)
        except OSError as exc:
            raise WatermarkError(f"Unable to load font face {face_index}") from exc

    def inspect(self, data: bytes) -> tuple[FontFace, ...]:
        faces: list[FontFace] = []
        for face_index in range(MAX_FONT_FACES):
            try:
                font = self.load(data, size=20, face_index=face_index)
            except WatermarkError:
                if face_index == 0:
                    raise WatermarkError("The uploaded file is not a readable TrueType/OpenType font")
                break
            try:
                family, style = font.getname()
            except Exception:
                family, style = "Unknown", "Regular"
            faces.append(FontFace(face_index, str(family or "Unknown"), str(style or "Regular")))
        if not faces:
            raise WatermarkError("The uploaded font has no selectable faces")
        return tuple(faces)


class FontFormatRegistry:
    def __init__(self) -> None:
        self._by_suffix: dict[str, FontFormatHandler] = {}

    def register(self, handler: FontFormatHandler) -> None:
        for suffix in handler.suffixes:
            normalized = suffix.lower()
            if normalized in self._by_suffix:
                raise ValueError(f"Font handler already registered for {normalized}")
            self._by_suffix[normalized] = handler

    def handler_for(self, file_name: str) -> FontFormatHandler:
        suffix = Path(file_name).suffix.lower()
        handler = self._by_suffix.get(suffix)
        if handler is None:
            supported = ", ".join(sorted(self._by_suffix))
            raise WatermarkError(f"Unsupported font format {suffix or '(none)'}; supported: {supported}")
        return handler

    @property
    def supported_suffixes(self) -> tuple[str, ...]:
        return tuple(sorted(self._by_suffix))


FONT_FORMATS = FontFormatRegistry()
FONT_FORMATS.register(PillowFontFormatHandler())


class WatermarkRenderer(Protocol):
    renderer_id: str

    def parse_spec(self, raw: dict[str, Any], font_store: "FontStore") -> TextWatermarkSpec:
        ...

    def render(
        self,
        image_path: Path,
        spec: TextWatermarkSpec,
        font_store: "FontStore",
    ) -> WatermarkRenderResult:
        ...


class WatermarkRendererRegistry:
    def __init__(self) -> None:
        self._renderers: dict[str, WatermarkRenderer] = {}

    def register(self, renderer: WatermarkRenderer) -> None:
        if renderer.renderer_id in self._renderers:
            raise ValueError(f"Watermark renderer already registered: {renderer.renderer_id}")
        self._renderers[renderer.renderer_id] = renderer

    def get(self, renderer_id: str) -> WatermarkRenderer:
        renderer = self._renderers.get(renderer_id)
        if renderer is None:
            supported = ", ".join(sorted(self._renderers))
            raise WatermarkError(f"Unsupported watermark renderer {renderer_id}; supported: {supported}")
        return renderer

    @property
    def renderer_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._renderers))


RENDERERS = WatermarkRendererRegistry()


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _bounded_float(value: Any, minimum: float, maximum: float, field: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise WatermarkError(f"{field} must be a number") from exc
    if not minimum <= parsed <= maximum:
        raise WatermarkError(f"{field} must be between {minimum} and {maximum}")
    return round(parsed, 4)


def _bounded_int(value: Any, minimum: int, maximum: int, field: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise WatermarkError(f"{field} must be an integer") from exc
    if not minimum <= parsed <= maximum:
        raise WatermarkError(f"{field} must be between {minimum} and {maximum}")
    return parsed


def _normalize_color(value: Any, field: str) -> str:
    try:
        rgb = ImageColor.getrgb(str(value).strip())
    except ValueError as exc:
        raise WatermarkError(f"{field} must be a valid RGB color") from exc
    if len(rgb) < 3:
        raise WatermarkError(f"{field} must be a valid RGB color")
    return "#{:02X}{:02X}{:02X}".format(*rgb[:3])


def _safe_font_file_name(value: Any, registry: FontFormatRegistry) -> str:
    file_name = str(value or "").strip()
    if not file_name:
        return ""
    if "/" in file_name or "\\" in file_name or Path(file_name).name != file_name:
        raise WatermarkError("font.file_name must be a font filename")
    registry.handler_for(file_name)
    return file_name


def _safe_uploaded_file_name(filename: str, registry: FontFormatRegistry) -> str:
    original = Path(filename).name.strip()
    registry.handler_for(original)
    suffix = Path(original).suffix.lower()
    stem = re.sub(r"[^A-Za-z0-9._ -]+", "_", Path(original).stem).strip(" ._")
    return f"{stem or 'watermark-font'}{suffix}"


class FontStore:
    def __init__(self, project_dir: Path, registry: FontFormatRegistry = FONT_FORMATS) -> None:
        self.project_dir = project_dir
        self.registry = registry

    @property
    def directory(self) -> Path:
        return self.project_dir / FONTS_DIRNAME

    def normalize_file_name(self, value: Any) -> str:
        return _safe_font_file_name(value, self.registry)

    def _path_for(self, file_name: str) -> Path:
        return self.directory / self.normalize_file_name(file_name)

    def inspect_file(self, path: Path) -> FontFile:
        handler = self.registry.handler_for(path.name)
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise WatermarkError(f"Unable to read font file: {path.name}") from exc
        return FontFile(path.name, handler.format_id, handler.inspect(data))

    def list_fonts(self) -> list[FontFile]:
        if not self.directory.exists():
            return []
        inventory: list[FontFile] = []
        for path in sorted(self.directory.iterdir(), key=lambda item: item.name.lower()):
            if not path.is_file():
                continue
            try:
                inventory.append(self.inspect_file(path))
            except WatermarkError:
                continue
        return inventory

    def import_font(self, filename: str, data: bytes) -> FontFile:
        if len(data) > MAX_FONT_UPLOAD_BYTES:
            maximum_mb = MAX_FONT_UPLOAD_BYTES // (1024 * 1024)
            raise WatermarkError(f"Font files must be smaller than {maximum_mb} MB")
        if not data:
            raise WatermarkError("The font file is empty")
        safe_name = _safe_uploaded_file_name(filename, self.registry)
        handler = self.registry.handler_for(safe_name)
        faces = handler.inspect(data)
        existing_bytes = (
            sum(path.stat().st_size for path in self.directory.iterdir() if path.is_file())
            if self.directory.exists() else 0
        )
        if existing_bytes + len(data) > MAX_FONT_STORAGE_BYTES:
            maximum_mb = MAX_FONT_STORAGE_BYTES // (1024 * 1024)
            raise WatermarkError(f"Watermark font storage is limited to {maximum_mb} MB")
        self.directory.mkdir(exist_ok=True)
        candidate = self.directory / safe_name
        sequence = 2
        while candidate.exists():
            candidate = self.directory / f"{Path(safe_name).stem}-{sequence}{Path(safe_name).suffix}"
            sequence += 1
        candidate.write_bytes(data)
        return FontFile(candidate.name, handler.format_id, faces)

    def delete_font(self, file_name: str) -> bool:
        path = self._path_for(file_name)
        if not path.is_file():
            return False
        path.unlink()
        return True

    def load_font(self, selection: FontSelection, size: int):
        if selection.file_name:
            path = self._path_for(selection.file_name)
            if not path.is_file():
                raise WatermarkError(f"Selected watermark font is missing: {selection.file_name}")
            handler = self.registry.handler_for(path.name)
            try:
                data = path.read_bytes()
            except OSError as exc:
                raise WatermarkError(f"Unable to read watermark font: {path.name}") from exc
            return handler.load(data, size, selection.face_index)

        system_font = self._find_system_font()
        if system_font is None:
            return ImageFont.load_default()
        handler = self.registry.handler_for(system_font.name)
        return handler.load(system_font.read_bytes(), size, 0)

    def has_font(self, file_name: str) -> bool:
        return bool(file_name) and self._path_for(file_name).is_file()

    @staticmethod
    def _find_system_font() -> Path | None:
        windows_fonts = Path(os.environ.get("WINDIR") or "C:/Windows") / "Fonts"
        candidates = [
            windows_fonts / "msyh.ttc",
            windows_fonts / "msjh.ttc",
            windows_fonts / "segoeui.ttf",
            windows_fonts / "arial.ttf",
            Path("/System/Library/Fonts/PingFang.ttc"),
            Path("/System/Library/Fonts/Supplemental/Arial Unicode.ttf"),
            Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
            Path("/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"),
            Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        ]
        return next((candidate for candidate in candidates if candidate.is_file()), None)


class TextWatermarkRenderer:
    renderer_id = "text"
    _positions = {"top_left", "top_right", "bottom_left", "bottom_right", "center"}

    def parse_spec(self, raw: dict[str, Any], font_store: FontStore) -> TextWatermarkSpec:
        enabled = _as_bool(raw.get("enabled", False))
        text = str(raw.get("text", "")).replace("\r\n", "\n").replace("\r", "\n").strip()
        if len(text) > MAX_TEXT_LENGTH:
            raise WatermarkError(f"Watermark text must be at most {MAX_TEXT_LENGTH} characters")
        if enabled and not text:
            raise WatermarkError("Watermark text is required when watermarking is enabled")

        font_raw = raw.get("font") if isinstance(raw.get("font"), dict) else {}
        file_name = font_store.normalize_file_name(font_raw.get("file_name", ""))
        if file_name and not font_store.has_font(file_name):
            raise WatermarkError("Selected watermark font was not found")
        face_index = _bounded_int(font_raw.get("face_index", 0), 0, MAX_FONT_FACES - 1, "font.face_index")
        if file_name:
            font_info = next((item for item in font_store.list_fonts() if item.file_name == file_name), None)
            if font_info is None or face_index not in {face.index for face in font_info.faces}:
                raise WatermarkError("Selected font face was not found")

        style = raw.get("style") if isinstance(raw.get("style"), dict) else {}
        position = str(style.get("position", "bottom_right")).strip().lower()
        if position not in self._positions:
            raise WatermarkError("style.position is invalid")
        return TextWatermarkSpec(
            version=_bounded_int(raw.get("version", 1), 1, 1, "version"),
            renderer=self.renderer_id,
            enabled=enabled,
            text=text,
            font=FontSelection(file_name=file_name, face_index=face_index),
            position=position,
            font_size_ratio=_bounded_float(style.get("font_size_ratio", 0.045), 0.01, 0.16, "style.font_size_ratio"),
            opacity=_bounded_float(style.get("opacity", 0.72), 0.05, 1.0, "style.opacity"),
            color=_normalize_color(style.get("color", "#FFFFFF"), "style.color"),
            stroke_color=_normalize_color(style.get("stroke_color", "#000000"), "style.stroke_color"),
            margin_ratio=_bounded_float(style.get("margin_ratio", 0.025), 0.0, 0.15, "style.margin_ratio"),
        )

    def render(self, image_path: Path, spec: TextWatermarkSpec, font_store: FontStore) -> WatermarkRenderResult:
        if not spec.enabled:
            return WatermarkRenderResult(self.renderer_id, False, str(image_path), spec.font.file_name, spec.font.face_index)
        try:
            with Image.open(image_path) as source:
                source.load()
                source_mode = source.mode
                canvas = source.convert("RGBA")
        except (OSError, ValueError) as exc:
            raise WatermarkError(f"Unable to open image for watermarking: {image_path.name}") from exc

        overlay = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)
        font, stroke_width, spacing, bbox, align = self._fit_font(draw, canvas.size, spec, font_store)
        margin = max(0, round(min(canvas.size) * spec.margin_ratio))
        xy = self._coordinates(bbox, canvas.size, margin, spec.position)
        alpha = round(255 * spec.opacity)
        draw.multiline_text(
            xy,
            spec.text,
            font=font,
            fill=(*ImageColor.getrgb(spec.color)[:3], alpha),
            spacing=spacing,
            align=align,
            stroke_width=stroke_width,
            stroke_fill=(*ImageColor.getrgb(spec.stroke_color)[:3], alpha),
        )
        result = Image.alpha_composite(canvas, overlay)
        temp_path = image_path.with_name(f".{image_path.stem}.watermark{image_path.suffix}")
        try:
            self._save_pixels_only(result, temp_path, source_mode)
            temp_path.replace(image_path)
        except OSError as exc:
            raise WatermarkError(f"Unable to save watermarked image: {image_path.name}") from exc
        finally:
            temp_path.unlink(missing_ok=True)

        return WatermarkRenderResult(self.renderer_id, True, str(image_path), spec.font.file_name, spec.font.face_index)

    def _fit_font(
        self,
        draw: ImageDraw.ImageDraw,
        image_size: tuple[int, int],
        spec: TextWatermarkSpec,
        font_store: FontStore,
    ) -> tuple[Any, int, int, tuple[int, int, int, int], str]:
        width, height = image_size
        margin = max(0, round(min(width, height) * spec.margin_ratio))
        font_size = max(12, round(min(width, height) * spec.font_size_ratio))
        align = "center" if spec.position == "center" else ("right" if spec.position.endswith("right") else "left")
        max_width = max(1, width - margin * 2)
        max_height = max(1, height - margin * 2)
        for _ in range(8):
            font = font_store.load_font(spec.font, font_size)
            stroke_width = max(1, round(font_size * 0.06))
            spacing = max(3, round(font_size * 0.18))
            bbox = draw.multiline_textbbox(
                (0, 0),
                spec.text,
                font=font,
                spacing=spacing,
                align=align,
                stroke_width=stroke_width,
            )
            text_width, text_height = bbox[2] - bbox[0], bbox[3] - bbox[1]
            if text_width <= max_width and text_height <= max_height:
                return font, stroke_width, spacing, bbox, align
            scale = min(max_width / max(1, text_width), max_height / max(1, text_height), 0.9)
            next_size = max(8, int(font_size * scale))
            if next_size >= font_size:
                return font, stroke_width, spacing, bbox, align
            font_size = next_size
        return font, stroke_width, spacing, bbox, align

    @staticmethod
    def _coordinates(
        bbox: tuple[int, int, int, int],
        image_size: tuple[int, int],
        margin: int,
        position: str,
    ) -> tuple[int, int]:
        left, top, right, bottom = bbox
        text_width, text_height = right - left, bottom - top
        width, height = image_size
        if position == "top_left":
            x, y = margin, margin
        elif position == "top_right":
            x, y = width - margin - text_width, margin
        elif position == "bottom_left":
            x, y = margin, height - margin - text_height
        elif position == "center":
            x, y = (width - text_width) // 2, (height - text_height) // 2
        else:
            x, y = width - margin - text_width, height - margin - text_height
        return x - left, y - top

    @staticmethod
    def _save_pixels_only(image: Image.Image, destination: Path, source_mode: str) -> None:
        suffix = destination.suffix.lower()
        if suffix in {".jpg", ".jpeg"}:
            image.convert("RGB").save(destination, "JPEG", quality=95, subsampling=0)
        elif suffix == ".webp":
            image.save(destination, "WEBP", quality=95)
        else:
            output = image if "A" in source_mode else image.convert("RGB")
            output.save(destination, "PNG")


RENDERERS.register(TextWatermarkRenderer())


class WatermarkService:
    def __init__(
        self,
        project_dir: Path,
        font_store: FontStore | None = None,
        renderer_registry: WatermarkRendererRegistry = RENDERERS,
    ) -> None:
        self.project_dir = project_dir
        self.font_store = font_store or FontStore(project_dir)
        self.renderer_registry = renderer_registry

    @property
    def config_path(self) -> Path:
        return self.project_dir / CONFIG_FILENAME

    def default_spec(self) -> TextWatermarkSpec:
        return TextWatermarkSpec()

    def load_config(self) -> TextWatermarkSpec:
        if not self.config_path.exists():
            return self.default_spec()
        try:
            raw = json.loads(self.config_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise WatermarkError("Watermark configuration is not valid JSON") from exc
        if not isinstance(raw, dict):
            raise WatermarkError("Watermark configuration must be an object")
        renderer_id = str(raw.get("renderer", "text")).strip().lower()
        renderer = self.renderer_registry.get(renderer_id)
        return renderer.parse_spec(raw, self.font_store)

    def save_config(self, raw: dict[str, Any]) -> TextWatermarkSpec:
        if not isinstance(raw, dict):
            raise WatermarkError("Watermark configuration must be an object")
        renderer_id = str(raw.get("renderer", "text")).strip().lower()
        renderer = self.renderer_registry.get(renderer_id)
        spec = renderer.parse_spec(raw, self.font_store)
        temp_path = self.config_path.with_suffix(".json.tmp")
        temp_path.write_text(json.dumps(spec.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        temp_path.replace(self.config_path)
        return spec

    def config_payload(self) -> dict[str, Any]:
        config_error = ""
        try:
            spec = self.load_config()
        except WatermarkError as exc:
            spec = self.default_spec()
            config_error = str(exc)
        payload = {
            "config": spec.to_dict(),
            "fonts": [font.to_dict() for font in self.font_store.list_fonts()],
            "supported_font_formats": list(self.font_store.registry.supported_suffixes),
            "supported_renderers": list(self.renderer_registry.renderer_ids),
        }
        if config_error:
            payload["config_error"] = config_error
        return payload

    def import_font(self, filename: str, data: bytes) -> FontFile:
        return self.font_store.import_font(filename, data)

    def delete_font(self, file_name: str) -> bool:
        try:
            spec = self.load_config()
        except WatermarkError:
            spec = None
        deleted = self.font_store.delete_font(file_name)
        if not deleted:
            return False
        if spec is not None and spec.font.file_name == file_name:
            self.save_config({
                **spec.to_dict(),
                "font": {"file_name": "", "face_index": 0},
            })
        return True

    def render(self, image_path: Path, spec: TextWatermarkSpec | None = None) -> WatermarkRenderResult:
        current = spec or self.load_config()
        renderer = self.renderer_registry.get(current.renderer)
        return renderer.render(image_path, current, self.font_store)
