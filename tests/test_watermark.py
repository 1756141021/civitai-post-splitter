from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PIL import Image, ImageChops, ImageDraw, PngImagePlugin
from werkzeug.datastructures import FileStorage
from werkzeug.test import EnvironBuilder, stream_encode_multipart

from pixiv.support import sanitize_image_for_pixiv
from watermark import (
    FontFormatRegistry,
    FontStore,
    PillowFontFormatHandler,
    TextWatermarkSpec,
    WatermarkError,
    WatermarkService,
    _save_pixels_only,
)


class WatermarkRenderingTests(unittest.TestCase):
    def test_sanitized_artifact_is_watermarked_without_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.png"
            source_info = PngImagePlugin.PngInfo()
            source_info.add_text("parameters", "prompt that must not survive")
            Image.new("RGB", (640, 480), (28, 40, 58)).save(source, "PNG", pnginfo=source_info)

            clean = sanitize_image_for_pixiv(source, root / "clean").output_path
            with Image.open(clean) as image:
                self.assertNotIn("parameters", image.info)
                before = image.convert("RGB").copy()

            service = WatermarkService(root)
            spec = service.save_config({
                "version": 1,
                "renderer": "text",
                "enabled": True,
                "text": "TEST WATERMARK",
                "font": {"file_name": "", "face_index": 0},
                "style": {
                    "position": "bottom_right",
                    "font_size_ratio": 0.08,
                    "opacity": 1.0,
                    "color": "#FFFFFF",
                    "stroke_color": "#000000",
                    "margin_ratio": 0.04,
                },
            })
            result = service.render(clean, spec)

            self.assertTrue(result.applied)
            self.assertEqual(result.renderer, "text")
            with Image.open(clean) as image:
                self.assertEqual(image.size, before.size)
                self.assertNotIn("parameters", image.info)
                after = image.convert("RGB")
                self.assertIsNotNone(ImageChops.difference(before, after).getbbox())


class WatermarkRegistryTests(unittest.TestCase):
    def test_injected_font_registry_validates_custom_format(self) -> None:
        class TestFontFormat(PillowFontFormatHandler):
            format_id = "test-truetype"
            suffixes = (".foo",)

        font_path = FontStore._find_system_font()
        if font_path is None:
            self.skipTest("No system TrueType/OpenType font is available")
        with tempfile.TemporaryDirectory() as temp_dir:
            registry = FontFormatRegistry()
            registry.register(TestFontFormat())
            store = FontStore(Path(temp_dir), registry)
            imported = store.import_font("future-format.foo", font_path.read_bytes())
            self.assertEqual(imported.format_id, "test-truetype")

            service = WatermarkService(Path(temp_dir), font_store=store)
            spec = service.save_config({
                "version": 1,
                "renderer": "text",
                "enabled": True,
                "text": "registry test",
                "font": {"file_name": imported.file_name, "face_index": 0},
                "style": {},
            })
            self.assertEqual(spec.font.file_name, imported.file_name)

    def test_invalid_saved_font_config_returns_recoverable_payload(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "watermark.json").write_text(
                '{"version":1,"renderer":"text","enabled":true,"text":"x",'
                '"font":{"file_name":"missing.ttf","face_index":0},"style":{}}',
                encoding="utf-8",
            )
            payload = WatermarkService(root).config_payload()
            self.assertIn("config_error", payload)
            self.assertFalse(payload["config"]["enabled"])


