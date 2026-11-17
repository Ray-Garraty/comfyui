"""High-frequency host and GPU vitals: the machine's black box.

A hard reset leaves nothing behind: the kernel stops executing and no process
gets a chance to write a farewell message. The only way to find the cause is to
have the state of the machine *before* the stop already on the platter, second
by second.

``vitals`` is a tiny stdlib-only recorder. One line per sample, flushed and
fsynced by default, appended to ``logs/vitals.log`` while the ComfyUI server
runs. It covers the three things that can plausibly take this platform down
under ComfyUI:

* host memory pressure (available, swap, pinned/unevictable, PSI, allocstall,
  compaction stalls);
* GPU memory and power state (VRAM, GTT, busy, clocks, temperature, power);
* the ComfyUI process itself (PSS, swap, KFD contexts, D-state tasks).

The key set is stable and short so ``tools/crashlab`` can parse the file
without importing this package, and a human can diff two lines by eye.

Line format (one sample per line, key=value, MiB/percent/°C/W/MHz)::

    2026-11-17T06:25:30.115+03:00 mono=302.11 pid=5133 total=32056 avail=24122 ...
    2026-11-17T06:25:41.006+03:00 event=guard avail=2036 floor=3072 pid=5133 action=stop

``avail`` is ``MemAvailable``: the number that decides whether the kernel can
satisfy an allocation without reclaiming. ``lag`` is how late this sample was
written compared with its schedule, so a userspace stall cannot hide.
"""

from __future__ import annotations

import contextlib
import os
import signal
import time
from datetime import datetime
from pathlib import Path

from .probe import Probe

MB = 1024 * 1024
DEFAULT_INTERVAL = 1.0
DEFAULT_MAX_BYTES = 8 * MB
D_STATE_NAMES = 5


def _int(raw: str | None) -> int | None:
    try:
        return int((raw or "").strip())
    except ValueError:
        return None


def _kb(raw: str | None) -> int | None:
    """meminfo values are in kB; the log speaks MiB."""
    value = _int(raw)
    return None if value is None else value // 1024


def _first(raw: str | None) -> str:
    parts = (raw or "").split()
    return parts[0] if parts else ""


def meminfo(probe: Probe) -> dict[str, int]:
    """Selected ``/proc/meminfo`` fields, in MiB."""
    wanted = {
        "MemTotal": "total",
        "MemFree": "free",
        "MemAvailable": "avail",
        "Buffers": "buffers",
        "Cached": "cached",
        "SwapTotal": "swap_total",
        "SwapFree": "swap_free",
        "Mlocked": "mlock",
        "Unevictable": "unevict",
        "Dirty": "dirty",
        "Writeback": "writeback",
        "AnonHugePages": "anonhuge",
        "SReclaimable": "sreclaim",
    }
    result: dict[str, int] = {}
    for line in (probe.read_text("/proc/meminfo") or "").splitlines():
        key, _, raw = line.partition(":")
        name = wanted.get(key.strip())
        if name is None:
            continue
        value = _kb(_first(raw))
        if value is not None:
            result[name] = value
    return result


def pressure(probe: Probe) -> dict[str, float]:
    """Memory PSI: ``avg10`` for some/full plus the accumulated stall seconds."""
    result: dict[str, float] = {}
    for line in (probe.read_text("/proc/pressure/memory") or "").splitlines():
        parts = line.split()
        if not parts:
            continue
        kind = parts[0]
        fields = {}
        for part in parts[1:]:
            key, _, value = part.partition("=")
            fields[key] = value
        if "avg10" in fields:
            with contextlib.suppress(ValueError):
                result[f"psi_{kind}"] = float(fields["avg10"])
        if "total" in fields:
            with contextlib.suppress(ValueError):
                result[f"psi_{kind}_total"] = float(fields["total"]) / 1e6
    return result


def vmstat(probe: Probe) -> dict[str, int]:
    """Allocator stall counters. Their *delta* per second is what matters."""
    counters = {
        "allocstall": 0,
        "compactstall": 0,
        "oom": 0,
        "pgscandirect": 0,
        "pgstealdirect": 0,
        "pgmajfault": 0,
    }
    for line in (probe.read_text("/proc/vmstat") or "").splitlines():
        parts = line.split()
        if len(parts) != 2 or not parts[1].lstrip("-").isdigit():
            continue
        key, raw = parts
        value = int(raw)
        if key.startswith("allocstall"):
            counters["allocstall"] += value
        elif key == "compact_stall":
            counters["compactstall"] = value
        elif key == "oom_kill":
            counters["oom"] = value
        elif key == "pgscan_direct":
            counters["pgscandirect"] = value
        elif key == "pgsteal_direct":
            counters["pgstealdirect"] = value
        elif key == "pgmajfault":
            counters["pgmajfault"] = value
    return counters


