"""Unit tests for comfyprov.generate.

Never touches the real host, network, or sudo. Uses fake filesystem
(temporary files) and mocked urllib.request.
"""

from __future__ import annotations

import json
import struct
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from helpers import png_bytes

from comfyprov.generate import (
    ComfyUIClient,
    GenerationSettings,
    _extract_float,
    _extract_int,
    _extract_str,
    _parse_png_parameters,
    build_workflow,
    discover_models,
    import_metadata,
    interactive,
    run_generation,
)

# ---------------------------------------------------------------------------
# PNG parameter parsing
# ---------------------------------------------------------------------------


def _make_png_with_parameters(params: str) -> bytes:
    """Build a minimal PNG containing a tEXt chunk with 'parameters'."""
    signature = b"\x89PNG\r\n\x1a\n"

    def chunk(ctype: str, content: bytes) -> bytes:
        ctype_b = ctype.encode("latin-1")
        length = struct.pack(">I", len(content))
        crc_data = ctype_b + content
        import zlib

        crc = struct.pack(">I", zlib.crc32(crc_data) & 0xFFFFFFFF)
        return length + ctype_b + content + crc

    text = b"parameters\x00" + params.encode("utf-8")
    return signature + chunk("tEXt", text) + chunk("IEND", b"")


class TestPngMetadata(unittest.TestCase):
    def test_parse_png_parameters_found(self):
        params = "Steps: 6, CFG scale: 1.0, Sampler: er_sde, Scheduler: simple, Seed: 12345"
        png = _make_png_with_parameters(params)
        result = _parse_png_parameters(png)
        self.assertEqual(result, params)

    def test_parse_png_parameters_not_png(self):
        self.assertIsNone(_parse_png_parameters(b"not a png"))

    def test_parse_png_no_parameters_chunk(self):
        signature = b"\x89PNG\r\n\x1a\n"

        def chunk(ctype, content):
            ctype_b = ctype.encode("latin-1")
            length = struct.pack(">I", len(content))
            import zlib

            crc = struct.pack(">I", zlib.crc32(ctype_b + content) & 0xFFFFFFFF)
            return length + ctype_b + content + crc

        png = signature + chunk("tEXt", b"other\x00value") + chunk("IEND", b"")
        self.assertIsNone(_parse_png_parameters(png))

    def test_import_metadata_from_png_file(self):
        params = "Steps: 6, CFG scale: 1.0, Sampler: er_sde, Scheduler: simple, Seed: 42"
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tf:
            tf.write(_make_png_with_parameters(params))
            path = tf.name
        try:
            meta = import_metadata(path)
            self.assertIsNotNone(meta)
            assert meta is not None
            self.assertEqual(meta["steps"], 6)
            self.assertEqual(meta["cfg"], 1.0)
            self.assertEqual(meta["sampler"], "er_sde")
            self.assertEqual(meta["scheduler"], "simple")
            self.assertEqual(meta["seed"], 42)
        finally:
            Path(path).unlink(missing_ok=True)

    def test_import_metadata_missing_file(self):
        self.assertIsNone(import_metadata("/nonexistent/image.png"))

    def test_extractors(self):
        self.assertEqual(_extract_int("Steps: 8", r"Steps:\s*(\d+)"), 8)
        self.assertEqual(_extract_int("No steps", r"Steps:\s*(\d+)"), 0)
        self.assertEqual(_extract_float("CFG scale: 2.5", r"CFG scale:\s*([0-9.]+)"), 2.5)
        self.assertEqual(_extract_float("No cfg", r"CFG scale:\s*([0-9.]+)"), 1.0)
        self.assertEqual(_extract_str("Sampler: euler_a", r"Sampler:\s*([^\s,]+)"), "euler_a")
        self.assertEqual(_extract_str("No sampler", r"Sampler:\s*([^\s,]+)"), "")


# ---------------------------------------------------------------------------
# Model discovery
# ---------------------------------------------------------------------------


class TestDiscoverModels(unittest.TestCase):
    def test_discover_models_sorted(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "zeta.safetensors").write_text("x")
            Path(tmp, "alpha.safetensors").write_text("x")
            Path(tmp, "readme.txt").write_text("x")
            result = discover_models(tmp)
            self.assertEqual(result, ["alpha.safetensors", "zeta.safetensors"])

    def test_discover_models_empty_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(discover_models(tmp), [])

    def test_discover_models_missing_dir(self):
        self.assertEqual(discover_models("/nonexistent/dir"), [])


# ---------------------------------------------------------------------------
# Workflow construction
# ---------------------------------------------------------------------------


