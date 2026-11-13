"""Evidence -> verdict -> report. The only place that draws conclusions."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from . import parsers

CAUSE_ORDER = (
    "kernel-lockup",
    "gpu-hang",
    "oom",
    "gpu-memory",
    "process-abort",
    "silent-reset",
    "unlogged",
)

# Processes whose dumps are shutdown noise, never the cause of a reset.
NOISE_PROCESSES = ("Xorg", "code", "chrome", "csd-")


@dataclass
class Evidence:
    boot: parsers.Boot | None = None
    boot_clean: bool | None = None
    kernel: list[str] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    samples: dict[str, list[str]] = field(default_factory=dict)
    coredumps: list[parsers.Coredump] = field(default_factory=list)
    logs: list[parsers.LogTail] = field(default_factory=list)
    sessions: list[parsers.Session] = field(default_factory=list)
    pstore: list[tuple[str, str]] = field(default_factory=list)
    gpu: list[str] = field(default_factory=list)
    gdb_frames: dict[int, list[str]] = field(default_factory=dict)
    history: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass
class Verdict:
    cause: str
    summary: str
    evidence: list[str]
    next_steps: list[str]

    def to_dict(self) -> dict:
        return {
            "cause": self.cause,
            "summary": self.summary,
            "evidence": self.evidence,
            "next_steps": self.next_steps,
        }


def decide(ev: Evidence) -> Verdict:
    """Map the evidence to one cause. Ordered by specificity, not by guesswork."""
    counts = ev.counts

    if counts.get("hardware error") or counts.get("thermal"):
        return Verdict(
            "hardware",
            "hardware: machine check or thermal-critical events in the kernel log",
            _pick(ev, "hardware error", "thermal"),
            ["run a hardware stress test outside ComfyUI", "check sensors and EDAC counters"],
        )

    if counts.get("watchdog lockup"):
        return Verdict(
            "kernel-lockup",
            "kernel lockup: a watchdog fired, the machine stopped scheduling",
            _pick(ev, "watchdog lockup"),
            ["keep hardlockup_panic and pstore enabled", "capture the lockup stack on the next event"],
        )

    if counts.get("GPU hang/timeout") or counts.get("GPU page fault"):
        return Verdict(
            "gpu-hang",
            "GPU driver: hang, reset or page fault in amdgpu",
            _pick(ev, "GPU hang/timeout", "GPU page fault"),
            ["retry with --disable-pinned-memory and --lowvram", "compare ROCm and kernel versions"],
        )

    if counts.get("out of memory"):
        return Verdict(
            "oom",
            "host memory exhausted: the kernel killed processes (OOM)",
            _pick(ev, "out of memory"),
            ["enable swap", "lower ComfyUI pinned memory"],
        )

    if counts.get("framebuffer pin failed"):
        vram = _last_gpu(ev)
        return Verdict(
            "gpu-memory",
            "GPU memory accounting: the display plane could not pin its framebuffer (ENOMEM)",
            _pick(ev, "framebuffer pin failed", "amdgpu pin failed") + ([vram] if vram else []),
            ["raise COMFYUI_RESERVE_VRAM", "watch logs/session.log for VRAM/GTT growth"],
        )

    aborts = [
        core
        for core in ev.coredumps
        if core.signal in ("SIGABRT", "SIGSEGV", "SIGBUS") and core.culprit not in NOISE_PROCESSES
    ]
    if aborts:
        # Prefer a dump we could actually symbolize; otherwise the newest one.
        core = next((item for item in reversed(aborts) if ev.gdb_frames.get(item.pid)), aborts[-1])
        frames = ev.gdb_frames.get(core.pid, [])
        subsystem = parsers.blame(frames)
        detail = f"{core.culprit} {core.signal} ({core.meaning}) at {core.timestamp}"
        if subsystem:
            detail += f" in {subsystem}"
        origin = parsers.origin_frame(frames)
        if origin:
            detail += f"; origin: {origin[:110]}"
        return Verdict(
            "process-abort",
            f"process crashed: {detail}",
            frames[:6],
            [f"crashlab coredump {core.pid} --bt"],
        )

    if not ev.kernel:
        return Verdict(
            "unlogged",
            "inconclusive: the kernel log of that boot is gone",
            [f"journal has {len(ev.logs)} log file(s) but no kernel lines"],
            ["apply the durable logging configuration (provision.sh system)", "rerun after the next crash"],
        )

    last = ev.logs[0].name if ev.logs else "no log file"
    lost = [f"{log.name}: {log.zero_tail} zero bytes at the end" for log in ev.logs if log.zero_tail]
    history = [
        f"boot {row['boot']}: " + ", ".join(f"{name} x{hits}" for name, hits in row["signatures"].items())
        for row in ev.history
        if row["signatures"]
    ]
    steps = [
        "apply the durable logging configuration (./provision.sh system, run by the user)",
        "compare the boot end time with logs/session.log",
    ]
    if any("truncated" in note for note in ev.notes):
        steps.insert(0, "journald lost the tail of this boot: the logging configuration is not applied yet")
    return Verdict(
        "silent-reset",
        f"the machine died without a kernel signature; last active log: {last}",
        lost + ([f"last kernel line: {ev.kernel[-1][:160]}"] if ev.kernel else []) + history[:4],
        steps,
    )


def _pick(ev: Evidence, *names: str) -> list[str]:
    lines: list[str] = []
    for name in names:
        for sample in ev.samples.get(name, [])[:2]:
            lines.append(sample[:170])
    return lines


def _last_gpu(ev: Evidence) -> str:
    return ev.gpu[0] if ev.gpu else ""


def render(ev: Evidence, verdict: Verdict, generated: str | None = None) -> str:
    out = ["== crashlab =="]
    out.append(f"generated: {generated or datetime.now(UTC).astimezone().strftime('%Y-%m-%dT%H:%M:%S%z')}")
    state = {True: "clean", False: "unclean", None: "unknown"}[ev.boot_clean]
    out.append(
        f"boot:      {ev.boot.index if ev.boot else '?'} {ev.boot.start if ev.boot else ''} -> "
        f"{ev.boot.end if ev.boot else ''} ({state})"
    )
    out.append("")
    out.append(f"CAUSE:     {verdict.cause}")
    out.append(f"VERDICT:   {verdict.summary}")
    for note in ev.notes:
        out.append(f"note:      {note}")
    if verdict.evidence:
        out.append("")
        out.append("evidence:")
        out.extend(f"  {line}" for line in verdict.evidence)
    if ev.counts:
        out.append("")
        out.append("kernel signatures:")
        for name, hits in sorted(ev.counts.items(), key=lambda item: -item[1]):
            out.append(f"  {hits:>5}  {name}")
    if ev.history:
        out.append("")
        out.append("boot history:")
        for row in ev.history:
            found = ", ".join(f"{name} x{hits}" for name, hits in row["signatures"].items()) or "no signatures"
            out.append(f"  boot {row['boot']}: {found}")
    if ev.coredumps:
        out.append("")
        out.append("core dumps:")
        for core in ev.coredumps:
            out.append(f"  {core.timestamp}  pid {core.pid:<7} {core.signal:<8} {core.command[:100]}")
    if ev.sessions:
        out.append("")
        out.append("launcher sessions (reserve-vram / memory at start):")
        for session in ev.sessions[-5:]:
            snap = session.snapshot[0] if session.snapshot else ""
            out.append(
                f"  {session.started}  pid {session.pid}  reserve={session.reserve_vram or '?'}"
                f" lowvram={session.lowvram or '?'}"
            )
            if snap:
                out.append(f"      {snap[:150]}")
    if ev.logs:
        out.append("")
        out.append("log files (newest first):")
        for log in ev.logs:
            mark = f"  [{log.zero_tail} zeros]" if log.zero_tail else ""
            out.append(f"  {log.name}{mark}")
            out.extend(f"      {line[:160]}" for line in log.lines)
    if ev.pstore:
        out.append("")
        out.append("pstore:")
        for name, text in ev.pstore:
            out.append(f"  --- {name}")
            out.extend(f"  {line[:160]}" for line in text.splitlines()[-12:])
    if ev.kernel:
        out.append("")
        out.append("kernel tail:")
        out.extend(f"  {line[:170]}" for line in ev.kernel[-15:])
    if verdict.next_steps:
        out.append("")
        out.append("next steps:")
        out.extend(f"  - {step}" for step in verdict.next_steps)
    return "\n".join(out)
