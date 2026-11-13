"""Minimal MOSS-Image generation used by ``verify --full``.

The workflow, the polling loop and the PNG validation live here so they can be
tested without a running ComfyUI: tests inject a fake HTTP client and a fake
clock.
"""

from __future__ import annotations

import os
import struct
import time
import uuid
import zlib
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


def png_extrema(path: str) -> tuple[int, int] | None:
    """Min/max value of any colour channel, or None for unsupported PNGs.

    A 512x512 PNG of pure black is a valid file of ~2 KiB that no container
    check can reject, yet it means the sampler or the decoder produced nothing.
    Only 8-bit RGB/RGBA, non-interlaced images are decoded, which is what
    ComfyUI's SaveImage writes.
    """
    try:
        with open(path, "rb") as handle:
            data = handle.read()
    except OSError:
        return None
    if not data.startswith(PNG_MAGIC):
        return None
    offset = len(PNG_MAGIC)
    header = b""
    payload = bytearray()
    while offset + 8 <= len(data):
        (length,) = struct.unpack(">I", data[offset : offset + 4])
        tag = data[offset + 4 : offset + 8]
        body = data[offset + 8 : offset + 8 + length]
        offset += 12 + length
        if tag == b"IHDR":
            header = body
        elif tag == b"IDAT":
            payload += body
        elif tag == b"IEND":
            break
    if len(header) != 13:
        return None
    width, height, depth, colour, _, _, interlace = struct.unpack(">IIBBBBB", header)
    channels = {2: 3, 6: 4}.get(colour)
    if depth != 8 or channels is None or interlace != 0:
        return None
    try:
        raw = zlib.decompress(bytes(payload))
    except zlib.error:
        return None
    stride = width * channels
    if len(raw) < height * (stride + 1):
        return None
    low, high = 255, 0
    previous = bytearray(stride)
    position = 0
    for _ in range(height):
        filter_type = raw[position]
        position += 1
        line = bytearray(raw[position : position + stride])
        position += stride
        if filter_type == 1:
            for index in range(channels, stride):
                line[index] = (line[index] + line[index - channels]) & 0xFF
        elif filter_type == 2:
            for index in range(stride):
                line[index] = (line[index] + previous[index]) & 0xFF
        elif filter_type == 3:
            for index in range(stride):
                left = line[index - channels] if index >= channels else 0
                line[index] = (line[index] + ((left + previous[index]) >> 1)) & 0xFF
        elif filter_type == 4:
            for index in range(stride):
                left = line[index - channels] if index >= channels else 0
                up = previous[index]
                up_left = previous[index - channels] if index >= channels else 0
                estimate = left + up - up_left
                distance_left, distance_up = abs(estimate - left), abs(estimate - up)
                distance_up_left = abs(estimate - up_left)
                if distance_left <= distance_up and distance_left <= distance_up_left:
                    predictor = left
                elif distance_up <= distance_up_left:
                    predictor = up
                else:
                    predictor = up_left
                line[index] = (line[index] + predictor) & 0xFF
        elif filter_type != 0:
            return None
        for index in range(stride):
            value = line[index]
            if value < low:
                low = value
            if value > high:
                high = value
        previous = line
    return low, high


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
            extrema = png_extrema(path)
            if extrema is not None and extrema[1] - extrema[0] < 8:
                return False, f"blank image: every colour channel is {extrema[0]}..{extrema[1]}"
            return True, f"{image.width}x{image.height}, {size / 1024:.0f} KiB"
    except ImportError:
        return True, f"{size / 1024:.0f} KiB (Pillow not available, header only)"
    except Exception as exc:
        return False, f"PIL failed: {exc}"


def validate_outputs(paths) -> list[str]:
    """Problems found in the produced files: blank frames, unreadable files.

    The generator and the e2e test share this check, so "an image exists" can
    never be confused with "an image was produced": a 512x512 PNG of pure black
    is a valid file and worthless output.
    """
    problems: list[str] = []
    for path in paths:
        ok, detail = check_image(str(path))
        if not ok:
            problems.append(f"{path}: {detail}")
    return problems


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
