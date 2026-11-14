"""Parser self-tests with canned fixtures. ``crashlab self-test`` runs these."""

from __future__ import annotations

import json
import unittest
from datetime import datetime

from . import parsers
from . import verdict as verdict_mod
from .host import Host, RunResult

LIST_BOOTS = """\
 -2 b544c54e5a88468aac0af33cbe658f1c Fri 2026-11-13 19:45:20 MSK Fri 2026-11-13 21:50:31 MSK
 -1 4bbbf1187c174a35825bfea93ad3a9bf Fri 2026-11-13 21:51:06 MSK Fri 2026-11-13 21:51:53 MSK
  0 bae93850ddd44bbf8c5962fb95c4fb21 Fri 2026-11-13 22:02:43 MSK Fri 2026-11-13 22:03:37 MSK
"""

PIN = "2026-11-13T21:46:18+03:00 vlabe-PC kernel: amdgpu 0000:03:00.0: {id} pin failed"
FRAMEBUF = (
    "2026-11-13T21:46:18+03:00 vlabe-PC kernel: "
    "[drm:amdgpu_dm_plane_helper_prepare_fb [amdgpu]] *ERROR* Failed to pin framebuffer with error -12"
)
KERNEL: str = "\n".join(
    [
        PIN.format(id="000000004132ef84"),
        FRAMEBUF,
        PIN.format(id="0000000064d4a76f"),
        FRAMEBUF,
    ]
)

COREDUMP_LIST = """\
Fri 2026-11-13 19:21:15 MSK    9614 1000 1000 SIGABRT present      /usr/bin/python3.12     391.4M
Fri 2026-11-13 21:53:57 MSK    2701 1000 1000 SIGBUS  present      /usr/share/code        35.2M
"""

COREDUMP_INFO = """\
           PID: 9614 (python)
        Signal: 6 (ABRT)
  Command Line: /home/vlabe/Documents/comfyui/venv/bin/python main.py --listen 127.0.0.1
     Executable: /usr/bin/python3.12
#0  0x00007a5685589024 in __pthread_kill_implementation (signo=6) from /lib/libc.so.6
#1  0x00007a5685001234 in abort () from /lib/libc.so.6
#2  0x00007a5670000abc in hipMalloc () from /home/vlabe/.venv/lib/libamdhip64.so.6
#3  0x00007a5660000000 in std::terminate () from /lib/libstdc++.so.6
"""

SESSION_LOG = """\
--- session 2026-11-13T22:02:10+03:00 (pid 6859) ---
2026-11-13T22:02:11+0300 snapshot: mem free 13980 MiB, available 28027 MiB, swap 0/0 MiB, load 1.64 1.48 0.92
  card1: vram 559/12272 MiB, gtt 389/16027 MiB
  reserve-vram=1.5 lowvram=0 log-level=INFO
"""


PAGE_FAULT = [
    "2026-11-13T22:18:52+03:00 vlabe-PC kernel: amdgpu 0000:03:00.0: [gfxhub] page fault (src_id:0 ring:40)",
    "2026-11-13T22:18:52+03:00 vlabe-PC kernel: amdgpu 0000:03:00.0:  Process python pid 7904 thread python pid 7904",
    "2026-11-13T22:18:52+03:00 vlabe-PC kernel: amdgpu 0000:03:00.0: GCVM_L2_PROTECTION_FAULT_STATUS:0x00841050",
    "2026-11-13T22:18:52+03:00 vlabe-PC kernel: amdgpu 0000:03:00.0:          Faulty UTCL2 client ID: TCP (0x8)",
]


