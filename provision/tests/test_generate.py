"""Unit tests for comfyprov.generate.

Never touches the real host, network, or sudo. Uses fake filesystem
(temporary files) and mocked urllib.request.
"""

from __future__ import annotations

import io
import json
import os
import struct
import tempfile
import unittest
from pathlib import Path
from typing import Any, ClassVar
from unittest import mock

from helpers import png_bytes

from comfyprov.generate import (
    ComfyUIClient,
    GeneratedImage,
    GenerationSettings,
    LogTee,
    _ask_float,
    _ask_int,
    _ask_yn,
    _extract_float,
    _extract_int,
    _extract_str,
    _parse_png_parameters,
    _report_progress,
    _select_from_list,
    _settings_summary,
    build_workflow,
    discover_models,
    import_metadata,
    interactive,
    interactive_round,
    main,
    register_private_text,
    run_generation,
    write_private,
    write_split,
)
from comfyprov.smoke import png_text_chunks

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

    def test_downloaded_prompt_metadata_is_stripped(self) -> None:
        """A server started without --disable-metadata must not leak a prompt."""
        settings = _settings(prompt="a private cat")
        fake = mock.Mock(spec=ComfyUIClient)
        fake.queue_prompt.return_value = "p1"
        fake.wait_for_completion.return_value = {
            "7": {"images": [{"filename": "0001.png", "subfolder": "", "type": "output"}]}
        }
        fake.download_image.return_value = png_bytes(
            64,
            64,
            gradient=True,
            text={"prompt": '{"4": {"inputs": {"text": "a private cat"}}}', "workflow": "{}"},
        )

        with tempfile.TemporaryDirectory() as tmp:
            run_generation(fake, settings, tmp)
            data = (Path(tmp) / "0001.png").read_bytes()

        self.assertEqual(png_text_chunks(data), [])
        self.assertNotIn(b"private cat", data)
        # Only the metadata changed: the pixels are identical to a clean PNG.
        self.assertEqual(data, png_bytes(64, 64, gradient=True))

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
        self.assertFalse(settings.random_seed)
        self.assertEqual(settings.prompt, "a cat")
        self.assertEqual(settings.neg_prompt, "")
        self.assertEqual(settings.use_tiled_vae, True)
        self.assertEqual(settings.tile_size, 512)

    def test_interactive_random_seed_is_flagged_and_drawn(self):
        inputs = iter(
            [
                "1",
                "1",
                "1",
                "512x512",
                "1",
                "4",
                "1.0",
                "1",
                "1",
                "-1",  # random seed
                "a cat",
                "",
            ]
        )
        with (
            mock.patch("builtins.input", lambda _: next(inputs)),
            mock.patch("random.randint", return_value=555) as draw,
        ):
            settings = interactive(
                available_models=["model.safetensors"],
                available_clip_models=["clip.safetensors"],
                available_vae_models=["vae.safetensors"],
            )
        draw.assert_called_once_with(0, 18446744073709551615)
        self.assertTrue(settings.random_seed)
        self.assertEqual(settings.seed, 555)

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
                "1",
                "1",
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


# ---------------------------------------------------------------------------
# Further rounds: same settings, only count and prompts are asked
# ---------------------------------------------------------------------------


def _settings(**overrides: Any) -> GenerationSettings:
    values: dict[str, Any] = {
        "model": "model.safetensors",
        "width": 512,
        "height": 512,
        "count": 1,
        "steps": 4,
        "cfg": 1.0,
        "sampler": "euler",
        "scheduler": "simple",
        "seed": 7,
        "prompt": "a cat",
        "neg_prompt": "",
        "clip_model": "clip.safetensors",
        "vae_model": "vae.safetensors",
        "use_tiled_vae": False,
        "tile_size": 512,
    }
    values.update(overrides)
    return GenerationSettings(**values)


