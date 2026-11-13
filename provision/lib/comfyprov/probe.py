"""Read-only access to the machine.

Every planner decision goes through this object, which makes the logic
testable: tests replace it with a fake that serves canned files, command
outputs and permission bits, so no root, no AMD GPU and no network are needed.

``Probe`` never writes, never calls sudo and never mutates state.
"""

from __future__ import annotations

import grp
import os
import shutil
import subprocess
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

_STATUS_PATH = "/var/lib/dpkg/status"


@dataclass(frozen=True)
class RunResult:
    code: int
    out: str = ""
    err: str = ""

    @property
    def ok(self) -> bool:
        return self.code == 0


def _real_run(argv: Sequence[str], timeout: float = 180.0, env: Mapping[str, str] | None = None) -> RunResult:
    try:
        completed = subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            timeout=timeout,
            env=dict(env) if env is not None else None,
            check=False,
        )
    except FileNotFoundError as exc:
        return RunResult(127, "", str(exc))
    except subprocess.TimeoutExpired:
        return RunResult(124, "", f"timeout after {timeout:.0f}s: {' '.join(argv)}")
    return RunResult(completed.returncode, completed.stdout, completed.stderr)


class Probe:
    """Root-aware, side-effect-free view of the host."""

    def __init__(
        self,
        root: str | Path = "/",
        runner=None,
        environ: Mapping[str, str] | None = None,
        session_groups: Iterable[str] | None = None,
        uid: int | None = None,
        user: str | None = None,
        free_bytes_fn=None,
    ) -> None:
        self.root = Path(root)
        self._runner = runner or _real_run
        self.environ = dict(os.environ if environ is None else environ)
        self._session_groups = None if session_groups is None else frozenset(session_groups)
        self._uid = uid
        self._user = user
        self._free_bytes_fn = free_bytes_fn

    # -- filesystem ----------------------------------------------------------
    def local(self, path: str | Path) -> Path:
        candidate = Path(path)
        if candidate.is_absolute():
            return self.root / candidate.relative_to("/")
        return self.root / candidate

    def read_text(self, path: str | Path) -> str | None:
        try:
            return self.local(path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None

    def read_bytes(self, path: str | Path) -> bytes | None:
        try:
            return self.local(path).read_bytes()
        except OSError:
            return None

    def exists(self, path: str | Path) -> bool:
        return self.local(path).exists()

    def is_file(self, path: str | Path) -> bool:
        return self.local(path).is_file()

    def is_dir(self, path: str | Path) -> bool:
        return self.local(path).is_dir()

    def is_symlink(self, path: str | Path) -> bool:
        return self.local(path).is_symlink()

    def readlink(self, path: str | Path) -> str | None:
        try:
            return os.readlink(self.local(path))
        except OSError:
            return None

    def resolve(self, path: str | Path) -> Path | None:
        try:
            return self.local(path).resolve()
        except OSError:
            return None

    def stat(self, path: str | Path):
        try:
            return self.local(path).stat()
        except OSError:
            return None

    def listdir(self, path: str | Path) -> list[str]:
        try:
            return sorted(os.listdir(self.local(path)))
        except OSError:
            return []

    def file_mode(self, path: str | Path) -> str | None:
        stat = self.stat(path)
        return None if stat is None else format(stat.st_mode & 0o777, "03o")

    def group_name(self, gid: int) -> str:
        for line in (self.read_text("/etc/group") or "").splitlines():
            parts = line.split(":")
            if len(parts) >= 4 and parts[2].isdigit() and int(parts[2]) == gid:
                return parts[0]
        try:
            return grp.getgrgid(gid).gr_name
        except KeyError:
            return str(gid)

    def node_perms(self, path: str | Path) -> tuple[str, str] | None:
        """Return (group name, octal mode) for a device node, or None."""
        stat = self.stat(path)
        if stat is None:
            return None
        return self.group_name(stat.st_gid), format(stat.st_mode & 0o777, "03o")

    def accessible(self, path: str | Path) -> bool:
        """True when the current process can read and write ``path``."""
        target = self.local(path)
        if not target.exists():
            return False
        return os.access(target, os.R_OK | os.W_OK)

    # -- commands ------------------------------------------------------------
    def run(self, argv: Sequence[str], timeout: float = 180.0, env: Mapping[str, str] | None = None) -> RunResult:
        return self._runner(list(argv), timeout=timeout, env=env)

    def which(self, command: str) -> str | None:
        path = self.environ.get("PATH", os.defpath)
        return shutil.which(command, path=path)

    def command_available(self, command: str) -> bool:
        return self.which(command) is not None

    # -- identity ------------------------------------------------------------
    @property
    def uid(self) -> int:
        return os.getuid() if self._uid is None else self._uid

    @property
    def user(self) -> str:
        if self._user is not None:
            return self._user
        try:
            import pwd

            return pwd.getpwuid(self.uid).pw_name
        except (KeyError, ImportError):
            return self.environ.get("USER", "unknown")

    @property
    def is_root(self) -> bool:
        return self.uid == 0

    @property
    def session_groups(self) -> frozenset[str]:
        if self._session_groups is not None:
            return self._session_groups
        result = self.run(["id", "-nG"])
        if not result.ok:
            return frozenset()
        return frozenset(result.out.split())

    # -- system data ---------------------------------------------------------
    def group_members(self, group: str) -> tuple[str, ...]:
        for line in (self.read_text("/etc/group") or "").splitlines():
            parts = line.split(":")
            if len(parts) >= 4 and parts[0] == group:
                return tuple(name for name in parts[3].split(",") if name)
        result = self.run(["getent", "group", group])
        if result.ok and result.out.strip():
            parts = result.out.strip().split(":")
            if len(parts) >= 4:
                return tuple(name for name in parts[3].split(",") if name)
        return ()

    def group_exists(self, group: str) -> bool:
        for line in (self.read_text("/etc/group") or "").splitlines():
            if line.split(":")[0] == group:
                return True
        return any(part.startswith(f"{group}:") for part in self._getent_group(group).splitlines())

    def _getent_group(self, group: str) -> str:
        result = self.run(["getent", "group", group])
        return result.out if result.ok else ""

    def is_mounted(self, path: str | Path) -> bool:
        if not self.is_dir(path):
            return False
        result = self.run(["mountpoint", "-q", str(path)])
        return result.ok

    def free_bytes(self, path: str | Path) -> int | None:
        if self._free_bytes_fn is not None:
            return self._free_bytes_fn(path)
        try:
            return shutil.disk_usage(self.local(path)).free
        except OSError:
            return None

    def packages_installed(self) -> frozenset[str]:
        text = self.read_text(_STATUS_PATH)
        if text is None:
            result = self.run(["dpkg-query", "-W", "-f=${Package}\n"])
            return frozenset(line for line in result.out.split()) if result.ok else frozenset()
        installed: set[str] = set()
        package = ""
        status = ""
        for line in text.splitlines():
            if line.startswith("Package: "):
                package = line.split(": ", 1)[1].strip()
            elif line.startswith("Status: "):
                status = line.split(": ", 1)[1].strip()
            elif not line.strip():
                if package and status.endswith(" installed"):
                    installed.add(package)
                package, status = "", ""
        if package and status.endswith(" installed"):
            installed.add(package)
        return frozenset(installed)

    def apt_available(self) -> frozenset[str]:
        result = self.run(["apt-cache", "pkgnames"], timeout=60)
        if not result.ok:
            return frozenset()
        return frozenset(line.strip() for line in result.out.splitlines() if line.strip())

    def sysctl_values(self, names: Iterable[str]) -> dict[str, int]:
        values: dict[str, int] = {}
        for name in names:
            raw = self.read_text("/proc/sys/" + name.replace(".", "/"))
            if raw is None:
                continue
            try:
                values[name] = int(raw.strip())
            except ValueError:
                continue
        return values

    def swap_active(self) -> frozenset[str]:
        result = self.run(["swapon", "--show=NAME", "--noheadings"])
        if not result.ok:
            return frozenset()
        return frozenset(line.strip() for line in result.out.splitlines() if line.strip())

    def file_size(self, path: str | Path) -> int | None:
        stat = self.stat(path)
        return None if stat is None else stat.st_size
