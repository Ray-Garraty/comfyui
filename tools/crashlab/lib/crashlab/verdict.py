"""Evidence -> verdict -> report. The only place that draws conclusions.

The report is a *decision first* document: one command must answer "what died,
what did not, what is missing" in well under a minute. Details live behind
``--full`` so they cannot bury the answer.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from . import parsers

# Seconds after which a signature is context, not a cause.
STALE_SIGNATURE_SECONDS = 300

FREEZE_GAP_SECONDS = 30

# A prompt queued this fast after the server came up was not typed by a human.
PROGRAMMATIC_PROMPT_SECONDS = 5.0

# A generation this much slower than the median is a memory/pressure symptom.
SLOW_GENERATION_FACTOR = 3.0

CAUSE_ORDER = (
    "hardware",
    "kernel-panic",
    "kernel-lockup",
    "gpu-hang",
    "oom",
    "gpu-memory",
    "process-abort",
    "platform-freeze",
    "silent-reset",
    "unlogged",
)

# Processes whose dumps are shutdown noise, never the cause of a reset.
NOISE_PROCESSES = ("Xorg", "code", "chrome", "csd-")


@dataclass
class Check:
    """One "decided by" row: what was looked at and what it said."""

    status: str  # confirmed | ruled out | unknown
    label: str
    detail: str = ""

    def render(self) -> str:
        return f"[{self.status}] {self.label}" + (f": {self.detail}" if self.detail else "")

    def to_dict(self) -> dict:
        return {"status": self.status, "label": self.label, "detail": self.detail}


@dataclass
class Evidence:
    boot: parsers.Boot | None = None
    boot_clean: bool | None = None
    kernel: list[str] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    samples: dict[str, list[str]] = field(default_factory=dict)
    windows: dict[str, parsers.SignatureWindow] = field(default_factory=dict)
    coredumps: list[parsers.Coredump] = field(default_factory=list)
    logs: list[parsers.LogTail] = field(default_factory=list)
    sessions: list[parsers.Session] = field(default_factory=list)
    pstore: list[tuple[str, str]] = field(default_factory=list)
    journal_last: str = ""
    config: dict[str, str] = field(default_factory=dict)
    gpu: list[str] = field(default_factory=list)
    gdb_frames: dict[int, list[str]] = field(default_factory=dict)
    boot_end: datetime | None = None
    last_kernel: datetime | None = None
    log_mtime: datetime | None = None
    journal_gaps: list[float] = field(default_factory=list)
    history: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    # -- clock and lifecycle -------------------------------------------------
    boot_start: datetime | None = None
    boot_runtime_s: float | None = None
    boot_list_start: str = ""
    next_boot: parsers.Boot | None = None
    next_start: datetime | None = None
    power_on: datetime | None = None
    startup: dict[str, float] = field(default_factory=dict)
    silence_s: float | None = None
    # -- what was ruled out --------------------------------------------------
    panic_record: str = "unknown"  # present | empty | unknown
    pstore_note: str = ""
    watchdog_armed: bool | None = None
    watchdog_detail: str = ""
    watchdog_denylist: list[str] = field(default_factory=list)
    blacklists: dict[str, str] = field(default_factory=dict)
    # -- what ran ------------------------------------------------------------
    events: list[parsers.AppEvent] = field(default_factory=list)
    app_stop: list[tuple[str, datetime]] = field(default_factory=list)
    pinned: list[tuple[datetime, int]] = field(default_factory=list)
    checks: list[Check] = field(default_factory=list)


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


# -- derived numbers ---------------------------------------------------------
def signature_age(ev: Evidence, moment: datetime | None) -> float | None:
    """Seconds between the newest matching line and the end of the boot."""
    if ev.boot_end is None or moment is None:
        return None
    return (ev.boot_end - moment).total_seconds()


def signature_age_of(ev: Evidence, name: str) -> float | None:
    """Age of one signature. Freshness is per signature: a KFD teardown hog
    55 s before the kill must not make an 8-minute-old pin burst look fresh."""
    window = ev.windows.get(name)
    if window and window.last is not None:
        return signature_age(ev, window.last)
    stamps = [parsers.parse_stamp(line.split(" ", 1)[0]) for line in ev.samples.get(name, [])]
    known = [stamp for stamp in stamps if stamp]
    if known:
        return signature_age(ev, max(known))
    return None


def is_fresh(ev: Evidence, *names: str) -> bool:
    """True when one of the signatures fired recently enough to be the cause.

    Without a boot end (unit fixtures, journal lost) the signature keeps its
    meaning: there is nothing to compare it with.
    """
    for name in names:
        if name not in ev.counts:
            continue
        age = signature_age_of(ev, name)
        if age is None or age <= STALE_SIGNATURE_SECONDS:
            return True
    return False


def newest_signature(ev: Evidence) -> datetime | None:
    stamps: list[datetime] = []
    for samples in ev.samples.values():
        for line in samples:
            stamp = parsers.parse_stamp(line.split(" ", 1)[0])
            if stamp:
                stamps.append(stamp)
    stamps.extend(window.last for window in ev.windows.values() if window.last)
    return max(stamps) if stamps else None


def journal_gap(ev: Evidence) -> float | None:
    """Seconds between the last journal entry and the last process log write.

    journald is a normal process: if it stopped writing long before the
    application did, the machine stopped scheduling rather than crashing.
    """
    if ev.boot_end is None or ev.log_mtime is None:
        return None
    gap = (ev.log_mtime - ev.boot_end).total_seconds()
    return gap if gap > 0 else None


def journal_stalled(ev: Evidence) -> tuple[bool, str]:
    """Is the final journal silence abnormal for this boot?

    An idle journal is not a stalled one. This machine logs nothing to the
    journal for minutes while ComfyUI works, so an 89 s gap at the end says
    nothing if the boot already had a 7-minute quiet stretch.
    """
    gap = journal_gap(ev)
    if gap is None or gap <= FREEZE_GAP_SECONDS:
        return False, ""
    longest = max(ev.journal_gaps, default=0.0)
    if longest > 0 and gap <= 3 * longest:
        return False, f"journal had a {int(longest)} s idle gap in this boot, so {int(gap)} s is normal"
    return True, f"journald stopped {int(gap)} s before the last application write"


def generation_stats(ev: Evidence) -> tuple[int, float | None]:
    """Number of finished prompts and the median duration in the window."""
    durations = [float(event.detail) for event in ev.events if event.kind == "done" and event.detail]
    if not durations:
        return 0, None
    return len(durations), statistics.median(durations)


def prompt_latency(ev: Evidence) -> dict[int, float]:
    """For every prompt event, seconds since the nearest earlier server start."""
    starts = [event.at for event in ev.events if event.kind == "start"]
    latency: dict[int, float] = {}
    for index, event in enumerate(ev.events):
        if event.kind != "prompt":
            continue
        earlier = [moment for moment in starts if moment <= event.at]
        if earlier:
            latency[index] = (event.at - max(earlier)).total_seconds()
    return latency


def sessions_in_window(ev: Evidence) -> list[parsers.Session]:
    """Launcher starts of the analysed boot, without the neighbours' noise."""
    if ev.boot_start is None:
        return list(ev.sessions)
    end = ev.power_on or ev.boot_end or datetime.max
    kept: list[parsers.Session] = []
    for session in ev.sessions:
        moment = parsers.parse_stamp(session.started)
        if moment is None:
            kept.append(session)
            continue
        if ev.boot_start - timedelta(minutes=1) <= moment <= end + timedelta(minutes=1):
            kept.append(session)
    return kept


