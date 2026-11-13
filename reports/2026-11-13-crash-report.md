# Crash report — spontaneous platform resets (2026-11-13)

**Status:** a platform-level hang produced by the amdgpu/ROCm stack; the resets
themselves are the chipset TCO watchdog firing. Power delivery is excluded: the
GPU is idle (≈9 W, 43 °C) at the moment of every reset, so this is not a load or
power event, and not a user action.
**Confidence:** high on the *mechanism* of the reset, medium on the *trigger*
inside the GPU stack (no kernel trace exists yet, by construction — see §6).

---

## 1. Summary

On 2026-11-13 this machine reset itself **7 times** during ComfyUI work. Every
reset happened while an amdgpu/KFD context was being created or torn down. The
kernel never logged a panic, an MCE, an AER or a thermal event, and `pstore` is
empty. During the last reset the chipset watchdog (`iTCO_wdt`, 30 s) was already
running, and the board came back exactly ~30 s after the kernel stopped writing
to the journal — i.e. the reset was the watchdog firing, and the kernel was
unable to run at all in that window, including its own watchdogs
(`hung_task=15 s`, `softlockup≈10 s`).

Reproduction of the analysis takes under one second:

```bash
./tools/crashlab/crashlab report --brief
```

---

## 2. Environment

| Item | Value |
|---|---|
| OS | Linux Mint 22.3 (Ubuntu 24.04 base), x86_64 |
| Kernel | `7.0.0-43-generic` (Ubuntu 7.0.0-43.43~24.04.1) |
| CPU / chipset | Intel Core i5-6500 / Intel B150 LPC/eSPI (`00:1f.0`, `8086:a148`) |
| GPU | AMD Radeon RX 6700 XT, Navi 22, **gfx1031**, 12 272 MB VRAM, 16 027 MB GTT |
| RAM / swap | 32 GB / `/swapfile` 16 GB (enabled 22:16:33 today), no zram |
| Host watchdog | `iTCO_wdt` v4, TCOBASE `0x0400`, heartbeat 30 s (loaded 23:06:35) |
| PyTorch | `2.13.0+rocm10.1.0`, ROCm `(7, 16)` |
| ComfyUI | `0.45.3`, `comfy-kitchen 0.2.41`, `comfy-aimdo 0.6.0` |
| Runs | `--reserve-vram 1.5`, `logs/comfyui.log` INFO + `comfyui-debug.log` DEBUG |
| Pinned memory | `Enabled pinned memory 15671` MiB (≈16 GiB) on every start |

---

## 3. Crash inventory

Boots are from `journalctl --list-boots`; "journal end" is the last record that
reached the disk, *not* the exact crash time (see §6).

| Boot | Started | Journal ends | Verdict |
|---|---|---|---|
| -7 | 17:44:24 | 18:37:30 | UNCLEAN |
| -6 | 18:38:02 | 19:44:21 | UNCLEAN |
| -5 | 19:45:20 | 21:50:31 | UNCLEAN |
| -4 | 21:51:06 | 22:01:43 | UNCLEAN |
| -3 | 22:02:43 | 22:42:57 | UNCLEAN |
| -2 | 22:45:38 | 22:54:12 | UNCLEAN |
| -1 | 22:55:30 | 23:06:57 | UNCLEAN |

Boots `-9`/`-8` (the morning) ended cleanly, so the machine is stable when
ComfyUI is not in the loop.

### 3.1 Last reset (boot -1), second by second

| Time | Event (source) |
|---|---|
| 22:55:30 | boot -1 |
| 23:06:35 | `iTCO_wdt: Found a Intel PCH TCO device (Version=4)` — `heartbeat=30 sec` (kernel) |
| 23:06:35 | `systemd[1]: Using hardware watchdog 'iTCO_wdt', device /dev/watchdog0` — `Watchdog running with a timeout of 30s` |
| 23:06:35 | sysctls in force: `hung_task_timeout_secs=15`, `watchdog_thresh=5`, `hardlockup_panic=1`, `panic=10` |
| 23:06:57 | **last journal record** (`vte-spawn-…scope: Consumed 3.242s CPU time`) |
| ~23:07:27 | TCO expires (~30 s after the last watchdog feed) → hardware reset |
| 23:07:43 | boot 0 |

Nothing was written between 23:06:57 and the reboot: no panic, no lockup
report, no hung task, no OOM, no MCE, no AER, no thermal alarm.

---

## 4. Evidence