def scheduler(probe: Probe) -> dict[str, int | str]:
    """Load average, running and blocked tasks, and who is blocked."""
    result: dict[str, int | str] = {}
    load = (probe.read_text("/proc/loadavg") or "").split()
    if len(load) >= 3:
        result["load"] = "/".join(load[:3])
    for line in (probe.read_text("/proc/stat") or "").splitlines():
        parts = line.split()
        if parts and parts[0] == "procs_running":
            result["run"] = int(parts[1]) if parts[1].isdigit() else 0
        elif parts and parts[0] == "procs_blocked":
            result["blocked"] = int(parts[1]) if parts[1].isdigit() else 0
    blocked = int(result.get("blocked", 0))
    if blocked:
        names = []
        for entry in probe.listdir("/proc"):
            if not entry.isdigit():
                continue
            stat = probe.read_text(f"/proc/{entry}/stat") or ""
            # The comm field can contain spaces and parentheses: state is the
            # first character after its closing parenthesis.
            close = stat.rfind(")")
            if close < 0 or len(stat) <= close + 2 or stat[close + 2] != "D":
                continue
            names.append((probe.read_text(f"/proc/{entry}/comm") or entry).strip())
            if len(names) >= D_STATE_NAMES:
                break
        result["dstate"] = ",".join(names) if names else "-"
    else:
        result["dstate"] = "-"
    return result