# -- what was ruled out ------------------------------------------------------
def build_checks(ev: Evidence) -> list[Check]:
    """The "decided by" rows. Every row is a question with an answer."""
    checks: list[Check] = []

    checks.append(
        Check(
            "ruled out" if ev.panic_record == "empty" else ("confirmed" if ev.panic_record == "present" else "unknown"),
            "kernel panic",
            ev.pstore_note
            or ("pstore has a record" if ev.panic_record == "present" else "no pstore evidence either way"),
        )
    )

    if ev.watchdog_armed is True:
        checks.append(Check("confirmed", "hardware watchdog", ev.watchdog_detail or "armed"))
    elif ev.watchdog_armed is False:
        detail = "no watchdog device in that boot"
        if ev.watchdog_denylist:
            detail += "; deny-listed: " + ", ".join(
                f"{module} ({ev.blacklists.get(module, 'modprobe.d')})" for module in ev.watchdog_denylist
            )
        checks.append(Check("ruled out", "hardware watchdog", detail))
    else:
        checks.append(Check("unknown", "hardware watchdog", "no journal evidence"))

    hardware_hits = [name for name in ("hardware error", "thermal") if ev.counts.get(name)]
    checks.append(
        Check(
            "confirmed" if hardware_hits else "ruled out",
            "hardware",
            ", ".join(f"{name} x{ev.counts[name]}" for name in hardware_hits)
            if hardware_hits
            else "no MCE/EDAC/AER/thermal line in this boot",
        )
    )

    snapshot = ev.sessions[-1].snapshot[0] if ev.sessions and ev.sessions[-1].snapshot else ""
    window_sessions = sessions_in_window(ev)
    if window_sessions and window_sessions[-1].snapshot:
        snapshot = window_sessions[-1].snapshot[0]
    if ev.counts.get("out of memory") or ev.counts.get("oom"):
        checks.append(Check("confirmed", "host memory", f"oom-kill x{ev.counts.get('out of memory', 0)}"))
    else:
        detail = "no oom-kill in this boot"
        numbers = parsers.parse_snapshot_line(snapshot) if snapshot else {}
        if numbers:
            detail += (
                f"; last start: {numbers.get('mem_available', '?')} MiB available, swap {numbers.get('swap', '?')}"
            )
        checks.append(Check("ruled out", "host memory", detail))

    if ev.boot_end is not None and ev.last_kernel is not None:
        gap = (ev.boot_end - ev.last_kernel).total_seconds()
        if gap > FREEZE_GAP_SECONDS:
            checks.append(
                Check(
                    "confirmed",
                    "kernel stopped first",
                    f"the kernel log ends {int(gap)} s before the last journal record",
                )
            )
        else:
            checks.append(Check("ruled out", "kernel stopped first", "the kernel was logging until the end"))
    else:
        checks.append(Check("unknown", "kernel stopped first", "no kernel log or no journal"))

    stalled, stall_detail = journal_stalled(ev)
    if stalled:
        checks.append(Check("confirmed", "journal stalled", stall_detail))
    elif stall_detail:
        checks.append(Check("ruled out", "journal stalled", stall_detail))

    aborts = [core for core in ev.coredumps if core.culprit not in NOISE_PROCESSES]
    checks.append(
        Check(
            "confirmed" if aborts else "ruled out",
            "process abort",
            ", ".join(f"pid {core.pid} {core.signal}" for core in aborts[:3])
            if aborts
            else "no core dump in this boot",
        )
    )

    checks.append(Check(*_gpu_check(ev)))
    if ev.silence_s is not None:
        checks.append(
            Check(
                "confirmed",
                "silent window",
                f"{int(ev.silence_s)} s between the last write and power-on",
            )
        )
    checks.append(
        Check(
            "unknown",
            "root cause",
            "a platform freeze leaves no trace on the machine; only netconsole,"
            " ramoops or a serial console can name it",
        )
    )
    return checks


