"""Interactive image generation via ComfyUI API.

Business logic for generate.sh: discover models, import generation settings
from Civitai images (PNG tEXt / JPEG EXIF), build a ComfyUI workflow, queue
it through the /prompt API, poll for completion, and download the resulting
images.

Pure-stdlib where possible (Pillow only as an optional EXIF fallback).
Designed for unit testing with fake filesystem / HTTP responses.
"""

from __future__ import annotations

import json
import os
import re
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from dataclasses import replace as dataclass_replace
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GenerationSettings:
    model: str
    width: int
    height: int
    count: int
    steps: int
    cfg: float
    sampler: str
    scheduler: str
    seed: int
    prompt: str
    neg_prompt: str
    clip_model: str
    vae_model: str


@dataclass(frozen=True)
class GeneratedImage:
    path: str
    filename: str


# ---------------------------------------------------------------------------
# Model discovery
# ---------------------------------------------------------------------------


def discover_models(diff_dir: str | os.PathLike[str]) -> list[str]:
    """Return sorted list of .safetensors filenames in ``diff_dir``."""
    p = Path(diff_dir)
    if not p.is_dir():
        return []
    return sorted(f.name for f in p.iterdir() if f.is_file() and f.suffix == ".safetensors")


def discover_clip_models(models_dir: str | os.PathLike[str]) -> list[str]:
    """Return sorted list of .safetensors filenames in text_encoders/."""
    return discover_models(Path(models_dir) / "text_encoders")


def discover_vae_models(models_dir: str | os.PathLike[str]) -> list[str]:
    """Return sorted list of .safetensors filenames in vae/."""
    return discover_models(Path(models_dir) / "vae")


# ---------------------------------------------------------------------------
# Metadata import from Civitai images
# ---------------------------------------------------------------------------


def _parse_png_parameters(data: bytes) -> str | None:
    """Extract ComfyUI 'parameters' from PNG tEXt chunks."""
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    pos = 8
    while pos + 8 <= len(data):
        length = struct.unpack(">I", data[pos : pos + 4])[0]
        ctype = data[pos + 4 : pos + 8].decode("latin-1", errors="replace")
        if ctype == "tEXt" and pos + 8 + length <= len(data):
            chunk = data[pos + 8 : pos + 8 + length]
            if b"parameters" in chunk:
                _, _, value = chunk.partition(b"\x00")
                return value.decode("utf-8", errors="replace")
        pos += 12 + length
    return None


def _parse_exif_parameters(path: Path) -> str | None:
    """Fallback: read JPEG EXIF ImageDescription / XPComment / UserComment."""
    try:
        from PIL import Image
        from PIL.ExifTags import TAGS

        with Image.open(path) as img:
            exif = img.getexif()
            for tag_id, value in exif.items():
                tag = TAGS.get(tag_id, tag_id)
                if tag in ("ImageDescription", "XPComment", "UserComment"):
                    return str(value)
    except Exception:
        return None
    return None


def import_metadata(image_path: str | os.PathLike[str]) -> dict[str, Any] | None:
    """Try to read generation parameters from a Civitai image.

    Returns a dict with keys steps/cfg/sampler/scheduler/seed if found,
    otherwise ``None``.
    """
    p = Path(image_path)
    if not p.is_file():
        return None

    data = p.read_bytes()
    params = _parse_png_parameters(data)
    if params is None:
        params = _parse_exif_parameters(p)

    if params is None:
        return None

    return {
        "raw": params,
        "steps": _extract_int(params, r"Steps:\s*(\d+)"),
        "cfg": _extract_float(params, r"CFG scale:\s*([0-9.]+)"),
        "sampler": _extract_str(params, r"Sampler:\s*([^\s,]+)"),
        "scheduler": _extract_str(params, r"Scheduler:\s*([^\s,]+)"),
        "seed": _extract_int(params, r"Seed:\s*(\d+)"),
    }


def _extract_int(text: str, pattern: str) -> int:
    m = re.search(pattern, text)
    return int(m.group(1)) if m else 0