class TestAskYn(unittest.TestCase):
    def test_yes(self):
        with mock.patch("builtins.input", return_value="y"):
            self.assertTrue(_ask_yn("Generate more images?"))
        with mock.patch("builtins.input", return_value="YES"):
            self.assertTrue(_ask_yn("Generate more images?"))

    def test_no(self):
        with mock.patch("builtins.input", return_value="n"):
            self.assertFalse(_ask_yn("Generate more images?"))
        with mock.patch("builtins.input", return_value="No"):
            self.assertFalse(_ask_yn("Generate more images?"))

    def test_empty_answer_uses_default(self):
        with mock.patch("builtins.input", return_value=""):
            self.assertFalse(_ask_yn("Generate more images?", default=False))
        with mock.patch("builtins.input", return_value=""):
            self.assertTrue(_ask_yn("Generate more images?", default=True))

    def test_invalid_answer_is_asked_again(self):
        with mock.patch("builtins.input", side_effect=["maybe", "y"]) as ask:
            self.assertTrue(_ask_yn("Generate more images?"))
        self.assertEqual(ask.call_count, 2)

    def test_end_of_input_means_the_default(self):
        """The e2e test pipes its answers and closes stdin: no traceback allowed."""
        with mock.patch("builtins.input", side_effect=EOFError):
            self.assertFalse(_ask_yn("Generate more images?", default=False))


class TestInteractiveRound(unittest.TestCase):
    def test_enter_repeats_the_previous_round(self):
        previous = _settings(count=2, prompt="a cat", neg_prompt="blurry", seed=42)
        answers = iter(["", "", ""])  # count, positive prompt, negative prompt
        with mock.patch("builtins.input", lambda _: next(answers)):
            result = interactive_round(previous)
        self.assertEqual(result, previous)

    def test_asks_only_count_and_prompts(self):
        previous = _settings(count=2, steps=9, cfg=3.5, width=640, height=480, sampler="heun", scheduler="karras")
        answers = iter(["3", "a dog", "ugly"])
        with mock.patch("builtins.input", lambda _: next(answers)):
            result = interactive_round(previous)
        self.assertEqual(result.count, 3)
        self.assertEqual(result.prompt, "a dog")
        self.assertEqual(result.neg_prompt, "ugly")
        self.assertEqual(
            (result.model, result.width, result.height, result.steps, result.cfg, result.sampler, result.scheduler),
            (previous.model, 640, 480, 9, 3.5, "heun", "karras"),
        )

    def test_fixed_seed_is_kept(self):
        previous = _settings(seed=1234)
        answers = iter(["", "", ""])
        with (
            mock.patch("builtins.input", lambda _: next(answers)),
            mock.patch("random.randint") as draw,
        ):
            result = interactive_round(previous)
        draw.assert_not_called()
        self.assertEqual(result.seed, 1234)

    def test_random_seed_is_drawn_again(self):
        previous = _settings(seed=1234, random_seed=True)
        answers = iter(["", "", ""])
        with (
            mock.patch("builtins.input", lambda _: next(answers)),
            mock.patch("random.randint", return_value=999) as draw,
        ):
            result = interactive_round(previous)
        draw.assert_called_once_with(0, 18446744073709551615)
        self.assertEqual(result.seed, 999)
        self.assertTrue(result.random_seed)

    def test_previous_prompts_are_shown_but_never_logged(self):
        """Prompts are user content: the log file must not receive them."""
        terminal, log = io.StringIO(), io.StringIO()
        tee = LogTee(terminal, log)
        previous = _settings(prompt="secret positive", neg_prompt="secret negative")
        answers = iter(["", "", ""])
        with mock.patch("sys.stdout", tee), mock.patch("builtins.input", lambda _: next(answers)):
            result = interactive_round(previous)
        self.assertEqual(result.prompt, "secret positive")
        self.assertIn("secret positive", terminal.getvalue())
        self.assertIn("secret negative", terminal.getvalue())
        self.assertNotIn("secret", log.getvalue())


