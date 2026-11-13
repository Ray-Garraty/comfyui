"""Test doubles: a fake host and a fake HTTP client.

The whole point of the comfyprov package is that planning can be exercised
without root, without an AMD GPU and without network access.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from comfyprov import config as config_module
from comfyprov import layout
from comfyprov.probe import Probe, RunResult


class FakeRunner:
    """Scripted command runner: matches by substring, records every call."""

    def __init__(self, responses=None, default: RunResult | None = None) -> None:
        self.responses = list(responses or [])
        self.calls: list[str] = []
        self.default = default or RunResult(0, "", "")

    def __call__(self, argv, timeout=180, env=None) -> RunResult:
        command = " ".join(str(item) for item in argv)
        self.calls.append(command)
        for fragment, result in self.responses:
            if fragment in command:
                return result
        return self.default


class FakeProbe(Probe):
    """A host described by dictionaries instead of a real filesystem."""

    def __init__(
        self,
        files: dict | None = None,
        dirs=None,
        symlinks: dict | None = None,
        modes: dict | None = None,
        perms: dict | None = None,
        accessible=(),
        listdir_map: dict | None = None,
        packages=(),
        apt=(),
        sysctl: dict | None = None,
        mounted=(),
        swap=(),
        session_groups=(),
        user: str = "vlabe",
        uid: int = 1000,
        free_gb: int = 500,
        runner=None,
        which_map: dict | None = None,
        home: str = "/home/vlabe",
        sizes: dict | None = None,
    ) -> None:
        super().__init__(
            root="/",
            runner=runner or FakeRunner(),
            environ={"PATH": "/usr/bin:/bin", "HOME": home},
            session_groups=session_groups,
            uid=uid,
            user=user,
            free_bytes_fn=lambda _path: free_gb * 1024**3,
        )
        self.files = {str(key): value for key, value in (files or {}).items()}
        self.dirs = {str(item) for item in (dirs or [])} | {"/"}
        self.symlinks = {str(key): value for key, value in (symlinks or {}).items()}
        self.modes = {str(key): value for key, value in (modes or {}).items()}
        self.perms = {str(key): value for key, value in (perms or {}).items()}
        self.accessible_paths = {str(item) for item in accessible}
        self.listdir_map = {str(key): list(value) for key, value in (listdir_map or {}).items()}
        self.installed = frozenset(packages)
        self.available = frozenset(apt)
        self.sysctl_map = dict(sysctl or {})
        self.mounted_paths = {str(item) for item in mounted}
        self.swap_paths = frozenset(str(item) for item in swap)
        self.size_map = {str(key): value for key, value in (sizes or {}).items()}
        self.which_map = {
            name: f"/usr/bin/{name}"
            for name in ("git", "curl", "jq", "flock", "sha256sum", "udisksctl", "python3.12", "python3")
        } | dict(which_map or {})

    # -- filesystem ----------------------------------------------------------
    def read_text(self, path):
        return self.files.get(str(path))

    def read_bytes(self, path):
        text = self.files.get(str(path))
        return None if text is None else text.encode()

    def exists(self, path):
        key = str(path)
        return key in self.files or key in self.dirs or key in self.symlinks or key in self.perms

    def is_file(self, path):
        return str(path) in self.files

    def is_dir(self, path):
        return str(path) in self.dirs

    def is_symlink(self, path):
        return str(path) in self.symlinks

    def readlink(self, path):
        target = self.symlinks.get(str(path))
        return target

    def listdir(self, path):
        return list(self.listdir_map.get(str(path), []))

    def file_mode(self, path):
        key = str(path)
        if key in self.modes:
            return self.modes[key]
        return "755" if key in self.dirs else "644"

    def node_perms(self, path):
        return self.perms.get(str(path))

    def accessible(self, path):
        return str(path) in self.accessible_paths

    def is_mounted(self, path):
        return str(path) in self.mounted_paths

    def file_size(self, path):
        return self.size_map.get(str(path))

    # -- system data ---------------------------------------------------------
    def packages_installed(self):
        return self.installed

    def apt_available(self):
        return self.available

    def sysctl_values(self, names):
        return {name: self.sysctl_map[name] for name in names if name in self.sysctl_map}

    def swap_active(self):
        return self.swap_paths

    def which(self, command):
        return self.which_map.get(command)

    def run(self, argv, timeout=180.0, env=None):
        return self._runner(list(argv), timeout=timeout, env=env)


@dataclass
class FakeResponse:
    body: str = "{}"
    status: int = 200


class FakeClient:
    """Stands in for comfyprov.server.HttpClient in verification tests."""

    def __init__(self, routes: dict | None = None, missing=()) -> None:
        self.routes = dict(routes or {})
        self.missing = tuple(missing)
        self.calls: list[str] = []

    def _lookup(self, url: str) -> FakeResponse:
        self.calls.append(url)
        for fragment in self.missing:
            if fragment in url:
                from comfyprov.server import HttpError

                raise HttpError(f"HTTP 404 for {url}")
        for fragment, response in self.routes.items():
            if fragment in url:
                return response
        from comfyprov.server import HttpError

        raise HttpError(f"HTTP 404 for {url}")

    def get_json(self, url, timeout=None):
        import json

        return json.loads(self._lookup(url).body or "{}")

    def get_text(self, url, timeout=None):
        return self._lookup(url).body

    def post_json(self, url, payload, timeout=None):
        import json

        return json.loads(self._lookup(url).body or "{}")


@dataclass
class Clock:
    now: float = 0.0
    sleep_calls: list = field(default_factory=list)

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleep_calls.append(seconds)
        self.now += seconds


def make_config(tmp_path: Path) -> config_module.Config:
    """Load the real env file with every path redirected into a temp dir."""
    tmp_path = Path(tmp_path)
    environ = {
        "HOME": str(tmp_path),
        "INSTALL_DIR": str(tmp_path / "app"),
        "BIG_MOUNT": str(tmp_path / "BIG"),
        "MODELS_DIR": str(tmp_path / "BIG" / "comfyui" / "models"),
        "CONFIG_HOME": str(tmp_path / "config"),
        "CIVITAI_TOKEN_FILE": str(tmp_path / "config" / "civitai.token"),
        "HF_TOKEN_FILE": str(tmp_path / "config" / "hf.token"),
        "APPLICATIONS_DIR": str(tmp_path / "applications"),
        "LOG_DIR": str(tmp_path / "app" / "logs"),
        "OUTPUT_DIR": str(tmp_path / "app" / "output"),
        "INPUT_DIR": str(tmp_path / "app" / "input"),
        "TEMP_DIR": str(tmp_path / "app" / "temp"),
        "USER_DIR": str(tmp_path / "app" / "user"),
        "SWAP_FILE": str(tmp_path / "swapfile"),
    }
    return config_module.load(environ=environ)


OS_RELEASE = """ID=linuxmint
ID_LIKE="ubuntu debian"
PRETTY_NAME="Linux Mint 22.3"
"""
GROUP_FILE_BARE = "render:x:992:\nvideo:x:44:\n"
GROUP_FILE_MEMBER = "render:x:992:vlabe\nvideo:x:44:vlabe\n"
TOPOLOGY = "gfx_target_version 100301\n"


def base_files(config, groups: str = GROUP_FILE_BARE) -> dict:
    return {
        "/etc/os-release": OS_RELEASE,
        "/etc/group": groups,
        "/etc/fstab": "",
        "/proc/sys/vm/max_map_count": "65530\n",
        "/proc/sys/fs/inotify/max_user_watches": "8192\n",
        "/sys/class/kfd/kfd/topology/nodes/0/properties": TOPOLOGY,
    }


def base_dirs(config) -> set:
    return {str(config.install_dir), "/dev", "/dev/dri", "/sys", "/sys/class"}


def base_listdir(config) -> dict:
    return {
        "/dev/dri": ["card1", "renderD128"],
        "/sys/class/kfd/kfd/topology/nodes": ["0"],
        str(config.install_dir): [],
        str(config.models_dir): [],
    }


def base_perms() -> dict:
    return {
        "/dev/kfd": ("root", "660"),
        "/dev/dri/renderD128": ("root", "660"),
        "/dev/dri/card1": ("root", "660"),
    }


def clean_host(config, **overrides) -> FakeProbe:
    """A freshly installed Linux Mint with an untouched AMD GPU."""
    kwargs = {
        "files": base_files(config),
        "dirs": base_dirs(config),
        "perms": base_perms(),
        "listdir_map": base_listdir(config),
        "apt": frozenset(layout.APT_REQUIRED + layout.APT_OPTIONAL),
        "sysctl": {"vm.max_map_count": 65530, "fs.inotify.max_user_watches": 8192},
        "mounted": (),
        "session_groups": ("vlabe",),
        "runner": FakeRunner(),
    }
    kwargs.update(overrides)
    return FakeProbe(**kwargs)


def configured_host(config, accessible: bool = True, session_groups=("vlabe", "render", "video")) -> FakeProbe:
    """A host where every GPU prerequisite is already in place."""
    files = base_files(config, groups=GROUP_FILE_MEMBER)
    files["/etc/sysctl.d/99-comfyui.conf"] = layout.SYSCTL_CONTENT
    files["/etc/udev/rules.d/99-comfyui-amd.rules"] = layout.UDEV_CONTENT
    perms = {
        "/dev/kfd": ("render", "660"),
        "/dev/dri/renderD128": ("render", "660"),
        "/dev/dri/card1": ("video", "660"),
    }
    dirs = base_dirs(config) | {str(config.config_home)}
    return FakeProbe(
        files=files,
        dirs=dirs,
        perms=perms,
        accessible=("/dev/kfd",) if accessible else (),
        listdir_map=base_listdir(config),
        packages=frozenset(layout.APT_REQUIRED + layout.APT_OPTIONAL),
        apt=frozenset(layout.APT_REQUIRED + layout.APT_OPTIONAL),
        sysctl=dict(layout.SYSCTL_VALUES),
        mounted=(str(config.big_mount),),
        session_groups=session_groups,
        modes={str(config.config_home): "700"},
        runner=FakeRunner(),
    )


# -- image fixtures ----------------------------------------------------------
def png_bytes(width: int, height: int, gradient: bool = False, level: int = 9) -> bytes:
    """A valid 8-bit RGB PNG built with zlib only.

    ``gradient=False`` produces a uniform (blank) frame: a valid file with no
    content, which is exactly what the output validator has to reject.
    """
    import struct
    import zlib

    raw = bytearray()
    for y in range(height):
        raw.append(0)  # filter type 0
        for x in range(width):
            if gradient:
                raw += bytes((x * 255 // max(width - 1, 1), y * 255 // max(height - 1, 1), 128))
            else:
                raw += b"\x00\x00\x00"

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return struct.pack(">I", len(payload)) + tag + payload + struct.pack(">I", zlib.crc32(tag + payload))

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(bytes(raw), level))
        + chunk(b"IEND", b"")
    )
