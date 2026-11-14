"""crashlab command line: report / boots / timeline / coredump / snapshot."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from . import __version__, parsers
from . import verdict as verdict_mod
from .host import Host

DEFAULT_ROOTS = ("/home/vlabe/Documents/comfyui", ".")
BOOT_KERNEL = 600
LOG_TAIL = 3
LOG_FILES = 12
JOURNAL_BOOTS = 20


def repo_root() -> Path:
    for candidate in DEFAULT_ROOTS:
        path = Path(candidate)
        if (path / "logs").is_dir():
            return path.resolve()
    return Path.cwd()


def collect_report(host: Host, repo: Path, boot: str) -> verdict_mod.Evidence:
    ev = verdict_mod.Evidence()

    boots = parsers.parse_list_boots("\n".join(host.lines(["journalctl", "--list-boots", "--no-pager"], JOURNAL_BOOTS)))
    for item in boots:
        if str(item.index) == boot:
            ev.boot = item
    if ev.boot is None:
        ev.notes.append(f"boot {boot} not found in the journal")
        return ev
    ev.boot_list_start = ev.boot.start

    tail = host.lines(["journalctl", "-b", boot, "-n", "10", "--no-pager"], 20)
    ev.boot_clean = parsers.is_clean_shutdown(tail) if tail else None

    ev.kernel = host.lines(
        ["journalctl", "-b", boot, "-k", "-n", str(BOOT_KERNEL), "--no-pager", "-o", "short-iso"], 60
    )
    if not ev.kernel:
        ev.notes.append("no kernel lines for that boot (journald not persistent, or journal incomplete)")
    ev.counts = parsers.count_signatures(ev.kernel)
    ev.samples = parsers.signature_samples(ev.kernel)
    ev.windows = parsers.signature_windows(ev.kernel)
    ev.last_kernel = _stamp_of(ev.kernel[-1]) if ev.kernel else None

    # The boot list's start is the RTC read at boot: this machine steps the
    # clock after NTP, which makes it later than the end. The monotonic clock
    # cannot lie, so boot_start = last_realtime - last_monotonic.
    anchor = _anchor(host, boot)
    ev.boot_start = anchor.boot_start if anchor else _parse(ev.boot.start)
    if anchor:
        ev.boot_runtime_s = anchor.monotonic_s
        ev.boot_end = anchor.realtime
    else:
        ev.boot_end = _parse(ev.boot.end)
        ev.notes.append("no journal anchor for that boot: the start time is the RTC stamp")

    ev.next_boot = next((item for item in boots if item.index > ev.boot.index), None) if ev.boot else None
    if ev.next_boot is not None:
        next_anchor = _anchor(host, str(ev.next_boot.index))
        if next_anchor:
            ev.next_start = next_anchor.boot_start
    newest = max((item.index for item in boots), default=0)
    ev.startup = parsers.parse_systemd_analyze("\n".join(host.lines(["systemd-analyze"], 30)))
    if ev.startup and ev.next_boot is not None and ev.next_boot.index == newest and ev.next_start:
        warmup = ev.startup.get("firmware", 0.0) + ev.startup.get("loader", 0.0)
        ev.power_on = ev.next_start - timedelta(seconds=warmup)

    argv = ["coredumpctl", "list", "--no-pager", "--since", ev.boot.start]
    start = ev.boot_start
    end = ev.boot_end
    for core in parsers.parse_coredump_list("\n".join(host.lines(argv, 30))):
        moment = parsers.parse_stamp(core.timestamp)
        if start and moment and moment < start:
            continue
        if end and moment and moment > end:
            continue
        ev.coredumps.append(core)

    journal_tail = host.lines(["journalctl", "-b", boot, "-n", "1", "--no-pager", "-o", "short-iso"], 20)
    if journal_tail:
        ev.journal_last = journal_tail[-1]
    if end and journal_tail:
        # The journal (not the kernel) is what must reach the disk: an idle
        # kernel logging nothing is normal, a journal that stops minutes before
        # the reset means the sync settings are not active.
        last_write = parsers.parse_stamp(ev.journal_last.split()[0])
        if last_write and (end - last_write).total_seconds() > 300:
            minutes = int((end - last_write).total_seconds() // 60)
            ev.notes.append(f"journal stops {minutes} min before the boot ended (durable logging not active)")

    ev.config = {
        "journald drop-in": _state(host, "/etc/systemd/journald.conf.d/99-comfyui.conf"),
        "rsyslog drop-in": _state(host, "/etc/rsyslog.d/49-comfyui-crash.conf"),
        "watchdog panic": _sysctl(host, "kernel.hardlockup_panic"),
        "panic timeout": _sysctl(host, "kernel.panic"),
        "hardware watchdog": _watchdog_state(host),
        "systemd watchdog device": host.lines(["systemctl", "show", "-p", "WatchdogDevice"], 10)[0].split("=", 1)[-1]
        or "none",
    }

    for offset in range(0, 3):
        index = ev.boot.index - offset
        lines = host.lines(
            ["journalctl", "-b", str(index), "-k", "-n", str(BOOT_KERNEL), "--no-pager", "-o", "short-iso"], 60
        )
        if not lines:
            continue
        ev.history.append({"boot": index, "signatures": parsers.count_signatures(lines)})

    ev.blacklists = _blacklists(host)
    _collect_watchdog(host, ev, boot)
    _collect_pstore(host, ev)
    _collect_logs(host, repo, ev)
    _collect_silence(ev)

    for name in host.listdir("/sys/class/drm"):
        if not name.startswith("card"):
            continue
        base = Path("/sys/class/drm") / name / "device"
        total = host.read_text(base / "mem_info_vram_total").strip()
        if not total:
            continue
        used = host.read_text(base / "mem_info_vram_used").strip()
        gtt_used = host.read_text(base / "mem_info_gtt_used").strip()
        gtt_total = host.read_text(base / "mem_info_gtt_total").strip()
        ev.gpu.append(f"{name}: vram {_mib(used)}/{_mib(total)} MiB, gtt {_mib(gtt_used)}/{_mib(gtt_total)} MiB")

    return ev


def _anchor(host: Host, boot: str) -> parsers.JournalAnchor | None:
    return parsers.parse_journal_anchor(
        "\n".join(host.lines(["journalctl", "-b", boot, "-n", "1", "-o", "json", "--no-pager"], 20))
    )


def _stamp_of(line: str) -> datetime | None:
    return parsers.parse_stamp(line.split(" ", 1)[0])


def _parse(stamp: str) -> datetime | None:
    return parsers.parse_stamp(stamp)


def _collect_watchdog(host: Host, ev: verdict_mod.Evidence, boot: str) -> None:
    """Was a hardware watchdog armed *in that boot*? Absence is a finding."""
    lines = host.lines(
        [
            "journalctl",
            "-b",
            boot,
            "-o",
            "short-iso",
            "--no-pager",
            "--case-sensitive=false",
            "-g",
            "hardware watchdog|watchdog device|deny-listed|Watchdog running",
        ],
        30,
    )
    armed, detail = parsers.watchdog_state(lines)
    ev.watchdog_denylist = parsers.denylisted_modules(lines)
    if armed is None and ev.watchdog_denylist:
        armed, detail = False, "kernel module deny-listed at boot"
    if armed is None and not host.listdir("/sys/class/watchdog"):
        armed, detail = False, "no watchdog device on this machine now"
    ev.watchdog_armed = armed
    ev.watchdog_detail = detail


def _collect_pstore(host: Host, ev: verdict_mod.Evidence) -> None:
    records, pstore_error = host.listdir_checked("/sys/fs/pstore")
    for name in records:
        text = host.read_text(Path("/sys/fs/pstore") / name)
        if text:
            ev.pstore.append((name, text[-4000:]))
    if ev.pstore:
        ev.panic_record = "present"
        ev.pstore_note = f"{len(ev.pstore)} record(s) in /sys/fs/pstore"
        return
    if not pstore_error:
        ev.panic_record = "empty"
        ev.pstore_note = "/sys/fs/pstore is empty"
        return

    # Root cannot read /sys/fs/pstore, but the boot after the crash already
    # archived it: systemd-pstore only skips when the directory is empty.
    after = str(ev.next_boot.index) if ev.next_boot is not None else None
    archived = host.listdir("/var/lib/systemd/pstore")
    if archived:
        ev.panic_record = "present"
        ev.pstore_note = f"systemd-pstore archived {len(archived)} record(s)"
        return
    if after:
        lines = host.lines(
            ["journalctl", "-b", after, "-o", "short-iso", "--no-pager", "-g", "pstore", "--case-sensitive=false"],
            20,
        )
        if parsers.pstore_state(lines) == "empty":
            ev.panic_record = "empty"
            ev.pstore_note = "efi_pstore registered at the next boot and /sys/fs/pstore was empty"
            return
    ev.panic_record = "unknown"
    ev.pstore_note = f"unreadable ({pstore_error}); run `sudo ls /sys/fs/pstore`"


def _collect_logs(host: Host, repo: Path, ev: verdict_mod.Evidence) -> None:
    log_dir = repo / "logs"
    start = ev.boot_start
    end = ev.power_on or ev.boot_end

    for name in host.listdir(log_dir):
        if not name.endswith(".log"):
            continue
        path = log_dir / name
        raw = host.read_bytes(path)
        if not raw:
            continue
        text = raw.decode("utf-8", "replace")
        moment = host.mtime(path)
        ev.logs.append(
            parsers.LogTail(
                name=name,
                mtime=moment,
                lines=parsers.tail_lines(text, LOG_TAIL),
                zero_tail=parsers.zero_tail(raw),
            )
        )
        written = datetime.fromtimestamp(moment) if moment else None
        if written and _in_window(written, start, end):
            ev.app_stop.append((name, written))
    ev.logs = parsers.logs_in_window(ev.logs, start, end)
    ev.logs.sort(key=lambda item: item.mtime, reverse=True)
    ev.logs = ev.logs[:LOG_FILES]
    ev.app_stop.sort(key=lambda item: item[1])
    newest = max((log.mtime for log in ev.logs), default=0.0)
    if newest:
        ev.log_mtime = datetime.fromtimestamp(newest)

    comfy = host.read_text(log_dir / "comfyui.log")
    generate = host.read_text(log_dir / "generate.log")
    events = parsers.parse_app_events(comfy, "comfyui.log")
    events += parsers.parse_app_events(generate, "generate.log")

    ev.sessions = parsers.parse_sessions(host.read_text(log_dir / "session.log"))
    for session in ev.sessions:
        moment = parsers.parse_stamp(session.started)
        if moment is None:
            continue
        numbers = parsers.parse_snapshot_line(session.snapshot[0]) if session.snapshot else {}
        detail = ""
        if numbers:
            detail = f"available {numbers.get('mem_available', '?')} MiB, swap {numbers.get('swap', '?')} MiB"
        events.append(parsers.AppEvent(moment, "session.log", "session-start", detail))

    ev.pinned = [(event.at, int(event.detail)) for event in events if event.kind == "pinned" and event.detail]
    events.sort(key=lambda event: event.at)
    last_step = max((index for index, event in enumerate(events) if event.kind == "generate-step"), default=None)
    events = [event for index, event in enumerate(events) if event.kind != "generate-step" or index == last_step]
    deduped: list[parsers.AppEvent] = []
    for event in events:
        if (
            deduped
            and deduped[-1].kind == event.kind
            and event.kind in ("generate-ready", "generate-start", "prompt")
            and (event.at - deduped[-1].at).total_seconds() <= 3
        ):
            deduped[-1] = event
            continue
        deduped.append(event)
    ev.events = [event for event in deduped if _in_window(event.at, start, end)]


def _collect_silence(ev: verdict_mod.Evidence) -> None:
    moments = [ev.log_mtime] if ev.log_mtime else []
    if ev.boot_end is not None:
        moments.append(ev.boot_end)
    moments.extend(event.at for event in ev.events)
    if not moments or ev.power_on is None:
        return
    last = max(moments)
    seconds = (ev.power_on - last).total_seconds()
    ev.silence_s = seconds if seconds >= 0 else None


def _in_window(moment: datetime, start: datetime | None, end: datetime | None) -> bool:
    if start is not None and moment < start - timedelta(minutes=1):
        return False
    return end is None or moment <= end + timedelta(minutes=1)


def _blacklists(host: Host) -> dict[str, str]:
    """Which file blacklists which module, preferring the running kernel's."""
    release = host.read_text("/proc/sys/kernel/osrelease").strip()
    found: dict[str, str] = {}
    for directory in ("/etc/modprobe.d", "/usr/lib/modprobe.d", "/run/modprobe.d"):
        for name in host.listdir(directory):
            if not name.endswith(".conf"):
                continue
            path = f"{directory}/{name}"
            preferred = bool(release) and release in name
            for module, where in parsers.parse_blacklist(host.read_text(path), path):
                if module not in found or preferred:
                    found[module] = where
    return found


