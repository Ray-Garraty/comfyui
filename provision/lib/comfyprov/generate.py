"""Interactive image generation via ComfyUI API.

Business logic for generate.sh: discover models, import generation settings
from Civitai images (PNG tEXt / JPEG EXIF), build a ComfyUI workflow, queue
it through the /prompt API, poll for completion, and download the resulting
images.

Pure-stdlib where possible (Pillow only as an optional EXIF fallback).
Designed for unit testing with fake filesystem / HTTP responses.
"""

from __future__ import annotations

import io
import json
import os
import re
import struct
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from dataclasses import replace as dataclass_replace
from pathlib import Path
from typing import Any

from .smoke import check_image, strip_png_text_chunks

# ---------------------------------------------------------------------------
# Output: terminal + log file without interactive latency
# ---------------------------------------------------------------------------

# Same format as the shell helpers in generate.sh: date '+%Y-%m-%d %H:%M:%S'.
LOG_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"

# What replaces a prompt in the log if it ever ends up in a message.
REDACTED = "[redacted]"


class LogTee(io.TextIOBase):
    """Write through to a terminal stream and to a log file, timestamping lines.

    Business requirement: no delay after the user presses Enter at interactive
    prompts.  Two rules follow from it:

    * the terminal copy is flushed on every ``write()``, so a prompt is on
      screen *before* ``input()`` blocks on stdin;
    * the log copy is produced here, in-process, instead of by a shell
      ``> >(while read line; do ...)`` loop: a consumer that falls behind (or
      forks ``date`` per line) used to block Python's ``write()`` right after
      Enter.

    Privacy rule: prompts are the user's private business. The log is a
    diagnostic artefact that outlives the session, so prompt text must never
    reach it. Values registered with :meth:`add_sensitive` are redacted from
    every line written to the log, and :meth:`write_split` lets a caller show
    raw user input on the terminal while logging only a sanitized version.

    The wrapped stream stays owned by the interpreter: :meth:`close` only
    detaches, it never closes the terminal.
    """

    def __init__(self, stream: Any, log: Any, time_format: str = LOG_TIME_FORMAT) -> None:
        self._stream = stream
        self._log = log
        self._time_format = time_format
        self._at_line_start = True
        self._lock = threading.Lock()
        self._sensitive: tuple[str, ...] = ()

    # -- text stream protocol -------------------------------------------
    def write(self, text: str) -> int:
        if not text:
            return 0
        with self._lock:
            self._stream.write(text)
            # Immediate flush: the prompt must be visible before input().
            self._stream.flush()
            self._write_log(text)
        return len(text)

    def write_private(self, text: str) -> int:
        """Write to the terminal only, never to the log file.

        Prompts are user content and must not be persisted: the log is meant
        for phases, timings and host state.
        """
        if not text:
            return 0
        with self._lock:
            self._stream.write(text)
            self._stream.flush()
        return len(text)

    def write_split(self, terminal_text: str, log_text: str) -> int:
        """Show ``terminal_text`` and log only ``log_text``.

        Diagnostics about user input ("Invalid value 'a cat', using 1") must
        echo what was typed on the terminal while the persisted log keeps a
        version without the user's data ("Invalid value, using 1").
        """
        if not terminal_text and not log_text:
            return 0
        with self._lock:
            self._stream.write(terminal_text)
            self._stream.flush()
            self._write_log(log_text)
        return len(terminal_text)

    def add_sensitive(self, *values: str) -> None:
        """Register strings that must never appear in the log.

        Defence in depth: prompt text is written with :func:`write_private` and
        error messages are sanitized, but an unexpected traceback must not be
        able to persist a prompt either. Longer values are replaced first so an
        overlapping pair cannot leave a visible remainder.
        """
        with self._lock:
            merged = {value for value in (*self._sensitive, *values) if value and value.strip()}
            self._sensitive = tuple(sorted(merged, key=len, reverse=True))

    def flush(self) -> None:
        with self._lock:
            self._stream.flush()
            if self._log is not None:
                self._log.flush()

    def close(self) -> None:
        """Flush and detach from the log; the terminal stream is not ours."""
        with self._lock:
            if self._log is not None:
                try:
                    self._stream.flush()
                    self._log.flush()
                    self._log.close()
                except (OSError, ValueError):
                    pass
                self._log = None

    def writable(self) -> bool:
        return True

    def readable(self) -> bool:
        return False

    def seekable(self) -> bool:
        return False

    def fileno(self) -> int:
        """input() calls sys.stdout.fileno() to decide whether to use readline."""
        return int(self._stream.fileno())

    def isatty(self) -> bool:
        return bool(self._stream.isatty())

    @property
    def encoding(self) -> str:  # type: ignore[override]
        return str(getattr(self._stream, "encoding", "utf-8") or "utf-8")

    @property
    def errors(self) -> str | None:  # type: ignore[override]
        return getattr(self._stream, "errors", None)  # type: ignore[no-any-return]

    # -- logging ---------------------------------------------------------
    def _redact(self, text: str) -> str:
        for secret in self._sensitive:
            if secret in text:
                text = text.replace(secret, REDACTED)
        return text

    def _write_log(self, text: str) -> None:
        """Append ``text`` to the log, prefixing each new line with a timestamp.

        Only ``\\n`` terminates a logged line: the generation progress redraws
        with ``\\r`` and must stay one readable line.
        """
        if self._log is None:
            return
        text = self._redact(text)
        try:
            for index, chunk in enumerate(text.split("\n")):
                if index:
                    self._log.write("\n")
                    self._at_line_start = True
                if not chunk:
                    continue
                if self._at_line_start:
                    self._log.write(time.strftime(self._time_format) + " ")
                    self._at_line_start = False
                self._log.write(chunk)
            self._log.flush()
        except (OSError, ValueError):
            # A full disk or a closed log must never break the prompts.
            self._log = None


