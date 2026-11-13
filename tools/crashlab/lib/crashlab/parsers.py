"""Pure parsers: text in, structured evidence out.

Every function here is deterministic and side-effect free, which is what makes
the whole toolkit testable without a crashed machine (see ``selftest.py``).

Input formats handled:

* ``journalctl --list-boots``
* ``journalctl -b N -n M`` / ``-k``
* ``coredumpctl list`` / ``coredumpctl info PID``
* the project's own ``logs/*.log`` files
* byte tails of log files (a hard reset leaves zeros behind)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

# -- signatures --------------------------------------------------------------
# name, pattern, weight. Weight orders the report only; it is not a probability.
SIGNATURES: tuple[tuple[str, re.Pattern[str], int], ...] = (
    ("amdgpu pin failed", re.compile(r"amdgpu .*pin failed"), 1),
    ("framebuffer pin failed", re.compile(r"Failed to pin framebuffer"), 2),
    ("GPU hang/timeout", re.compile(r"amdgpu_job_timedout|ring \S+ timeout|GPU reset"), 9),
    ("GPU page fault", re.compile(r"amdgpu: .*(VM_L2|page fault|IH ring)"), 9),
    ("watchdog lockup", re.compile(r"soft lockup|hard LOCKUP|watchdog: BUG|hung_task"), 8),
    ("out of memory", re.compile(r"Out of memory|oom-kill|invoked oom-killer"), 7),
    ("hardware error", re.compile(r"Hardware Error|Machine check|MCE:|EDAC .*error"), 10),
    ("thermal", re.compile(r"critical temperature|thermal.*throttl"), 10),
    ("PCIe error", re.compile(r"pcieport .*AER|AER: Corrected|PCIe Bus Error"), 6),
    ("xHCI/USB reset", re.compile(r"xhci_hcd .*(reset|died)"), 3),
)

SHUTDOWN_MARKERS = re.compile(
    r"(systemd\[1\]|systemd-shutdown).*(Reached target (poweroff|reboot|shutdown|halt|kexec)\.target)",
    re.IGNORECASE,
)

SIGNAL_MEANING = {
    "SIGABRT": "abort() / assert in native code",
    "SIGSEGV": "invalid memory access",
    "SIGBUS": "bad memory mapping (truncated file or failing memory)",
    "SIGILL": "illegal instruction (wrong CPU/GPU code path)",
    "SIGFPE": "arithmetic exception",
    "SIGKILL": "killed by the kernel (OOM or admin)",
    "SIGTRAP": "breakpoint / trap instruction",
}

# Subsystem attribution from stack frames.
FRAME_HINTS: tuple[tuple[str, str], ...] = (
    ("libamdhip64", "HIP runtime (libamdhip64)"),
    ("librocblas", "rocBLAS"),
    ("librocsolver", "rocSOLVER"),
    ("libMIOpen", "MIOpen"),
    ("librocprofiler", "ROCm profiler"),
    ("libhip", "HIP runtime"),
    ("c10_cuda_check_implementation", "PyTorch HIP/CUDA call check (c10)"),
    ("c10::cuda::SetDevice", "PyTorch device switch (c10::cuda::SetDevice)"),
    ("comfy_aimdo", "comfy-aimdo (dynamic VRAM allocator)"),
    ("comfy_kitchen", "comfy-kitchen (quantized ops)"),
    ("libtorch", "PyTorch (libtorch)"),
    ("libpython", "CPython"),
    ("_rocm_sdk", "ROCm SDK libraries"),
    ("libcuda", "CUDA shim"),
    ("libnvidia", "NVIDIA driver"),
)

BOOT_ROW = re.compile(
    r"^\s*(?P<idx>-?\d+)\s+(?P<id>[0-9a-f]{32})\s+(?P<start>\S+ \S+ \S+ \S+)\s+(?P<end>\S+ \S+ \S+ \S+)"
)
CORE_ROW = re.compile(
    r"^\s*(?P<ts>\S+ \S+ \S+(?: \S+)?)\s+(?P<pid>\d+)\s+(?P<uid>\d+)\s+(?P<gid>\d+)\s+"
    r"(?P<sig>[A-Z]{4,})\s+(?P<state>\S+)\s+(?P<exe>/\S+|\[[^\]]*\]|\S+)"
)
INFO_FIELD = re.compile(r"^\s{2,}(?P<key>[A-Z][A-Za-z ]+):\s+(?P<value>.+)$")
FRAME_ROW = re.compile(r"^#(?P<n>\d+)\s+(?P<rest>.*)$")
FRAME_FROM = re.compile(r"from\s+(?P<lib>\S+)")
FRAME_FUNC = re.compile(r"in\s+(?P<fn>.+?)(?:\s+\(|$)")

SESSION_HEADER = re.compile(r"^--- session (?P<ts>\S+) \(pid (?P<pid>\d+)\) ---$")
RESERVE_ROW = re.compile(r"reserve-vram=(?P<reserve>\S+) lowvram=(?P<lowvram>\S+)")


# -- signatures --------------------------------------------------------------
def count_signatures(lines: list[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for name, pattern, _weight in SIGNATURES:
        hits = sum(1 for line in lines if pattern.search(line))
        if hits:
            counts[name] = hits
    return counts


def signature_samples(lines: list[str], limit: int = 3) -> dict[str, list[str]]:
    """The exact lines behind each count, for the evidence section."""
    samples: dict[str, list[str]] = {}
    for name, pattern, _weight in SIGNATURES:
        hits = [line.strip() for line in lines if pattern.search(line)]
        if hits:
            samples[name] = hits[:limit]
    return samples


# -- boots -------------------------------------------------------------------
@dataclass(frozen=True)
class Boot:
    index: int
    boot_id: str
    start: str
    end: str

    def to_dict(self) -> dict:
        return {"index": self.index, "id": self.boot_id, "start": self.start, "end": self.end}


def parse_list_boots(text: str) -> list[Boot]:
    boots: list[Boot] = []
    for line in text.splitlines():
        match = BOOT_ROW.match(line)
        if match:
            boots.append(
                Boot(
                    index=int(match.group("idx")),
                    boot_id=match.group("id"),
                    start=match.group("start"),
                    end=match.group("end"),
                )
            )
    return boots


def is_clean_shutdown(lines: list[str]) -> bool:
    return any(SHUTDOWN_MARKERS.search(line) for line in lines)


# -- log files ---------------------------------------------------------------
@dataclass
class LogTail:
    name: str
    mtime: float
    lines: list[str] = field(default_factory=list)
    zero_tail: int = 0

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "mtime": self.mtime,
            "lines": self.lines,
            "zero_tail": self.zero_tail,
        }


def tail_lines(text: str, count: int = 3, width: int = 200) -> list[str]:
    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    return [line[:width] for line in lines[-count:]]


def zero_tail(data: bytes) -> int:
    """Number of trailing NUL bytes: proof that the page cache was lost."""
    trimmed = data.rstrip(b"\x00")
    return len(data) - len(trimmed)


@dataclass(frozen=True)
class Session:
    started: str
    pid: int
    reserve_vram: str
    lowvram: str
    snapshot: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "started": self.started,
            "pid": self.pid,
            "reserve_vram": self.reserve_vram,
            "lowvram": self.lowvram,
            "snapshot": list(self.snapshot),
        }


def parse_sessions(text: str) -> list[Session]:
    """Parse the launcher's ``logs/session.log`` into one record per start."""
    sessions: list[Session] = []
    current: dict | None = None
    for line in text.splitlines():
        header = SESSION_HEADER.match(line.strip())
        if header:
            if current is not None:
                sessions.append(_session(current))
            current = {
                "ts": header.group("ts"),
                "pid": int(header.group("pid")),
                "reserve": "",
                "lowvram": "",
                "snap": [],
            }
            continue
        if current is None:
            continue
        if "snapshot:" in line:
            current["snap"].append(line.strip())
        settings = RESERVE_ROW.search(line)
        if settings:
            current["reserve"] = settings.group("reserve")
            current["lowvram"] = settings.group("lowvram")
    if current is not None:
        sessions.append(_session(current))
    return sessions


