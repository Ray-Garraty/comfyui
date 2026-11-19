"""Tests for the single-source configuration parser."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from comfyprov import config


class ParseEnvTests(unittest.TestCase):
    def test_defaults_and_expansion(self) -> None:
        text = '# comment\n: "${BASE:=/srv/app}"\n: "${DATA:=$BASE/data}"\n: "${PORT:=8188}"\n'
        values = config.parse_env(text, environ={})
        self.assertEqual(values["BASE"], "/srv/app")
        self.assertEqual(values["DATA"], "/srv/app/data")
        self.assertEqual(values["PORT"], "8188")

    def test_environment_wins_over_defaults(self) -> None:
        text = ': "${PORT:=8188}"\ndirect=value\n'
        values = config.parse_env(text, environ={"PORT": "9999"})
        self.assertEqual(values["PORT"], "9999")
        self.assertEqual(values["direct"], "value")

    def test_plain_assignment_is_supported(self) -> None:
        values = config.parse_env("A=1\nB=$A/2\n", environ={})
        self.assertEqual(values, {"A": "1", "B": "1/2"})

    def test_unparsable_line_is_reported_with_number(self) -> None:
        with self.assertRaises(config.ConfigError) as caught:
            config.parse_env("good=1\nthis is not an assignment\n")
        self.assertIn("line 2", str(caught.exception))

    def test_undefined_variable_is_an_error(self) -> None:
        with self.assertRaises(config.ConfigError):
            config.parse_env("X=$NOPE/1\n", environ={})


class RealConfigTests(unittest.TestCase):
    def test_repository_config_is_valid(self) -> None:
        cfg = config.load()
        self.assertTrue(str(cfg.install_dir).startswith("/"))
        self.assertTrue(cfg.gpu_gfx == "auto" or cfg.gpu_gfx.startswith("gfx"))
        self.assertTrue(cfg.torch_tag.startswith(cfg.torch_version))
        self.assertTrue(str(cfg.models_dir).startswith(str(cfg.big_mount) + "/"))

    def test_big_device_expands_the_uuid(self) -> None:
        cfg = config.load()
        self.assertEqual(cfg.big_device, f"/dev/disk/by-uuid/{cfg.big_uuid}")

    def test_environment_overrides_the_file(self) -> None:
        cfg = config.load(environ={"COMFYUI_PORT": "9000", "BIG_MOUNT": "/mnt/big", "MODELS_DIR": "/mnt/big/models"})
        self.assertEqual(cfg.port, 9000)
        self.assertEqual(str(cfg.models_dir), "/mnt/big/models")

    def test_invalid_port_is_rejected(self) -> None:
        with self.assertRaises(config.ConfigError):
            config.load(environ={"COMFYUI_PORT": "99999"})

    def test_relative_path_is_rejected(self) -> None:
        with self.assertRaises(config.ConfigError):
            config.load(environ={"INSTALL_DIR": "relative/path"})

    def test_models_must_live_on_big(self) -> None:
        with self.assertRaises(config.ConfigError):
            config.load(environ={"BIG_MOUNT": "/mnt/big", "MODELS_DIR": "/var/models"})

    def test_missing_setting_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "provision.env"
            path.write_text(': "${INSTALL_DIR:=/srv/app}"\n', encoding="utf-8")
            with self.assertRaises(config.ConfigError) as caught:
                config.load(path, environ={})
            self.assertIn("missing settings", str(caught.exception))

    def test_invalid_gfx_value_is_rejected(self) -> None:
        with self.assertRaises(config.ConfigError):
            config.load(environ={"GPU_GFX": "navi22"})

    def test_localhost_only(self) -> None:
        with self.assertRaises(config.ConfigError):
            config.load(environ={"COMFYUI_LISTEN": "0.0.0.0"})

    def test_prompt_metadata_is_disabled_by_default(self) -> None:
        self.assertTrue(config.load().comfyui_disable_metadata)
        self.assertFalse(config.load(environ={"COMFYUI_DISABLE_METADATA": "0"}).comfyui_disable_metadata)


if __name__ == "__main__":
    unittest.main()