def install_log_tee(log_path: str | os.PathLike[str] | None) -> LogTee | None:
    """Mirror stdout/stderr into ``log_path``; return None when there is no path.

    Generation output is logged from inside Python so that no shell pipe sits
    between the interpreter and the terminal.  If the log cannot be opened the
    terminal keeps working as before.
    """
    if not log_path:
        return None
    try:
        # The handle is owned by the returned tee for the whole process lifetime.
        handle = open(os.fspath(log_path), "a", encoding="utf-8", errors="replace")  # noqa: SIM115
    except OSError as exc:
        print(f"WARN: cannot write log file {log_path}: {exc}", file=sys.stderr)
        return None
    tee = LogTee(sys.stdout, handle)
    sys.stdout = tee  # type: ignore[assignment]
    sys.stderr = tee  # type: ignore[assignment]
    return tee


def write_private(text: str) -> None:
    """Write ``text`` to the terminal only, never to the log file.

    Two kinds of output are not persisted: user prompts (private content) and
    the once-per-second ``Generating…`` repaint (hundreds of near-identical
    lines that bury the phases). The log keeps what is needed after a crash:
    phases, timings, produced paths and host state.
    """
    stream = sys.stdout
    private = getattr(stream, "write_private", None)
    if callable(private):
        private(text)
        return
    stream.write(text)
    stream.flush()


def write_split(terminal_text: str, log_text: str) -> None:
    """Echo user input on the terminal and keep it out of the log.

    Used by every diagnostic that would otherwise repeat what the user typed
    into the wrong field ("Invalid value 'a cat', using 1"). The terminal sees
    the raw input because it is already on the screen; the log gets
    ``log_text``, which says what happened without repeating the data.
    """
    stream = sys.stdout
    split = getattr(stream, "write_split", None)
    if callable(split):
        split(terminal_text, log_text)
        return
    stream.write(terminal_text)
    stream.flush()


