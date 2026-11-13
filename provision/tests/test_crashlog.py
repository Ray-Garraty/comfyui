"""Tests for crash evidence collection and the crashlog planner step."""

from __future__ import annotations

import unittest

from helpers import FakeProbe, FakeRunner, base_dirs, base_files, base_listdir, base_perms, clean_host, make_config

from comfyprov import crashlog, layout
from comfyprov.facts import collect
from comfyprov.plan import Options, Planner
from comfyprov.probe import RunResult

KERNEL_LINES = "\n".join(
    [
        "2026-11-13T21:46:18+03:00 vlabe-PC kernel: amdgpu 0000:03:00.0: 000000004132ef84 pin failed",
        "2026-11-13T21:46:18+03:00 vlabe-PC kernel: [drm:amdgpu_dm_plane_helper_prepare_fb [amdgpu]] "
        "*ERROR* Failed to pin framebuffer with error -12",
        "2026-11-13T21:47:23+03:00 vlabe-PC kernel: amdgpu 0000:03:00.0: 0000000064d4a76f pin failed",
    ]
)
BOOT_LINES = "2026-11-13T21:21:00+03:00 vlabe-PC systemd[1]: Started ComfyUI session."


def crash_files() -> dict:
    return {
        str(layout.JOURNALD_DROPIN): layout.JOURNALD_CONTENT,
        str(layout.RSYSLOG_DROPIN): layout.RSYSLOG_CONTENT,
        str(layout.CRASH_SYSCTL_FILE): layout.CRASH_SYSCTL_CONTENT,
    }


def crash_probe(config, **overrides) -> FakeProbe:
    kwargs = {
        "files": {**base_files(config), **crash_files()},
        "dirs": base_dirs(config),
        "perms": base_perms(),
        "listdir_map": base_listdir(config),
        "packages": frozenset(layout.APT_REQUIRED + layout.APT_OPTIONAL) | {"rsyslog"},
        "sysctl": dict(layout.CRASH_SYSCTL_VALUES),
        "session_groups": ("vlabe",),
        "runner": FakeRunner(
            [
                ("systemctl is-enabled", RunResult(0, "static\n", "")),
                ("journalctl -b -1 -k", RunResult(0, KERNEL_LINES, "")),
                ("journalctl -b -1", RunResult(0, BOOT_LINES, "")),
            ]
        ),
    }
    kwargs.update(overrides)
    return FakeProbe(**kwargs)