class BootTests(unittest.TestCase):
    def test_boot_list_is_parsed(self) -> None:
        boots = parsers.parse_list_boots(LIST_BOOTS)
        self.assertEqual([item.index for item in boots], [-2, -1, 0])
        self.assertEqual(boots[1].boot_id[:4], "4bbb")
        self.assertEqual(boots[0].start, "Fri 2026-11-13 19:45:20 MSK")

    def test_shutdown_marker(self) -> None:
        self.assertTrue(parsers.is_clean_shutdown(["systemd[1]: Reached target poweroff.target"]))
        self.assertFalse(parsers.is_clean_shutdown(["systemd[1051]: Reached target shutdown.target - Shutdown."]))
        self.assertFalse(parsers.is_clean_shutdown(["kernel: amdgpu ..."]))


class SignatureTests(unittest.TestCase):
    def test_counts_and_samples(self) -> None:
        counts = parsers.count_signatures(KERNEL.splitlines())
        self.assertEqual(counts["amdgpu pin failed"], 2)
        self.assertEqual(counts["framebuffer pin failed"], 2)
        samples = parsers.signature_samples(KERNEL.splitlines(), limit=1)
        self.assertIn("pin failed", samples["amdgpu pin failed"][0])

    def test_gpu_page_fault_is_recognised(self) -> None:
        counts = parsers.count_signatures(PAGE_FAULT)
        self.assertEqual(counts.get("GPU page fault"), 3)

    def test_reset_is_not_confused_with_oom(self) -> None:
        counts = parsers.count_signatures(["kernel: Out of memory: Killed process 42"])
        self.assertEqual(counts.get("out of memory"), 1)
        self.assertNotIn("amdgpu pin failed", counts)


class LogTailTests(unittest.TestCase):
    def test_zero_tail_marks_hard_reset(self) -> None:
        self.assertEqual(parsers.zero_tail(b"line\n\x00\x00\x00"), 3)
        self.assertEqual(parsers.zero_tail(b"line\n"), 0)

    def test_tail_lines_are_trimmed(self) -> None:
        self.assertEqual(parsers.tail_lines("a\n\nb\nc\n", 2), ["b", "c"])

    def test_session_parsing(self) -> None:
        sessions = parsers.parse_sessions(SESSION_LOG)
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].reserve_vram, "1.5")
        self.assertIn("mem free 13980 MiB", sessions[0].snapshot[0])
        self.assertEqual(parsers.parse_snapshot_line(sessions[0].snapshot[0])["swap"], "0/0")
        self.assertEqual(parsers.parse_gpu_line("  card1: vram 559/12272 MiB, gtt 389/16027 MiB")["vram"], "559/12272")


class CoredumpTests(unittest.TestCase):
    def test_list_rows(self) -> None:
        cores = parsers.parse_coredump_list(COREDUMP_LIST)
        self.assertEqual(len(cores), 2)
        self.assertEqual(cores[0].pid, 9614)
        self.assertEqual(cores[0].signal, "SIGABRT")
        self.assertEqual(cores[0].culprit, "python3.12")
        self.assertEqual(cores[1].signal, "SIGBUS")

    def test_info_and_frames(self) -> None:
        info = parsers.parse_coredump_info(COREDUMP_INFO)
        self.assertEqual(info["Signal"], "6 (ABRT)")
        frames = parsers.parse_frames(COREDUMP_INFO)
        self.assertEqual(len(frames), 4)
        self.assertEqual(parsers.blame(frames), "HIP runtime (libamdhip64)")
        self.assertIn("/lib/libc.so.6", parsers.frame_libraries(frames))

    def test_unknown_subsystem(self) -> None:
        self.assertEqual(parsers.blame(["#0 0x1 in main () from /usr/bin/app"]), "")