class TestProgressOutput(unittest.TestCase):
    def test_progress_ticks_are_terminal_only(self) -> None:
        terminal, log = io.StringIO(), io.StringIO()
        tee = LogTee(terminal, log)
        with mock.patch("sys.stdout", tee):
            _report_progress(46)
            _report_progress(47)
            write_private("\n")
        self.assertIn("47s elapsed", terminal.getvalue())
        self.assertEqual(log.getvalue(), "", "the once-per-second repaint must not be logged")

    def test_private_output_without_a_tee_still_reaches_stdout(self) -> None:
        stream = io.StringIO()
        with mock.patch("sys.stdout", stream):
            write_private("plain\n")
        self.assertEqual(stream.getvalue(), "plain\n")


# ---------------------------------------------------------------------------
# Prompt privacy: logs must not persist prompts or mistyped input
# ---------------------------------------------------------------------------


class TestLogRedaction(unittest.TestCase):
    SECRET = "a private photo of a grey cat"

    def _tee(self) -> tuple[LogTee, io.StringIO, io.StringIO]:
        terminal, log = io.StringIO(), io.StringIO()
        return LogTee(terminal, log), terminal, log

    def test_registered_prompts_are_redacted_from_the_log(self) -> None:
        tee, terminal, log = self._tee()
        tee.add_sensitive(self.SECRET)
        with mock.patch("sys.stdout", tee):
            print(f"queueing: {self.SECRET}")
        self.assertIn(self.SECRET, terminal.getvalue())
        self.assertNotIn("grey cat", log.getvalue())
        self.assertIn("[redacted]", log.getvalue())

    def test_redaction_also_covers_tracebacks_and_multiline_text(self) -> None:
        tee, _terminal, log = self._tee()
        tee.add_sensitive(self.SECRET)
        with mock.patch("sys.stdout", tee):
            print(f"Traceback:\n  RuntimeError: could not queue {self.SECRET}\n")
        self.assertNotIn("grey cat", log.getvalue())

    def test_the_longest_secret_is_replaced_first(self) -> None:
        tee, _terminal, log = self._tee()
        tee.add_sensitive("private", "a private cat")
        with mock.patch("sys.stdout", tee):
            print("a private cat")
        self.assertIn("[redacted]", log.getvalue())
        self.assertNotIn("private cat", log.getvalue())

    def test_blank_secrets_are_ignored(self) -> None:
        tee, _terminal, log = self._tee()
        tee.add_sensitive("", "   ")
        with mock.patch("sys.stdout", tee):
            print("nothing secret")
        self.assertIn("nothing secret", log.getvalue())

    def test_write_split_keeps_user_input_out_of_the_log(self) -> None:
        tee, terminal, log = self._tee()
        with mock.patch("sys.stdout", tee):
            write_split("Invalid value 'a cat', using 1\n", "Invalid value, using 1\n")
        self.assertIn("a cat", terminal.getvalue())
        self.assertIn("Invalid value, using 1", log.getvalue())
        self.assertNotIn("'a cat'", log.getvalue())

    def test_write_split_without_a_tee_still_shows_the_text(self) -> None:
        stream = io.StringIO()
        with mock.patch("sys.stdout", stream):
            write_split("raw\n", "safe\n")
        self.assertEqual(stream.getvalue(), "raw\n")

    def test_register_private_text_without_a_tee_is_harmless(self) -> None:
        stream = io.StringIO()
        with mock.patch("sys.stdout", stream):
            register_private_text("a cat")
        self.assertEqual(stream.getvalue(), "")