def _extract_float(text: str, pattern: str) -> float:
    m = re.search(pattern, text)
    return float(m.group(1)) if m else 1.0


def _extract_str(text: str, pattern: str) -> str:
    m = re.search(pattern, text)
    return m.group(1) if m else ""


# ---------------------------------------------------------------------------
# Workflow construction
# ---------------------------------------------------------------------------


def build_workflow(settings: GenerationSettings) -> dict[str, Any]:
    """Build a ComfyUI prompt workflow for MOSS Image generation.

    MOSS Image models (Jib Mix, SFT) do not embed CLIP or VAE weights, so
    they must be loaded separately via UNETLoader / CLIPLoader / VAELoader.
    """
    return {
        "1": {
            "inputs": {
                "unet_name": settings.model,
                "weight_dtype": "default",
            },
            "class_type": "UNETLoader",
        },
        "2": {
            "inputs": {"clip_name": settings.clip_model, "type": "flux2"},
            "class_type": "CLIPLoader",
        },
        "3": {
            "inputs": {"vae_name": settings.vae_model},
            "class_type": "VAELoader",
        },
        "4": {
            "inputs": {"text": settings.prompt, "clip": ["2", 0]},
            "class_type": "CLIPTextEncode",
        },
        "5": {
            "inputs": {"text": settings.neg_prompt, "clip": ["2", 0]},
            "class_type": "CLIPTextEncode",
        },
        "6": {
            "inputs": {
                "width": settings.width,
                "height": settings.height,
                "batch_size": settings.count,
            },
            "class_type": "EmptyLatentImage",
        },
        "7": {
            "inputs": {
                "seed": settings.seed,
                "steps": settings.steps,
                "cfg": settings.cfg,
                "sampler_name": settings.sampler,
                "scheduler": settings.scheduler,
                "denoise": 1.0,
                "model": ["1", 0],
                "positive": ["4", 0],
                "negative": ["5", 0],
                "latent_image": ["6", 0],
            },
            "class_type": "KSampler",
        },
        "8": {
            "inputs": {"samples": ["7", 0], "vae": ["3", 0]},
            "class_type": "VAEDecode",
        },
        "9": {
            "inputs": {
                "images": ["8", 0],
                "filename_prefix": "ComfyUI",
            },
            "class_type": "SaveImage",
        },
    }


# ---------------------------------------------------------------------------
# ComfyUI HTTP API
# ---------------------------------------------------------------------------


class ComfyUIClient:
    """Thin wrapper around ComfyUI's REST API (injective for tests)."""

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")

    def queue_prompt(self, workflow: dict[str, Any]) -> str:
        payload = {"prompt": workflow, "client_id": "generate-sh"}
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/prompt",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"failed to queue prompt: HTTP {exc.code}: {body}") from exc
        except urllib.error.URLError as exc:  # pragma: no cover - network
            raise RuntimeError(f"failed to queue prompt: {exc}") from exc
        prompt_id = result.get("prompt_id")
        if not prompt_id:
            raise RuntimeError("ComfyUI did not return a prompt_id")
        return prompt_id

    def wait_for_completion(
        self,
        prompt_id: str,
        timeout_s: int = 600,
        progress_callback: Callable[[int], None] | None = None,
    ) -> dict:
        history_url = f"{self.base_url}/history/{prompt_id}"
        deadline = time.time() + timeout_s
        start = time.time()
        last_tick = 0.0
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(history_url, timeout=10) as resp:
                    history = json.loads(resp.read().decode("utf-8"))
            except urllib.error.URLError:
                elapsed = int(time.time() - start)
                if progress_callback and elapsed - last_tick >= 1:
                    progress_callback(elapsed)
                    last_tick = float(elapsed)
                time.sleep(1)
                continue
            if prompt_id in history:
                return history[prompt_id].get("outputs", {})
            elapsed = int(time.time() - start)
            if progress_callback and elapsed - last_tick >= 1:
                progress_callback(elapsed)
                last_tick = float(elapsed)
            time.sleep(1)
        raise TimeoutError(f"timeout waiting for prompt {prompt_id} to finish")

    def download_image(self, filename: str, subfolder: str, ftype: str) -> bytes:
        params = urllib.parse.urlencode({"filename": filename, "subfolder": subfolder, "type": ftype})
        url = f"{self.base_url}/view?{params}"
        with urllib.request.urlopen(url, timeout=60) as resp:
            return resp.read()


