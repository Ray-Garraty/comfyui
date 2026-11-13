"""Minimal MOSS-Image generation used by ``verify --full``.

The workflow, the polling loop and the PNG validation live here so they can be
tested without a running ComfyUI: tests inject a fake HTTP client and a fake
clock.
"""

from __future__ import annotations

import os
import time
import uuid
from collections.abc import Callable

from .server import HistoryResult, HttpClient, HttpError, interpret_history

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def build_workflow(
    diffusion: str,
    text_encoder: str,
    vae: str,
    prompt: str,
    negative: str = "",
    width: int = 768,
    height: int = 768,
    steps: int = 8,
    cfg: float = 1.0,
    sampler: str = "er_sde",
    scheduler: str = "simple",
    seed: int = 12345,
    prefix: str = "smoke_test",
) -> dict:
    return {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": diffusion, "weight_dtype": "default"}},
        "2": {
            "class_type": "CLIPLoader",
            "inputs": {"clip_name": text_encoder, "type": "moss_image", "device": "default"},
        },
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": vae}},
        "4": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["2", 0], "text": prompt}},
        "5": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["2", 0], "text": negative}},
        "6": {"class_type": "EmptyFlux2LatentImage", "inputs": {"width": width, "height": height, "batch_size": 1}},
        "7": {
            "class_type": "KSampler",
            "inputs": {
                "model": ["1", 0],
                "positive": ["4", 0],
                "negative": ["5", 0],
                "latent_image": ["6", 0],
                "seed": seed,
                "steps": steps,
                "cfg": cfg,
                "sampler_name": sampler,
                "scheduler": scheduler,
                "denoise": 1.0,
            },
        },
        "8": {"class_type": "VAEDecode", "inputs": {"samples": ["7", 0], "vae": ["3", 0]}},
        "9": {"class_type": "SaveImage", "inputs": {"images": ["8", 0], "filename_prefix": prefix}},
    }


def check_image(path: str) -> tuple[bool, str]:
    """Validate a generated PNG without requiring Pillow."""
    try:
        size = os.path.getsize(path)
    except OSError as exc:
        return False, str(exc)
    if size < 1024:
        return False, f"file is suspiciously small ({size} bytes)"
    try:
        with open(path, "rb") as handle:
            header = handle.read(8)
    except OSError as exc:
        return False, str(exc)
    if header != PNG_MAGIC:
        return False, "not a PNG file"
    try:
        from PIL import Image

        with Image.open(path) as image:
            image.load()
            return True, f"{image.width}x{image.height}, {size / 1024:.0f} KiB"
    except ImportError:
        return True, f"{size / 1024:.0f} KiB (Pillow not available, header only)"
    except Exception as exc:
        return False, f"PIL failed: {exc}"


def run_generation(
    client: HttpClient,
    base_url: str,
    workflow: dict,
    output_dir: str,
    timeout: float = 1800.0,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    progress: Callable[[str], None] = print,
) -> tuple[bool, str]:
    """Queue the workflow, wait for completion and validate the images."""
    base = base_url.rstrip("/")
    try:
        response = client.post_json(f"{base}/prompt", {"prompt": workflow, "client_id": f"provision-{uuid.uuid4()}"})
    except HttpError as exc:
        return False, f"/prompt failed: {exc}"

    node_errors = response.get("node_errors") or {}
    if node_errors:
        return False, f"node errors: {node_errors}"
    prompt_id = response.get("prompt_id")
    if not prompt_id:
        return False, f"no prompt_id in response: {response}"

    progress(f"queued prompt {prompt_id}")
    started = clock()
    deadline = started + timeout
    entry = None
    last_note = started
    while clock() < deadline:
        sleep(2.0)
        try:
            history = client.get_json(f"{base}/history/{prompt_id}")
        except HttpError:
            continue
        entry = history.get(prompt_id)
        if entry:
            break
        now = clock()
        if now - last_note > 15:
            last_note = now
            progress(f"waiting... {now - started:.0f}s")

    result: HistoryResult = interpret_history(entry)
    if not result.ok:
        return False, f"{result.detail} (after {clock() - started:.0f}s)"

    failures: list[str] = []
    for image in result.images:
        path = os.path.join(output_dir, image.get("subfolder") or "", image.get("filename") or "")
        ok, detail = check_image(path)
        if ok:
            progress(f"{path} ({detail})")
        else:
            failures.append(f"{path}: {detail}")
    if failures:
        return False, "; ".join(failures)
    return True, f"generation finished in {clock() - started:.1f}s"