def _session(raw: dict) -> Session:
    return Session(
        started=raw["ts"],
        pid=raw["pid"],
        reserve_vram=raw["reserve"],
        lowvram=raw["lowvram"],
        snapshot=tuple(raw["snap"]),
    )


def parse_snapshot_line(line: str) -> dict[str, str]:
    """Pull the numbers out of one ``snapshot:`` line (best effort)."""
    values: dict[str, str] = {}
    for key, pattern in (
        ("mem_free", r"mem free (\d+) MiB"),
        ("mem_available", r"available (\d+) MiB"),
        ("swap", r"swap (\d+/\d+) MiB"),
        ("load", r"load ([\d. ]+)"),
    ):
        match = re.search(pattern, line)
        if match:
            values[key] = match.group(1).strip()
    return values


def parse_gpu_line(line: str) -> dict[str, str]:
    """``card1: vram 663/12272 MiB, gtt 399/16027 MiB`` -> dict."""
    match = re.search(r"(?P<card>card\d+): vram (?P<vram>\S+) MiB, gtt (?P<gtt>\S+) MiB", line)
    return match.groupdict() if match else {}


# -- core dumps --------------------------------------------------------------
@dataclass(frozen=True)
class Coredump:
    pid: int
    signal: str
    command: str
    timestamp: str
    storage: str = ""

    @property
    def culprit(self) -> str:
        return self.command.rsplit("/", 1)[-1] or self.command

    @property
    def meaning(self) -> str:
        return SIGNAL_MEANING.get(self.signal, self.signal)

    def to_dict(self) -> dict:
        return {
            "pid": self.pid,
            "signal": self.signal,
            "meaning": self.meaning,
            "command": self.command,
            "timestamp": self.timestamp,
            "storage": self.storage,
        }