class WatermarkPipelineIsolationTests(unittest.TestCase):
    def test_civitai_only_upload_does_not_load_watermark_configuration(self) -> None:
        import civitai_splitter

        with patch.object(civitai_splitter, "WatermarkService", side_effect=AssertionError("must not load")):
            service, spec = civitai_splitter._load_watermark_for_targets(["civitai"])
        self.assertIsNone(service)
        self.assertIsNone(spec)

    def test_watermark_failure_keeps_civitai_pending_and_blocks_pixiv(self) -> None:
        import civitai_splitter

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.png"
            civitai_copy = root / "civitai.png"
            clean_copy = root / "clean.png"
            Image.new("RGB", (80, 80), "white").save(source)
            Image.new("RGB", (80, 80), "white").save(civitai_copy)
            Image.new("RGB", (80, 80), "white").save(clean_copy)
            payload = {
                "raw_candidates": [], "metadata_entity_hits": [], "popularity_decisions": [],
                "final_tags": [], "entity_tags": [], "rejected_tags": [], "domain": "original",
                "title_ja": "", "title_zh": "", "caption_ja": "", "caption_zh": "",
                "age_restriction": "all_ages", "ai_generated": False,
            }
            metadata = {"available": True, "status": "clean", "detected_types": [], "details": []}
            service = Mock()
            service.render.side_effect = WatermarkError("render failed")
            spec = TextWatermarkSpec(enabled=True, text="watermark")
            with patch.object(civitai_splitter, "strip_prompts_keep_lora", return_value=civitai_copy), \
                 patch.object(civitai_splitter, "sanitize_image_for_pixiv", return_value=SimpleNamespace(output_path=clean_copy)), \
                 patch.object(civitai_splitter, "build_pixiv_payload", return_value=payload), \
                 patch.object(civitai_splitter, "append_validation_case"), \
                 patch.object(civitai_splitter.log, "error"):
                manifest, pixiv_ready = civitai_splitter.create_upload_manifest(
                    image_path=source,
                    targets=["civitai", "pixiv"],
                    files={"validation": root / "validation.json"},
                    hain_bridge=SimpleNamespace(read_metadata=lambda _: metadata),
                    alias_data={}, popularity_data={}, age_rules={},
                    civitai_dir=root, pixiv_dir=root,
                    pixiv_privacy="public", pixiv_allow_tag_edits=False,
                    watermark_service=service, watermark_spec=spec,
                )
            self.assertEqual(manifest["status_by_target"]["civitai"], "pending")
            self.assertEqual(manifest["status_by_target"]["pixiv"], "failed")
            self.assertFalse(pixiv_ready)
            self.assertEqual(manifest["watermark"]["status"], "failed")


class WatermarkApiTests(unittest.TestCase):
    def setUp(self) -> None:
        import web_server

        self._temp_dir = tempfile.TemporaryDirectory()
        self._web_server = web_server
        self._original_script_dir = web_server.SCRIPT_DIR
        web_server.SCRIPT_DIR = Path(self._temp_dir.name)
        self.client = web_server.app.test_client()

    def tearDown(self) -> None:
        self._web_server.SCRIPT_DIR = self._original_script_dir
        self._temp_dir.cleanup()

    def test_config_and_font_api_contract(self) -> None:
        remote = self.client.get("/api/watermark-config", environ_overrides={"REMOTE_ADDR": "192.0.2.1"})
        self.assertEqual(remote.status_code, 403)

        initial = self.client.get("/api/watermark-config")
        self.assertEqual(initial.status_code, 200)
        payload = initial.get_json()
        self.assertEqual(payload["config"]["renderer"], "text")
        self.assertIn(".ttf", payload["supported_font_formats"])
        self.assertIn(".otf", payload["supported_font_formats"])

        invalid = self.client.post("/api/watermark-config", json={
            "version": 1,
            "renderer": "text",
            "enabled": True,
            "text": "",
            "font": {"file_name": "", "face_index": 0},
            "style": {},
        })
        self.assertEqual(invalid.status_code, 400)

        font_path = FontStore._find_system_font()
        if font_path is None:
            self.skipTest("No system TrueType/OpenType font is available")
        stream, content_length, boundary = stream_encode_multipart(
            {"font": FileStorage(stream=io.BytesIO(font_path.read_bytes()), filename=font_path.name)},
            use_tempfile=False,
        )
        builder = EnvironBuilder(
            path="/api/watermark-font",
            method="POST",
            input_stream=stream,
            content_length=content_length,
            content_type=f"multipart/form-data; boundary={boundary}",
        )
        try:
            imported = self.client.open(builder)
        finally:
            stream.close()
        self.assertEqual(imported.status_code, 200)
        imported_payload = imported.get_json()
        font = imported_payload["font"]
        self.assertTrue(font["faces"])

        saved = self.client.post("/api/watermark-config", json={
            "version": 1,
            "renderer": "text",
            "enabled": True,
            "text": "API WATERMARK",
            "font": {"file_name": font["file_name"], "face_index": font["faces"][0]["index"]},
            "style": {
                "position": "top_left",
                "font_size_ratio": 0.04,
                "opacity": 0.7,
                "color": "#FFFFFF",
                "stroke_color": "#000000",
                "margin_ratio": 0.02,
            },
        })
        self.assertEqual(saved.status_code, 200)
        self.assertEqual(saved.get_json()["config"]["font"]["file_name"], font["file_name"])