# -- verdict -----------------------------------------------------------------
def decide(ev: Evidence) -> Verdict:
    """Map the evidence to one cause. Ordered by specificity, not by guesswork."""
    counts = ev.counts
    if not ev.checks:
        ev.checks = build_checks(ev)
    gap = journal_gap(ev)
    age = signature_age(ev, newest_signature(ev))
    fresh_watchdog = is_fresh(ev, "watchdog lockup")

    if ev.panic_record == "present":
        text = "\n".join(text for _name, text in ev.pstore)
        return Verdict(
            "kernel-panic",
            "kernel panic: pstore kept a firmware record of it",
            [f"pstore: {name}" for name, _text in ev.pstore] + parsers.tail_lines(text, 6),
            ["read the pstore block above", "crashlab coredump <pid> for the process side"],
        )

    if counts.get("kernel panic"):
        return Verdict(
            "kernel-panic",
            f"kernel panic: {_panic_reason(ev)}",
            _pick(ev, "kernel panic") + _panic_frames(ev),
            ["read the panic block above", "crashlab coredump <pid> for the process side"],
        )

    stalled, stall_detail = journal_stalled(ev)
    if stalled and not fresh_watchdog:
        return Verdict(
            "freeze",
            "the system stopped scheduling: journald went silent while the application kept logging",
            [stall_detail],
            [
                "lower kernel.watchdog_thresh and kernel.hung_task_timeout_secs so the freeze panics",
                "arm a hardware watchdog and set RuntimeWatchdogSec",
            ],
        )

    if counts.get("hardware error") or counts.get("thermal"):
        return Verdict(
            "hardware",
            "hardware: machine check or thermal-critical events in the kernel log",
            _pick(ev, "hardware error", "thermal"),
            ["run a hardware stress test outside ComfyUI", "check sensors and EDAC counters"],
        )

    if fresh_watchdog:
        return Verdict(
            "kernel-lockup",
            "kernel lockup: a watchdog fired, the machine stopped scheduling",
            _sig_evidence(ev, "watchdog lockup"),
            ["keep hardlockup_panic and pstore enabled", "capture the lockup stack on the next event"],
        )

    if is_fresh(ev, "GPU hang/timeout", "GPU page fault"):
        return Verdict(
            "gpu-hang",
            "GPU driver: hang, reset or page fault in amdgpu",
            _sig_evidence(ev, "GPU hang/timeout", "GPU page fault"),
            ["retry with --disable-pinned-memory and --lowvram", "compare ROCm and kernel versions"],
        )

    if counts.get("out of memory"):
        return Verdict(
            "oom",
            "host memory exhausted: the kernel killed processes (OOM)",
            _sig_evidence(ev, "out of memory"),
            ["enable swap", "lower ComfyUI pinned memory"],
        )

    if is_fresh(ev, "framebuffer pin failed", "amdgpu pin failed"):
        vram = _last_gpu(ev)
        return Verdict(
            "gpu-memory",
            "GPU memory accounting: the display plane could not pin its framebuffer (ENOMEM)",
            _sig_evidence(ev, "framebuffer pin failed", "amdgpu pin failed") + ([vram] if vram else []),
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

    if not ev.kernel and not ev.events:
        return Verdict(
            "unlogged",
            "inconclusive: the journal of that boot is gone and no application log covers it",
            [f"journal has {len(ev.logs)} log file(s) but no kernel or application lines"],
            ["apply the durable logging configuration (provision.sh system)", "rerun after the next crash"],
        )

    if age is not None and age > STALE_SIGNATURE_SECONDS:
        return _platform_or_silent(ev, age)

    return _platform_or_silent(ev, age)


def _platform_or_silent(ev: Evidence, age: float | None) -> Verdict:
    """No fresh kernel signature: decide between a platform freeze and "unknown"."""
    stale = [
        f"{name} x{window.hits} ({_clock(window.first, ev.boot_start)}..{_clock(window.last, ev.boot_start)})"
        for name, window in sorted(ev.windows.items(), key=lambda item: -item[1].hits)
    ]
    if not stale:
        stale = [
            f"{name} x{hits} (last one {int((age or 0) // 60)} min before the reset)"
            for name, hits in sorted(ev.counts.items(), key=lambda item: -item[1])
        ]
    last_kernel = _tail_line(ev.kernel)
    evidence = stale[:4]
    if last_kernel:
        evidence.append(f"last kernel line: {last_kernel[:150]}")

    if ev.panic_record == "empty" and ev.watchdog_armed is False:
        if ev.silence_s is not None:
            evidence.insert(0, f"{int(ev.silence_s)} s of complete silence before power-on")
        if ev.watchdog_denylist:
            evidence.append(
                "watchdog module deny-listed: "
                + ", ".join(f"{name} by {ev.blacklists.get(name, 'modprobe.d')}" for name in ev.watchdog_denylist)
            )
        evidence.append("no panic record: efi_pstore was registered and systemd-pstore found /sys/fs/pstore empty")
        starts = sum(1 for event in ev.events if event.kind == "session-start")
        churn = f"; {starts} GPU contexts came and went in this boot" if starts else ""
        return Verdict(
            "platform-freeze",
            "the machine stopped executing with no kernel message. It did not panic and did not"
            " reboot itself (no hardware watchdog was armed), so the reset was external" + churn,
            evidence,
            [
                "arm a hardware watchdog: iTCO_wdt is deny-listed, so modules-load.d cannot load it",
                "add netconsole to a second machine: it is the only way to read the last lines of a freeze",
                "run `sudo ls /sys/fs/pstore` after the next event to confirm the panic path is still untouched",
                "reduce GPU context churn: one ComfyUI start per session, no parallel test cycles",
            ],
        )

    steps = [
        "apply the durable logging configuration (./provision.sh system, run by the user)",
        "compare the boot end time with logs/session.log",
    ]
    if any("truncated" in note for note in ev.notes):
        steps.insert(0, "journald lost the tail of this boot: the logging configuration is not applied yet")
    lost = [f"{log.name}: {log.zero_tail} zero bytes at the end" for log in ev.logs if log.zero_tail]
    return Verdict(
        "silent-reset",
        "the machine died without a fresh kernel signature; the panic and watchdog questions are unanswered",
        lost + evidence,
        steps,
    )


# -- rendering ---------------------------------------------------------------
def _clock(moment: datetime | None, reference: datetime | None = None) -> str:
    if moment is None:
        return "?"
    if reference is not None and moment.date() == reference.date():
        return moment.strftime("%H:%M:%S")
    return moment.strftime("%Y-%m-%d %H:%M:%S")


def _tail_line(lines: list[str]) -> str:
    return lines[-1] if lines else ""


def _duration(seconds: float | None) -> str:
    if seconds is None:
        return "?"
    total = int(seconds)
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


def timeline(ev: Evidence, limit: int = 18) -> list[str]:
    """The last events of the boot, one line each, human ordered."""
    rows: list[tuple[datetime, str]] = []
    latency = prompt_latency(ev)
    finished, median = generation_stats(ev)
    for index, event in enumerate(ev.events):
        if event.kind == "start":
            continue
        text = _event_text(event, index, latency, finished, median)
        if text:
            rows.append((event.at, text))
    for name, window in ev.windows.items():
        if window.last is None:
            continue
        span = (
            _clock(window.first, ev.boot_start)
            if window.first == window.last
            else f"{_clock(window.first, ev.boot_start)}..{_clock(window.last, ev.boot_start)}"
        )
        rows.append((window.last, f"kernel · {name} x{window.hits} ({span})"))
    for session in sessions_in_window(ev):
        moment = parsers.parse_stamp(session.started)
        if moment is None:
            continue
        numbers = parsers.parse_snapshot_line(session.snapshot[0]) if session.snapshot else {}
        detail = ""
        if numbers:
            detail = f" (available {numbers.get('mem_available', '?')} MiB, swap {numbers.get('swap', '?')} MiB)"
        rows.append((moment, f"launcher · server start pid {session.pid}{detail}"))
    rows.sort(key=lambda row: row[0])
    return [f"{_clock(moment, ev.boot_start)}  {text}" for moment, text in rows[-limit:]]


def _event_text(
    event: parsers.AppEvent,
    index: int,
    latency: dict[int, float],
    finished: int,
    median: float | None,
) -> str:
    if event.kind == "prompt":
        delta = latency.get(index)
        if delta is not None and delta <= PROGRAMMATIC_PROMPT_SECONDS:
            return f"prompt queued {delta:.1f} s after the server came up (programmatic, not typed)"
        return "prompt submitted"
    if event.kind == "done":
        try:
            seconds = float(event.detail)
        except (TypeError, ValueError):
            return "generation finished"
        text = f"generation finished in {seconds:.0f} s"
        if finished >= 3 and median and seconds > SLOW_GENERATION_FACTOR * median:
            text += f" (slow: {seconds / median:.0f}x the median of {finished})"
        return text
    if event.kind == "pinned":
        return f"ComfyUI pinned-memory ceiling {event.detail} MiB"
    if event.kind == "vram":
        return f"ComfyUI sees VRAM/RAM {event.detail} MB"
    if event.kind == "generate-start":
        return "generate.sh started"
    if event.kind == "generate-ready":
        return "generate.sh: ComfyUI ready"
    if event.kind == "generate-step":
        return f"generate.sh · last step reached: {event.detail}"
    return ""


def drift(ev: Evidence) -> list[str]:
    """Configuration that changed between boots and can explain a new crash."""
    rows: list[str] = []
    if ev.boot_start and ev.boot_list_start:
        listed = parsers.parse_stamp(ev.boot_list_start)
        if listed and abs((listed - ev.boot_start).total_seconds()) > 120:
            rows.append(
                f"wall clock was off by {_duration(abs((listed - ev.boot_start).total_seconds()))} at boot: "
                f"journal list says {listed:%Y-%m-%d %H:%M:%S}, monotonic clock says {ev.boot_start:%Y-%m-%d %H:%M:%S}"
            )
    changes = _pinned_changes(ev)
    rows.extend(changes)
    for module in ev.watchdog_denylist:
        rows.append(f"{module} is deny-listed by {ev.blacklists.get(module, 'modprobe.d')} (no watchdog armed)")
    if ev.startup:
        total = ev.startup.get("firmware", 0.0) + ev.startup.get("loader", 0.0)
        if total:
            rows.append(f"firmware+loader take {total:.1f} s before the kernel starts (used for the power-on estimate)")
    return rows


def _pinned_changes(ev: Evidence) -> list[str]:
    if len(ev.pinned) < 2:
        return []
    distinct: list[tuple[datetime, int]] = []
    for moment, mib in ev.pinned:
        if not distinct or distinct[-1][1] != mib:
            distinct.append((moment, mib))
    if len(distinct) < 2:
        return []
    rows: list[str] = []
    for (moment, mib), (_, previous) in zip(distinct[1:], distinct, strict=False):
        rows.append(
            f"pinned-memory ceiling {previous} -> {mib} MiB at {moment:%Y-%m-%d %H:%M:%S}{_swap_transition(ev, moment)}"
        )
    return rows


def _swap_transition(ev: Evidence, moment: datetime) -> str:
    """Name the mechanism behind a pinned-ceiling jump.

    ComfyUI sizes MAX_PINNED_MEMORY as
    ``max(ram*0.40, min(ram*0.90, ram-4GiB, ram+swap-16GiB))``
    (ComfyUI/comfy/model_management.py). Without swap the last term wins and the
    ceiling is ``ram-16GiB``; as soon as a 16 GiB swapfile exists it becomes
    ``ram-4GiB``, i.e. 12 GiB more on this machine. That is a *drift* between
    boots, not a ComfyUI setting, so it belongs in the report.
    """
    rows = _swap_totals(ev)
    owner = None
    index = -1
    for position, (started, _total) in enumerate(rows):
        if started <= moment:
            owner = position
    if owner is None:
        return ""
    index = owner
    before = rows[index - 1][1] if index > 0 else None
    after = rows[index][1]
    if before is not None and after is not None and before != after:
        if before == 0 and after > 0:
            return (
                f"; the {after} MiB swapfile became active, so ComfyUI's pinned cap moved"
                f" from ram-16GiB to ram-4GiB (model_management.py)"
            )
        if after == 0:
            return f"; swap removed ({before} -> 0 MiB), so the pinned cap fell back to ram-16GiB"
        return f"; swap changed {before} -> {after} MiB, which moves ComfyUI's pinned cap"
    if after is not None:
        return f"; swap {after} MiB"
    return ""


def _swap_totals(ev: Evidence) -> list[tuple[datetime, int | None]]:
    rows: list[tuple[datetime, int | None]] = []
    for session in ev.sessions:
        started = parsers.parse_stamp(session.started)
        if not started or not session.snapshot:
            continue
        raw = parsers.parse_snapshot_line(session.snapshot[0]).get("swap", "")
        total = raw.split("/", 1)[-1]
        rows.append((started, int(total) if total.isdigit() else None))
    return rows


def render(ev: Evidence, verdict: Verdict, generated: str | None = None) -> str:
    out = ["== crashlab =="]
    out.append(f"generated: {generated or datetime.now(UTC).astimezone().strftime('%Y-%m-%dT%H:%M:%S%z')}")
    out.append(boot_line(ev))
    if ev.next_boot is not None:
        out.append(next_boot_line(ev))
    if ev.silence_s is not None:
        out.append(f"silence:   {int(ev.silence_s)} s between the last write and power-on")
    out.append("")
    out.append(f"CAUSE:     {verdict.cause}")
    out.append(f"VERDICT:   {verdict.summary}")
    for note in ev.notes:
        out.append(f"note:      {note}")

    if ev.checks:
        out.append("")
        out.append("decided by:")
        out.extend(f"  {check.render()}" for check in ev.checks)

    if verdict.evidence:
        out.append("")
        out.append("evidence:")
        out.extend(f"  {line}" for line in verdict.evidence)

    rows = timeline(ev)
    if rows:
        out.append("")
        out.append("what ran (last events of the boot):")
        out.extend(f"  {row}" for row in rows)

    if ev.app_stop:
        out.append("")
        out.append("log stop order:")
        out.append("  " + " | ".join(f"{name} {_clock(moment)}" for name, moment in ev.app_stop))

    rows = drift(ev)
    if rows:
        out.append("")
        out.append("drift:")
        out.extend(f"  {row}" for row in rows)

    if ev.history:
        out.append("")
        out.append("boot history:")
        for row in ev.history:
            found = ", ".join(f"{name} x{hits}" for name, hits in row["signatures"].items()) or "no signatures"
            out.append(f"  boot {row['boot']}: {found}")

    if verdict.next_steps:
        out.append("")
        out.append("next steps:")
        out.extend(f"  - {step}" for step in verdict.next_steps)
    out.append("")
    out.append("run `crashlab report --full` for every raw line behind this verdict.")
    return "\n".join(out)


def boot_line(ev: Evidence) -> str:
    state = {True: "clean", False: "unclean", None: "unknown"}[ev.boot_clean]
    index = ev.boot.index if ev.boot else "?"
    start = _clock(ev.boot_start) if ev.boot_start else (ev.boot.start if ev.boot else "")
    if ev.boot_start and ev.boot_start.date() != datetime.now().date():
        start = ev.boot_start.strftime("%Y-%m-%d %H:%M:%S")
    end = _clock(ev.boot_end) if ev.boot_end else (ev.boot.end if ev.boot else "")
    ran = f", ran {_duration(ev.boot_runtime_s)}" if ev.boot_runtime_s else ""
    return f"boot:      {index} started {start}{ran}, ended {end} ({state})"


def next_boot_line(ev: Evidence) -> str:
    assert ev.next_boot is not None
    parts = [f"next boot: {ev.next_boot.index}"]
    if ev.power_on is not None:
        parts.append(f"power-on {_clock(ev.power_on, ev.power_on)}")
    if ev.next_start is not None:
        parts.append(f"kernel {_clock(ev.next_start, ev.next_start)}")
    firmware = ev.startup.get("firmware")
    loader = ev.startup.get("loader")
    if firmware is not None and loader is not None:
        parts.append(f"firmware+loader {firmware + loader:.1f} s")
    return "  ".join(parts)


def render_full(ev: Evidence, verdict: Verdict, generated: str | None = None) -> str:
    """Everything, for when the compact view is not enough."""
    out = [render(ev, verdict, generated), "", "== raw evidence =="]
    if ev.counts:
        out.append("kernel signatures:")
        for name, hits in sorted(ev.counts.items(), key=lambda item: -item[1]):
            window = ev.windows.get(name)
            span = (
                f" ({_clock(window.first, ev.boot_start)}..{_clock(window.last, ev.boot_start)})"
                if window and window.first
                else ""
            )
            out.append(f"  {hits:>5}  {name}{span}")
    if ev.coredumps:
        out.append("core dumps:")
        for core in ev.coredumps:
            out.append(f"  {core.timestamp}  pid {core.pid:<7} {core.signal:<8} {core.command[:100]}")
    if ev.logs:
        out.append("log files (newest first):")
        for log in ev.logs:
            mark = f"  [{log.zero_tail} zeros]" if log.zero_tail else ""
            out.append(f"  {log.name}{mark}")
            out.extend(f"      {line[:160]}" for line in log.lines)
    if ev.pstore:
        out.append("pstore:")
        for name, text in ev.pstore:
            out.append(f"  --- {name}")
            out.extend(f"  {line[:160]}" for line in text.splitlines()[-12:])
    if ev.kernel:
        out.append("kernel tail:")
        out.extend(f"  {line[:170]}" for line in ev.kernel[-15:])
    if ev.config or ev.journal_last:
        out.append("logging chain:")
        for key, value in ev.config.items():
            out.append(f"  {key}: {value}")
        if ev.journal_last:
            out.append(f"  last journal entry: {ev.journal_last[:120]}")
    return "\n".join(out)


def _panic_reason(ev: Evidence) -> str:
    for line in reversed(ev.kernel):
        if "Kernel panic" in line:
            return line.split("kernel:", 1)[-1].strip()[:160]
    return "see the trace below"


def _panic_frames(ev: Evidence, limit: int = 8) -> list[str]:
    """RIP plus the Call Trace block: the two things that name the culprit."""
    frames: list[str] = []
    for line in ev.kernel:
        body = line.split("kernel:", 1)[-1].strip()
        if "RIP:" in body:
            frames.append(body[:170])
        if len(frames) >= limit:
            return frames
    in_trace = False
    for line in ev.kernel:
        body = line.split("kernel:", 1)[-1].strip()
        if body.startswith("Call Trace"):
            in_trace = True
            continue
        if not in_trace:
            continue
        if body.startswith("---[") or body.startswith("</TASK>") or not body:
            break
        frames.append(body[:170])
        if len(frames) >= limit:
            break
    return frames


def _pick(ev: Evidence, *names: str) -> list[str]:
    lines: list[str] = []
    for name in names:
        for sample in ev.samples.get(name, [])[:2]:
            lines.append(sample[:170])
    return lines


def _sig_evidence(ev: Evidence, *names: str) -> list[str]:
    """One line per signature: how many, when, and how long before the kill."""
    rows: list[str] = []
    for name in names:
        window = ev.windows.get(name)
        if window and window.hits:
            age = signature_age_of(ev, name)
            when = f"{_clock(window.first, ev.boot_start)}..{_clock(window.last, ev.boot_start)}"
            tail = f"; last one {_duration(age)} before the end" if age is not None else ""
            rows.append(f"{name} x{window.hits} ({when}{tail})")
            continue
        rows.extend(sample[:170] for sample in ev.samples.get(name, [])[:2])
    return rows


def _gpu_check(ev: Evidence) -> tuple[str, str, str]:
    rows: list[str] = []
    stale = True
    for name in (
        "framebuffer pin failed",
        "amdgpu pin failed",
        "GPU page fault",
        "GPU hang/timeout",
        "KFD teardown hog",
    ):
        window = ev.windows.get(name)
        if not window or window.last is None:
            continue
        age = signature_age_of(ev, name)
        if age is None or age <= STALE_SIGNATURE_SECONDS:
            stale = False
        parts = [f"{name} x{window.hits}"]
        if window.first != window.last:
            parts.append(f"({_clock(window.first, ev.boot_start)}..{_clock(window.last, ev.boot_start)})")
        rows.append(" ".join(parts))
    if not rows:
        return ("ruled out", "GPU signature", "no GPU signature in this boot")
    detail = "; ".join(rows)
    if stale:
        return ("context", "GPU signatures", f"earlier in the boot, not at the kill: {detail}")
    return ("confirmed", "GPU signatures", detail)


def _last_gpu(ev: Evidence) -> str:
    return ev.gpu[0] if ev.gpu else ""