### 4.1 GPU faults precede the resets

From boot -3 (`journalctl -b -3 -k`):

```
amdgpu 0000:03:00.0: [gfxhub] page fault (src_id:0 ring:40 vmid:8 pasid:58)
amdgpu 0000:03:00.0:  Process python pid 7904 thread python pid 7904
amdgpu 0000:03:00.0:   in page starting at address 0x000075e47ee00000 from client 0x1b (UTCL2)
amdgpu 0000:03:00.0: GCVM_L2_PROTECTION_FAULT_STATUS:0x00841050
amdgpu 0000:03:00.0:          Faulty UTCL2 client ID: TCP (0x8)
amdgpu 0000:03:00.0:          PERMISSION_FAULTS: 0x5
```

15 seconds later that process aborted: `coredumpctl info 7904` → `SIGABRT`.

### 4.2 GPU memory accounting failures

92 pairs of these in boot -5, 2 pairs in boot -3:

```
amdgpu 0000:03:00.0: 0000000064d4a76f pin failed
[drm:amdgpu_dm_plane_helper_prepare_fb [amdgpu]] *ERROR* Failed to pin framebuffer with error -12
```

`-12` is `ENOMEM`: the **display plane** could not get VRAM/GTT while ComfyUI
staged a 7 002 MB text encoder and a 5 783 MB diffusion model. Symptoms of this
stage: desktop stutter, black frames in the output directory.

### 4.3 Process-level aborts

`coredumpctl`, Python process, one representative stack (`gdb bt`, 15 s):

```
#0  __pthread_kill_implementation (signo=6)
#9  __GI_abort
#16 c10::cuda::c10_cuda_check_implementation(...)
#17 c10::cuda::SetDevice(...)
#18 c10::cuda::impl::CUDAGuardImpl::uncheckedSetDevice(...)
```

A HIP call failed during a device switch and `std::terminate` aborted the
process. `crashlab` attributes it as `blame: PyTorch HIP/CUDA call check (c10)`.

### 4.4 KFD teardown pressure

```
workqueue: kfd_process_wq_release [amdgpu] hogged CPU for >10000us 11 times
```

Every ComfyUI exit triggers this path; resets cluster around process
start/stop, not around sampling.

### 4.5 Coincidence with the test hook (now known)

The commit hook runs `./provision.sh test`. It used to start ComfyUI **twice**
(the full pytest run contained the e2e test, then the e2e marker ran it again).
4 of the 7 resets landed inside that double start/stop cycle. This is a risk
multiplier, not the cause.

**Fixed:** the unit pass now runs with `-m "not e2e"` and the e2e marker runs
exactly once per commit — one ComfyUI start/stop cycle instead of two.
`provision/tests/test_commit_gate.py` pins both the filter and the "e2e runs
once" rule, so the double cycle cannot come back unnoticed.

---

## 5. Analysis

### 5.1 What the reset is

The OS did not crash. The **platform stopped executing**:

- `hung_task_panic=1` with a 15 s timeout did not fire → no task sat in `D`.
- `watchdog_thresh=5` (soft lockup ≈10 s) did not fire → no CPU was spinning in
  the kernel with interrupts enabled.
- Yet PID 1 stopped feeding `/dev/watchdog0`, and only the chipset TCO (an
  independent timer) ended the stall.

That combination means the CPU was not running the kernel at all — the machine
hung below the level any kernel watchdog can observe. When that happens, no
software on the machine can leave a trace; the TCO reset is the only way out,
and it is exactly what we see (≈30 s of silence, then a cold boot).

### 5.2 What the trigger is

Every reset is adjacent to amdgpu activity, and the kernel log records the GPU
failing *before* the stall (`[gfxhub] page fault`, `pin failed`, c10 abort in
`SetDevice`). The software stack in that path is unusually deep and unusual:

- `comfy_kitchen_config/sitecustomize.py` patches `torch._int_mm` and filters
  comfy-kitchen's int8 linear handler (RDNA2 has no matrix cores; hipblasLt
  answers `HIPBLAS_STATUS_INVALID_VALUE`);
- comfy-kitchen registers custom int8 ops (`comfy_kitchen.int8_linear`);
- comfy-aimdo performs dynamic VRAM staging;
- ComfyUI pins up to 16 GiB of host memory and allocates GTT in the same range.