class TestBuildWorkflow(unittest.TestCase):
    def test_build_workflow_structure(self):
        settings = GenerationSettings(
            model="Jib_Mix_Moss_V2_H_int8_00001_.safetensors",
            clip_model="qwen3.5_9b_w4a8_gptq.safetensors",
            vae_model="flux2-vae_small.safetensors",
            width=1024,
            height=1024,
            count=1,
            steps=6,
            cfg=1.0,
            sampler="er_sde",
            scheduler="simple",
            seed=-1,
            prompt="a cat",
            neg_prompt="",
            use_tiled_vae=False,
            tile_size=512,
        )
        wf = build_workflow(settings)

        self.assertEqual(wf["1"]["class_type"], "UNETLoader")
        self.assertEqual(wf["1"]["inputs"]["unet_name"], settings.model)

        self.assertEqual(wf["2"]["class_type"], "CLIPLoader")
        self.assertEqual(wf["2"]["inputs"]["clip_name"], settings.clip_model)

        self.assertEqual(wf["3"]["class_type"], "VAELoader")
        self.assertEqual(wf["3"]["inputs"]["vae_name"], settings.vae_model)

        self.assertEqual(wf["4"]["inputs"]["text"], "a cat")
        self.assertEqual(wf["4"]["inputs"]["clip"], ["2", 0])
        self.assertEqual(wf["5"]["inputs"]["text"], "")
        self.assertEqual(wf["5"]["inputs"]["clip"], ["2", 0])

        self.assertEqual(wf["6"]["inputs"]["width"], 1024)
        self.assertEqual(wf["6"]["inputs"]["height"], 1024)
        self.assertEqual(wf["6"]["inputs"]["batch_size"], 1)

        ks = wf["7"]["inputs"]
        self.assertEqual(ks["seed"], -1)
        self.assertEqual(ks["steps"], 6)
        self.assertEqual(ks["cfg"], 1.0)
        self.assertEqual(ks["sampler_name"], "er_sde")
        self.assertEqual(ks["scheduler"], "simple")
        self.assertEqual(ks["model"], ["1", 0])
        self.assertEqual(ks["positive"], ["4", 0])
        self.assertEqual(ks["negative"], ["5", 0])
        self.assertEqual(ks["latent_image"], ["6", 0])

        self.assertEqual(wf["8"]["inputs"]["vae"], ["3", 0])
        self.assertEqual(wf["9"]["inputs"]["images"], ["8", 0])
        self.assertEqual(wf["9"]["inputs"]["filename_prefix"], "ComfyUI")


# ---------------------------------------------------------------------------
# ComfyUI client (mocked HTTP)
# ---------------------------------------------------------------------------


class FakeUrlopen:
    """Context-manager fake for urllib.request.urlopen."""

    def __init__(self, responses: dict[str, tuple[int, bytes]]) -> None:
        self.responses = responses
        self.calls: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return b""

    def getcode(self):
        return 200


class FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class TestComfyUIClient(unittest.TestCase):
    def setUp(self):
        self.client = ComfyUIClient("http://127.0.0.1:8188")

    @mock.patch("comfyprov.generate.urllib.request.urlopen")
    def test_queue_prompt_returns_id(self, mock_urlopen):
        mock_urlopen.return_value = FakeResponse(json.dumps({"prompt_id": "abc-123"}).encode())
        prompt_id = self.client.queue_prompt({"1": {"inputs": {}, "class_type": "X"}})
        self.assertEqual(prompt_id, "abc-123")
        called_url = mock_urlopen.call_args[0][0]
        self.assertIn("/prompt", called_url.full_url if hasattr(called_url, "full_url") else str(called_url))

    @mock.patch("comfyprov.generate.urllib.request.urlopen")
    def test_wait_for_completion_polls(self, mock_urlopen):
        history = {
            "abc-123": {"outputs": {"7": {"images": [{"filename": "img.png", "subfolder": "", "type": "output"}]}}}
        }
        # First call: not ready. Second call: ready.
        responses = iter(
            [
                FakeResponse(b"{}"),
                FakeResponse(json.dumps(history).encode()),
            ]
        )
        mock_urlopen.side_effect = lambda *a, **k: next(responses)

        outputs = self.client.wait_for_completion("abc-123", timeout_s=10)
        self.assertIn("7", outputs)

    @mock.patch("comfyprov.generate.urllib.request.urlopen")
    def test_download_image(self, mock_urlopen):
        mock_urlopen.return_value = FakeResponse(b"PNGDATA")
        data = self.client.download_image("img.png", "", "output")
        self.assertEqual(data, b"PNGDATA")