PANIC: str = """\
2026-11-13T22:54:12+03:00 vlabe-PC kernel: BUG: unable to handle page fault for address: ffffc900
2026-11-13T22:54:12+03:00 vlabe-PC kernel: Oops: 0000 [#1] PREEMPT SMP NOPTI
2026-11-13T22:54:12+03:00 vlabe-PC kernel: RIP: 0010:amdgpu_vm_bo_update+0x1a/0x40 [amdgpu]
2026-11-13T22:54:12+03:00 vlabe-PC kernel: Kernel panic - not syncing: Fatal exception
2026-11-13T22:54:12+03:00 vlabe-PC kernel: Call Trace:
2026-11-13T22:54:12+03:00 vlabe-PC kernel:  <TASK>
2026-11-13T22:54:12+03:00 vlabe-PC kernel:  amdgpu_vm_bo_update+0x1a/0x40 [amdgpu]
2026-11-13T22:54:12+03:00 vlabe-PC kernel:  amdgpu_gem_va_ioctl+0x2b2/0x3f0 [amdgpu]
2026-11-13T22:54:12+03:00 vlabe-PC kernel:  </TASK>
2026-11-13T22:54:13+03:00 vlabe-PC kernel: ---[ end trace 0000000000000000 ]---
"""


class PanicTests(unittest.TestCase):
    def test_panic_is_recognised_and_wins_over_a_freeze(self) -> None:
        ev = verdict_mod.Evidence(
            kernel=PANIC.splitlines(),
            boot_end=datetime(2026, 11, 13, 22, 54, 12),
            journal_last="2026-11-13T22:54:12+03:00 vlabe-PC kernel: Kernel panic - not syncing",
            logs=[parsers.LogTail("comfyui-debug.log", 0.0, ["x"], 0)],
        )
        ev.log_mtime = datetime(2026, 11, 13, 22, 55, 4)
        ev.counts = parsers.count_signatures(ev.kernel)
        ev.samples = parsers.signature_samples(ev.kernel)
        result = verdict_mod.decide(ev)
        self.assertEqual(result.cause, "kernel-panic")
        self.assertTrue(any("RIP:" in line for line in result.evidence), result.evidence)
        self.assertTrue(any("amdgpu_gem_va_ioctl" in line for line in result.evidence), result.evidence)


class WindowTests(unittest.TestCase):
    def test_logs_from_a_later_boot_are_excluded(self) -> None:
        """A log written after the crashed boot must not inflate the freeze gap."""
        logs = [
            parsers.LogTail("old.log", datetime(2026, 11, 13, 23, 6, 40).timestamp(), ["a"]),
            parsers.LogTail("new.log", datetime(2026, 11, 13, 23, 9, 45).timestamp(), ["b"]),
        ]
        kept = parsers.logs_in_window(
            logs,
            datetime(2026, 11, 13, 22, 55, 30),
            datetime(2026, 11, 13, 23, 6, 57),
        )
        self.assertEqual([log.name for log in kept], ["old.log"])


class FreezeTests(unittest.TestCase):
    def test_gap_between_journal_and_process_log_is_a_freeze(self) -> None:
        """journald stopped 52 s before the last process write: the box stopped scheduling."""
        ev = verdict_mod.Evidence(
            journal_last="2026-11-13T22:54:12+03:00 vlabe-PC systemd[1]: something",
            boot_end=datetime(2026, 11, 13, 22, 54, 12),
            logs=[parsers.LogTail("comfyui-debug.log", 0.0, ["Trying to load custom node"], 514)],
        )
        ev.log_mtime = datetime(2026, 11, 13, 22, 55, 4)
        result = verdict_mod.decide(ev)
        self.assertEqual(result.cause, "freeze")
        self.assertTrue(any("52" in line for line in result.evidence), result.evidence)

    def test_unreadable_pstore_is_not_reported_as_empty(self) -> None:
        import tempfile
        from pathlib import Path as P

        with tempfile.TemporaryDirectory() as tmp:
            path = P(tmp) / "pstore"
            path.mkdir()
            path.chmod(0o000)
            names, error = Host().listdir_checked(str(path))
        self.assertEqual(names, [])
        self.assertIn("permission", error.lower())


