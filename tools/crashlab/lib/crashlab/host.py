"""Read-only host access with an injectable runner and root.

The parsers in ``parsers.py`` are pure functions over text; this module only
does IO, so tests can feed canned journals without touching the machine.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

Runner = Callable[[Sequence[str], float], "RunResult"]


@dataclass(frozen=True)
class RunResult:
    code: int
    out: str = ""
    err: str = ""

    @property
    def ok(self) -> bool:
        return self.code == 0


def real_run(argv: Sequence[str], timeout: float = 60.0) -> RunResult:
    try:
        done = subprocess.run(list(argv), capture_output=True, text=True, timeout=timeout, check=False)
    except FileNotFoundError as exc:
        return RunResult(127, "", str(exc))
    except subprocess.TimeoutExpired:
        return RunResult(124, "", f"timeout after {timeout:.0f}s")
    return RunResult(done.returncode, done.stdout, done.stderr)


class Host:
    """Thin, side-effect-free view of the machine."""

    def __init__(self, root: str | Path = "/", runner: Runner | None = None) -> None:
        self.root = Path(root)
        self._runner = runner or real_run

    def local(self, path: str | Path) -> Path:
        candidate = Path(path)
        return self.root / candidate.relative_to("/") if candidate.is_absolute() else self.root / candidate

    def read_text(self, path: str | Path) -> str:
        try:
            return self.local(path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    def read_bytes(self, path: str | Path) -> bytes:
        try:
            return self.local(path).read_bytes()
        except OSError:
            return b""

    def listdir(self, path: str | Path) -> list[str]:
        try:
            return sorted(os.listdir(self.local(path)))
        except OSError:
            return []

    def mtime(self, path: str | Path) -> float:
        try:
            return self.local(path).stat().st_mtime
        except OSError:
            return 0.0

    def exists(self, path: str | Path) -> bool:
        return self.local(path).exists()

    def run(self, argv: Sequence[str], timeout: float = 60.0) -> RunResult:
        return self._runner(list(argv), timeout)

    def lines(self, argv: Sequence[str], timeout: float = 60.0) -> list[str]:
        result = self.run(argv, timeout)
        return [line for line in result.out.splitlines() if line.strip()] if result.ok else []

    def which(self, command: str) -> str:
        for directory in os.environ.get("PATH", "/usr/bin:/bin").split(os.pathsep):
            candidate = Path(directory) / command
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)
        return ""