def run_generation(
    client: ComfyUIClient,
    settings: GenerationSettings,
    output_dir: str | os.PathLike[str],
    progress_callback: Callable[[int], None] | None = None,
) -> list[GeneratedImage]:
    """Queue a workflow, wait for completion, download images."""
    workflow = build_workflow(settings)
    prompt_id = client.queue_prompt(workflow)
    outputs = client.wait_for_completion(prompt_id, progress_callback=progress_callback)

    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    saved: list[GeneratedImage] = []
    for _node_id, node_out in outputs.items():
        for img in node_out.get("images", []):
            filename = img["filename"]
            subfolder = img.get("subfolder", "")
            ftype = img.get("type", "output")
            data = client.download_image(filename, subfolder, ftype)
            out_path = out_dir / filename
            out_path.write_bytes(data)
            saved.append(GeneratedImage(path=str(out_path), filename=filename))
    return saved


# ---------------------------------------------------------------------------
# Interactive entry point
# ---------------------------------------------------------------------------


def _ask(prompt: str, default: str | None = None) -> str:
    prompt = f"{prompt} [default {default}]: " if default is not None else f"{prompt}: "
    value = input(prompt)
    return value if value.strip() else (default or "")


def _ask_int(prompt: str, default: int, minimum: int = 1) -> int:
    while True:
        raw = _ask(prompt, str(default))
        try:
            value = int(raw)
        except ValueError:
            print(f"Invalid value '{raw}', using {default}")
            return default
        if value < minimum:
            print(f"Value must be >= {minimum}, using {default}")
            return default
        return value


def _ask_float(prompt: str, default: float) -> float:
    while True:
        raw = _ask(prompt, str(default))
        try:
            return float(raw)
        except ValueError:
            print(f"Invalid value '{raw}', using {default}")
            return default


def _select_from_list(title: str, options: list[str], default_index: int = 1) -> str:
    """Show a numbered list and return the selected option."""
    print(f"\n{title}:")
    print("------------------------------------")
    for i, name in enumerate(options, start=1):
        marker = " (default)" if i == default_index else ""
        print(f"  {i}) {name}{marker}")
    print()
    while True:
        sel = input(f"Select [1-{len(options)}]: ").strip()
        if not sel:
            return options[default_index - 1]
        if sel.isdigit() and 1 <= int(sel) <= len(options):
            return options[int(sel) - 1]
        # Allow free-form input for advanced users.
        if sel:
            return sel
        print(f"Invalid selection. Enter a number between 1 and {len(options)}.")


# Common ComfyUI samplers / schedulers (safe defaults for MOSS Image).
SAMPLERS = [
    "euler",
    "euler_a",
    "heun",
    "dpm_2",
    "dpm_2_a",
    "lpm",
    "lpm_a",
    "dpm_fast",
    "dpm_adaptive",
    "dpmpp_2m",
    "dpmpp_2m_sde",
    "dpmpp_sde",
    "dpmpp_2m_sde_gpu",
    "dpmpp_3m_sde",
    "dpmpp_3m_sde_gpu",
    "dpmpp_sde_gpu",
    "ddim",
    "plms",
    "unipc",
    "poi",
    "er_sde",
    "er_sde_plus",
]

SCHEDULERS = [
    "simple",
    "normal",
    "karras",
    "exponential",
    "polyexponential",
    "ddpmsampler",
    "sgm_uniform",
    "beta",
    "linear_quadratic",
]