class VerdictTests(unittest.TestCase):
    def evidence(self, **kwargs) -> verdict_mod.Evidence:
        ev = verdict_mod.Evidence()
        for key, value in kwargs.items():
            setattr(ev, key, value)
        ev.counts = kwargs.get("counts") or parsers.count_signatures(ev.kernel)
        ev.samples = parsers.signature_samples(ev.kernel)
        return ev

    def test_pin_failures_mean_gpu_memory(self) -> None:
        result = verdict_mod.decide(self.evidence(kernel=KERNEL.splitlines()))
        self.assertEqual(result.cause, "gpu-memory")

    def test_hardware_wins_over_everything(self) -> None:
        kernel = [*KERNEL.splitlines(), "kernel: mce: [Hardware Error]: Machine check events logged"]
        self.assertEqual(verdict_mod.decide(self.evidence(kernel=kernel)).cause, "hardware")

    def test_abort_without_kernel_lines(self) -> None:
        cores = parsers.parse_coredump_list(COREDUMP_LIST)
        frames = parsers.parse_frames(COREDUMP_INFO)
        result = verdict_mod.decide(self.evidence(coredumps=cores, gdb_frames={9614: frames}))
        self.assertEqual(result.cause, "process-abort")
        self.assertIn("HIP", result.summary)

    def test_empty_journal_is_inconclusive(self) -> None:
        self.assertEqual(verdict_mod.decide(self.evidence()).cause, "unlogged")

    def test_stale_signatures_become_context_not_a_verdict(self) -> None:
        """pin failures 10 minutes before the reset did not cause it by themselves."""
        kernel = KERNEL.splitlines()
        ev = self.evidence(kernel=kernel, boot_end=datetime(2026, 11, 13, 22, 42, 57))
        result = verdict_mod.decide(ev)
        self.assertEqual(result.cause, "silent-reset")
        self.assertTrue(any("before the reset" in line for line in result.evidence), result.evidence)

    def test_recent_signatures_keep_their_cause(self) -> None:
        ev = self.evidence(kernel=KERNEL.splitlines(), boot_end=datetime(2026, 11, 13, 21, 47, 40))
        self.assertEqual(verdict_mod.decide(ev).cause, "gpu-memory")

    def test_silent_reset_names_the_last_log(self) -> None:
        logs = [parsers.LogTail("comfyui.log", 0.0, ["Prompt executed"], 2833)]
        result = verdict_mod.decide(self.evidence(kernel=["kernel: hello"], logs=logs))
        self.assertEqual(result.cause, "silent-reset")
        self.assertTrue(any("zero bytes" in line for line in result.evidence))


class ClockTests(unittest.TestCase):
    def test_anchor_subtracts_monotonic_from_realtime(self) -> None:
        """The RTC can be hours wrong at boot; ``realtime - monotonic`` cannot."""
        end = datetime(2026, 11, 14, 8, 31, 21, 9687)
        payload = {
            "__REALTIME_TIMESTAMP": str(int(end.timestamp()) * 1_000_000 + 9687),
            "__MONOTONIC_TIMESTAMP": str(3389 * 1_000_000 + 9687),
        }
        anchor = parsers.parse_journal_anchor(json.dumps(payload))
        self.assertIsNotNone(anchor)
        assert anchor is not None
        self.assertEqual(anchor.boot_start, datetime(2026, 11, 14, 7, 34, 52))
        self.assertAlmostEqual(anchor.monotonic_s, 3389.009687)

    def test_anchor_survives_a_list_payload(self) -> None:
        payload = [
            {"__REALTIME_TIMESTAMP": "1763105692009687", "__MONOTONIC_TIMESTAMP": "3389009687"},
            {"__REALTIME_TIMESTAMP": "1763105700000000", "__MONOTONIC_TIMESTAMP": "3397000000"},
        ]
        self.assertIsNotNone(parsers.parse_journal_anchor(json.dumps(payload)))

    def test_anchor_rejects_garbage(self) -> None:
        self.assertIsNone(parsers.parse_journal_anchor("not json"))
        self.assertIsNone(parsers.parse_journal_anchor("{}"))

    def test_systemd_analyze_rows(self) -> None:
        text = (
            "Startup finished in 10.037s (firmware) + 5.187s (loader) + 2.345s (kernel)"
            " + 15.783s (userspace) = 33.352s\n"
            "graphical.target reached after 15.2s in userspace.\n"
        )
        values = parsers.parse_systemd_analyze(text)
        self.assertAlmostEqual(values["firmware"], 10.037)
        self.assertAlmostEqual(values["loader"], 5.187)
        self.assertAlmostEqual(values["userspace"], 15.783)