class ImageWatermarkRenderingTests(unittest.TestCase):
    def test_rgba_watermark_on_jpeg_blends_with_alpha(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            base_path = root / "base.jpg"
            Image.new("RGB", (400, 300), (200, 200, 200)).save(base_path, "JPEG")
            mark_path = root / "mark.png"
            mark = Image.new("RGBA", (60, 60), (0, 0, 0, 0))
            ImageDraw.Draw(mark).ellipse([5, 5, 55, 55], fill=(255, 0, 0, 255))
            mark.save(mark_path, "PNG")

            service = WatermarkService(root)
            file_name = service.import_image("mark.png", mark_path.read_bytes())
            spec = service.save_config({
                "version": 1,
                "renderer": "image",
                "enabled": True,
                "image": {"file_name": file_name},
                "style": {"position": "center", "size_ratio": 0.15, "opacity": 1.0, "margin_ratio": 0.0},
            })
            result = service.render(base_path, spec)

            self.assertTrue(result.applied)
            self.assertEqual(result.renderer, "image")
            with Image.open(base_path) as out:
                self.assertEqual(out.mode, "RGB")
                cx, cy = out.width // 2, out.height // 2
                red = out.getpixel((cx, cy))
                self.assertGreater(red[0], 250)   # 水印红圆中心：红分量高
                self.assertLess(red[1], 100)      # 绿/蓝分量低
                self.assertEqual(out.getpixel((2, 2)), (200, 200, 200))  # 角落未被污染

    def test_save_pixels_only_jpeg_alpha_uses_white_background(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            img = Image.new("RGBA", (40, 30), (0, 0, 0, 0))
            ImageDraw.Draw(img).rectangle([0, 0, 19, 29], fill=(10, 20, 30, 255))
            dest = Path(temp_dir) / "out.jpg"
            _save_pixels_only(img, dest, "RGBA")
            with Image.open(dest) as out:
                self.assertEqual(out.getpixel((25, 15)), (255, 255, 255))  # 透明区 -> 白底
                self.assertEqual(out.getpixel((5, 15)), (10, 20, 30))      # 不透明区保留

    def test_image_watermark_requires_selected_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = WatermarkService(Path(temp_dir))
            with self.assertRaises(WatermarkError):
                service.save_config({
                    "version": 1,
                    "renderer": "image",
                    "enabled": True,
                    "image": {"file_name": "missing.png"},
                    "style": {},
                })


class ImageWatermarkApiTests(unittest.TestCase):
    def setUp(self) -> None:
        import web_server

        self._temp_dir = tempfile.TemporaryDirectory()
        self._web_server = web_server
        self._original_script_dir = web_server.SCRIPT_DIR
        web_server.SCRIPT_DIR = Path(self._temp_dir.name)
        self.client = web_server.app.test_client()

    def tearDown(self) -> None:
        self._web_server.SCRIPT_DIR = self._original_script_dir
        self._temp_dir.cleanup()

    def test_import_preview_and_delete_image_contract(self) -> None:
        remote = self.client.get("/api/watermark-image/anything.png", environ_overrides={"REMOTE_ADDR": "192.0.2.1"})
        self.assertEqual(remote.status_code, 403)

        png_bytes = io.BytesIO()
        mark = Image.new("RGBA", (40, 40), (0, 0, 0, 0))
        ImageDraw.Draw(mark).ellipse([2, 2, 37, 37], fill=(255, 0, 0, 255))
        mark.save(png_bytes, "PNG")
        png_bytes.seek(0)

        stream, content_length, boundary = stream_encode_multipart(
            {"image": FileStorage(stream=png_bytes, filename="logo.png")},
            use_tempfile=False,
        )
        builder = EnvironBuilder(
            path="/api/watermark-image",
            method="POST",
            input_stream=stream,
            content_length=content_length,
            content_type=f"multipart/form-data; boundary={boundary}",
        )
        try:
            imported = self.client.open(builder)
        finally:
            stream.close()
        self.assertEqual(imported.status_code, 200)
        payload = imported.get_json()
        self.assertIn("logo.png", payload["images"])
        self.assertIn(".png", payload["supported_image_formats"])

        preview = self.client.get(f"/api/watermark-image/{payload['file_name']}")
        self.assertEqual(preview.status_code, 200)
        try:
            with Image.open(io.BytesIO(preview.data)) as served:
                self.assertEqual(served.mode, "RGBA")  # alpha 保留
        finally:
            preview.close()

        saved = self.client.post("/api/watermark-config", json={
            "version": 1,
            "renderer": "image",
            "enabled": True,
            "image": {"file_name": payload["file_name"]},
            "style": {"position": "bottom_right", "size_ratio": 0.2, "opacity": 0.9, "margin_ratio": 0.02},
        })
        self.assertEqual(saved.status_code, 200)
        self.assertEqual(saved.get_json()["config"]["renderer"], "image")

        deleted = self.client.delete(f"/api/watermark-image/{payload['file_name']}")
        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(deleted.get_json()["config"]["enabled"], False)  # 删除引用图时自动禁用


if __name__ == "__main__":
    unittest.main()
