"""Tests for the vitals black box (``comfyprov.vitals``).

The recorder is what makes an unexplained reset explainable, so its parsing and
its tripwire are pinned here with a fake host: no GPU, no /proc, no root.
"""

from __future__ import annotations

import signal
import subprocess
import tempfile
import unittest
from pathlib import Path

from helpers import FakeProbe

from comfyprov import vitals

MEMINFO = """MemTotal:       32824984 kB
MemFree:        18796512 kB
MemAvailable:   28717688 kB
Buffers:          620000 kB
Cached:          9400000 kB
SwapTotal:      16777212 kB
SwapFree:       16777212 kB
Mlocked:        10485760 kB
Unevictable:    10485760 kB
Dirty:              3000 kB
Writeback:            0 kB
AnonHugePages:    524288 kB
SReclaimable:    1500000 kB
SomethingElse:         1 kB
"""

PRESSURE = """some avg10=12.34 avg60=3.00 avg300=1.00 total=98765432
full avg10=5.00 avg60=1.00 avg300=0.10 total=12345678
"""

VMSTAT = """nr_free_pages 100
allocstall_movable 11
allocstall_dma 1
compact_stall 7
oom_kill 0
pgscan_direct 4242
pgsteal_direct 4000
pgmajfault 16117
"""

STAT = """cpu  1 2 3
procs_running 3
procs_blocked 1
"""

LOADAVG = "2.50 1.75 1.00 2/512 4242\n"
SMAPS_ROLLUP = """Rss:            12000000 kB
Pss:             9000000 kB
Pss_Anon:        8000000 kB
Pss_File:         500000 kB
Pss_Shmem:        500000 kB
SwapPss:         1024000 kB
Private_Dirty:   7000000 kB
"""


def host_probe(**overrides) -> FakeProbe:
    files = {
        "/proc/meminfo": MEMINFO,
        "/proc/pressure/memory": PRESSURE,
        "/proc/vmstat": VMSTAT,
        "/proc/stat": STAT,
        "/proc/loadavg": LOADAVG,
        "/proc/uptime": "302.11 900.00\n",
        "/proc/5133/smaps_rollup": SMAPS_ROLLUP,
        "/sys/class/drm/card0/mem_info_vram_total": "0\n",
        "/sys/class/drm/card1/device/mem_info_vram_total": str(12272 * vitals.MB),
        "/sys/class/drm/card1/device/mem_info_vram_used": str(6792 * vitals.MB),
        "/sys/class/drm/card1/device/mem_info_gtt_total": str(16027 * vitals.MB),
        "/sys/class/drm/card1/device/mem_info_gtt_used": str(2048 * vitals.MB),
        "/sys/class/drm/card1/device/gpu_busy_percent": "98\n",
        "/sys/class/drm/card1/device/hwmon/hwmon4/temp1_input": "72000\n",
        "/sys/class/drm/card1/device/hwmon/hwmon4/power1_average": "180000000\n",
        "/sys/class/drm/card1/device/hwmon/hwmon4/freq1_input": "2500000000\n",
        "/sys/class/drm/card1/device/hwmon/hwmon4/freq2_input": "1000000000\n",
        "/proc/5100/stat": "5100 (python) D 0 0 0\n",
        "/proc/5100/comm": "python\n",
        "/proc/999/stat": "999 (kworker/0:1) S 0 0 0\n",
        "/proc/999/comm": "kworker/0:1\n",
    }
    files.update(overrides.pop("files", {}))
    listdir = {
        "/sys/class/drm": ["card0", "card1", "card1-DP-1", "renderD128", "version"],
        "/sys/class/drm/card1/device/hwmon": ["hwmon4"],
        "/sys/class/kfd/kfd/proc": ["5100", "5133"],
        "/proc": ["1", "999", "5100", "self", "meminfo"],
    }
    listdir.update(overrides.pop("listdir", {}))
    return FakeProbe(files=files, listdir_map=listdir, **overrides)


class MeminfoTests(unittest.TestCase):
    def test_minutes_are_converted_to_mib(self) -> None:
        values = vitals.meminfo(host_probe())
        self.assertEqual(values["total"], 32055)
        self.assertEqual(values["avail"], 28044)
        self.assertEqual(values["swap_total"], 16383)
        self.assertEqual(values["mlock"], 10240)
        self.assertEqual(values["unevict"], 10240)
        self.assertNotIn("somethingelse", values)

    def test_missing_proc_yields_no_crash(self) -> None:
        self.assertEqual(vitals.meminfo(FakeProbe()), {})


class PressureTests(unittest.TestCase):
    def test_psi_average_and_total_seconds(self) -> None:
        values = vitals.pressure(host_probe())
        self.assertEqual(values["psi_some"], 12.34)
        self.assertEqual(values["psi_full"], 5.00)
        self.assertAlmostEqual(values["psi_some_total"], 98.765, places=2)
        self.assertIn("psi_full_total", values)


class VmstatTests(unittest.TestCase):
    def test_allocstall_is_summed_over_zones(self) -> None:
        values = vitals.vmstat(host_probe())
        self.assertEqual(values["allocstall"], 12)
        self.assertEqual(values["compactstall"], 7)
        self.assertEqual(values["pgscandirect"], 4242)
        self.assertEqual(values["oom"], 0)
        self.assertEqual(values["pgmajfault"], 16117)


