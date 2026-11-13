"""Server-side verification: /system_stats, ComfyUI-Manager and a real sample.

The shell starts the temporary server and stops it; this module performs all
checks and turns HTTP answers into findings plus an exit code.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import models, smoke
from .actions import EXIT_OK, EXIT_VERIFY
from .config import Config
from .probe import Probe
from .report import FAIL, OK, WARN, Finding
from .server import HttpClient, HttpError, manager_version, parse_stats


@dataclass(frozen=True)
class DiffusionChoice:
    key: str
    filename: str
    steps: int
    cfg: float
    text_encoder: str
    vae: str


PREFERRED = ("jibmix_v2", "moss_sft")


def resolve_diffusion(manifest: dict, models_dir: Path, probe: Probe) -> DiffusionChoice | None:
    """Pick the best installed MOSS diffusion model plus its shared TE/VAE."""
    for key in PREFERRED:
        entry = manifest.get(key)
        if entry is None or not probe.is_file(models_dir / entry.relative_path):
            continue
        te = manifest.get("moss_te")
        vae = manifest.get("moss_vae")
        if te is None or vae is None:
            return None
        return DiffusionChoice(
            key=key,
            filename=entry.filename,
            steps=models.VERIFY_STEPS.get(key, 8),
            cfg=models.VERIFY_CFG.get(key, 1.0),
            text_encoder=te.filename,
            vae=vae.filename,
        )
    return None


def verify_server(
    client: HttpClient,
    url: str,
    config: Config,
    manifest: dict,
    probe: Probe,
    full: bool = False,
    output_dir: Path | None = None,
    progress: Callable[[str], None] = print,
) -> tuple[list[Finding], int]:
    findings: list[Finding] = []
    base = url.rstrip("/")

    try:
        stats = parse_stats(client.get_json(f"{base}/system_stats", timeout=15))
    except HttpError as exc:
        return [Finding(FAIL, "/system_stats", str(exc))], EXIT_VERIFY

    if not stats["comfyui_version"]:
        findings.append(Finding(FAIL, "ComfyUI version", "missing in /system_stats"))
    else:
        findings.append(Finding(OK, "ComfyUI", stats["comfyui_version"]))
    if stats["pytorch_version"]:
        findings.append(Finding(OK, "PyTorch", stats["pytorch_version"]))
    if stats["python_version"]:
        findings.append(Finding(OK, "Python", stats["python_version"]))
    if stats["device_name"]:
        findings.append(Finding(OK, "Device", f"{stats['device_name']} ({stats['device_type']})"))
    if stats["vram_total"]:
        findings.append(Finding(OK, "VRAM total", f"{stats['vram_total'] / 1024**3:.1f} GiB"))

    version = manager_version(client, base)
    if version:
        findings.append(Finding(OK, "ComfyUI-Manager", version))
    elif probe.is_dir(config.comfyui_dir / "custom_nodes" / "ComfyUI-Manager"):
        findings.append(Finding(WARN, "ComfyUI-Manager", "no answer on /manager/version; check the logs"))
    else:
        findings.append(Finding(FAIL, "ComfyUI-Manager", "not installed; run ./provision.sh manager"))

    if not full:
        findings.append(Finding(OK, "generation", "skipped; re-run with --full after downloading models"))
        return findings, EXIT_OK

    choice = resolve_diffusion(manifest, config.models_dir, probe)
    if choice is None:
        findings.append(Finding(FAIL, "MOSS model", "no diffusion model found; run ./provision.sh models --set jibmix"))
        return findings, EXIT_VERIFY

    findings.append(Finding(OK, "MOSS model", f"{choice.key} ({choice.filename})"))
    workflow = smoke.build_workflow(
        diffusion=choice.filename,
        text_encoder=choice.text_encoder,
        vae=choice.vae,
        prompt="A studio photograph of a red ceramic mug on a wooden table, soft window light",
        steps=choice.steps,
        cfg=choice.cfg,
    )
    ok, detail = smoke.run_generation(
        client,
        base,
        workflow,
        str(output_dir or config.output_dir),
        timeout=float(config.get("VERIFY_TIMEOUT", "1800")),
        progress=progress,
    )
    findings.append(Finding(OK if ok else FAIL, "generation", detail))
    return findings, EXIT_OK if ok else EXIT_VERIFY