class TestRunGeneration(unittest.TestCase):
    def test_run_generation_downloads(self):
        settings = GenerationSettings(
            model="model.safetensors",
            clip_model="clip.safetensors",
            vae_model="vae.safetensors",
            width=512,
            height=512,
            count=1,
            steps=4,
            cfg=1.0,
            sampler="euler",
            scheduler="simple",
            seed=1,
            prompt="hi",
            neg_prompt="",
            use_tiled_vae=False,
            tile_size=512,
        )

        fake = mock.Mock(spec=ComfyUIClient)
        fake.queue_prompt.return_value = "p1"
        fake.wait_for_completion.return_value = {
            "7": {
                "images": [
                    {
                        "filename": "0001.png",
                        "subfolder": "",
                        "type": "output",
                    }
                ]
            }
        }
        fake.download_image.return_value = png_bytes(32, 32, gradient=True)

        with tempfile.TemporaryDirectory() as tmp:
            saved = run_generation(fake, settings, tmp)
            self.assertEqual(len(saved), 1)
            out_file = Path(tmp) / "0001.png"
            self.assertTrue(out_file.is_file())
            self.assertEqual(out_file.read_bytes(), fake.download_image.return_value)

    def test_blank_image_is_an_error(self) -> None:
        """A black 512x512 PNG is a valid file and worthless output."""
        settings = GenerationSettings(
            model="model.safetensors",
            clip_model="clip.safetensors",
            vae_model="vae.safetensors",
            width=512,
            height=512,
            count=1,
            steps=4,
            cfg=1.0,
            sampler="euler",
            scheduler="simple",
            seed=1,
            prompt="hi",
            neg_prompt="",
            use_tiled_vae=False,
            tile_size=512,
        )
        fake = mock.Mock(spec=ComfyUIClient)
        fake.queue_prompt.return_value = "p1"
        fake.wait_for_completion.return_value = {
            "7": {"images": [{"filename": "blank.png", "subfolder": "", "type": "output"}]}
        }
        fake.download_image.return_value = png_bytes(512, 512, level=0)

        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(RuntimeError) as caught:
            run_generation(fake, settings, tmp)
        self.assertIn("blank", str(caught.exception))
        self.assertIn("blank.png", str(caught.exception))


# ---------------------------------------------------------------------------
# Interactive input (stdin monkeypatch)
# ---------------------------------------------------------------------------


class TestInteractive(unittest.TestCase):
    def test_interactive_defaults(self):
        inputs = iter(
            [
                "1",  # select model #1
                "1",  # select CLIP model #1
                "1",  # select VAE model #1
                "512x512",  # size
                "2",  # count
                "4",  # steps
                "1.0",  # cfg
                "1",  # sampler -> euler
                "1",  # scheduler -> simple
                "42",  # seed
                "a cat",  # prompt
                "",  # neg prompt
            ]
        )
        with mock.patch("builtins.input", lambda _: next(inputs)):
            settings = interactive(
                available_models=["model.safetensors"],
                available_clip_models=["clip.safetensors"],
                available_vae_models=["vae.safetensors"],
            )
        self.assertEqual(settings.model, "model.safetensors")
        self.assertEqual(settings.clip_model, "clip.safetensors")
        self.assertEqual(settings.vae_model, "vae.safetensors")
        self.assertEqual(settings.width, 512)
        self.assertEqual(settings.height, 512)
        self.assertEqual(settings.count, 2)
        self.assertEqual(settings.steps, 4)
        self.assertEqual(settings.cfg, 1.0)
        self.assertEqual(settings.sampler, "euler")
        self.assertEqual(settings.scheduler, "simple")
        self.assertEqual(settings.seed, 42)
        self.assertEqual(settings.prompt, "a cat")
        self.assertEqual(settings.neg_prompt, "")
        self.assertEqual(settings.use_tiled_vae, True)
        self.assertEqual(settings.tile_size, 512)

    def test_interactive_empty_prompt_retries_until_nonempty(self):
        inputs = iter(
            [
                "1",  # select model #1
                "1",  # select CLIP model #1
                "1",  # select VAE model #1
                "512x512",
                "1",
                "4",
                "1.0",
                "euler",
                "simple",
                "1",
                "",  # empty prompt -> must retry
                "  ",  # whitespace-only prompt -> must retry
                "a cat",  # valid prompt
                "",  # neg prompt
            ]
        )
        with mock.patch("builtins.input", lambda _: next(inputs)):
            settings = interactive(
                available_models=["model.safetensors"],
                available_clip_models=["clip.safetensors"],
                available_vae_models=["vae.safetensors"],
            )
        self.assertEqual(settings.prompt, "a cat")
        self.assertEqual(settings.neg_prompt, "")


if __name__ == "__main__":
    unittest.main()