def register_private_text(*values: str) -> None:
    """Feed prompt strings to the log tee so they can never be persisted."""
    add = getattr(sys.stdout, "add_sensitive", None)
    if callable(add):
        add(*values)


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------


# Seed range accepted by KSampler (unsigned 64-bit).
MAX_SEED = 18446744073709551615


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
    use_tiled_vae: bool
    tile_size: int
    # True when the user asked for a random seed (-1). A further round then
    # draws a new one instead of repeating the previous images.
    random_seed: bool = False


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
            "inputs": (
                {
                    "samples": ["7", 0],
                    "vae": ["3", 0],
                    "tile_size": settings.tile_size,
                    "overlap": 64,
                    "temporal_size": 64,
                    "temporal_overlap": 8,
                }
                if settings.use_tiled_vae
                else {"samples": ["7", 0], "vae": ["3", 0]}
            ),
            "class_type": "VAEDecodeTiled" if settings.use_tiled_vae else "VAEDecode",
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
    """Queue a workflow, wait for completion, download images.

    Images are generated one-by-one to minimise peak VRAM usage on RDNA2.
    """
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    saved: list[GeneratedImage] = []

    for i in range(settings.count):
        image_seed = settings.seed
        if image_seed >= 0:
            image_seed = settings.seed + i

        single = dataclass_replace(
            settings,
            count=1,
            seed=image_seed,
        )
        workflow = build_workflow(single)
        prompt_id = client.queue_prompt(workflow)
        outputs = client.wait_for_completion(prompt_id, progress_callback=progress_callback)

        for _node_id, node_out in outputs.items():
            for img in node_out.get("images", []):
                filename = img["filename"]
                subfolder = img.get("subfolder", "")
                ftype = img.get("type", "output")
                data = client.download_image(filename, subfolder, ftype)
                # The server runs with --disable-metadata, but a stale instance
                # (started before that flag, or by hand) would still embed the
                # prompt. Strip it here so no prompt can reach the output.
                out_path = out_dir / filename
                out_path.write_bytes(strip_png_text_chunks(data))
                ok, detail = check_image(str(out_path))
                if not ok:
                    # A 512x512 PNG of pure black is a valid file and no image:
                    # report it now instead of counting it as a success.
                    raise RuntimeError(f"{out_path}: {detail}")
                saved.append(GeneratedImage(path=str(out_path), filename=filename))

    return saved


# ---------------------------------------------------------------------------
# Interactive entry point
# ---------------------------------------------------------------------------


def _draw_seed() -> int:
    """Return a fresh random seed for a round the user asked to randomise."""
    import random

    return random.randint(0, MAX_SEED)


def _ask(prompt: str, default: str | None = None) -> str:
    prompt = f"{prompt} [default {default}]: " if default is not None else f"{prompt}: "
    sys.stdout.flush()
    value = input(prompt)
    return value if value.strip() else (default or "")


def _ask_yn(question: str, default: bool = False) -> bool:
    """Ask a yes/no question.

    End of input (Ctrl-D, a closed pipe, a non-interactive caller) is an answer
    too: it means ``default`` and lets the script finish cleanly instead of
    dying with an EOFError traceback.
    """
    marker = "y" if default else "n"
    while True:
        sys.stdout.flush()
        try:
            raw = input(f"{question} (y/n) [{marker}]: ")
        except EOFError:
            print()
            return default
        answer = raw.strip().lower()
        if not answer:
            return default
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False
        write_split(
            f"Invalid answer '{raw}', expected 'y' or 'n'\n",
            "Invalid answer, expected 'y' or 'n'\n",
        )


def _ask_reuse(label: str, previous: str) -> str:
    """Ask for a text value; an empty answer keeps ``previous``.

    Prompts are long tag lists, so the previous value is printed on its own
    line: ``Prompt [default <everything the user typed>]:`` would not fit.
    """
    if previous:
        write_private(f"Previous {label}: {previous}\n")
    sys.stdout.flush()
    raw = input(f"{label.capitalize()} [Enter = repeated]: ")
    return raw if raw.strip() else previous