def _mib(raw: str) -> str:
    try:
        return str(int(raw) // (1024 * 1024))
    except (TypeError, ValueError):
        return "?"


def _watchdog_state(host: Host) -> str:
    names = host.listdir("/sys/class/watchdog")
    if not names:
        return "MISSING (no /sys/class/watchdog entry)"
    identity = host.read_text(f"/sys/class/watchdog/{names[0]}/identity").strip()
    timeout = host.read_text(f"/sys/class/watchdog/{names[0]}/timeout").strip()
    return f"{identity or names[0]} (timeout {timeout or '?'} s)"


def _state(host: Host, path: str) -> str:
    return "installed" if host.exists(path) else "MISSING (durable logging not applied)"


def _sysctl(host: Host, name: str) -> str:
    value = host.read_text("/proc/sys/" + name.replace(".", "/")).strip()
    return f"{name}={value or '?'}"


def cmd_report(host: Host, args) -> int:
    repo = Path(args.repo).resolve() if args.repo else repo_root()
    ev = collect_report(host, repo, args.boot)
    result = verdict_mod.decide(ev)
    if args.json:
        payload = {
            "version": __version__,
            "boot": ev.boot.to_dict() if ev.boot else None,
            "boot_clean": ev.boot_clean,
            "boot_start": ev.boot_start.isoformat() if ev.boot_start else None,
            "boot_runtime_s": ev.boot_runtime_s,
            "next_boot": ev.next_boot.to_dict() if ev.next_boot else None,
            "power_on": ev.power_on.isoformat() if ev.power_on else None,
            "silence_s": ev.silence_s,
            "panic_record": ev.panic_record,
            "watchdog_armed": ev.watchdog_armed,
            "watchdog_denylist": ev.watchdog_denylist,
            "checks": [check.to_dict() for check in ev.checks],
            "verdict": result.to_dict(),
            "signatures": ev.counts,
            "signature_windows": {name: window.to_dict() for name, window in ev.windows.items()},
            "timeline": verdict_mod.timeline(ev),
            "drift": verdict_mod.drift(ev),
            "coredumps": [core.to_dict() for core in ev.coredumps],
            "sessions": [session.to_dict() for session in verdict_mod.sessions_in_window(ev)],
            "history": ev.history,
            "logs": [log.to_dict() for log in ev.logs],
            "gpu": ev.gpu,
            "pstore": [{"name": name, "text": text} for name, text in ev.pstore],
            "notes": ev.notes,
        }
        text = json.dumps(payload, indent=2)
    elif args.brief:
        lines = [verdict_mod.boot_line(ev)]
        lines.append(f"CAUSE:     {result.cause}")
        lines.append(f"VERDICT:   {result.summary}")
        lines.extend(f"  {line}" for line in result.evidence[:4])
        decided = [check for check in ev.checks if check.status != "unknown"][:4]
        if decided:
            lines.append("ruled out: " + "; ".join(f"{check.label} ({check.detail})" for check in decided))
        lines.append("next:      " + (result.next_steps[0] if result.next_steps else "-"))
        text = "\n".join(lines)
    elif args.full:
        text = verdict_mod.render_full(ev, result)
    else:
        text = verdict_mod.render(ev, result)
    print(text)
    if args.out:
        target = Path(args.out)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text + "\n", encoding="utf-8")
        print(f"written: {target}", file=sys.stderr)
    return 0


