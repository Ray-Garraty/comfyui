"""End-to-end test for generate.sh.

Pipes all interactive answers into generate.sh and verifies that images are
produced in the output directory. Requires ComfyUI running and model files
present on disk — not a unit test, run manually before releases.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

INSTALL_DIR = Path(__file__).resolve().parents[2]
GENERATE_SH = INSTALL_DIR / "generate.sh"
OUTPUT_DIR = INSTALL_DIR / "output"


@pytest.mark.e2e
def test_generate_e2e_defaults() -> None:
    """Full generation with default settings and a simple prompt."""
    if not GENERATE_SH.is_file():
        pytest.skip(f"{GENERATE_SH} not found")

    # Pipe answers for all interactive prompts:
    #   1        -> select model #1
    #   1        -> select CLIP model #1
    #   1        -> select VAE model #1
    #   512x512  -> image size (small for fast test)
    #   1        -> count
    #   4        -> steps
    #   1.0      -> cfg
    #   1        -> sampler #1 (euler)
    #   1        -> scheduler #1 (simple)
    #   -1       -> seed (random)
    #   a cute cat -> prompt
    #            -> negative prompt (empty)
    stdin_input = (
        "\n".join(
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
                "-1",
                "a cute cat",
                "",
            ]
        )
        + "\n"
    )

    # Clean output dir before test
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    before = set(OUTPUT_DIR.iterdir())

    env = os.environ.copy()
    env["PYTHONPATH"] = str(INSTALL_DIR / "provision" / "lib") + os.pathsep + str(INSTALL_DIR / "comfy_kitchen_config")

    result = subprocess.run(
        ["bash", str(GENERATE_SH)],
        input=stdin_input,
        text=True,
        capture_output=True,
        timeout=600,
        env=env,
    )

    # Print output for debugging on failure
    if result.returncode != 0:
        print("STDOUT:", result.stdout)
        print("STDERR:", result.stderr)

    assert result.returncode == 0, f"generate.sh failed: {result.stderr}"

    after = set(OUTPUT_DIR.iterdir())
    new_files = after - before

    # At least one new image file should appear
    image_files = [f for f in new_files if f.suffix in (".png", ".jpg", ".jpeg", ".webp")]
    assert len(image_files) >= 1, f"No new images found in {OUTPUT_DIR}"

    # Clean up generated files
    for f in image_files:
        f.unlink(missing_ok=True)
