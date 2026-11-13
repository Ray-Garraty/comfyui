"""crashlab command line: report / boots / timeline / coredump / snapshot."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from . import __version__, parsers
from . import verdict as verdict_mod
from .host import Host

DEFAULT_ROOTS = ("/home/vlabe/Documents/comfyui", ".")
BOOT_KERNEL = 600
LOG_TAIL = 3
LOG_FILES = 12


def repo_root() -> Path:
    for candidate in DEFAULT_ROOTS:
        path = Path(candidate)
        if (path / "logs").is_dir():
            return path.resolve()
    return Path.cwd()


def collect_report(host: Host, repo: Path, boot: str) -> verdict_mod.Evidence:
    ev = verdict_mod.Evidence()

    for item in parsers.parse_list_boots("\n".join(host.lines(["journalctl", "--list-boots", "--no-pager"], 20))):
        if str(item.index) == boot:
            ev.boot = item
    if ev.boot is None:
        ev.notes.append(f"boot {boot} not found in the journal")

    tail = host.lines(["journalctl", "-b", boot, "-n", "10", "--no-pager"], 20)
    ev.boot_clean = parsers.is_clean_shutdown(tail) if tail else None

    ev.boot_end = parsers.parse_stamp(ev.boot.end) if ev.boot else None
    ev.kernel = host.lines(
        ["journalctl", "-b", boot, "-k", "-n", str(BOOT_KERNEL), "--no-pager", "-o", "short-iso"], 60
    )
    if not ev.kernel:
        ev.notes.append("no kernel lines for that boot (journald not persistent, or journal incomplete)")
    ev.counts = parsers.count_signatures(ev.kernel)
    ev.samples = parsers.signature_samples(ev.kernel)

    since = ev.boot.start if ev.boot else ""
    argv = ["coredumpctl", "list", "--no-pager"] + (["--since", since] if since else [])
    start = parsers.parse_stamp(ev.boot.start) if ev.boot else None
    end = parsers.parse_stamp(ev.boot.end) if ev.boot else None
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
    if ev.boot and end and journal_tail:
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
    }

    for offset in range(0, 3):
        index = (ev.boot.index if ev.boot else -1) - offset
        lines = host.lines(
            ["journalctl", "-b", str(index), "-k", "-n", str(BOOT_KERNEL), "--no-pager", "-o", "short-iso"], 60
        )
        if not lines:
            continue
        ev.history.append({"boot": index, "signatures": parsers.count_signatures(lines)})

    log_dir = repo / "logs"
    for name in host.listdir(log_dir):
        if not name.endswith(".log"):
            continue
        path = log_dir / name
        text = host.read_text(path)
        raw = host.read_bytes(path)
        if not text and not raw:
            continue
        ev.logs.append(
            parsers.LogTail(
                name=name,
                mtime=host.mtime(path),
                lines=parsers.tail_lines(text, LOG_TAIL),
                zero_tail=parsers.zero_tail(raw),
            )
        )
    ev.logs.sort(key=lambda item: item.mtime, reverse=True)
    ev.logs = ev.logs[:LOG_FILES]

    boot_start = parsers.parse_stamp(ev.boot.start) if ev.boot else None
    # The journal's last entry is not the crash time: it is only the last record
    # that reached the disk. The real upper bound is the next boot's start.
    bound = None
    if ev.boot is not None:
        later = [
            item
            for item in parsers.parse_list_boots(
                "\n".join(host.lines(["journalctl", "--list-boots", "--no-pager"], 20))
            )
            if item.index > ev.boot.index
        ]
        if later:
            bound = parsers.parse_stamp(min(later, key=lambda item: item.index).start)
    ev.logs = parsers.logs_in_window(ev.logs, boot_start, bound)
    newest = max((log.mtime for log in ev.logs), default=0.0)
    if newest:
        ev.log_mtime = datetime.fromtimestamp(newest)

    ev.sessions = parsers.parse_sessions(host.read_text(log_dir / "session.log"))

    records, pstore_error = host.listdir_checked("/sys/fs/pstore")
    if pstore_error:
        ev.config["pstore"] = f"unreadable: {pstore_error} (root required)"
    for name in records:
        text = host.read_text(Path("/sys/fs/pstore") / name)
        if text:
            ev.pstore.append((name, text[-4000:]))

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
            "verdict": result.to_dict(),
            "signatures": ev.counts,
            "coredumps": [core.to_dict() for core in ev.coredumps],
            "sessions": [session.to_dict() for session in ev.sessions],
            "history": ev.history,
            "logs": [log.to_dict() for log in ev.logs],
            "gpu": ev.gpu,
            "pstore": [{"name": name, "text": text} for name, text in ev.pstore],
            "notes": ev.notes,
        }
        text = json.dumps(payload, indent=2)
    elif args.brief:
        index = ev.boot.index if ev.boot else "?"
        start = ev.boot.start if ev.boot else ""
        end = ev.boot.end if ev.boot else ""
        lines = [f"boot:      {index} {start} -> {end}"]
        lines.append(f"CAUSE:     {result.cause}")
        lines.append(f"VERDICT:   {result.summary}")
        lines.extend(f"  {line}" for line in result.evidence[:5])
        chain = ", ".join(f"{key}={value}" for key, value in ev.config.items())
        if chain:
            lines.append(f"chain:     {chain}")
        lines.append("next:      " + (result.next_steps[0] if result.next_steps else "-"))
        text = "\n".join(lines)
    else:
        text = verdict_mod.render(ev, result)
    print(text)
    if args.out:
        target = Path(args.out)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text + "\n", encoding="utf-8")
        print(f"written: {target}", file=sys.stderr)
    return 0


def cmd_boots(host: Host, args) -> int:
    boots = parsers.parse_list_boots("\n".join(host.lines(["journalctl", "--list-boots", "--no-pager"], 20)))
    rows = []
    newest = max((item.index for item in boots), default=0)
    for item in boots:
        tail = host.lines(["journalctl", "-b", str(item.index), "-n", "10", "--no-pager"], 20)
        clean = None if item.index == newest else (parsers.is_clean_shutdown(tail) if tail else None)
        rows.append({"boot": item.to_dict(), "clean": clean, "running": item.index == newest})
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    for row in rows:
        state = "running" if row["running"] else {True: "clean", False: "UNCLEAN", None: "unknown"}[row["clean"]]
        boot = row["boot"]
        print(f"{boot['index']:>3}  {boot['start']}  {boot['end']}  {state}")
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