def boot_rows(host: Host) -> list[dict]:
    """Boot inventory: clean state plus the true start when the RTC was wrong."""
    boots = parsers.parse_list_boots("\n".join(host.lines(["journalctl", "--list-boots", "--no-pager"], 40)))
    rows = []
    newest = max((item.index for item in boots), default=0)
    for item in boots:
        tail = host.lines(["journalctl", "-b", str(item.index), "-n", "10", "--no-pager"], 20)
        clean = None if item.index == newest else (parsers.is_clean_shutdown(tail) if tail else None)
        start = parsers.parse_stamp(item.start)
        end = parsers.parse_stamp(item.end)
        skewed = bool(start and end and start > end)
        real = _anchor(host, str(item.index)) if skewed else None
        rows.append(
            {
                "boot": item.to_dict(),
                "clean": clean,
                "running": item.index == newest,
                "clock_skew": skewed,
                "real_start": real.boot_start.isoformat() if real else None,
                "runtime_s": real.monotonic_s if real else None,
            }
        )
    return rows


def format_boot_row(row: dict) -> str:
    state = "running" if row["running"] else {True: "clean", False: "UNCLEAN", None: "unknown"}[row["clean"]]
    boot = row["boot"]
    start = boot["start"]
    if row["real_start"]:
        try:
            real = datetime.fromisoformat(row["real_start"])
        except ValueError:
            real = None
        shown = real.strftime("%Y-%m-%d %H:%M:%S") if real else row["real_start"]
        start = f"{shown}  (clock list: {boot['start']})"
    return f"{boot['index']:>3}  {start}  {boot['end']}  {state}"