class TestMistypedInputPrivacy(unittest.TestCase):
    """A prompt typed into the wrong field is user data too."""

    def _tee(self) -> tuple[LogTee, io.StringIO, io.StringIO]:
        terminal, log = io.StringIO(), io.StringIO()
        return LogTee(terminal, log), terminal, log

    def test_prompt_typed_instead_of_the_image_count_is_not_logged(self) -> None:
        tee, terminal, log = self._tee()
        answers = iter(["1", "1", "1", "512x512", "a private cat", "4", "1.0", "1", "1", "7", "a dog", ""])
        with mock.patch("sys.stdout", tee), mock.patch("builtins.input", lambda _: next(answers)):
            settings = interactive(
                available_models=["model.safetensors"],
                available_clip_models=["clip.safetensors"],
                available_vae_models=["vae.safetensors"],
            )
        self.assertEqual(settings.count, 1)
        self.assertIn("a private cat", terminal.getvalue())
        self.assertNotIn("private cat", log.getvalue())
        self.assertNotIn("a dog", log.getvalue(), "the real prompt must stay out as well")

    def test_prompt_typed_instead_of_the_image_size_is_not_logged(self) -> None:
        tee, _terminal, log = self._tee()
        answers = iter(["1", "1", "1", "a private cat", "1", "4", "1.0", "1", "1", "7", "a dog", ""])
        with mock.patch("sys.stdout", tee), mock.patch("builtins.input", lambda _: next(answers)):
            settings = interactive(
                available_models=["model.safetensors"],
                available_clip_models=["clip.safetensors"],
                available_vae_models=["vae.safetensors"],
            )
        self.assertEqual((settings.width, settings.height), (1024, 1024))
        self.assertNotIn("private cat", log.getvalue())

    def test_prompt_typed_instead_of_a_list_number_is_not_logged(self) -> None:
        tee, terminal, log = self._tee()
        answers = iter(["a private cat", "1"])
        with mock.patch("sys.stdout", tee), mock.patch("builtins.input", lambda _: next(answers)):
            selected = _select_from_list("Models", ["model.safetensors"])
        self.assertEqual(selected, "model.safetensors")
        self.assertIn("a private cat", terminal.getvalue())
        self.assertNotIn("private cat", log.getvalue())

    def test_prompt_typed_instead_of_yes_no_is_not_logged(self) -> None:
        tee, _terminal, log = self._tee()
        answers = iter(["a private cat", "n"])
        with mock.patch("sys.stdout", tee), mock.patch("builtins.input", lambda _: next(answers)):
            self.assertFalse(_ask_yn("Generate more images?"))
        self.assertNotIn("private cat", log.getvalue())

    def test_invalid_numbers_and_floats_are_logged_without_the_value(self) -> None:
        tee, _terminal, log = self._tee()
        with mock.patch("sys.stdout", tee), mock.patch("builtins.input", return_value="a private cat"):
            self.assertEqual(_ask_int("Count", 3), 3)
            self.assertEqual(_ask_float("CFG", 1.0), 1.0)
        self.assertNotIn("private cat", log.getvalue())
        self.assertEqual(log.getvalue().count("Invalid value, using"), 2)

    def test_the_settings_summary_never_contains_a_prompt(self) -> None:
        summary = _settings_summary(_settings(prompt="a private cat", neg_prompt="ugly private dog"))
        self.assertNotIn("private", summary)
        self.assertIn("model.safetensors", summary)

    def test_interactive_round_registers_the_new_prompts_for_redaction(self) -> None:
        tee, _terminal, log = self._tee()
        previous = _settings(prompt="old private cat")
        answers = iter(["", "new private dog", ""])
        with mock.patch("sys.stdout", tee), mock.patch("builtins.input", lambda _: next(answers)):
            interactive_round(previous)
            print("error while queueing new private dog")
        self.assertNotIn("private dog", log.getvalue())
        self.assertIn("[redacted]", log.getvalue())