def parse_coredump_list(text: str) -> list[Coredump]:
    cores: list[Coredump] = []
    for line in text.splitlines():
        match = CORE_ROW.match(line)
        if not match:
            continue
        cores.append(
            Coredump(
                pid=int(match.group("pid")),
                signal=match.group("sig"),
                command=match.group("exe").strip(),
                timestamp=match.group("ts").strip(),
                storage=match.group("state"),
            )
        )
    return cores


def parse_coredump_info(text: str) -> dict[str, str]:
    info: dict[str, str] = {}
    for line in text.splitlines():
        match = INFO_FIELD.match(line)
        if match:
            info.setdefault(match.group("key"), match.group("value").strip())
    return info


def parse_frames(text: str, limit: int = 12) -> list[str]:
    """Keep the raw ``#N`` lines: they carry function, library and offsets."""
    frames: list[str] = []
    for line in text.splitlines():
        match = FRAME_ROW.match(line.strip())
        if not match:
            continue
        frame = f"#{match.group('n')} {match.group('rest').strip()}"
        if frames and frames[-1].split(" ", 2)[-1] == frame.split(" ", 2)[-1]:
            continue  # duplicate frame emitted by both the journal and gdb
        frames.append(frame)
        if len(frames) >= limit:
            break
    return frames


ORIGIN = re.compile(r"(c10::|hipMalloc|hipFree|rocblas|miopen|comfy_aimdo|comfy_kitchen)")


def origin_frame(frames: list[str]) -> str:
    """First frame that names the subsystem which actually failed."""
    for frame in frames:
        if ORIGIN.search(frame):
            return frame.strip()
    return ""


def blame(frames: list[str]) -> str:
    joined = " ".join(frames)
    for needle, label in FRAME_HINTS:
        if needle in joined:
            return label
    return ""


def frame_functions(frames: list[str]) -> list[str]:
    names: list[str] = []
    for frame in frames:
        match = FRAME_FUNC.search(frame)
        if match:
            names.append(match.group("fn").strip())
    return names


def frame_libraries(frames: list[str]) -> list[str]:
    libs: list[str] = []
    for frame in frames:
        match = FRAME_FROM.search(frame)
        if match and match.group("lib") not in libs:
            libs.append(match.group("lib"))
    return libs


# -- misc --------------------------------------------------------------------
def parse_stamp(text: str) -> datetime | None:
    """Parse ``Fri 2026-11-13 19:45:20 MSK`` or ``2026-11-13T21:50:31+0300`` (naive, local time)."""
    raw = re.sub(r"[+-]\d\d:?\d\d$", "", text.strip())
    candidates = [raw, " ".join(raw.split()[:4]), " ".join(raw.split()[:3])]
    for candidate in candidates:
        for pattern in ("%a %Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
            try:
                return datetime.strptime(candidate, pattern)
            except ValueError:
                continue
    return None