def _ask_int(prompt: str, default: int, minimum: int = 1) -> int:
    while True:
        raw = _ask(prompt, str(default))
        try:
            value = int(raw)
        except ValueError:
            write_split(f"Invalid value '{raw}', using {default}\n", f"Invalid value, using {default}\n")
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
            write_split(f"Invalid value '{raw}', using {default}\n", f"Invalid value, using {default}\n")
            return default


def _select_from_list(title: str, options: list[str], default_index: int = 1) -> str:
    """Show a numbered list and return the selected option.

    Only a number is accepted. Returning arbitrary text used to look like a
    convenience for advanced users, but a mistyped prompt then became a model
    name, reached the server and was echoed back in its error log.
    """
    if not options:
        raise ValueError(f"no candidates for {title.lower()}; check the model directories")
    print(f"\n{title}:")
    print("------------------------------------")
    for i, name in enumerate(options, start=1):
        marker = " (default)" if i == default_index else ""
        print(f"  {i}) {name}{marker}")
    print()
    sys.stdout.flush()
    while True:
        sel = input(f"Select [1-{len(options)}]: ").strip()
        if not sel:
            return options[default_index - 1]
        if sel.isdigit() and 1 <= int(sel) <= len(options):
            return options[int(sel) - 1]
        write_split(
            f"Invalid selection '{sel}', enter a number between 1 and {len(options)}.\n",
            f"Invalid selection, enter a number between 1 and {len(options)}.\n",
        )


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

    if available_models is not None:
        model = _select_from_list("Available generative models on disk", available_models, 1)
    else:
        model = _ask("Model", model) or model

    if available_clip_models is not None:
        clip_model = _select_from_list("Text encoders (CLIP)", available_clip_models, 1)
    else:
        clip_model = _ask("Text encoder (CLIP) model", clip_model) or clip_model

    if available_vae_models is not None:
        vae_model = _select_from_list("VAE models", available_vae_models, 1)
    else:
        vae_model = _ask("VAE model", vae_model) or vae_model

    size_raw = _ask("Image size (WxH)", f"{width}x{height}")
    try:
        width_s, height_s = size_raw.split("x")
        width, height = int(width_s), int(height_s)
    except ValueError:
        write_split(
            f"Invalid size '{size_raw}', using {width}x{height}\n",
            f"Invalid size, using {width}x{height}\n",
        )
    count = _ask_int("Number of images", count)
    # Images are generated one-by-one to minimise peak VRAM usage on RDNA2.
    steps = _ask_int("Steps", steps)
    cfg = _ask_float("CFG", cfg)
    sampler_default = sampler if sampler in SAMPLERS else SAMPLERS[0]
    sampler = _select_from_list("Samplers", SAMPLERS, SAMPLERS.index(sampler_default) + 1)

    scheduler_default = scheduler if scheduler in SCHEDULERS else SCHEDULERS[0]
    scheduler = _select_from_list("Schedulers", SCHEDULERS, SCHEDULERS.index(scheduler_default) + 1)

    # Tiled VAE decode reduces peak VRAM usage during the final decode stage.
    # Tiled VAE decode is on by default on RDNA2 (12 GB VRAM, no matrix cores)
    # to avoid OOM during the final latent-to-image decode.
    use_tiled_vae = settings_source.use_tiled_vae if settings_source else True
    tile_size = settings_source.tile_size if settings_source else 512

    seed_raw = _ask("Seed (-1 for random)", str(seed))
    try:
        seed = int(seed_raw)
    except ValueError:
        seed = -1
    random_seed = seed == -1
    if random_seed:
        seed = _draw_seed()
    while True:
        prompt = _ask("Prompt")
        if prompt.strip():
            break
        print("Prompt cannot be empty")
    neg_prompt = _ask("Negative prompt")

    register_private_text(prompt, neg_prompt)

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
        use_tiled_vae=use_tiled_vae,
        tile_size=tile_size,
        random_seed=random_seed,
    )


