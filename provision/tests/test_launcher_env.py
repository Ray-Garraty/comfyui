"""The launcher must put ``comfy_kitchen_config`` on ``PYTHONPATH``.

``comfy_kitchen_config/sitecustomize.py`` is what keeps int8 models usable on
RDNA2 (gfx1031 has no matrix cores, so hipblasLt answers
``HIPBLAS_STATUS_INVALID_VALUE``):

* it patches ``torch._int_mm`` to fall back to a bf16 matmul;
* it filters comfy-kitchen's int8 linear handler.

Python imports ``sitecustomize`` only when its directory is on ``sys.path``,
i.e. when ``PYTHONPATH`` contains ``comfy_kitchen_config``. If the server is
started without it, everything looks healthy until the first int8 matmul:

    RuntimeError: CUDA error: HIPBLAS_STATUS_INVALID_VALUE when calling
    `hipblasLtMatmulAlgoGetHeuristic(...)`

Those failures are silent for the machine (no crash) but produce no image, so
the contract is pinned here twice: the launcher exports the path, and
``generate.sh`` exports it before it starts the server (the child inherits the
environment, not the other way round).
"""

from __future__ import annotations

import os
import subprocess
import unittest
from pathlib import Path

INSTALL_DIR = Path(__file__).resolve().parents[2]
START_SH = INSTALL_DIR / "start-comfyui.sh"
GENERATE_SH = INSTALL_DIR / "generate.sh"


def _export_line(text: str) -> str:
    for line in text.splitlines():
        if "export PYTHONPATH=" in line:
            return line
    return ""


class LauncherPythonPathTests(unittest.TestCase):
    def test_start_script_exports_the_kitchen_config_directory(self) -> None:
        line = _export_line(START_SH.read_text(encoding="utf-8"))
        self.assertTrue(line, "start-comfyui.sh must export PYTHONPATH")
        self.assertIn("comfy_kitchen_config", line)

    def test_dry_run_reports_the_exported_pythonpath(self) -> None:
        result = subprocess.run(
            [str(START_SH), "--dry-run", "--no-browser", "--no-mount"],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr[-1500:])
        self.assertIn("PYTHONPATH=", result.stdout)
        self.assertIn("comfy_kitchen_config", result.stdout)

    def test_generate_exports_the_path_before_it_starts_the_server(self) -> None:
        text = GENERATE_SH.read_text(encoding="utf-8")
        export_at = text.find("export PYTHONPATH=")
        start_at = text.find('start-comfyui.sh" --no-browser')
        self.assertNotEqual(export_at, -1, "generate.sh must export PYTHONPATH")
        self.assertNotEqual(start_at, -1, "generate.sh must start the server through the launcher")
        self.assertLess(
            export_at,
            start_at,
            "PYTHONPATH must be exported before the server starts, otherwise the child "
            "process runs without sitecustomize and int8 models fail on hipblasLt",
        )


class LauncherPromptMetadataTests(unittest.TestCase):
    """Prompts are private: no generated file may carry them.

    ComfyUI writes the API prompt and the whole workflow into PNG tEXt chunks
    unless ``--disable-metadata`` is passed. The launcher must pass it, and the
    temporary server started by ``verify`` must pass it too.
    """

    def _dry_run(self, **overrides: str) -> subprocess.CompletedProcess[str]:
        env = {key: value for key, value in os.environ.items() if key != "COMFYUI_DISABLE_METADATA"}
        env.update(overrides)
        return subprocess.run(
            [str(START_SH), "--dry-run", "--no-browser", "--no-mount"],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
            env=env,
        )

    def test_launcher_passes_disable_metadata_by_default(self) -> None:
        result = self._dry_run()
        self.assertEqual(result.returncode, 0, result.stderr[-1500:])
        self.assertIn("--disable-metadata", result.stdout)
        self.assertIn("Metadata:   disabled", result.stdout)

    def test_metadata_can_still_be_enabled_explicitly(self) -> None:
        result = self._dry_run(COMFYUI_DISABLE_METADATA="0")
        self.assertEqual(result.returncode, 0, result.stderr[-1500:])
        self.assertNotIn("--disable-metadata", result.stdout)
        self.assertNotIn("Metadata:   disabled", result.stdout)

    def test_verify_starts_its_temporary_server_without_metadata(self) -> None:
        text = (INSTALL_DIR / "provision.sh").read_text(encoding="utf-8")
        self.assertIn("COMFYUI_DISABLE_METADATA", text)
        self.assertIn("--disable-metadata", text)
        self.assertIn('"${metadata_args[@]}"', text, "the verify server must receive the flag")


if __name__ == "__main__":
    unittest.main()
