"""Single source of configuration: ``provision/config/provision.env``.

The env file is valid bash (``: "${VAR:=default}"``) and is sourced by the
thin shell layer, so paths and versions are never duplicated in Python. This
module parses the same file without executing it, applies the same
``environment wins over default`` rule, and validates the values.

The file is intentionally small: it holds *data*, not decisions.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "provision.env"

_DEFAULT_RE = re.compile(r'^:\s*"\$\{([A-Za-z_][A-Za-z0-9_]*):=(.*)\}"\s*$')
_ASSIGN_RE = re.compile(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
_REF_RE = re.compile(r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))")
_GFX_RE = re.compile(r"^(auto|gfx[0-9a-f]{3,4})$")
_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off", ""}


class ConfigError(RuntimeError):
    """Raised when the env file is missing, malformed or fails validation."""


def _expand(value: str, values: Mapping[str, str], environ: Mapping[str, str]) -> str:
    def repl(match: re.Match[str]) -> str:
        name = match.group(1) or match.group(2)
        if name in values:
            return values[name]
        if name in environ:
            return environ[name]
        raise ConfigError(f"undefined variable ${name} in value {value!r}")

    for _ in range(8):
        expanded = _REF_RE.sub(repl, value)
        if expanded == value:
            return expanded
        value = expanded
    raise ConfigError(f"recursive variable expansion in value {value!r}")


def parse_env(text: str, environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """Parse the env file into a plain dict.

    Both supported forms carry a default; the process environment overrides it,
    matching bash's ``: "${VAR:=default}"``.
    """
    # Bash sees the whole process environment, so mirror that: a passed
    # mapping overrides os.environ, it does not replace it.
    env = dict(os.environ)
    if environ is not None:
        env.update(environ)
    values: dict[str, str] = {}
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        default = _DEFAULT_RE.match(line)
        assign = _ASSIGN_RE.match(line)
        if default:
            name, template = default.group(1), default.group(2)
        elif assign:
            name, template = assign.group(1), assign.group(2)
        else:
            raise ConfigError(f"line {lineno}: cannot parse {raw!r}")
        value = env.get(name, template)
        values[name] = _expand(value.strip().strip('"'), values, env)
    return values


def _as_bool(key: str, value: str) -> bool:
    lowered = value.strip().lower()
    if lowered in _TRUE:
        return True
    if lowered in _FALSE:
        return False
    raise ConfigError(f"{key}={value!r} is not a boolean (use 0 or 1)")


@dataclass(frozen=True)
class Config:
    """Validated, typed view over the env file."""

    values: Mapping[str, str]
    path: Path

    # -- generic accessors ---------------------------------------------------
    def get(self, key: str, default: str | None = None) -> str:
        if key in self.values:
            return self.values[key]
        if default is None:
            raise ConfigError(f"{self.path}: missing required setting {key}")
        return default

    def integer(self, key: str, default: int | None = None) -> int:
        raw = self.get(key, None if default is None else str(default))
        try:
            return int(raw)
        except ValueError:
            raise ConfigError(f"{key}={raw!r} is not an integer") from None

    def number(self, key: str, default: float | None = None) -> float:
        raw = self.get(key, None if default is None else str(default))
        try:
            return float(raw)
        except ValueError:
            raise ConfigError(f"{key}={raw!r} is not a number") from None

    def boolean(self, key: str, default: bool = False) -> bool:
        raw = self.get(key, "1" if default else "0")
        return _as_bool(key, raw)

    def directory(self, key: str) -> Path:
        return Path(self.get(key))

    # -- paths ---------------------------------------------------------------
    @property
    def install_dir(self) -> Path:
        return self.directory("INSTALL_DIR")

    @property
    def comfyui_dir(self) -> Path:
        return self.directory("COMFYUI_DIR")

    @property
    def venv_dir(self) -> Path:
        return self.directory("VENV_DIR")

    @property
    def venv_python(self) -> Path:
        return self.venv_dir / "bin" / "python"

    @property
    def venv_pip(self) -> Path:
        return self.venv_dir / "bin" / "pip"

    @property
    def log_dir(self) -> Path:
        return self.directory("LOG_DIR")

    @property
    def output_dir(self) -> Path:
        return self.directory("OUTPUT_DIR")

    @property
    def input_dir(self) -> Path:
        return self.directory("INPUT_DIR")

    @property
    def temp_dir(self) -> Path:
        return self.directory("TEMP_DIR")

    @property
    def user_dir(self) -> Path:
        return self.directory("USER_DIR")

    @property
    def big_mount(self) -> Path:
        return self.directory("BIG_MOUNT")

    @property
    def big_uuid(self) -> str:
        return self.get("BIG_UUID")

    @property
    def big_device(self) -> str:
        return self.get("BIG_DEVICE", f"/dev/disk/by-uuid/{self.big_uuid}")

    @property
    def models_dir(self) -> Path:
        return self.directory("MODELS_DIR")

    @property
    def config_home(self) -> Path:
        return self.directory("CONFIG_HOME")

    @property
    def civitai_token_file(self) -> Path:
        return self.directory("CIVITAI_TOKEN_FILE")

    @property
    def hf_token_file(self) -> Path:
        return self.directory("HF_TOKEN_FILE")

    @property
    def state_dir(self) -> Path:
        return self.install_dir / "provision" / ".state"

    @property
    def launcher_path(self) -> Path:
        return self.install_dir / "start-comfyui.sh"

    @property
    def icon_path(self) -> Path:
        return self.install_dir / "icon.svg"

    @property
    def applications_dir(self) -> Path:
        return Path(self.get("APPLICATIONS_DIR", str(Path.home() / ".local/share/applications")))

    @property
    def desktop_entry_path(self) -> Path:
        return self.applications_dir / "comfyui.desktop"

    # -- versions ------------------------------------------------------------
    @property
    def comfyui_ref(self) -> str:
        return self.get("COMFYUI_REF")

    @property
    def manager_ref(self) -> str:
        return self.get("MANAGER_REF")

    @property
    def gpu_gfx(self) -> str:
        return self.get("GPU_GFX")

    @property
    def torch_index(self) -> str:
        return self.get("TORCH_INDEX")

    @property
    def torch_version(self) -> str:
        return self.get("TORCH_VERSION")

    @property
    def torchvision_version(self) -> str:
        return self.get("TORCHVISION_VERSION")

    @property
    def torchaudio_version(self) -> str:
        return self.get("TORCHAUDIO_VERSION")

    @property
    def rocm_version(self) -> str:
        return self.get("ROCM_VERSION")

    @property
    def torch_tag(self) -> str:
        return f"{self.torch_version}+rocm{self.rocm_version}"

    @property
    def torchvision_tag(self) -> str:
        return f"{self.torchvision_version}+rocm{self.rocm_version}"

    @property
    def torchaudio_tag(self) -> str:
        return f"{self.torchaudio_version}+rocm{self.rocm_version}"

    # -- runtime -------------------------------------------------------------
    @property
    def listen(self) -> str:
        return self.get("COMFYUI_LISTEN")

    @property
    def port(self) -> int:
        return self.integer("COMFYUI_PORT")

    @property
    def reserve_vram(self) -> str:
        return self.get("COMFYUI_RESERVE_VRAM")

    @property
    def lowvram(self) -> bool:
        return self.boolean("COMFYUI_LOWVRAM")

    @property
    def log_level(self) -> str:
        return self.get("COMFYUI_LOG_LEVEL", "INFO")

    @property
    def debug_log(self) -> bool:
        return self.boolean("COMFYUI_DEBUG_LOG", True)

    @property
    def log_sync_interval(self) -> int:
        return self.integer("LOG_SYNC_INTERVAL", 5)

    @property
    def extra_args(self) -> tuple[str, ...]:
        raw = self.get("COMFYUI_EXTRA_ARGS", "")
        return tuple(part for part in raw.split() if part)

    # -- black box -----------------------------------------------------------
    @property
    def comfyui_vitals(self) -> bool:
        return self.boolean("COMFYUI_VITALS", True)

    @property
    def vitals_interval(self) -> float:
        return self.number("VITALS_INTERVAL", 1.0)

    @property
    def vitals_max_mb(self) -> int:
        return self.integer("VITALS_MAX_MB", 8)

    @property
    def vitals_guard_avail_mb(self) -> int:
        return self.integer("VITALS_GUARD_AVAIL_MB", 1024)

    @property
    def comfyui_disable_pinned_memory(self) -> bool:
        return self.boolean("COMFYUI_DISABLE_PINNED_MEMORY", False)

    # -- behaviour -----------------------------------------------------------
    @property
    def install_manager(self) -> bool:
        return self.boolean("INSTALL_MANAGER")

    @property
    def create_desktop_icon(self) -> bool:
        return self.boolean("CREATE_DESKTOP_ICON")

    @property
    def enable_crash_capture(self) -> bool:
        return self.boolean("ENABLE_CRASH_CAPTURE", True)

    @property
    def enable_swap(self) -> bool:
        return self.boolean("ENABLE_SWAP")

    @property
    def swap_size(self) -> str:
        return self.get("SWAP_SIZE")

    @property
    def swap_file(self) -> Path:
        return Path(self.get("SWAP_FILE"))

    # -- reporting -----------------------------------------------------------
    def describe(self) -> str:
        return f"ComfyUI {self.comfyui_ref}, Manager {self.manager_ref}, torch {self.torch_tag}, gfx {self.gpu_gfx}"


_REQUIRED = (
    "INSTALL_DIR",
    "COMFYUI_DIR",
    "VENV_DIR",
    "LOG_DIR",
    "OUTPUT_DIR",
    "INPUT_DIR",
    "TEMP_DIR",
    "USER_DIR",
    "BIG_MOUNT",
    "BIG_UUID",
    "MODELS_DIR",
    "CONFIG_HOME",
    "CIVITAI_TOKEN_FILE",
    "HF_TOKEN_FILE",
    "COMFYUI_REF",
    "MANAGER_REF",
    "GPU_GFX",
    "TORCH_INDEX",
    "TORCH_VERSION",
    "TORCHVISION_VERSION",
    "TORCHAUDIO_VERSION",
    "ROCM_VERSION",
    "COMFYUI_LISTEN",
    "COMFYUI_PORT",
    "COMFYUI_RESERVE_VRAM",
    "COMFYUI_LOWVRAM",
    "COMFYUI_LOG_LEVEL",
    "COMFYUI_DEBUG_LOG",
    "LOG_SYNC_INTERVAL",
    "INSTALL_MANAGER",
    "CREATE_DESKTOP_ICON",
    "ENABLE_CRASH_CAPTURE",
    "ENABLE_SWAP",
    "SWAP_SIZE",
    "SWAP_FILE",
)


def validate(config: Config) -> None:
    """Reject values that would fail confusingly much later."""
    missing = [key for key in _REQUIRED if key not in config.values]
    if missing:
        raise ConfigError(f"{config.path}: missing settings: {', '.join(missing)}")

    for key in (
        "INSTALL_DIR",
        "COMFYUI_DIR",
        "VENV_DIR",
        "LOG_DIR",
        "OUTPUT_DIR",
        "INPUT_DIR",
        "TEMP_DIR",
        "USER_DIR",
        "BIG_MOUNT",
        "MODELS_DIR",
        "CONFIG_HOME",
        "SWAP_FILE",
    ):
        if not config.get(key).startswith("/"):
            raise ConfigError(f"{key} must be an absolute path (got {config.get(key)!r})")

    if not _GFX_RE.match(config.gpu_gfx):
        raise ConfigError(f"GPU_GFX must be 'auto' or a gfx target such as gfx1031 (got {config.gpu_gfx!r})")

    port = config.port
    if not 1 <= port <= 65535:
        raise ConfigError(f"COMFYUI_PORT out of range: {port}")

    if not config.get("COMFYUI_LISTEN").startswith("127.") and config.get("COMFYUI_LISTEN") != "localhost":
        raise ConfigError("COMFYUI_LISTEN must stay on localhost (127.0.0.1); remote exposure is not supported")

    if not str(config.models_dir).startswith(str(config.big_mount) + "/"):
        raise ConfigError(f"MODELS_DIR ({config.models_dir}) must live inside BIG_MOUNT ({config.big_mount})")

    for key in ("COMFYUI_REF", "MANAGER_REF", "TORCH_VERSION", "ROCM_VERSION"):
        if not config.get(key).strip():
            raise ConfigError(f"{key} must not be empty")

    config.boolean("COMFYUI_LOWVRAM")
    config.boolean("COMFYUI_DEBUG_LOG")
    config.boolean("INSTALL_MANAGER")
    config.boolean("CREATE_DESKTOP_ICON")
    config.boolean("ENABLE_CRASH_CAPTURE")
    config.boolean("ENABLE_SWAP")

    if config.log_sync_interval < 0:
        raise ConfigError(f"LOG_SYNC_INTERVAL must be >= 0 (got {config.log_sync_interval})")


def load(path: str | Path | None = None, environ: Mapping[str, str] | None = None) -> Config:
    """Load and validate the configuration; raises ConfigError on problems."""
    target = Path(path) if path is not None else CONFIG_PATH
    try:
        text = target.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read {target}: {exc}") from exc
    config = Config(values=parse_env(text, environ), path=target)
    validate(config)
    return config