def _settings_summary(settings: GenerationSettings) -> str:
    seed = "random" if settings.random_seed else str(settings.seed)
    tiled = f"on (tile {settings.tile_size})" if settings.use_tiled_vae else "off"
    return (
        f"{settings.model}, {settings.width}x{settings.height}, steps {settings.steps}, "
        f"cfg {settings.cfg:g}, {settings.sampler}/{settings.scheduler}, "
        f"tiled VAE {tiled}, seed: {seed}"
    )


def interactive_round(previous: GenerationSettings) -> GenerationSettings:
    """Ask only what changes between rounds: count and the two prompts.

    Everything else (model, size, steps, cfg, sampler/scheduler, VAE settings)
    is reused from ``previous``. An empty answer keeps the previous value, so
    pressing Enter through the block reproduces the last round. A round that
    started from a random seed draws a new one: same prompt and same seed would
    otherwise write the same images again.
    """
    print("\n--- Next round (same settings) ---")
    print(_settings_summary(previous))
    count = _ask_int("Number of images", previous.count)
    prompt = _ask_reuse("positive prompt", previous.prompt)
    neg_prompt = _ask_reuse("negative prompt", previous.neg_prompt)
    register_private_text(prompt, neg_prompt)
    seed = _draw_seed() if previous.random_seed else previous.seed
    return dataclass_replace(
        previous,
        count=count,
        prompt=prompt,
        neg_prompt=neg_prompt,
        seed=seed,
    )


def _report_progress(elapsed_s: int) -> None:
    """Repaint the elapsed-time ticker on the terminal, never in the log."""
    write_private(f"\rGenerating… {elapsed_s}s elapsed")


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

    # Duplicate stdout/stderr to the log file (path passed via env by
    # generate.sh) while keeping the terminal as the real stdout, so that
    # interactive prompts appear instantly with no pipe buffering.
    log_file = os.environ.get("GENERATE_LOG_FILE")
    if log_file:
        install_log_tee(log_file)

    available_models = discover_models(diff_dir) if diff_dir else None
    available_clip_models = discover_models(clip_dir) if clip_dir else None
    available_vae_models = discover_models(vae_dir) if vae_dir else None

    try:
        settings = interactive(
            available_models=available_models,
            available_clip_models=available_clip_models,
            available_vae_models=available_vae_models,
        )
    except ValueError as exc:
        # An empty model directory is a setup problem, not a prompt: fail before
        # asking anything (a free-form answer there would become a file name).
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    # Override model with explicit name from argv if provided
    if model_names:
        settings = dataclass_replace(settings, model=model_names[0])

    client = ComfyUIClient(api_url)
    total: list[GeneratedImage] = []
    round_no = 1
    while True:
        register_private_text(settings.prompt, settings.neg_prompt)
        try:
            saved = run_generation(
                client,
                settings,
                output_dir,
                progress_callback=_report_progress,
            )
        except (RuntimeError, TimeoutError, ValueError) as exc:
            write_private("\n")
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1

        # Terminate the repaint on the terminal only: it was never logged.
        write_private("\n")
        if not saved:
            print(f"ERROR: No images were generated to {output_dir}", file=sys.stderr)
            return 1
        total.extend(saved)
        print(f"\nRound {round_no}: generated {len(saved)} image(s) to {output_dir}:")
        for img in saved:
            print(f"  {img.path}")

        # The ComfyUI server is stopped by generate.sh, i.e. only after this
        # loop ends: further rounds reuse the loaded model and VAE.
        if not _ask_yn("Generate more images with the same settings?", default=False):
            break
        settings = interactive_round(settings)
        round_no += 1

    if round_no > 1:
        print(f"\nTotal: {len(total)} image(s) to {output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