def _mib(raw: str | None) -> str:
    value = _int(raw)
    return "?" if value is None else str(value // MB)


def _hwmon(probe: Probe, device: str, name: str) -> str | None:
    for entry in probe.listdir(f"{device}/hwmon"):
        raw = probe.read_text(f"{device}/hwmon/{entry}/{name}")
        if raw and raw.strip():
            return raw.strip()
    return None


def gpu(probe: Probe) -> dict[str, str]:
    """Memory, utilisation and power of the first DRM device that has VRAM."""
    for entry in probe.listdir("/sys/class/drm"):
        if not entry.startswith("card") or "-" in entry:
            continue
        device = f"/sys/class/drm/{entry}/device"
        if not (probe.read_text(f"{device}/mem_info_vram_total") or "").strip():
            continue
        fields: dict[str, str] = {}
        total = _int(probe.read_text(f"{device}/mem_info_vram_total"))
        used = _int(probe.read_text(f"{device}/mem_info_vram_used"))
        gtt_total = _int(probe.read_text(f"{device}/mem_info_gtt_total"))
        gtt_used = _int(probe.read_text(f"{device}/mem_info_gtt_used"))
        if total is not None:
            fields["vram"] = f"{(used or 0) // MB}/{total // MB}"
        if gtt_total is not None:
            fields["gtt"] = f"{(gtt_used or 0) // MB}/{gtt_total // MB}"
        busy = _int(probe.read_text(f"{device}/gpu_busy_percent"))
        if busy is not None:
            fields["gpu"] = str(busy)
        temp = _int(_hwmon(probe, device, "temp1_input"))
        if temp is not None:
            fields["temp"] = str(temp // 1000)
        power = _int(_hwmon(probe, device, "power1_average"))
        if power is not None:
            fields["power"] = f"{power / 1e6:.1f}"
        for source, key in (("freq1_input", "sclk"), ("freq2_input", "mclk")):
            value = _int(_hwmon(probe, device, source))
            if value is not None:
                fields[key] = str(value // 1_000_000)
        return fields
    return {}


def process(probe: Probe, pid: int) -> dict[str, str]:
    """PSS/swap of the watched process, from smaps_rollup when available."""
    result: dict[str, str] = {}
    rollup = probe.read_text(f"/proc/{pid}/smaps_rollup")
    if rollup:
        wanted = {
            "Rss": "rss",
            "Pss": "pss",
            "Pss_Anon": "pssanon",
            "SwapPss": "swappss",
            "Private_Dirty": "privatedirty",
        }
        for line in rollup.splitlines():
            key, _, raw = line.partition(":")
            name = wanted.get(key.strip())
            if name is None:
                continue
            value = _kb(_first(raw))
            if value is not None:
                result[name] = str(value)
    if "pss" not in result:
        for line in (probe.read_text(f"/proc/{pid}/status") or "").splitlines():
            key, _, raw = line.partition(":")
            name = key.strip()
            if name not in ("VmRSS", "VmSwap"):
                continue
            value = _kb(_first(raw))
            if value is not None:
                result["rss" if name == "VmRSS" else "swappss"] = str(value)
    return result


def kfd_contexts(probe: Probe) -> int:
    """KFD processes: one per open compute context.

    The GPU cannot be idle while contexts are alive, and context churn is this
    machine's known trigger.
    """
    return len([entry for entry in probe.listdir("/sys/class/kfd/kfd/proc") if entry.isdigit()])


def sample(probe: Probe, pid: int | None = None, lag: float = 0.0) -> tuple[datetime, dict[str, str]]:
    """One complete sample. Values are strings in MiB, percent, °C, W, MHz."""
    uptime = _first(probe.read_text("/proc/uptime"))
    now = datetime.now().astimezone()
    fields: dict[str, str] = {}
    if uptime:
        fields["mono"] = uptime
    if pid is not None:
        fields["pid"] = str(pid)
    mem = meminfo(probe)
    for key in ("total", "avail", "free"):
        if key in mem:
            fields[key] = str(mem[key])
    if "swap_total" in mem:
        swap_used = mem["swap_total"] - mem.get("swap_free", mem["swap_total"])
        fields["swap"] = f"{swap_used}/{mem['swap_total']}"
    for key in ("mlock", "unevict", "dirty", "writeback", "anonhuge"):
        if key in mem:
            fields[key] = str(mem[key])
    for key, value in pressure(probe).items():
        fields[key] = f"{value:.2f}" if isinstance(value, float) else str(value)
    for key, value in vmstat(probe).items():
        fields[key] = str(value)
    for key, value in scheduler(probe).items():
        fields[key] = str(value)
    fields.update(gpu(probe))
    if pid is not None:
        fields.update(process(probe, pid))
    fields["kfd"] = str(kfd_contexts(probe))
    fields["lag"] = f"{lag:.2f}"
    return now, fields


def format_sample(now: datetime, fields: dict[str, str]) -> str:
    stamp = now.isoformat(timespec="milliseconds")
    body = " ".join(f"{key}={value}" for key, value in fields.items())
    return f"{stamp} {body}".rstrip()


def format_event(kind: str, **fields: object) -> str:
    """A non-sample line: ``event=guard avail=2036 floor=3072 action=stop``."""
    stamp = datetime.now().astimezone().isoformat(timespec="milliseconds")
    body = " ".join(f"{key}={value}" for key, value in fields.items())
    return f"{stamp} event={kind} {body}".rstrip()


def _rotate(handle, path: Path, max_bytes: int):
    """Roll the log over once it reaches ``max_bytes``; returns the open handle."""
    try:
        if path.stat().st_size < max_bytes:
            return handle
        handle.close()
        path.replace(path.with_name(path.name + ".1"))
        return path.open("a", encoding="utf-8")
    except OSError:
        return handle


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def run(
    out: str | Path,
    interval: float = DEFAULT_INTERVAL,
    pid: int | None = None,
    max_bytes: int = DEFAULT_MAX_BYTES,
    fsync: bool = True,
    probe: Probe | None = None,
    iterations: int | None = None,
    guard_avail_mb: int = 0,
) -> int:
    """Append samples to ``out`` until the watched process or this one stops.

    ``iterations`` exists for tests; the launcher leaves it unset. The recorder
    is deliberately self-contained: it must keep working when ComfyUI, torch or
    the venv are the things that broke.

    ``guard_avail_mb`` turns the recorder into a tripwire: when ``MemAvailable``
    drops below that floor the watched process is asked to stop, so the run
    ends with an explained shutdown instead of an unexplained reset.
    """
    path = Path(out)
    path.parent.mkdir(parents=True, exist_ok=True)
    host = probe or Probe()
    stop = False

    def request_stop(_signum, _frame) -> None:
        nonlocal stop
        stop = True

    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, request_stop)

    count = 0
    expected = time.monotonic()
    handle = path.open("a", encoding="utf-8")
    try:
        while not stop:
            handle = _rotate(handle, path, max_bytes)
            started = time.monotonic()
            lag = max(0.0, started - expected)
            now, fields = sample(host, pid=pid, lag=lag)
            handle.write(format_sample(now, fields) + "\n")
            handle.flush()
            if fsync:
                os.fsync(handle.fileno())

            if guard_avail_mb > 0 and pid is not None:
                available = _int(fields.get("avail"))
                if available is not None and available < guard_avail_mb:
                    handle.write(
                        format_event(
                            "guard",
                            avail=available,
                            floor=guard_avail_mb,
                            pid=pid,
                            action="stop",
                        )
                        + "\n"
                    )
                    handle.flush()
                    if fsync:
                        os.fsync(handle.fileno())
                    with contextlib.suppress(OSError):
                        os.kill(pid, signal.SIGTERM)
                    break

            count += 1
            if iterations is not None and count >= iterations:
                break
            if pid is not None and not _alive(pid):
                break
            expected = started + interval
            remaining = expected - time.monotonic()
            if remaining > 0:
                time.sleep(remaining)
    finally:
        handle.close()
    return 0