class TestNoFreeFormFileNames(unittest.TestCase):
    """A typed sentence must never become a model name sent to the server.

    ComfyUI answers ``value_not_in_list`` for an unknown checkpoint and logs the
    received value, which would persist the prompt in ``comfyui.log``.
    """

    def test_empty_selection_list_is_an_error(self) -> None:
        with self.assertRaises(ValueError):
            _select_from_list("Available generative models on disk", [])

    def test_empty_model_directory_fails_before_reading_input(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch("builtins.input") as ask:
            rc = main(["http://127.0.0.1:8188", tmp, tmp, "", ""])
        self.assertEqual(rc, 1)
        ask.assert_not_called()


class TestMainRounds(unittest.TestCase):
    ARGV: ClassVar[list[str]] = ["http://127.0.0.1:8188", "/tmp/out", "", "", ""]

    def test_declining_another_round_runs_once(self) -> None:
        first = _settings()
        produced = [GeneratedImage(path="/tmp/out/0001.png", filename="0001.png")]
        with (
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch("comfyprov.generate.interactive", return_value=first),
            mock.patch("comfyprov.generate.run_generation", return_value=produced) as run,
            mock.patch("comfyprov.generate._ask_yn", return_value=False) as ask_more,
        ):
            self.assertEqual(main(self.ARGV), 0)
        self.assertEqual(run.call_count, 1)
        ask_more.assert_called_once()

    def test_another_round_reuses_settings_and_runs_again(self) -> None:
        first = _settings(count=1, prompt="a cat", neg_prompt="blurry")
        second = _settings(count=2, prompt="a dog", neg_prompt="ugly")
        produced = [GeneratedImage(path="/tmp/out/0001.png", filename="0001.png")]
        with (
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch("comfyprov.generate.interactive", return_value=first),
            mock.patch("comfyprov.generate.interactive_round", return_value=second) as next_round,
            mock.patch("comfyprov.generate.run_generation", side_effect=[produced, produced]) as run,
            mock.patch("comfyprov.generate._ask_yn", side_effect=[True, False]),
        ):
            self.assertEqual(main(self.ARGV), 0)
        self.assertEqual(run.call_count, 2)
        self.assertEqual([call.args[1] for call in run.call_args_list], [first, second])
        next_round.assert_called_once_with(first)

    def test_model_from_argv_applies_to_every_round(self) -> None:
        first = _settings()
        produced = [GeneratedImage(path="/tmp/out/0001.png", filename="0001.png")]
        with (
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch("comfyprov.generate.interactive", return_value=first),
            mock.patch("comfyprov.generate.run_generation", return_value=produced) as run,
            mock.patch("comfyprov.generate._ask_yn", return_value=False),
        ):
            self.assertEqual(main([*self.ARGV, "explicit.safetensors"]), 0)
        self.assertEqual(run.call_args.args[1].model, "explicit.safetensors")

    def test_generation_error_exits_with_one(self) -> None:
        first = _settings()
        with (
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch("comfyprov.generate.interactive", return_value=first),
            mock.patch("comfyprov.generate.run_generation", side_effect=RuntimeError("boom")),
            mock.patch("comfyprov.generate._ask_yn") as ask_more,
        ):
            self.assertEqual(main(self.ARGV), 1)
        ask_more.assert_not_called()

    def test_error_messages_are_redacted_before_they_reach_the_log(self) -> None:
        """An error body can echo the workflow: the log must not keep it."""
        first = _settings(prompt="a private cat", neg_prompt="ugly private dog")
        terminal, log = io.StringIO(), io.StringIO()
        tee = LogTee(terminal, log)
        error = RuntimeError("HTTP 400: node 4 rejected text 'a private cat' / 'ugly private dog'")
        with (
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch("sys.stdout", tee),
            mock.patch("sys.stderr", tee),
            mock.patch("comfyprov.generate.interactive", return_value=first),
            mock.patch("comfyprov.generate.run_generation", side_effect=error),
            mock.patch("comfyprov.generate._ask_yn"),
        ):
            self.assertEqual(main(self.ARGV), 1)
        self.assertIn("a private cat", terminal.getvalue(), "the terminal may show the real error")
        self.assertNotIn("private cat", log.getvalue())
        self.assertNotIn("private dog", log.getvalue())
        self.assertIn("[redacted]", log.getvalue())


if __name__ == "__main__":
    unittest.main()
