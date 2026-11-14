"""A single read-only snapshot of the machine.

``collect()`` performs all probing once; planners and reports then work on
plain frozen data. No planner touches the filesystem, which is what makes the
plans testable and the dry-run honest.
"""

from __future__ import annotations

import platform
from dataclasses import dataclass
from pathlib import Path

from . import gpu, layout
from .config import Config
from .probe import Probe

GIB = 1024**3


@dataclass(frozen=True)
class CheckoutFact:
    path: Path
    exists: bool
    is_git: bool
    origin: str
    head: str
    clean: bool

    @property
    def installed(self) -> bool:
        return self.exists and self.is_git


@dataclass(frozen=True)
class Facts:
    user: str
    uid: int
    arch: str
    kernel: str
    os_id: str
    os_pretty: str
    os_like: str
    python312: str | None
    tools: frozenset[str]
    packages: frozenset[str]
    apt_available: frozenset[str]
    groups: tuple[gpu.GroupFact, ...]
    kfd_present: bool
    kfd_accessible: bool
    gfx: str | None
    gfx_source: str
    render_nodes: tuple[str, ...]
    card_nodes: tuple[str, ...]
    device_perms: dict[str, tuple[str, str] | None]
    big_mounted: bool
    install_free_gb: int | None
    models_free_gb: int | None
    models_dir_exists: bool
    sysctl: dict[str, int]
    files: dict[str, str | None]
    stamps: dict[str, str | None]
    checkouts: dict[str, CheckoutFact]
    venv_python: str | None
    venv_version: str | None
    torch_versions: dict[str, str]
    swap_active: frozenset[str]
    fstab: str
    desktop_shortcut: Path
    pstore: tuple[str, ...]
    dirs: frozenset[str]
    pstore_enabled: bool
    watchdog_present: bool
    isa_bridge: str
    acpi_wdat: bool

    # -- helpers -------------------------------------------------------------
    def group(self, name: str) -> gpu.GroupFact | None:
        for fact in self.groups:
            if fact.name == name:
                return fact
        return None

    def file(self, key: str) -> str | None:
        return self.files.get(key)

    def dir_exists(self, path) -> bool:
        return str(path) in self.dirs

    def stamp(self, key: str) -> str | None:
        return self.stamps.get(key)

    def checkout(self, key: str) -> CheckoutFact:
        return self.checkouts[key]

    @property
    def os_label(self) -> str:
        return self.os_pretty or self.os_id or "unknown"

    @property
    def missing_python(self) -> bool:
        return self.python312 is None


def _os_release(probe: Probe) -> tuple[str, str, str]:
    text = probe.read_text("/etc/os-release") or ""
    values: dict[str, str] = {}
    for line in text.splitlines():
        if "=" not in line:
            continue
        key, _, raw = line.partition("=")
        values[key.strip()] = raw.strip().strip('"')
    return values.get("ID", ""), values.get("PRETTY_NAME", ""), values.get("ID_LIKE", "")


def _checkout(probe: Probe, path: Path) -> CheckoutFact:
    exists = probe.exists(path)
    is_git = probe.is_dir(path / ".git")
    origin = ""
    head = ""
    clean = True
    if is_git:
        remote = probe.run(["git", "-C", str(path), "remote", "get-url", "origin"])
        origin = remote.out.strip() if remote.ok else ""
        rev = probe.run(["git", "-C", str(path), "rev-parse", "HEAD"])
        head = rev.out.strip() if rev.ok else ""
        status = probe.run(["git", "-C", str(path), "status", "--porcelain"])
        clean = status.ok and not status.out.strip()
    return CheckoutFact(path=path, exists=exists, is_git=is_git, origin=origin, head=head, clean=clean)


