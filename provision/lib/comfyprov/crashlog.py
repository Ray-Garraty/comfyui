"""Crash evidence: read-only diagnostics and cheap host snapshots.

Every hard reset on this machine lost the interesting part of the log because
the kernel messages were still in the page cache. The ``crashlog`` planner step
fixes the logging; this module reads what is left afterwards:

* ``/sys/fs/pstore`` — firmware/ramoops records that survive a reset;
* ``journalctl -b -1`` — the tail of the previous (crashed) boot;
* signature counts (amdgpu pin failures, OOM, watchdogs, hardware errors);
* ``snapshot()`` — a few lines about memory and GPU usage, written to the logs
  by ``start-comfyui.sh`` and ``generate.sh`` so the state before a crash is
  known even when the crash itself leaves nothing.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass

from . import layout
from .config import Config
from .probe import Probe
from .report import FAIL, OK, WARN, Finding

KERNEL_TAIL_LINES = 400
PSTORE_MAX_BYTES = 8192
MB = 1024 * 1024


@dataclass(frozen=True)
class Signature:
    name: str
    pattern: re.Pattern[str]


# Ordered by how much they matter for a frozen desktop.
SIGNATURES: tuple[Signature, ...] = (
    Signature("amdgpu pin failed", re.compile(r"amdgpu .*pin failed")),
    Signature("framebuffer pin failed", re.compile(r"Failed to pin framebuffer")),
    Signature("GPU hang/timeout", re.compile(r"amdgpu_job_timedout|GPU reset|ring \S+ timeout", re.IGNORECASE)),
    Signature("watchdog lockup", re.compile(r"soft lockup|hard LOCKUP|watchdog: BUG", re.IGNORECASE)),
    Signature("out of memory", re.compile(r"Out of memory|oom-kill|invoked oom-killer")),
    Signature("hardware error", re.compile(r"Hardware Error|Machine check|MCE:|EDAC .*error", re.IGNORECASE)),
)

CLEAN_SHUTDOWN = re.compile(
    r"systemd\[1\]: .*Reached target (poweroff|reboot|shutdown|halt|kexec)\.target", re.IGNORECASE
)


@dataclass(frozen=True)
class Evidence:
    pstore: tuple[tuple[str, str], ...]
    kernel_tail: tuple[str, ...]
    boot_tail: tuple[str, ...]
    counts: dict[str, int]
    last_kernel_line: str
    clean_shutdown: bool
    available: bool

    @property
    def crashes(self) -> bool:
        return bool(self.available and not self.clean_shutdown)

    def summary(self) -> str:
        if not self.available:
            return "journal is unavailable; enable persistent journald logging"
        state = "clean shutdown" if self.clean_shutdown else "unclean shutdown (crash)"
        return f"{state}, last kernel line {self.last_kernel_line or 'unknown'}"


def _journal(probe: Probe, *args: str, timeout: float = 30.0) -> list[str]:
    result = probe.run(["journalctl", *args, "--no-pager", "-o", "short-iso"], timeout=timeout)
    if not result.ok:
        return []
    return [line for line in result.out.splitlines() if line.strip()]


def read_pstore(probe: Probe, limit: int = 4) -> tuple[tuple[str, str], ...]:
    records: list[tuple[str, str]] = []
    for name in probe.listdir(layout.PSTORE_DIR)[:limit]:
        text = probe.read_text(layout.PSTORE_DIR / name)
        if text:
            records.append((name, text[-PSTORE_MAX_BYTES:]))
    return tuple(records)


def collect(probe: Probe, boot: str = "-1") -> Evidence:
    """Read the evidence left by the previous boot."""
    kernel_tail = tuple(_journal(probe, "-b", boot, "-k", "-n", str(KERNEL_TAIL_LINES)))
    boot_tail = tuple(_journal(probe, "-b", boot, "-n", "40"))
    available = bool(kernel_tail or boot_tail)

    counts = {signature.name: 0 for signature in SIGNATURES}
    last_line = ""
    for line in kernel_tail:
        last_line = line
        for signature in SIGNATURES:
            if signature.pattern.search(line):
                counts[signature.name] += 1

    clean = any(CLEAN_SHUTDOWN.search(line) for line in boot_tail)
    return Evidence(
        pstore=read_pstore(probe),
        kernel_tail=kernel_tail,
        boot_tail=boot_tail,
        counts=counts,
        last_kernel_line=last_line,
        clean_shutdown=clean,
        available=available,
    )


def findings(config: Config, probe: Probe, facts, evidence: Evidence | None = None) -> list[Finding]:
    """Doctor-style findings about the crash-capture chain."""
    evidence = evidence if evidence is not None else collect(probe)
    results: list[Finding] = []

    journald = probe.read_text(layout.JOURNALD_DROPIN)
    if journald == layout.JOURNALD_CONTENT:
        results.append(Finding(OK, "journald", "durable (persistent, SyncIntervalSec=1, no rate limit)"))
    elif journald:
        results.append(Finding(WARN, "journald", f"{layout.JOURNALD_DROPIN} differs; re-run './provision.sh system'"))
    else:
        results.append(Finding(WARN, "journald", "no durable logging; run './provision.sh system'"))

    if probe.read_text(layout.RSYSLOG_DROPIN) == layout.RSYSLOG_CONTENT:
        results.append(Finding(OK, "kernel log copy", str(layout.KERNEL_LOG_COPY)))
    elif "rsyslog" in probe.packages_installed():
        results.append(Finding(WARN, "kernel log copy", "rsyslog drop-in missing; run './provision.sh system'"))
    else:
        results.append(Finding(WARN, "kernel log copy", "rsyslog is not installed"))

    sysctl = probe.sysctl_values(layout.CRASH_SYSCTL_VALUES)
    wrong = [name for name, value in layout.CRASH_SYSCTL_VALUES.items() if sysctl.get(name) != value]
    if not wrong:
        results.append(Finding(OK, "watchdog", "lockups panic and reboot (kernel.panic=10)"))
    else:
        results.append(Finding(WARN, "watchdog", f"not applied: {', '.join(sorted(wrong))}"))

    if evidence.pstore:
        results.append(
            Finding(OK, "pstore", f"{len(evidence.pstore)} record(s): {', '.join(n for n, _ in evidence.pstore)}")
        )
    else:
        results.append(Finding(WARN, "pstore", "no firmware records (normal when the firmware backend is off)"))

    results.append(Finding(WARN if evidence.crashes else OK, "previous boot", evidence.summary()))
    for signature in SIGNATURES:
        count = evidence.counts.get(signature.name, 0)
        if count:
            level = FAIL if signature.name in ("hardware error", "out of memory") else WARN
            results.append(Finding(level, f"previous boot: {signature.name}", f"{count} line(s)"))

    # --- new crash-capture features ---
    fw_service = probe.read_text(layout.FORCE_WATCHDOG_SERVICE)
    if fw_service and "modprobe" in fw_service:
        wd = "present" if facts.watchdog_present else "absent"
        results.append(
            Finding(
                OK if facts.watchdog_present else WARN,
                "force-watchdog",
                f"oneshot unit will modprobe the watchdog despite denylist (watchdog device: {wd})",
            )
        )
    else:
        results.append(
            Finding(
                WARN,
                "force-watchdog",
                "not installed; run './provision.sh system' (needed when iTCO_wdt is deny-listed)",
            )
        )

    nc = probe.read_text(layout.NETCONSOLE_MODULE_CONF)
    if nc:
        results.append(Finding(OK, "netconsole", "module configured for out-of-band kernel log capture"))
    else:
        results.append(
            Finding(
                WARN,
                "netconsole",
                "not configured; the only way to read the last lines of a platform freeze",
            )
        )

    rp = probe.read_text(layout.RAMOOPS_MODULE_CONF)
    if rp:
        results.append(Finding(OK, "ramoops", "persistent RAM backend configured for pstore"))
    else:
        results.append(Finding(WARN, "ramoops", "not configured; no post-reset dmesg tail"))

    cd = probe.read_text(layout.CRASH_DOCTOR)
    if cd:
        results.append(Finding(OK, "crash-doctor", "installed at /usr/local/bin/crash-doctor"))
    else:
        results.append(
            Finding(
                WARN,
                "crash-doctor",
                "not installed; run './provision.sh system' for automated root cause",
            )
        )

    return results


def format_report(evidence: Evidence) -> str:
    """Plain-text dump of the collected evidence."""
    sections = ["== crash evidence ==", f"previous boot: {evidence.summary()}"]
    for signature in SIGNATURES:
        count = evidence.counts.get(signature.name, 0)
        if count:
            sections.append(f"  {signature.name}: {count} line(s)")

    if evidence.pstore:
        sections.append("")
        sections.append(f"== pstore ({len(evidence.pstore)} record(s)) ==")
        for name, text in evidence.pstore:
            sections.append(f"--- {name}")
            sections.extend(text.splitlines()[-60:])
    else:
        sections.append("")
        sections.append("== pstore: no records ==")

    if evidence.kernel_tail:
        sections.append("")
        sections.append(f"== previous boot, last {len(evidence.kernel_tail)} kernel lines ==")
        sections.extend(evidence.kernel_tail[-60:])
    return "\n".join(sections)


def _read_meminfo(probe: Probe) -> dict[str, int]:
    values: dict[str, int] = {}
    for line in (probe.read_text("/proc/meminfo") or "").splitlines():
        key, _, raw = line.partition(":")
        parts = raw.split()
        if parts and parts[0].isdigit():
            values[key.strip()] = int(parts[0]) * 1024
    return values


def _gpu_memory(probe: Probe) -> list[str]:
    lines: list[str] = []
    for name in probe.listdir("/sys/class/drm"):
        if not re.fullmatch(r"card\d+", name):
            continue
        base = f"/sys/class/drm/{name}/device"
        used = probe.read_text(f"{base}/mem_info_vram_used")
        total = probe.read_text(f"{base}/mem_info_vram_total")
        gtt_used = probe.read_text(f"{base}/mem_info_gtt_used")
        gtt_total = probe.read_text(f"{base}/mem_info_gtt_total")
        if not total:
            continue

        def mib(raw: str | None) -> str:
            try:
                return str(int(raw or "0") // MB)
            except ValueError:
                return "?"

        lines.append(f"{name}: vram {mib(used)}/{mib(total)} MiB, gtt {mib(gtt_used)}/{mib(gtt_total)} MiB")
    return lines


def snapshot(probe: Probe, now: str | None = None) -> str:
    """Short, fsync-friendly state dump for the launcher and the generator."""
    mem = _read_meminfo(probe)
    stamp = now or time.strftime("%Y-%m-%dT%H:%M:%S%z")
    load = (probe.read_text("/proc/loadavg") or "").split()
    swap_used = mem.get("SwapTotal", 0) - mem.get("SwapFree", 0)
    lines = [
        f"{stamp} snapshot:"
        f" mem free {mem.get('MemFree', 0) // MB} MiB,"
        f" available {mem.get('MemAvailable', 0) // MB} MiB,"
        f" swap {swap_used // MB}/{mem.get('SwapTotal', 0) // MB} MiB,"
        f" load {' '.join(load[:3]) or 'unknown'}"
    ]
    lines.extend(f"  {line}" for line in _gpu_memory(probe))
    return "\n".join(lines)
