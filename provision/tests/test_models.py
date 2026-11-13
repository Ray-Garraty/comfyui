"""Tests for the model manifest and file verification."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from comfyprov import models

MANIFEST = (
    """# key\tsource\ttarget\tfilename\tsize_bytes\tsha256\turl
te\thf\ttext_encoders\tte.safetensors\t10\t"""
    + "a" * 64
    + """\thttps://example.com/te
model\tcivitai\tdiffusion_models\tmodel.safetensors\t20\t"""
    + "b" * 64
    + """\thttps://example.com/model
"""
)


class ManifestTests(unittest.TestCase):
    def test_real_manifest_is_consistent(self) -> None:
        manifest = models.load_manifest()
        self.assertIn("jibmix_v2", manifest)
        self.assertEqual(manifest["jibmix_v2"].target, "diffusion_models")
        for entry in manifest.values():
            self.assertGreater(entry.size, 0)
            self.assertEqual(len(entry.sha256), 64)
            self.assertIn(entry.source, ("hf", "civitai"))

    def test_parse_valid_manifest(self) -> None:
        parsed = models.parse_manifest(MANIFEST)
        self.assertEqual(set(parsed), {"te", "model"})
        self.assertTrue(parsed["model"].requires_token)
        self.assertFalse(parsed["te"].requires_token)

    def test_duplicate_key_is_rejected(self) -> None:
        doubled = MANIFEST + MANIFEST.splitlines()[1] + "\n"
        with self.assertRaises(models.ManifestError) as caught:
            models.parse_manifest(doubled)
        self.assertIn("duplicate key", str(caught.exception))

    def test_bad_column_count_is_rejected(self) -> None:
        with self.assertRaises(models.ManifestError):
            models.parse_manifest("only\tthree\tcolumns\n")

    def test_bad_checksum_is_rejected(self) -> None:
        broken = MANIFEST.replace("a" * 64, "xyz")
        with self.assertRaises(models.ManifestError):
            models.parse_manifest(broken)

    def test_negative_size_is_rejected(self) -> None:
        broken = MANIFEST.replace("\t10\t", "\t-10\t")
        with self.assertRaises(models.ManifestError):
            models.parse_manifest(broken)


class SetSelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manifest = models.load_manifest()

    def test_jibmix_set(self) -> None:
        keys = [entry.key for entry in models.select(self.manifest, "jibmix")]
        self.assertEqual(keys, ["jibmix_v2", "moss_te", "moss_vae"])

    def test_both_set_has_four_entries(self) -> None:
        self.assertEqual(len(models.select(self.manifest, "both")), 4)

    def test_unknown_set_is_rejected(self) -> None:
        with self.assertRaises(models.ManifestError):
            models.select(self.manifest, "sdxl")

    def test_check_without_set_covers_everything(self) -> None:
        self.assertEqual(len(models.entries_for_check(self.manifest, None)), len(self.manifest))


class VerificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manifest = models.parse_manifest(MANIFEST)
        self.entry = self.manifest["te"]

    def test_size_and_sha_are_checked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "te.safetensors"
            path.write_bytes(b"0123456789")
            result = models.check_entry(self.entry, path, verify_sha=True)
            self.assertFalse(result.ok)
            self.assertEqual(result.state, "sha")

    def test_size_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "te.safetensors"
            path.write_bytes(b"short")
            result = models.check_entry(self.entry, path)
            self.assertEqual(result.state, "size")

    def test_missing_file(self) -> None:
        result = models.check_entry(self.entry, Path("/nonexistent/nope"))
        self.assertEqual(result.state, "missing")

    def test_size_only_match(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "te.safetensors"
            path.write_bytes(b"0123456789")
            self.assertTrue(models.check_entry(self.entry, path).ok)

    def test_injected_size_provider(self) -> None:
        result = models.check_entry(self.entry, Path("/virtual/te"), size_of=lambda _p: 10)
        self.assertTrue(result.ok)

    def test_sha256_of_matches_hashlib(self) -> None:
        import hashlib

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "data.bin"
            payload = b"comfyui" * 1000
            path.write_bytes(payload)
            self.assertEqual(models.sha256_of(path), hashlib.sha256(payload).hexdigest())


if __name__ == "__main__":
    unittest.main()