def cmd_boots(host: Host, args) -> int:
    rows = boot_rows(host)
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    for row in rows:
        print(format_boot_row(row))
    return 0


def cmd_timeline(host: Host, args) -> int:
    repo = Path(args.repo).resolve() if args.repo else repo_root()
    ev = collect_report(host, repo, args.boot)
    for log in ev.logs:
        mark = f"  [{log.zero_tail} zero bytes at the end -> hard reset]" if log.zero_tail else ""
        print(f"{log.name}{mark}")
        for line in log.lines:
            print(f"    {line}")
    return 0


def cmd_coredump(host: Host, args) -> int:
    if args.pid:
        cores = parsers.parse_coredump_list("\n".join(host.lines(["coredumpctl", "list", "--no-pager"], 30)))
        core = next((item for item in cores if item.pid == args.pid), None)
        info_text = "\n".join(host.lines(["coredumpctl", "info", str(args.pid), "--no-pager"], 120))
        info = parsers.parse_coredump_info(info_text)
        frames = parsers.parse_frames(info_text)
        if not frames and args.bt:
            frames = _gdb_frames(host, args.pid)
        payload = {
            "pid": args.pid,
            "signal": info.get("Signal", core.signal if core else ""),
            "command": info.get("Command Line", core.command if core else ""),
            "blame": parsers.blame(frames),
            "libraries": parsers.frame_libraries(frames),
            "frames": frames,
        }
        if args.json:
            print(json.dumps(payload, indent=2))
            return 0
        print(f"pid:     {args.pid}")
        print(f"signal:  {payload['signal']} {parsers.SIGNAL_MEANING.get(payload['signal'], '')}")
        print(f"command: {payload['command'][:170]}")
        print(f"blame:   {payload['blame'] or 'unknown'}")
        for frame in frames:
            print(f"  {frame[:170]}")
        return 0

    cores = parsers.parse_coredump_list("\n".join(host.lines(["coredumpctl", "list", "--no-pager"], 30)))
    if args.json:
        print(json.dumps([core.to_dict() for core in cores], indent=2))
        return 0
    for core in cores:
        print(f"{core.timestamp}  pid {core.pid:<7} {core.signal:<8} {core.culprit:<20} {core.meaning}")
    return 0


