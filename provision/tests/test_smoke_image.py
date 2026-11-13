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

from comfyprov.smoke import check_image, png_extrema, validate_outputs


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


if __name__ == "__main__":
    unittest.main()
