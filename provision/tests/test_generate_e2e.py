"""End-to-end test for generate.sh.

Runs the real script against a real ComfyUI and a real model, then validates
what landed on disk. The test is written so that it cannot hide the bugs it is
supposed to catch:

* it does **not** inject ``PYTHONPATH`` — building the environment is
  ``generate.sh``'s job, and injecting ``comfy_kitchen_config`` here used to
  hide the fact that a server started by the script ran without
  ``sitecustomize`` (int8 models then failed with HIPBLAS_STATUS_INVALID_VALUE);
* it validates the produced image with ``comfyprov.smoke.check_image``, which
  rejects blank frames — a 512x512 PNG of pure black is a valid file, and
  accepting it cements broken output;
* on failure it prints the tail of the generator log and the launcher log, so a
  red run explains itself instead of only failing an assertion.

Not a unit test: it needs the models and a working GPU. Run with
``./provision.sh verify --full`` or ``pytest -m e2e``.

Business requirement: this test is mandatory for every commit. It must never
skip — not for a missing script, a missing model, a dead GPU or any other
reason. A skip hides exactly the breakage the test exists to catch, so every
unmet precondition is turned into a failure below and in ``provision.sh test``.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from comfyprov.smoke import validate_outputs

INSTALL_DIR = Path(__file__).resolve().parents[2]
GENERATE_SH = INSTALL_DIR / "generate.sh"
OUTPUT_DIR = INSTALL_DIR / "output"
LOG_DIR = INSTALL_DIR / "logs"


def _tail(path: Path, lines: int = 40) -> str:
    try:
        content = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return f"<{path}: unreadable>"
    return "\n".join(content[-lines:])


def _diagnostics() -> str:
    return (
        f"\n--- {LOG_DIR / 'generate.log'} ---\n{_tail(LOG_DIR / 'generate.log')}\n"
        f"\n--- {LOG_DIR / 'comfyui-start.log'} ---\n{_tail(LOG_DIR / 'comfyui-start.log')}\n"
    )


# Interactive answers, in prompt order: model, CLIP, VAE, size, count, steps,
# cfg, sampler, scheduler, seed, prompt, negative prompt.
ANSWERS = "\n".join(["1", "1", "1", "512x512", "1", "4", "1.0", "1", "1", "-1", "a cute cat", ""]) + "\n"


@pytest.mark.e2e
def test_generate_sh_produces_a_non_blank_image() -> None:
    # A skip would let the commit through with nothing tested, so a missing
    # script is a failure. There is no option and no excuse to skip e2e.
    if not GENERATE_SH.is_file():
        pytest.fail(f"{GENERATE_SH} not found; e2e tests cannot be skipped")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    before = set(OUTPUT_DIR.iterdir())

    env = os.environ.copy()
    # The script must establish this itself; see the module docstring.
    env.pop("PYTHONPATH", None)

    result = subprocess.run(
        ["bash", str(GENERATE_SH)],
        input=ANSWERS,
        text=True,
        capture_output=True,
        timeout=900,
        env=env,
    )

    if result.returncode != 0:
        pytest.fail(
            f"generate.sh exited {result.returncode}\n"
            f"stdout:\n{result.stdout[-2000:]}\nstderr:\n{result.stderr[-2000:]}{_diagnostics()}"
        )

    created = [path for path in set(OUTPUT_DIR.iterdir()) - before if path.suffix == ".png"]
    assert created, f"no PNG was created in {OUTPUT_DIR}{_diagnostics()}"

    problems = validate_outputs([str(image) for image in created])
    assert not problems, "generator produced unusable images:\n  " + "\n  ".join(problems) + _diagnostics()

    # The int8 patches only work when the launcher exports comfy_kitchen_config;
    # without them the run fails on hipblasLt, so this is the behavioural proof
    # that the environment was built by the scripts, not by the test.
    assert "[sitecustomize]" in _tail(LOG_DIR / "comfyui-start.log", 200), (
        f"the server ran without sitecustomize (int8 patches inactive){_diagnostics()}"
    )

    print(f"generated: {sorted(path.name for path in created)}")