class SchedulerTests(unittest.TestCase):
    def test_blocked_tasks_are_named(self) -> None:
        values = vitals.scheduler(host_probe())
        self.assertEqual(values["load"], "2.50/1.75/1.00")
        self.assertEqual(values["run"], 3)
        self.assertEqual(values["blocked"], 1)
        self.assertEqual(values["dstate"], "python")

    def test_no_blocked_tasks_skips_the_scan(self) -> None:
        values = vitals.scheduler(host_probe(files={"/proc/stat": "procs_running 1\nprocs_blocked 0\n"}))
        self.assertEqual(values["dstate"], "-")


class GpuTests(unittest.TestCase):
    def test_first_device_with_vram_wins(self) -> None:
        values = vitals.gpu(host_probe())
        self.assertEqual(values["vram"], "6792/12272")
        self.assertEqual(values["gtt"], "2048/16027")
        self.assertEqual(values["gpu"], "98")
        self.assertEqual(values["temp"], "72")
        self.assertEqual(values["power"], "180.0")
        self.assertEqual(values["sclk"], "2500")
        self.assertEqual(values["mclk"], "1000")

    def test_no_drm_device_is_not_an_error(self) -> None:
        self.assertEqual(vitals.gpu(host_probe(listdir={"/sys/class/drm": []})), {})


class ProcessTests(unittest.TestCase):
    def test_smaps_rollup_is_preferred(self) -> None:
        values = vitals.process(host_probe(), 5133)
        self.assertEqual(values["pss"], "8789")
        self.assertEqual(values["swappss"], "1000")
        self.assertEqual(values["privatedirty"], "6835")

    def test_status_is_the_fallback(self) -> None:
        probe = host_probe(files={"/proc/77/status": "Name:\tpython\nVmRSS:\t  2048000 kB\nVmSwap:\t 102400 kB\n"})
        values = vitals.process(probe, 77)
        self.assertEqual(values["rss"], "2000")
        self.assertEqual(values["swappss"], "100")


class SampleTests(unittest.TestCase):
    def test_sample_carries_the_keys_crashlab_needs(self) -> None:
        _, fields = vitals.sample(host_probe(), pid=5133, lag=0.25)
        for key in ("mono", "pid", "avail", "swap", "psi_some", "allocstall", "vram", "gtt", "kfd", "pss", "lag"):
            self.assertIn(key, fields)
        self.assertEqual(fields["swap"], "0/16383")
        self.assertEqual(fields["kfd"], "2")
        self.assertEqual(fields["lag"], "0.25")

    def test_line_has_one_isotime_then_key_values(self) -> None:
        now, fields = vitals.sample(host_probe(), pid=5133)
        line = vitals.format_sample(now, fields)
        self.assertRegex(line, r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}[+-]\d\d:\d\d mono=302\.11 ")
        self.assertEqual(line.count("="), len(fields))
        self.assertNotIn("\n", line)


class GuardTests(unittest.TestCase):
    def test_guard_marker_names_the_floor(self) -> None:
        line = vitals.format_event("guard", avail=512, floor=1024, pid=7, action="stop")
        self.assertIn("event=guard ", line)
        self.assertIn("avail=512", line)
        self.assertIn("floor=1024", line)


class RunTests(unittest.TestCase):
    def test_rotation_keeps_the_previous_window(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "vitals.log"
            # A file already at the limit rolls over before the next sample is
            # written, so the old window survives in .1 instead of being lost.
            out.write_text("2026-11-17T00:00:00.000+03:00 mono=1 avail=1000\n")
            vitals.run(out, interval=0, probe=host_probe(), iterations=1, fsync=False, max_bytes=1)
            self.assertEqual(len(out.with_name("vitals.log.1").read_text().splitlines()), 1)
            self.assertEqual(len(out.read_text().splitlines()), 1)

    def test_guard_stops_the_watched_process(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "vitals.log"
            child = subprocess.Popen(["sleep", "30"])
            try:
                probe = host_probe(files={"/proc/meminfo": MEMINFO.replace("28717688", "1024")})
                vitals.run(
                    out,
                    interval=0,
                    pid=child.pid,
                    probe=probe,
                    iterations=5,
                    fsync=False,
                    guard_avail_mb=2048,
                )
                child.wait(timeout=10)
                self.assertEqual(child.returncode, -signal.SIGTERM)
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait()
            text = out.read_text()
            self.assertIn("event=guard ", text)
            self.assertIn(f"pid={child.pid}", text)

    def test_watched_process_exit_ends_the_recording(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "vitals.log"
            child = subprocess.Popen(["true"])
            child.wait()
            vitals.run(out, interval=0, pid=child.pid, probe=host_probe(), fsync=False, iterations=50)
            self.assertEqual(len(out.read_text().splitlines()), 1)

    def test_output_directory_is_created(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "logs" / "vitals.log"
            vitals.run(out, interval=0, probe=host_probe(), iterations=1, fsync=False)
            self.assertTrue(out.exists())


if __name__ == "__main__":
    unittest.main()