**Load is not the trigger.** Every reset happens while the GPU is idle
(ComfyUI starting, sampler not running); the faults that precede it
(`[gfxhub] page fault`, `pin failed`, c10 abort) were produced by *earlier*
context work in the same boot. The mechanism is therefore residual state, not
current draw: **gfx1031 + ROCm 10.1 + comfy-kitchen/comfy-aimdo leave the GPU
and/or the KFD context in a broken state, and the next context operation —
still at idle load — takes the memory/PCIe fabric down with it.**

### 5.3 Hypotheses ruled out

| Hypothesis | Why it is rejected |
|---|---|
| User pressed reset | The board comes back on its own, ~30 s after the kernel goes silent, matching the TCO timeout — a manual reset would not be that regular |
| **PSU / VRM / GPU power delivery** | **Excluded.** At the moment of every reset the GPU is idle: `PPT ≈ 9 W` of a 211 W cap, `edge 43 °C` / `junction 44 °C` (crit 110 °C), `vram 556/12272 MiB`, load average 1.0–1.7 on 4 threads. No MCE, no brownout, no thermal alarm. A power event cannot manufacture a 30 s stall that ends in a cold boot at those numbers — and the resets cluster at ComfyUI *startup*, i.e. when the GPU does no work at all |
| Thermal | GPU `edge/junction` 43–44 °C, `crit` 110 °C; CPU package 40 °C, `crit` 100 °C; no throttling or thermal events in any boot |
| Host RAM exhaustion | `available` 25–29 GiB at every start, swap present and unused, no OOM killer |
| Disk full / IO hang | 118–120 GB free, no ext4 errors, journald and ComfyUI write to the same disk before the stall |
| The logging pipeline (initially suspected) | A real defect existed (missing drop-in directory, silent write failure, stripped trailing newlines, 60 s hung-task timeout) and was fixed — but the last reset happened **with** durable logging active and still produced nothing, which is the signature of a platform hang |

---

## 6. Why the kernel trace does not exist

This is not a logging failure. It is a physical limitation of the failure mode:

1. The kernel stops running → nothing is written to `dmesg`, the journal, or
   `pstore`.
2. `pstore` would need firmware/ramoops storage; `efi_pstore` is loaded but the
   firmware provides no records, and on a platform hang the kernel cannot write
   to it either.
3. `kdump` needs a panic; a platform hang never reaches the panic path.

The only sources that can capture this class of failure are **outside** the
hung machine: netconsole over the network to a second host, or a firmware-level
log (BMC — not present on a desktop board).

---

## 7. Instrumentation delivered during this investigation

Runtime (no sudo):
- `logs/comfyui.log` (INFO) + `logs/comfyui-debug.log` (DEBUG, second
  `--verbose` target), `logs/comfyui-server.log` (console mirror),
  `logs/comfyui-start.log` (console of a server started by `generate.sh`, moved
  out of `/tmp` so a reset cannot lose it), `logs/session.log` (RAM/swap/load +
  VRAM/GTT at every launch);
- `PYTHONFAULTHANDLER=1`; a `sync -f` loop over the log files every
  `LOG_SYNC_INTERVAL` s, which exits with its parent and does not hold the
  generator's `flock`;
- the generator validates every downloaded image (`smoke.check_image`) and now
  fails on blank frames instead of reporting success.

System (the `system` step, sudo):
- `/etc/systemd/journald.conf.d/99-comfyui.conf` — persistent journal,
  `SyncIntervalSec=1`, no rate limiting (installed 22:53:59);
- `/etc/rsyslog.d/49-comfyui-crash.conf` — fsync'd copy in
  `/var/log/comfyui-kernel.log`;
- `/etc/sysctl.d/99-comfyui-crash.conf` — `hardlockup_panic`, `softlockup_panic`,
  `hung_task_panic`, `hung_task_timeout_secs=15`, `watchdog_thresh=5`,
  `panic_on_oops`, `panic=10`, `printk_devkmsg=on`;
- `/etc/modules-load.d/comfyui-watchdog.conf` — watchdog driver chosen from the
  hardware (ACPI WDAT → `wdat_wdt`, Intel LPC → `iTCO_wdt`, AMD → `sp5100_tco`,
  otherwise `nct6775`);
- `/etc/systemd/system.conf.d/99-comfyui-watchdog.conf` — `RuntimeWatchdogSec=30s`,
  `RebootWatchdogSec=5min`.

Executor and planner defects fixed (all with regression tests):
- root writes create their parent directory (`install -D`) — `/etc/systemd/
  journald.conf.d` did not exist, so the journald drop-in was never written
  while the step reported success;