class SignatureWindowTests(unittest.TestCase):
    def test_window_keeps_first_last_and_count(self) -> None:
        lines = [
            "2026-11-14T08:20:02+03:00 vlabe-PC kernel: amdgpu 0000:03:00.0: 12 pin failed",
            "2026-11-14T08:21:40+03:00 vlabe-PC kernel: amdgpu 0000:03:00.0: 34 pin failed",
            "2026-11-14T08:22:35+03:00 vlabe-PC kernel: amdgpu 0000:03:00.0: 56 pin failed",
        ]
        window = parsers.signature_windows(lines)["amdgpu pin failed"]
        self.assertEqual(window.hits, 3)
        self.assertEqual(window.first, datetime(2026, 11, 14, 8, 20, 2))
        self.assertEqual(window.last, datetime(2026, 11, 14, 8, 22, 35))

    def test_kfd_teardown_hog_is_a_signature(self) -> None:
        lines = [
            "2026-11-14T08:28:57+03:00 vlabe-PC kernel: workqueue: kfd_process_wq_release"
            " [amdgpu] hogged CPU for >10000us 4 times, consider switching to WQ_UNBOUND"
        ]
        self.assertEqual(parsers.count_signatures(lines).get("KFD teardown hog"), 1)


class RuledOutTests(unittest.TestCase):
    def test_watchdog_armed_line(self) -> None:
        armed, detail = parsers.watchdog_state(
            [
                "Nov 14 08:31:44 host systemd[1]: Using hardware watchdog 'iTCO_wdt', version 0, device /dev/watchdog0",
                "Nov 14 08:31:44 host systemd[1]: Watchdog running with a timeout of 10s.",
            ]
        )
        self.assertTrue(armed)
        self.assertIn("iTCO_wdt", detail)
        self.assertIn("10", detail)

    def test_no_watchdog_line_means_not_armed(self) -> None:
        armed, _detail = parsers.watchdog_state(["Nov 14 08:31:44 host systemd[1]: Reached target basic.target"])
        self.assertFalse(armed)

    def test_denylisted_modules(self) -> None:
        lines = [
            "Nov 14 08:31:53 host systemd-modules-load[322]: Module 'iTCO_wdt' is deny-listed (by kmod)",
            "Nov 14 08:31:53 host systemd-modules-load[322]: Module 'iTCO_wdt' is deny-listed (by kmod)",
        ]
        self.assertEqual(parsers.denylisted_modules(lines), ["iTCO_wdt"])

    def test_blacklist_file(self) -> None:
        text = "# a comment\nblacklist iTCO_wdt\n# chipset watchdog\nblacklist sp5100_tco\n"
        rows = parsers.parse_blacklist(text, "/usr/lib/modprobe.d/blacklist.conf")
        self.assertEqual([module for module, _path in rows], ["iTCO_wdt", "sp5100_tco"])

    def test_pstore_states(self) -> None:
        skipped = [
            "Nov 14 08:31:53 host systemd[1]: systemd-pstore.service - Platform Persistent Storage"
            " Archival was skipped because of an unmet condition check"
            " (ConditionDirectoryNotEmpty=/sys/fs/pstore)."
        ]
        self.assertEqual(parsers.pstore_state(skipped), "empty")
        self.assertEqual(parsers.pstore_state(["kernel: pstore: Registered efi_pstore"]), "unknown")
        self.assertEqual(parsers.pstore_state(["kernel: Registered efi_pstore as persistent store backend"]), "unknown")