def _torch_versions(probe: Probe, config: Config) -> dict[str, str]:
    pip = config.venv_pip
    if not probe.is_file(pip):
        return {}
    result = probe.run([str(pip), "show", "torch", "torchvision", "torchaudio"], timeout=60)
    if not result.ok:
        return {}
    versions: dict[str, str] = {}
    name = ""
    for line in result.out.splitlines():
        if line.startswith("Name: "):
            name = line.split(": ", 1)[1].strip()
        elif line.startswith("Version: ") and name:
            versions[name] = line.split(": ", 1)[1].strip()
    return versions


def _python312(probe: Probe) -> str | None:
    for candidate in ("python3.12", "python3"):
        resolved = probe.which(candidate)
        if not resolved:
            continue
        result = probe.run([resolved, "-c", "import sys; print('%d.%d' % sys.version_info[:2])"])
        if result.ok and result.out.strip() == "3.12":
            return resolved
    return None


def rev_parse(probe: Probe, path: Path, ref: str) -> str | None:
    """Commit id of ``ref`` in a local checkout, or None when not fetched yet."""
    if not ref:
        return None
    result = probe.run(["git", "-C", str(path), "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"])
    return result.out.strip() if result.ok and result.out.strip() else None


def local_tags(probe: Probe, path: Path) -> tuple[str, ...]:
    result = probe.run(["git", "-C", str(path), "tag", "--sort=-v:refname"])
    return tuple(line.strip() for line in result.out.splitlines() if line.strip()) if result.ok else ()


