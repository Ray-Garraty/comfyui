"""Static layout details: directories, packages, managed config files.

These are *facts about the installation*, not decisions, so they live in one
place and are consumed by the planners, the launcher and the tests.
"""

from __future__ import annotations

from pathlib import Path

# ComfyUI's model folders. Keep in sync with the models.tsv "target" column.
MODELS_SUBDIRS: tuple[str, ...] = (
    "checkpoints",
    "configs",
    "loras",
    "vae",
    "text_encoders",
    "clip",
    "diffusion_models",
    "unet",
    "clip_vision",
    "style_models",
    "embeddings",
    "controlnet",
    "upscale_models",
    "latent_upscale_models",
    "geometry_estimation",
    "model_patches",
    "audio_encoders",
)

APP_SUBDIRS: tuple[str, ...] = ("logs", "output", "input", "temp", "user")

# apt packages. Required ones are fatal when missing, optional ones are skipped
# when the distribution does not carry them.
APT_REQUIRED: tuple[str, ...] = (
    "git",
    "curl",
    "ca-certificates",
    "jq",
    "unzip",
    "ffmpeg",
    "build-essential",
    "cmake",
    "ninja-build",
    "pkg-config",
    "python3.12-venv",
    "python3.12-dev",
    "python3-pip",
    "rsync",
    "bc",
    "file",
    "xdg-utils",
    "util-linux",
    "procps",
)

APT_OPTIONAL: tuple[str, ...] = (
    "libgl1",
    "libglib2.0-0t64",
    "libsm6",
    "libxext6",
    "libxrender1",
    "libnuma1",
    "libpciaccess0",
    "libdrm2",
    "libelf1",
    "libgomp1",
    "libstdc++6",
    "p7zip-full",
    "clinfo",
)

# Ubuntu 24.04 renamed libglib2.0-0; keep the older name as a fallback.
APT_RENAMES: dict[str, tuple[str, ...]] = {
    "libglib2.0-0t64": ("libglib2.0-0",),
}

COMMANDS_REQUIRED: tuple[str, ...] = ("git", "curl", "jq", "flock", "sha256sum", "udisksctl")

SYSCTL_FILE = Path("/etc/sysctl.d/99-comfyui.conf")
SYSCTL_VALUES: dict[str, int] = {
    "vm.max_map_count": 1048576,
    "fs.inotify.max_user_watches": 524288,
}
SYSCTL_CONTENT = "# ComfyUI / large safetensors models\n" + "".join(
    f"{key} = {value}\n" for key, value in SYSCTL_VALUES.items()
)

UDEV_FILE = Path("/etc/udev/rules.d/99-comfyui-amd.rules")
UDEV_RULE_MARKER = "ComfyUI / ROCm"
UDEV_CONTENT = """# ComfyUI / ROCm access to AMD compute and render nodes
KERNEL=="kfd", GROUP="render", MODE="0660"
SUBSYSTEM=="drm", KERNEL=="renderD*", GROUP="render", MODE="0660"
SUBSYSTEM=="drm", KERNEL=="card[0-9]*", GROUP="video", MODE="0660"
"""

DEVTMP_MODE = "660"
RENDER_GROUPS: tuple[str, ...] = ("render", "video")

COMFYUI_REPO = "https://github.com/Comfy-Org/ComfyUI.git"
MANAGER_REPO = "https://github.com/Comfy-Org/ComfyUI-Manager.git"
COMFYUI_ORIGINS: tuple[str, ...] = ("Comfy-Org/ComfyUI", "comfyanonymous/ComfyUI")

ICON_SVG = """<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 128 128" width="128" height="128">
  <defs>
    <linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="#1b1f2a"/>
      <stop offset="1" stop-color="#0b0d12"/>
    </linearGradient>
    <linearGradient id="spark" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="#7ee787"/>
      <stop offset="0.5" stop-color="#58a6ff"/>
      <stop offset="1" stop-color="#bc8cff"/>
    </linearGradient>
  </defs>
  <rect x="4" y="4" width="120" height="120" rx="26" fill="url(#bg)" stroke="#30363d" stroke-width="2"/>
  <path d="M64 20 L74 48 L102 58 L74 68 L64 96 L54 68 L26 58 L54 48 Z" fill="url(#spark)" opacity="0.95"/>
  <circle cx="64" cy="58" r="7" fill="#0b0d12"/>
  <text x="64" y="112" font-family="DejaVu Sans, sans-serif" font-size="14" font-weight="bold"
        text-anchor="middle" fill="#c9d1d9">ComfyUI</text>
</svg>
"""

VERIFY_PORTS = tuple(range(18188, 18201))

# Content markers for expensive idempotent steps. Written by the executor only
# after a successful run, hashed content is compared by the planners.
STAMPS: dict[str, str] = {
    "pip": "pip.stamp",
    "torch": "torch.stamp",
    "requirements": "requirements.stamp",
    "manager": "manager.stamp",
}


def desktop_entry(config) -> str:
    """Render the .desktop file for the launcher."""
    exec_value = desktop_quote(str(config.launcher_path))
    icon_value = desktop_quote(str(config.icon_path))
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Version=1.0\n"
        "Name=ComfyUI\n"
        "GenericName=Image Generation\n"
        "Comment=Local ComfyUI (MOSS Image / diffusion models)\n"
        f"Exec={exec_value}\n"
        f"Path={config.install_dir}\n"
        f"Icon={icon_value}\n"
        "Terminal=true\n"
        "Categories=Graphics;2DGraphics;Qt;\n"
        "Keywords=ComfyUI;AI;image;diffusion;stable;generation;\n"
        "StartupNotify=true\n"
        "StartupWMClass=ComfyUI\n"
    )


def desktop_quote(value: str) -> str:
    if any(char in value for char in " \t\n\"'\\$`"):
        value = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{value}"'
    return value


def constraints_text(config) -> str:
    return (
        f"torch=={config.torch_version}\n"
        f"torchvision=={config.torchvision_version}\n"
        f"torchaudio=={config.torchaudio_version}\n"
    )


def python_module_path() -> Path:
    """Directory that must be on PYTHONPATH for ``-m comfyprov``."""
    return Path(__file__).resolve().parents[1]


_UNITS = {"k": 1024, "m": 1024**2, "g": 1024**3, "t": 1024**4}


def parse_size(text: str) -> int:
    """Parse ``16G`` / ``512M`` / ``1024`` into bytes."""
    raw = text.strip().lower()
    if not raw:
        raise ValueError("empty size")
    suffix = raw[-1]
    if suffix in _UNITS:
        number, unit = raw[:-1], _UNITS[suffix]
    else:
        number, unit = raw, 1
    try:
        value = float(number)
    except ValueError:
        raise ValueError(f"cannot parse size {text!r}") from None
    if value <= 0:
        raise ValueError(f"size must be positive: {text!r}")
    return int(value * unit)