APP_LOG = """\
2026-11-14T08:28:15.123456+03:00 [INFO] Starting server
2026-11-14T08:28:15.223456+03:00 [INFO] Total VRAM 12272 MB, total RAM 32056 MB
2026-11-14T08:28:19.323456+03:00 [INFO] Enabled pinned memory 27959
2026-11-14T08:28:24.423456+03:00 [INFO] got prompt
2026-11-14T08:28:56.523456+03:00 [INFO] Prompt executed in 32.1 seconds
"""

GENERATE_LOG = """\
2026-11-14T08:28:05+0300 === generate.sh started ===
2026-11-14T08:28:14+0300 Available generative models on disk:
2026-11-14T08:28:20+0300 ComfyUI is ready at http://127.0.0.1:8188
2026-11-14T08:28:21+0300 Text encoders (CLIP):
"""


class AppEventTests(unittest.TestCase):
    def test_comfyui_events(self) -> None:
        events = parsers.parse_app_events(APP_LOG, "comfyui.log")
        kinds = [event.kind for event in events]
        self.assertEqual(kinds, ["start", "vram", "pinned", "prompt", "done"])
        pinned = next(event for event in events if event.kind == "pinned")
        self.assertEqual(pinned.detail, "27959")
        done = next(event for event in events if event.kind == "done")
        self.assertEqual(done.detail, "32.1")
        self.assertEqual(events[0].at, datetime(2026, 11, 14, 8, 28, 15, 123456))

    def test_generate_events(self) -> None:
        events = parsers.parse_app_events(GENERATE_LOG, "generate.log")
        kinds = [event.kind for event in events]
        self.assertEqual(kinds, ["generate-start", "generate-step", "generate-ready", "generate-step"])
        self.assertEqual(events[1].detail, "Available generative models on disk")

    def test_untimestamped_lines_are_ignored(self) -> None:
        self.assertEqual(parsers.parse_app_events("  [INFO] got prompt\n", "comfyui.log"), [])


class PlatformFreezeTests(unittest.TestCase):
    def evidence(self) -> verdict_mod.Evidence:
        """The 2026-11-14 08:31 shape: no panic, no watchdog, 8 s of silence."""
        ev = verdict_mod.Evidence(
            kernel=KERNEL.splitlines(),
            boot_end=datetime(2026, 11, 14, 8, 31, 21),
            silence_s=8.0,
            panic_record="empty",
            pstore_note="efi_pstore registered at the next boot and /sys/fs/pstore was empty",
            watchdog_armed=False,
            watchdog_detail="no watchdog device in that boot",
            watchdog_denylist=["iTCO_wdt"],
            blacklists={"iTCO_wdt": "/usr/lib/modprobe.d/blacklist_x.conf"},
            events=[
                parsers.AppEvent(datetime(2026, 11, 14, 7, 35, 47), "session.log", "session-start"),
                parsers.AppEvent(datetime(2026, 11, 14, 8, 30, 56), "session.log", "session-start"),
            ],
        )
        ev.last_kernel = datetime(2026, 11, 14, 8, 30, 26)
        ev.counts = parsers.count_signatures(ev.kernel)
        ev.samples = parsers.signature_samples(ev.kernel)
        ev.windows = parsers.signature_windows(ev.kernel)
        return ev

    def test_verdict_names_an_external_reset(self) -> None:
        result = verdict_mod.decide(self.evidence())
        self.assertEqual(result.cause, "platform-freeze")
        self.assertIn("external", result.summary)
        self.assertTrue(any("2 GPU contexts" in line for line in [result.summary]))

    def test_checks_rule_out_panic_and_watchdog(self) -> None:
        ev = self.evidence()
        verdict_mod.decide(ev)
        rows = {check.label: check.status for check in ev.checks}
        self.assertEqual(rows["kernel panic"], "ruled out")
        self.assertEqual(rows["hardware watchdog"], "ruled out")
        self.assertEqual(rows["host memory"], "ruled out")

    def test_no_panic_evidence_keeps_it_unproven(self) -> None:
        ev = self.evidence()
        ev.panic_record = "unknown"
        self.assertEqual(verdict_mod.decide(ev).cause, "silent-reset")