class SystemLoggingTests(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile
        from pathlib import Path

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = make_config(Path(self.tmp.name))

    def plan(self, probe, config=None):
        config = config or self.config
        return Planner(config, collect(config, probe), probe, Options()).build("system")

    def test_durable_logging_is_planned(self) -> None:
        probe = clean_host(self.config, packages=frozenset(layout.APT_REQUIRED) | {"rsyslog"})
        plan = self.plan(probe)
        ids = {action.id for action in plan.actions}
        self.assertIn(f"write:{layout.JOURNALD_DROPIN}", ids)
        self.assertIn(f"write:{layout.RSYSLOG_DROPIN}", ids)
        self.assertIn(f"write:{layout.CRASH_SYSCTL_FILE}", ids)
        self.assertIn("crash-sysctl", ids)
        self.assertTrue(all(action.sudo for action in plan.actions if action.id.startswith("write:")))
        self.assertEqual(plan.status, "pending")

    def test_configured_host_is_idempotent(self) -> None:
        plan = self.plan(crash_probe(self.config))
        pending = {action.id for action in plan.pending}
        for ident in ("journald-restart", "rsyslog-reload", "crash-sysctl", "pstore-collect"):
            self.assertNotIn(ident, pending)
        ids = {action.id for action in plan.actions}
        self.assertIn(f"write:{layout.JOURNALD_DROPIN}", ids)

    def test_lockups_become_panics(self) -> None:
        content = layout.CRASH_SYSCTL_CONTENT
        self.assertIn("kernel.hardlockup_panic = 1", content)
        self.assertIn("kernel.panic = 10", content)
        self.assertIn("kernel.printk_devkmsg = on", content)

    def test_journald_syncs_every_second_without_rate_limit(self) -> None:
        content = layout.JOURNALD_CONTENT
        self.assertIn("SyncIntervalSec=1", content)
        self.assertIn("RateLimitIntervalSec=0", content)

    def test_capture_can_be_disabled(self) -> None:
        from dataclasses import replace

        disabled = replace(self.config, values={**self.config.values, "ENABLE_CRASH_CAPTURE": "0"})
        plan = self.plan(clean_host(disabled), config=disabled)
        ids = {action.id for action in plan.actions}
        self.assertNotIn(f"write:{layout.JOURNALD_DROPIN}", ids)
        self.assertTrue(any("disabled" in note for note in plan.notes))

    def test_rsyslog_is_skipped_when_missing(self) -> None:
        probe = clean_host(self.config, packages=frozenset(layout.APT_REQUIRED))
        plan = self.plan(probe)
        ids = {action.id for action in plan.actions}
        self.assertNotIn(f"write:{layout.RSYSLOG_DROPIN}", ids)
        self.assertTrue(any("rsyslog" in note for note in plan.notes))


class EvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile
        from pathlib import Path

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = make_config(Path(self.tmp.name))

    def test_signatures_are_counted(self) -> None:
        evidence = crashlog.collect(crash_probe(self.config))
        self.assertTrue(evidence.available)
        self.assertEqual(evidence.counts["amdgpu pin failed"], 2)
        self.assertEqual(evidence.counts["framebuffer pin failed"], 1)
        self.assertEqual(evidence.counts["out of memory"], 0)
        self.assertFalse(evidence.clean_shutdown)
        self.assertTrue(evidence.crashes)
        self.assertIn("pin failed", evidence.last_kernel_line)

    def test_clean_shutdown_is_recognised(self) -> None:
        probe = crash_probe(
            self.config,
            runner=FakeRunner(
                [
                    ("systemctl is-enabled", RunResult(0, "static\n", "")),
                    ("journalctl -b -1 -k", RunResult(0, KERNEL_LINES, "")),
                    ("journalctl -b -1", RunResult(0, "systemd[1]: Reached target poweroff.target", "")),
                ]
            ),
        )
        evidence = crashlog.collect(probe)
        self.assertTrue(evidence.clean_shutdown)
        self.assertFalse(evidence.crashes)

    def test_missing_journal_is_reported(self) -> None:
        probe = crash_probe(self.config, runner=FakeRunner([("journalctl", RunResult(1, "", "no journal"))]))
        evidence = crashlog.collect(probe)
        self.assertFalse(evidence.available)
        self.assertIn("unavailable", evidence.summary())

    def test_pstore_records_are_read(self) -> None:
        probe = crash_probe(
            self.config,
            listdir_map={"/sys/fs/pstore": ["dmesg-ramoops-0"]},
            files={**base_files(self.config), "/sys/fs/pstore/dmesg-ramoops-0": "panic at 21:50"},
        )
        evidence = crashlog.collect(probe)
        self.assertEqual(evidence.pstore, (("dmesg-ramoops-0", "panic at 21:50"),))
        self.assertIn("pstore", crashlog.format_report(evidence))

    def test_findings_cover_the_whole_chain(self) -> None:
        probe = crash_probe(self.config)
        findings = crashlog.findings(self.config, probe, crashlog.collect(probe))
        names = {item.name for item in findings}
        self.assertIn("journald", names)
        self.assertIn("watchdog", names)
        self.assertIn("kernel log copy", names)
        self.assertIn("previous boot", names)
        self.assertIn("previous boot: amdgpu pin failed", names)

    def test_missing_configuration_warns(self) -> None:
        probe = clean_host(self.config)
        findings = crashlog.findings(self.config, probe)
        levels = {item.name: item.level for item in findings}
        self.assertEqual(levels["journald"], "warn")
        self.assertEqual(levels["watchdog"], "warn")


class SnapshotTests(unittest.TestCase):
    def test_snapshot_reports_memory_and_gpu(self) -> None:
        probe = FakeProbe(
            files={
                "/proc/meminfo": "MemTotal: 32768 kB\nMemFree: 1024 kB\nMemAvailable: 2048 kB\n"
                "SwapTotal: 16384 kB\nSwapFree: 16384 kB\n",
                "/proc/loadavg": "0.50 0.40 0.30 1/100 4242\n",
                "/sys/class/drm/card1/device/mem_info_vram_used": str(1024 * 1024 * 1024),
                "/sys/class/drm/card1/device/mem_info_vram_total": str(12 * 1024 * 1024 * 1024),
                "/sys/class/drm/card1/device/mem_info_gtt_used": str(2 * 1024 * 1024 * 1024),
                "/sys/class/drm/card1/device/mem_info_gtt_total": str(16 * 1024 * 1024 * 1024),
            },
            listdir_map={"/sys/class/drm": ["card1", "renderD128", "version"]},
        )
        text = crashlog.snapshot(probe, now="2026-11-13T21:50:31+0300")
        self.assertIn("2026-11-13T21:50:31+0300 snapshot:", text)
        self.assertIn("swap 0/16 MiB", text)
        self.assertIn("load 0.50 0.40 0.30", text)
        self.assertIn("card1: vram 1024/12288 MiB, gtt 2048/16384 MiB", text)

    def test_snapshot_without_procfs_still_prints(self) -> None:
        text = crashlog.snapshot(FakeProbe(), now="now")
        self.assertTrue(text.startswith("now snapshot:"))


if __name__ == "__main__":
    unittest.main()