def collect(config: Config, probe: Probe | None = None) -> Facts:
    probe = probe or Probe()
    os_id, os_pretty, os_like = _os_release(probe)

    session = probe.session_groups
    groups = tuple(
        gpu.GroupFact(
            name=name,
            exists=probe.group_exists(name),
            members=probe.group_members(name),
            in_session=name in session,
        )
        for name in layout.RENDER_GROUPS
    )

    kfd_present = probe.exists(gpu.KFD_PATH)
    kfd_accessible = probe.accessible(gpu.KFD_PATH) if kfd_present else False

    detected = gpu.gfx_from_topology(probe)
    if config.gpu_gfx != "auto":
        gfx, gfx_source = config.gpu_gfx, "config"
    else:
        gfx, gfx_source = detected, "kfd" if detected else "none"

    install_free = probe.free_bytes(config.install_dir)
    models_free = probe.free_bytes(config.big_mount)
    big_mounted = probe.is_mounted(config.big_mount)

    sysctl = probe.sysctl_values({**layout.SYSCTL_VALUES, **layout.CRASH_SYSCTL_VALUES})
    state = config.state_dir
    stamps = {name: probe.read_text(state / filename) for name, filename in layout.STAMPS.items()}
    shortcut = desktop_shortcut(probe)
    isa_bridge = ""
    for line in probe.run(["lspci"], timeout=20).out.splitlines():
        if "ISA bridge" in line:
            isa_bridge = line
            break
    acpi_wdat = any(name.startswith("WDAT") for name in probe.listdir("/sys/bus/acpi/devices"))

    pstore_state = probe.run(["systemctl", "is-enabled", layout.PSTORE_UNIT], timeout=15)
    pstore_enabled = pstore_state.ok and pstore_state.out.strip() in layout.PSTORE_ENABLED_STATES
    files = {
        "sysctl": probe.read_text(layout.SYSCTL_FILE),
        "crash_sysctl": probe.read_text(layout.CRASH_SYSCTL_FILE),
        "journald": probe.read_text(layout.JOURNALD_DROPIN),
        "watchdog": probe.read_text(layout.WATCHDOG_DROPIN),
        "modules_load": probe.read_text(layout.MODULES_LOAD_FILE),
        "rsyslog": probe.read_text(layout.RSYSLOG_DROPIN),
        "udev": probe.read_text(layout.UDEV_FILE),
        "force_watchdog_service": probe.read_text(layout.FORCE_WATCHDOG_SERVICE),
        "force_watchdog_override": probe.read_text(layout.FORCE_WATCHDOG_DIR / "override.conf"),
        "netconsole_module": probe.read_text(layout.NETCONSOLE_MODULE_CONF),
        "netconsole_conf": probe.read_text(layout.NETCONSOLE_CONF),
        "ramoops_module": probe.read_text(layout.RAMOOPS_MODULE_CONF),
        "ramoops_conf": probe.read_text(layout.RAMOOPS_CONF),
        "crash_doctor": probe.read_text(layout.CRASH_DOCTOR),
        "log_sync": probe.read_text(layout.LOG_SYNC_SCRIPT),
        "constraints": probe.read_text(state / "constraints.txt"),
        "icon": probe.read_text(config.icon_path),
        "desktop_entry": probe.read_text(config.desktop_entry_path),
        "desktop_shortcut": probe.read_text(shortcut),
        "civitai_token": probe.read_text(config.civitai_token_file),
    }

    venv_python = str(config.venv_python) if probe.is_file(config.venv_python) else None
    venv_version = None
    if venv_python:
        result = probe.run([venv_python, "-c", "import sys; print('%d.%d' % sys.version_info[:2])"], timeout=30)
        venv_version = result.out.strip() if result.ok else None

    render = gpu.render_nodes(probe)
    cards = gpu.card_nodes(probe)
    device_paths = [gpu.KFD_PATH, *render, *cards]

    return Facts(
        user=probe.user,
        uid=probe.uid,
        arch=platform.machine(),
        kernel=platform.release(),
        os_id=os_id,
        os_pretty=os_pretty,
        os_like=os_like,
        python312=_python312(probe),
        tools=frozenset(cmd for cmd in layout.COMMANDS_REQUIRED if probe.command_available(cmd)),
        packages=probe.packages_installed(),
        apt_available=probe.apt_available(),
        groups=groups,
        kfd_present=kfd_present,
        kfd_accessible=kfd_accessible,
        gfx=gfx,
        gfx_source=gfx_source,
        render_nodes=render,
        card_nodes=cards,
        device_perms={path: probe.node_perms(path) for path in device_paths},
        big_mounted=big_mounted,
        install_free_gb=None if install_free is None else install_free // GIB,
        models_free_gb=None if models_free is None else models_free // GIB,
        models_dir_exists=probe.is_dir(config.models_dir),
        sysctl=sysctl,
        files=files,
        stamps=stamps,
        checkouts={
            "comfyui": _checkout(probe, config.comfyui_dir),
            "manager": _checkout(probe, config.comfyui_dir / "custom_nodes" / "ComfyUI-Manager"),
        },
        venv_python=venv_python,
        venv_version=venv_version,
        torch_versions=_torch_versions(probe, config),
        swap_active=probe.swap_active(),
        fstab=probe.read_text("/etc/fstab") or "",
        desktop_shortcut=shortcut,
        pstore=tuple(probe.listdir(layout.PSTORE_DIR)),
        dirs=frozenset(
            str(path)
            for path in (
                layout.JOURNALD_DROPIN.parent,
                layout.RSYSLOG_DROPIN.parent,
                layout.CRASH_SYSCTL_FILE.parent,
                layout.FORCE_WATCHDOG_DIR,
                layout.CRASH_DOCTOR_DIR,
            )
            if probe.is_dir(path)
        ),
        pstore_enabled=pstore_enabled,
        watchdog_present=bool(probe.listdir("/sys/class/watchdog")),
        isa_bridge=isa_bridge,
        acpi_wdat=acpi_wdat,
    )


def desktop_shortcut(probe: Probe) -> Path:
    """XDG desktop directory, falling back to ~/Desktop."""
    result = probe.run(["xdg-user-dir", "DESKTOP"], timeout=15)
    if result.ok and result.out.strip():
        return Path(result.out.strip()) / "comfyui.desktop"
    home = Path(probe.environ.get("HOME") or Path.home())
    return home / "Desktop" / "comfyui.desktop"