class DriftTests(unittest.TestCase):
    SESSIONS = """\
--- session 2026-11-13T22:16:11+03:00 (pid 6140) ---
2026-11-13T22:16:11+0300 snapshot: mem free 23081 MiB, available 27179 MiB, swap 0/0 MiB, load 3.93 3.71 1.90
--- session 2026-11-13T22:17:16+03:00 (pid 7362) ---
2026-11-13T22:17:16+0300 snapshot: mem free 10732 MiB, available 25476 MiB, swap 0/16383 MiB, load 5.35 4.16 2.18
"""

    def test_pinned_jump_names_the_swapfile(self) -> None:
        ev = verdict_mod.Evidence(
            boot_start=datetime(2026, 11, 14, 7, 34, 52),
            boot_list_start="Sat 2026-11-14 10:34:57 MSK",
            sessions=parsers.parse_sessions(self.SESSIONS),
            pinned=[
                (datetime(2026, 11, 13, 22, 16, 12), 15671),
                (datetime(2026, 11, 13, 22, 17, 21), 27959),
            ],
        )
        rows = verdict_mod.drift(ev)
        self.assertTrue(any("15671 -> 27959" in row for row in rows), rows)
        self.assertTrue(any("ram-4GiB" in row for row in rows), rows)
        self.assertTrue(any("wall clock was off" in row for row in rows), rows)

    def test_swap_transition_without_history_is_silent(self) -> None:
        ev = verdict_mod.Evidence(
            pinned=[(datetime(2026, 11, 13, 22, 16, 12), 1), (datetime(2026, 11, 13, 22, 17, 21), 2)]
        )
        self.assertEqual(verdict_mod._swap_transition(ev, datetime(2026, 11, 13, 22, 17, 21)), "")


BOOTS_SKEW = """\
 -1 aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa Fri 2026-11-13 21:51:06 MSK Fri 2026-11-13 21:51:53 MSK
  0 bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb Sat 2026-11-14 10:34:57 MSK Sat 2026-11-14 08:31:21 MSK
"""


class BootRowTests(unittest.TestCase):
    def test_rtc_skewed_boot_uses_the_monotonic_start(self) -> None:
        end = datetime(2026, 11, 14, 8, 31, 21, 9687)
        anchor = json.dumps(
            {
                "__REALTIME_TIMESTAMP": str(int(end.timestamp()) * 1_000_000 + 9687),
                "__MONOTONIC_TIMESTAMP": str(3389 * 1_000_000 + 9687),
            }
        )

        def runner(argv, _timeout=60.0):
            joined = " ".join(argv)
            if "--list-boots" in joined:
                return RunResult(0, BOOTS_SKEW)
            if "-o json" in joined:
                return RunResult(0, anchor)
            if "-n 10" in joined:
                return RunResult(0, "systemd[1]: Reached target poweroff.target")
            return RunResult(0, "")

        from . import __main__ as cli

        rows = cli.boot_rows(Host(runner=runner))
        skewed = next(row for row in rows if row["boot"]["index"] == 0)
        self.assertTrue(skewed["clock_skew"])
        self.assertEqual(skewed["real_start"], "2026-11-14T07:34:52")
        self.assertAlmostEqual(skewed["runtime_s"], 3389.009687)
        self.assertIn("(clock list:", cli.format_boot_row(skewed))


def run() -> int:
    suite = unittest.defaultTestLoader.loadTestsFromModule(__import__(__name__, fromlist=["x"]))
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(run())