- `apply_plan` aborts when an action fails instead of swallowing the exit code;
- the plan decoder no longer strips the trailing newline of file contents;
- an active swapfile is not `mkswap`ed again;
- the watchdog module matches the chipset (was hardcoded to `sp5100_tco` on an
  Intel board).

Analysis tooling: `tools/crashlab` (standalone, stdlib only, no `provision/`,
no venv, no sudo, read-only):
`report`, `report --brief`, `report --json`, `boots`, `timeline`,
`coredump [PID] [--bt]`, `snapshot`, `self-test` (22 fixture tests).
It recognises `kernel-panic`, `freeze`, `gpu-hang`, `gpu-memory`,
`process-abort`, `oom`, `hardware`, `silent-reset`, `unlogged`; it rejects
stale evidence (a signature older than 5 min is context, not a cause) and
reports unreadable `pstore` as "root required" instead of "no records".

---

## 8. Prevention plan

Ordered by cost; change one variable at a time and keep the measurement rule:
**5 generations + 2 server restarts per experiment, then
`./tools/crashlab/crashlab boots` must show no new `UNCLEAN`.**

| # | Change | Rationale |
|---|---|---|
| 0 | `./provision.sh system` | restores the 30 s TCO watchdog (correct `iTCO_wdt` now) and the 15 s hung-task panic |
| 1 | `--disable-pinned-memory` | removes the ≈16 GiB pinned allocation that correlates with `pin failed` |
| 2 | non-int8 model (`models --set sft`) | the int8 path is where `hipblasLt` and the GPU page fault appear |
| 3 | remove comfy-kitchen int8 dispatch | its custom linear op sits directly under the fauling frame |
| 4 | `--lowvram` | keeps the diffusion model out of VRAM; fewer staging cycles |
| 5 | `pcie_aspm=off amdgpu.aspm=0` (cmdline) | ASPM transitions on Navi 22 are a classic platform-hang source; `aspm=-1` today |
| 6 | `processor.max_cstate=1` (cmdline) | deep C-states plus GPU wakeups can hang the fabric |
| 7 | kernel `6.8`/`6.11` LTS | `7.0.0-43` is a very new amdgpu; regressions are common |
| 8 | reload the GPU between sessions (`rmmod`/`modprobe amdgpu`, or a reboot before a long session) | tests the residual-state mechanism directly: if a fresh module instance never hangs, the fault is accumulated driver/GPU state, not the workload |
| 9 | run with `--cpu` for one session | proves the GPU path is required for the hang at all (no GPU context, no reset) |

Additional instrumentation worth adding (needs a decision):
- `ramoops` reserved region in the kernel cmdline (post-reset tail, if the
  kernel was still alive);
- netconsole to a second machine (the only way to see the last lines of a true
  platform hang);
- `kdump` with `crashkernel=` (vmcore on panic, once a panic actually fires).

### Acceptance criterion

24 hours and ≥30 start/generate cycles with no new `UNCLEAN` boot, and no new
`silent-reset`/`freeze` verdict for boots after the chosen mitigation.

---

## 9. Open questions

1. Does the hang require GPU work at all, or only GPU *context* creation? (All
   observed events are adjacent to context create/teardown.)
2. Which layer faults first: hipBLAS-Lt int8, comfy-aimdo staging, or the
   kernel's `amdgpu_vm_bo_update` path? A panic or a vmcore would answer it.
3. Is the platform hang reproducible with `amdgpu` alone (e.g. a HIP matmul
   loop) without ComfyUI? That would separate the driver from the ComfyUI
   software stack.
4. Do ASPM/C-state parameters alter the rate? (Steps 5–6.)

---

## 10. Appendix — commands used

```bash
# one-second answer
./tools/crashlab/crashlab report --brief
./tools/crashlab/crashlab report --json

# boot inventory and per-file evidence
./tools/crashlab/crashlab boots
./tools/crashlab/crashlab timeline
./tools/crashlab/crashlab coredump
./tools/crashlab/crashlab coredump 7904 --bt

# raw sources
journalctl --list-boots --no-pager
journalctl -b -1 -k -n 600 --no-pager -o short-iso
journalctl -b -1 --no-pager -o short-iso | tail -40
coredumpctl list --no-pager

# plan and state of the logging chain
./provision.sh --dry-run system
./provision.sh doctor
```