def _gdb_frames(host: Host, pid: int) -> list[str]:
    gdb = host.which("gdb") or "/usr/bin/gdb"
    with tempfile.TemporaryDirectory() as tmp:
        core = Path(tmp) / f"core.{pid}"
        dump = host.run(["coredumpctl", "dump", str(pid), "--output", str(core)], 300)
        if not dump.ok or not core.exists():
            return []
        executable = "/usr/bin/python3.12"
        result = host.run([gdb, "-q", "-batch", "-ex", "bt 25", executable, str(core)], 600)
        return parsers.parse_frames(result.out, limit=20)


def cmd_snapshot(host: Host, args) -> int:
    meminfo = {}
    for line in host.read_text("/proc/meminfo").splitlines():
        key, _, rest = line.partition(":")
        parts = rest.split()
        if parts and parts[0].isdigit():
            meminfo[key] = int(parts[0]) * 1024
    mib = 1024 * 1024
    load = host.read_text("/proc/loadavg").split()
    swap_used = (meminfo.get("SwapTotal", 0) - meminfo.get("SwapFree", 0)) // mib
    print(
        f"snapshot: mem free {meminfo.get('MemFree', 0) // mib} MiB,"
        f" available {meminfo.get('MemAvailable', 0) // mib} MiB,"
        f" swap {swap_used}/{meminfo.get('SwapTotal', 0) // mib} MiB,"
        f" load {' '.join(load[:3]) or '?'}"
    )
    for name in host.listdir("/sys/class/drm"):
        if not name.startswith("card"):
            continue
        base = Path("/sys/class/drm") / name / "device"
        total = host.read_text(base / "mem_info_vram_total").strip()
        if not total:
            continue
        print(
            f"  {name}: vram {_mib(host.read_text(base / 'mem_info_vram_used').strip())}/{_mib(total)} MiB,"
            f" gtt {_mib(host.read_text(base / 'mem_info_gtt_used').strip())}/"
            f"{_mib(host.read_text(base / 'mem_info_gtt_total').strip())} MiB"
        )
    return 0