def interactive(
    available_models: list[str] | None = None,
    available_clip_models: list[str] | None = None,
    available_vae_models: list[str] | None = None,
    settings_source: GenerationSettings | None = None,
) -> GenerationSettings:
    """Prompt the user for generation parameters (stdin/stdout)."""
    model = settings_source.model if settings_source else ""
    width = settings_source.width if settings_source else 1024
    height = settings_source.height if settings_source else 1024
    count = settings_source.count if settings_source else 1
    steps = settings_source.steps if settings_source else 6
    cfg = settings_source.cfg if settings_source else 1.0
    sampler = settings_source.sampler if settings_source else "er_sde"
    scheduler = settings_source.scheduler if settings_source else "simple"
    seed = settings_source.seed if settings_source else -1
    clip_model = settings_source.clip_model if settings_source else ""
    vae_model = settings_source.vae_model if settings_source else ""

    if available_models:
        model = _select_from_list("Available generative models on disk", available_models, 1)
    else:
        model = _ask("Model", model) or model

    if available_clip_models:
        clip_model = _select_from_list("Text encoders (CLIP)", available_clip_models, 1)
    else:
        clip_model = _ask("Text encoder (CLIP) model", clip_model) or clip_model

    if available_vae_models:
        vae_model = _select_from_list("VAE models", available_vae_models, 1)
    else:
        vae_model = _ask("VAE model", vae_model) or vae_model

    size_raw = _ask("Image size (WxH)", f"{width}x{height}")
    try:
        width_s, height_s = size_raw.split("x")
        width, height = int(width_s), int(height_s)
    except ValueError:
        print(f"Invalid size '{size_raw}', using {width}x{height}")
    count = _ask_int("Number of images", count)
    steps = _ask_int("Steps", steps)
    cfg = _ask_float("CFG", cfg)
    sampler_default = sampler if sampler in SAMPLERS else SAMPLERS[0]
    sampler = _select_from_list("Samplers", SAMPLERS, SAMPLERS.index(sampler_default) + 1)

    scheduler_default = scheduler if scheduler in SCHEDULERS else SCHEDULERS[0]
    scheduler = _select_from_list("Schedulers", SCHEDULERS, SCHEDULERS.index(scheduler_default) + 1)
    seed_raw = _ask("Seed (-1 for random)", str(seed))
    try:
        seed = int(seed_raw)
    except ValueError:
        seed = -1
    if seed == -1:
        import random

        seed = random.randint(0, 18446744073709551615)
    prompt = _ask("Prompt")
    if not prompt.strip():
        raise ValueError("Prompt cannot be empty")
    neg_prompt = _ask("Negative prompt")

    return GenerationSettings(
        model=model,
        width=width,
        height=height,
        count=count,
        steps=steps,
        cfg=cfg,
        sampler=sampler,
        scheduler=scheduler,
        seed=seed,
        prompt=prompt,
        neg_prompt=neg_prompt,
        clip_model=clip_model,
        vae_model=vae_model,
    )


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) < 2:
        print(
            "Usage: python -m comfyprov.generate <api_url> <output_dir>"
            " <diff_dir> <clip_dir> <vae_dir> [model_names...]",
            file=sys.stderr,
        )
        return 2

    api_url, output_dir = argv[0], argv[1]
    diff_dir = argv[2] if len(argv) > 2 else ""
    clip_dir = argv[3] if len(argv) > 3 else ""
    vae_dir = argv[4] if len(argv) > 4 else ""
    model_names = argv[5:] if len(argv) > 5 else []

    available_models = discover_models(diff_dir) if diff_dir else None
    available_clip_models = discover_models(clip_dir) if clip_dir else None
    available_vae_models = discover_models(vae_dir) if vae_dir else None

    settings = interactive(
        available_models=available_models,
        available_clip_models=available_clip_models,
        available_vae_models=available_vae_models,
    )

    # Override model with explicit name from argv if provided
    if model_names:
        settings = dataclass_replace(settings, model=model_names[0])

    client = ComfyUIClient(api_url)
    try:
        saved = run_generation(
            client,
            settings,
            output_dir,
            progress_callback=lambda secs: print(f"\rGenerating… {secs}s elapsed", end="", flush=True),
        )
    except (RuntimeError, TimeoutError, ValueError) as exc:
        print()
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print()
    print(f"\nGenerated {len(saved)} image(s) to {output_dir}:")
    for img in saved:
        print(f"  {img.path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
