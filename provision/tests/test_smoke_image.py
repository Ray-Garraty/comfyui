"""``smoke.check_image`` must reject images that carry no content.

``ComfyUI_00017_.png`` and ``ComfyUI_00018_.png`` on this machine were 512x512
files of pure black, 1979 bytes each: the workflow "succeeded", the PNG was
valid, and the old check passed it. A test that accepts such an output cements
broken behaviour, so the validator now looks at the pixels, not only at the
container.

The PNGs are built here with zlib only, so the test has no image dependency.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from helpers import png_bytes

from comfyprov.smoke import (
    check_image,
    png_extrema,
    png_text_chunks,
    strip_png_text_chunks,
    strip_text_metadata,
    validate_outputs,
)


class PngExtremaTests(unittest.TestCase):
    def test_uniform_image_has_a_zero_range(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "black.png"
            path.write_bytes(png_bytes(16, 16))
            self.assertEqual(png_extrema(str(path)), (0, 0))

    def test_gradient_image_has_a_full_range(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "gradient.png"
            path.write_bytes(png_bytes(16, 16, gradient=True))
            low, high = png_extrema(str(path)) or (-1, -1)
            self.assertGreaterEqual(high - low, 200)

    def test_unsupported_png_returns_none(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "broken.png"
            path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"nonsense")
            self.assertIsNone(png_extrema(str(path)))


class CheckImageTests(unittest.TestCase):
    def test_black_image_is_rejected(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "black.png"
            # Stored (uncompressed) blocks: a real black frame compressed to
            # ~2 KiB is rejected earlier by the size check, which would hide the
            # pixel check this test is about.
            path.write_bytes(png_bytes(512, 512, level=0))
            ok, detail = check_image(str(path))
            self.assertFalse(ok)
            self.assertIn("blank", detail)

    def test_gradient_image_is_accepted(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "gradient.png"
            path.write_bytes(png_bytes(512, 512, gradient=True))
            ok, detail = check_image(str(path))
            self.assertTrue(ok, detail)
            self.assertIn("512x512", detail)

    def test_tiny_file_is_rejected(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "small.png"
            path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 10)
            ok, _ = check_image(str(path))
            self.assertFalse(ok)


class ValidateOutputsTests(unittest.TestCase):
    """The same helper the e2e test and the generator use."""

    def test_blank_outputs_are_reported(self) -> None:
        with TemporaryDirectory() as tmp:
            good = Path(tmp) / "good.png"
            good.write_bytes(png_bytes(64, 64, gradient=True))
            blank = Path(tmp) / "blank.png"
            blank.write_bytes(png_bytes(64, 64))
            problems = validate_outputs([str(good), str(blank)])
        self.assertEqual(len(problems), 1)
        self.assertIn("blank.png", problems[0])
        self.assertIn("blank", problems[0])

    def test_clean_outputs_report_nothing(self) -> None:
        with TemporaryDirectory() as tmp:
            good = Path(tmp) / "good.png"
            good.write_bytes(png_bytes(64, 64, gradient=True))
            self.assertEqual(validate_outputs([str(good)]), [])


class PngTextChunkTests(unittest.TestCase):
    """A prompt is private: it must never sit inside a generated PNG."""

    PROMPT = "a studio photo of a secret cat"

    def test_text_chunks_are_detected(self) -> None:
        data = png_bytes(32, 32, gradient=True, text={"prompt": self.PROMPT})
        self.assertEqual(png_text_chunks(data), ["tEXt"])

    def test_plain_png_has_no_text_chunks(self) -> None:
        self.assertEqual(png_text_chunks(png_bytes(32, 32, gradient=True)), [])

    def test_non_png_is_ignored(self) -> None:
        self.assertEqual(png_text_chunks(b"not a png"), [])
        self.assertEqual(strip_png_text_chunks(b"not a png"), b"not a png")

    def test_strip_removes_prompt_and_keeps_the_pixels(self) -> None:
        data = png_bytes(
            32,
            32,
            gradient=True,
            text={"prompt": self.PROMPT, "workflow": '{"1": {}}', "parameters": self.PROMPT},
        )
        stripped = strip_png_text_chunks(data)
        self.assertEqual(png_text_chunks(stripped), [])
        self.assertNotIn(b"secret cat", stripped)
        # Nothing else changed: IHDR/IDAT/IEND are byte-identical to a clean PNG.
        self.assertEqual(stripped, png_bytes(32, 32, gradient=True))

    def test_strip_of_a_clean_png_is_a_no_op(self) -> None:
        data = png_bytes(32, 32, gradient=True)
        self.assertEqual(strip_png_text_chunks(data), data)

    def test_strip_leaves_a_truncated_file_alone(self) -> None:
        data = png_bytes(32, 32, gradient=True, text={"prompt": self.PROMPT})
        truncated = data[:16]
        self.assertEqual(strip_png_text_chunks(truncated), truncated)

    def test_validate_outputs_rejects_embedded_prompts(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "leaky.png"
            path.write_bytes(png_bytes(64, 64, gradient=True, text={"prompt": self.PROMPT}))
            problems = validate_outputs([str(path)])
        self.assertEqual(len(problems), 1)
        self.assertIn("leaky.png", problems[0])
        self.assertIn("prompt metadata", problems[0])
        self.assertNotIn(self.PROMPT, problems[0], "the report must not repeat the prompt")

    def test_strip_text_metadata_cleans_a_file_in_place(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "smoke.png"
            path.write_bytes(png_bytes(64, 64, gradient=True, text={"prompt": self.PROMPT}))
            removed = strip_text_metadata(str(path))
            self.assertEqual(removed, ["tEXt"])
            self.assertEqual(path.read_bytes(), png_bytes(64, 64, gradient=True))
            self.assertEqual(strip_text_metadata(str(path)), [], "second pass has nothing to do")

    def test_strip_text_metadata_tolerates_missing_and_broken_files(self) -> None:
        with TemporaryDirectory() as tmp:
            missing = str(Path(tmp) / "missing.png")
            self.assertEqual(strip_text_metadata(missing), [])
            broken = Path(tmp) / "broken.png"
            broken.write_bytes(b"not a png")
            self.assertEqual(strip_text_metadata(str(broken)), [])
            self.assertEqual(broken.read_bytes(), b"not a png")


if __name__ == "__main__":
    unittest.main()