def cmd_selftest(_host: Host, _args) -> int:
    from .selftest import run

    return run()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="crashlab", description="Crash forensics for this machine (read-only)")
    parser.add_argument("--version", action="version", version=f"crashlab {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    report = sub.add_parser("report", help="verdict and evidence for a boot")
    report.add_argument("--boot", default="-1", help="journal boot index (default -1: previous boot)")
    report.add_argument("--repo", default=None, help="repository root (default: auto)")
    report.add_argument("--json", action="store_true")
    report.add_argument("--out", default=None, help="write the report to this file")
    report.add_argument("--brief", action="store_true", help="cause + evidence + next step only")
    report.add_argument("--full", action="store_true", help="add every raw line behind the verdict")

    boots = sub.add_parser("boots", help="boot inventory with clean/unclean state")
    boots.add_argument("--json", action="store_true")

    timeline = sub.add_parser("timeline", help="last lines of every log file")
    timeline.add_argument("--boot", default="-1")
    timeline.add_argument("--repo", default=None)

    core = sub.add_parser("coredump", help="list core dumps or explain one")
    core.add_argument("pid", nargs="?", type=int, default=0)
    core.add_argument("--bt", action="store_true", help="run gdb when the journal has no frames")
    core.add_argument("--json", action="store_true")

    sub.add_parser("snapshot", help="current RAM/swap/VRAM/GTT")
    sub.add_parser("self-test", help="run the parser self tests")
    return parser


HANDLERS = {
    "report": cmd_report,
    "boots": cmd_boots,
    "timeline": cmd_timeline,
    "coredump": cmd_coredump,
    "snapshot": cmd_snapshot,
    "self-test": cmd_selftest,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return HANDLERS[args.command](Host(), args)


if __name__ == "__main__":
    sys.exit(main())
