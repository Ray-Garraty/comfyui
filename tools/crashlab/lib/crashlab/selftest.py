"""Parser self-tests with canned fixtures. ``crashlab self-test`` runs these."""

from __future__ import annotations

import unittest
from datetime import datetime

from . import parsers
from . import verdict as verdict_mod
from .host import Host

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
KERNEL = "\n".join(
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


PANIC = """\
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


def run() -> int:
    suite = unittest.defaultTestLoader.loadTestsFromModule(__import__(__name__, fromlist=["x"]))
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(run())
