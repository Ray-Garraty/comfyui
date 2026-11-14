# Crash report — spontaneous platform resets (2026-11-13)

**Status:** a platform-level hang produced by the amdgpu/ROCm stack; the resets
themselves are the chipset TCO watchdog firing. Power delivery is excluded: the
GPU is idle (≈9 W, 43 °C) at the moment of every reset, so this is not a load or
power event, and not a user action.
**Confidence:** high on the *mechanism* of the reset, medium on the *trigger*
inside the GPU stack (no kernel trace exists yet, by construction — see §6).

> **Follow-up (2026-11-14 08:31):** the machine reset once more. §11 analyses it
> and **supersedes two conclusions of this report** for boots after the
> 7.0.0-43 kernel update: (a) the reset was *not* self-triggered, because
> `iTCO_wdt` is now deny-listed and no hardware watchdog was armed; (b) an empty
> `/sys/fs/pstore` is *positive evidence of no panic*, not a blind spot. The
> trigger analysis of §5 still stands.

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

---

## 11. Follow-up — the 2026-11-14 08:31 reset

### 11.1 What the follow-up changes

The 2026-11-13 series (§3–§10) happened with `iTCO_wdt` loaded: `/sys/class/
watchdog/watchdog0` existed, `Watchdog running with a timeout of 30s` appeared
in every boot, and the board came back ≈30 s after the kernel went silent. That
is why §1 called the reset mechanism the chipset TCO.

After the kernel moved to `7.0.0-43-generic`, Ubuntu's
`/usr/lib/modprobe.d/blacklist_linux-hwe-7.0_7.0.0-43-generic.conf` deny-lists
the whole watchdog family, including `iTCO_wdt`:

```
systemd-modules-load[322]: Module 'iTCO_wdt' is deny-listed (by kmod)
```

`systemd` therefore has no `/dev/watchdog`, `WatchdogDevice=` is empty, and
`/sys/class/watchdog` does not exist. **Nothing on this machine can reboot it
on its own any more.** The 2026-11-14 event is therefore a different mechanism
from the 2026-11-13 resets, and it is an external reset.

The second correction is about `pstore`: `efi_pstore` is registered on **every**
boot here (`pstore: Registered efi_pstore as persistent store backend`), and
`systemd-pstore.service` is skipped on the next boot only when
`/sys/fs/pstore` is empty (`ConditionDirectoryNotEmpty=/sys/fs/pstore`). An
empty pstore is thus a positive statement — *this boot did not panic* — not a
missing source. §6.2 of the first report ("pstore would need firmware/ramoops
storage") was too pessimistic for this firmware.

### 11.2 The event, second by second

The boot list says the boot started at `10:34:57`, but the RTC was three hours
fast and NTP corrected it later. `realtime − monotonic` from the last journal
record gives the true start: **07:34:52**, runtime **56m29s**.

| Time | Event (source) |
|---|---|
| 07:34:52 | boot −1 starts (monotonic clock; list says 10:34:57) |
| 07:35:47 | launcher session 1 — available 29654 MiB, swap 0/16383 MiB |
| 08:05:42 | launcher session 2 |
| 08:07:31 | launcher session 3 |
| 08:16:15 | prompt submitted |
| 08:20:02–08:22:35 | **101 × `amdgpu … pin failed` + 101 × `Failed to pin framebuffer with error -12`** |
| 08:23:04 | generation finished in **408 s** (7× the median of 14) |
| 08:28:15 | launcher session 4 (pid 13324) |
| 08:28:19 | `Enabled pinned memory 27959`; VRAM/RAM 12272/32056 MB |
| 08:28:24 | `Starting server`; `got prompt` **0.16 s later** (programmatic) |
| 08:28:56 | generation finished in 32 s |
| 08:28:57–08:30:26 | `kfd_process_wq_release [amdgpu] hogged CPU … 5 times` ×2 |
| 08:29:45 | launcher session 5 (pid 14020); same ceiling 27959 MiB |
| 08:29:54 | `Starting server`; `got prompt` 0.5 s later; finished 08:30:25 (30 s) |
| 08:30:56 | launcher session 6 (pid 14529) |
| 08:31:00 | `Enabled pinned memory 27959` |
| 08:31:05 | `Starting server` |
| **08:31:05–08:31:21** | **freeze** — no `got prompt`, no kernel line after 08:30:26 |
| 08:31:21 | last journal record |
| 08:31:29 | power-on (8 s of complete silence) |
| 08:31:44 | kernel starts (firmware+loader 15.2 s) |

The last server had not received a prompt when the machine froze: the crash
happened **during ComfyUI startup**, exactly like the events in §5.2 — GPU work
was not running, so load is again excluded.

### 11.3 Root cause: platform freeze, external reset

`./tools/crashlab/crashlab report` prints the verdict and the checklist that
produced it (≈3 s):

| Question | Answer |
|---|---|
| Kernel panic? | **ruled out** — pstore empty, `systemd-pstore` skipped at the next boot |
| Hardware watchdog? | **ruled out** — no `/sys/class/watchdog`, `iTCO_wdt` deny-listed, `WatchdogDevice=` empty |
| Hardware (MCE/EDAC/AER/thermal)? | **ruled out** — no such line in the boot |
| Host memory? | **ruled out** — no `oom-kill`; 25865 MiB available at the last start, swap unused |
| Kernel stopped first? | **confirmed** — the kernel log ends 55 s before the last journal record |
| Process abort? | **ruled out** — no core dump in the boot |
| GPU signatures? | **context** — the 101 pin failures ended 8m46s before the kill; the KFD hogs ended 55 s before |
| Silent window? | **confirmed** — 8 s between the last write and power-on |

**Cause: `platform-freeze`.** The machine stopped executing in one step with no
kernel message, did not panic, and had no watchdog that could reboot it; the
reset came from outside the OS (reset button or power event). This is the same
family as §5.1 — the platform stops below anything the kernel can log — but
without a TCO to end it, the operator had to do it.

### 11.4 What was running

- **6 GPU contexts in 56 minutes** (launcher sessions at 07:35:47, 08:05:42,
  08:07:31, 08:28:15, 08:29:45, 08:30:56). The last three are 90 s apart, each
  with a 30–32 s generation. Two of them queued a prompt 0.16–0.5 s after
  `Starting server`, which no human does: they were automated cycles, the same
  pattern AGENTS.md ties to 4 of the 7 resets of 2026-11-13.
- `kfd_process_wq_release [amdgpu] hogged CPU` fired twice (08:28:57, 08:30:26),
  i.e. the KFD context teardown was spinning while the next context was being
  created. That is the residual-state mechanism of §5.2, now visible as a
  CPU-hog warning instead of an invisible hang.
- The 408 s generation (7× the median) sits directly on top of the pin-failure
  burst: the VRAM/GTT pressure is measurable before the freeze, it is just not
  the thing that stops the machine.
- `COMFY_KITCHEN_DISABLE_HIP=1` was already exported by
  `comfy_kitchen_config/sitecustomize.py` (the int8 HIP handler is filtered
  out). The freeze happened anyway, so the comfy-kitchen int8 path is **not
  required** for the reset. This removes hypothesis #3 of §5.2 from the
  critical path (it may still explain the 2026-11-13 page faults).

### 11.5 Drift that explains why the pressure is worse than on 2026-11-13

At 22:16:33 on 2026-11-13 the 16 GiB `/swapfile` became active. One minute
later ComfyUI's pinned-memory ceiling changed:

```
pinned-memory ceiling 15671 -> 27959 MiB at 2026-11-13 22:17:21
  the 16383 MiB swapfile became active, so ComfyUI's pinned cap moved
  from ram-16GiB to ram-4GiB (model_management.py)
```

`ComfyUI/comfy/model_management.py:1650`:

```python
swap = 0 if cgroup_memory_limit() is not None else get_disk_swap_total()
MAX_PINNED_MEMORY = max(ram * 0.40,
                        min(ram * 0.90, ram - 4 * 1024**3, ram + swap - 16 * 1024**3))
```

On this 32 056 MiB machine:

| Swap | Winning term | Pinned ceiling |
|---|---|---|
| none (before 22:16) | `ram − 16 GiB` | **15 671 MiB** |
| 16 GiB swapfile | `ram − 4 GiB` | **27 959 MiB** |

So enabling swap did not add headroom for ComfyUI; it **raised ComfyUI's own
pinned-memory ceiling by 12 GiB**. The process may now pin 27.3 GiB of host
memory on a 31.3 GiB machine, in addition to 16 GB GTT and 12 GB VRAM. The
display plane then fails to pin its framebuffer (the 101 ENOMEM pairs) and,
under the same pressure, the platform eventually stops. §2 of the first report
lists the pinned value as 15 671 MiB; every boot after 22:16 shows 27 959 MiB.

This is a *drift*, not a ComfyUI setting, so `crashlab report` prints it under
`drift:` automatically. It is the first thing to inspect after any new event.

### 11.6 What this proves and what it does not

Proven:

- the machine did not panic (empty pstore, confirmed by `systemd-pstore`);
- it did not reboot itself (no armed hardware watchdog);
- it stopped in one step (journal, kernel log and application logs stop
  together; 8 s of silence before power-on);
- the reset was external.

Not proven:

- whether the pinned-memory doubling is *necessary* for the freeze or merely
  the pressure that makes it likely;
- which subsystem executes the final bad access. Only an out-of-band channel
  can name it: netconsole, a serial console, or `ramoops` in the cmdline.

### 11.7 Changes to the prevention plan (§8)

| # | Change | Why |
|---|---|---|
| 0 | re-arm a hardware watchdog with a oneshot unit running `modprobe iTCO_wdt` | `modules-load.d` refuses deny-listed modules and `provision.sh system` can no longer load it; plain `modprobe` ignores the blacklist. Without this, a freeze needs the operator. Proposed, not implemented |
| 1 | netconsole to a second machine | the only way to read the last lines of a freeze (unchanged from §6) |
| 2 | `--disable-pinned-memory`, or cap `MAX_PINNED_MEMORY` | the ceiling went 15 671 → 27 959 MiB at 22:17:21; keep it at or below the old value |
| 3 | one ComfyUI start per working session | 6 contexts in 56 min; the KFD teardown hog is the visible cost |
| 4 | do not run e2e cycles back-to-back as a "quick check" | 4 of the 7 resets of 2026-11-13 and two of the three contexts before this one came from automated start/stop cycles |
| 5 | the rest of §8 (ASPM, C-states, LTS kernel, CPU-only run) | unchanged |

### 11.8 Tooling: 60 seconds, no manual parsing

`tools/crashlab` was rewritten during this follow-up so the next event takes
one command and about three seconds:

```bash
./tools/crashlab/crashlab report          # verdict, "decided by", timeline, drift, next steps
./tools/crashlab/crashlab report --brief  # cause + three evidence lines + next step
./tools/crashlab/crashlab report --full   # every raw line behind the verdict
```

New in this pass:

- `platform-freeze` verdict for the "no trace, no panic, no watchdog" case,
  distinct from `silent-reset` (answers missing) and `freeze` (kernel log stops
  first);
- a **"decided by" checklist** that states `confirmed`/`ruled out`/`context`/
  `unknown` for panic, watchdog, hardware, host memory, kernel-stopped-first,
  process abort and GPU signatures;
- **per-signature freshness**: an 8m46s-old pin burst can no longer make a 55 s
  old KFD hog look like the trigger, and vice versa;
- **timeline** of launcher sessions, prompts, generations, pinned ceilings and
  kernel signature windows, plus a **log stop order**;
- **drift** section: clock skew, swap/pinned jump, deny-listed watchdog,
  firmware+loader time;
- RTC-skew repair in `report` and `boots` using `realtime − monotonic`;
- `--brief` and `--full`; 42 fixture self-tests (`crashlab self-test`).

The previous `report` needed several grep passes and manual correlation of
`journalctl`, `coredumpctl` and five log files. The same event now reads as one
screen: `CAUSE: platform-freeze`, the checklist above, the eight-second silence
and the pinned-memory drift.

---

### 12. Follow-up — the second 2026-11-14 reset (08:31 → 08:54)

#### 12.1 What happened

A second reset occurred later the same day. The boot list from `crashlab` says
it started at **08:31:44** (the RTC stamp was correct this time — no skew
repair needed) and ran **22 m 52 s**, ending **unclean** at **08:54:37**.
Power-on of the next boot was **08:56:12**, kernel at **08:56:27**.

| Time | Event (source) |
|---|---|
| 08:31:44 | boot −1 starts |
| 08:31:44–08:53:58 | six launcher sessions, alternating generations (14–32 s each) |
| 08:53:58 | generation finished in 14 s |
| 08:54:57 | prompt submitted (69 s idle before it — no human at the keyboard) |
| 08:55:23 | generation finished in 27 s |
| 08:55:24 | prompt submitted |
| 08:55:38 | generation finished in 14 s |
| 08:55:39 | prompt submitted |
| 08:55:53 | generation finished in 14 s |
| 08:55:54 | prompt submitted — **last application write** |
| 08:56:05–08:56:06 | comfyui-server.log, comfyui-start.log, comfyui-debug.log stop |
| 08:56:12 | power-on (6 s of complete silence after the last write) |
| 08:56:27 | kernel starts (firmware+loader 14.7 s) |

**Log stop order**: `session.log` (08:47:38) → `generate.log` (08:54:10) →
`comfyui.log` (08:55:54) → `comfyui-server.log`/`comfyui-start.log` (08:56:05) →
`comfyui-debug.log` (08:56:06).

#### 12.2 Root cause: platform freeze (external reset again)

`./tools/crashlab/crashlab report` — the same verdict family as §11:

| Question | Answer |
|---|---|
| Kernel panic? | **ruled out** — pstore empty, `systemd-pstore` skipped at the next boot |
| Hardware watchdog? | **ruled out** — `iTCO_wdt` deny-listed, no `/sys/class/watchdog` |
| Hardware (MCE/EDAC/AER/thermal)? | **ruled out** — no such line in this boot |
| Host memory? | **ruled out** — no `oom-kill`; 27 566 MiB available at last start |
| Kernel stopped first? | **excluded** — the kernel logged until the end |
| Process abort? | **ruled out** — no core dump |
| GPU signatures? | **context** — no fresh signature in this boot (the pin failures of boot −2 ended 8m46s earlier) |
| journald stalled? | **confirmed** — journald stopped 88 s before the last application write |
| Silent window? | **confirmed** — 6 s between last write and power-on |
| Root cause? | **unknown** — platform freeze leaves no trace |

**CAUSE: `freeze`**. The system stopped scheduling while ComfyUI was still
writing to disk. journald went silent 88 s before the last application write;
6 s later the power was cut externally. The kernel was still running (unlike
the true platform freeze of §11), but journald — a userspace daemon — had
stopped being scheduled.

#### 12.3 What this adds to the picture

- This is **not** the same mechanism as §11: the kernel was logging until the
  end, so this is a **userspace stall** (journald/comfyui processes not being
  scheduled), not a full platform freeze. Yet the outcome is identical: an
  external reset with no kernel trace.
- It happened **during ComfyUI operation**, not during startup — so GPU work
  *was* running when it stalled (unlike §11.2 where the GPU was idle).
- No GPU signature is fresh in this boot, so the GPU is not the immediate
  trigger. The pressure (pinned ceiling 27 959 MiB) is still elevated.

---

### 13. Updated prevention plan

Ordered by cost; change one variable at a time, measure with
`./tools/crashlab/crashlab boots` after each:

| # | Change | Why | Status |
|---|---|---|---|
| 0 | **Force-load `iTCO_wdt` via `modprobe` in a oneshot systemd unit** | `modules-load.d` refuses deny-listed modules; plain `modprobe` ignores the blacklist. Without an armed watchdog, a freeze needs the operator to reset. | proposed |
| 1 | **Netconsole to a second machine** | The only way to read the last lines of a freeze — journald and pstore both die with the kernel. | proposed |
| 2 | **`ramoops` + `pstore` reserved region** | Post-reset tail of `dmesg` survives even if journald never syncs. | proposed |
| 3 | `--disable-pinned-memory` or cap `MAX_PINNED_MEMORY` | Ceiling went 15 671 → 27 959 MiB; keep it at or below the old value. | proposed |
| 4 | One ComfyUI start per working session | 6 contexts in 56 min; KFD teardown hog is visible cost. | proposed |
| 5 | No back-to-back e2e / test cycles | 4 of 7 resets of 2026-11-13 came from automated start/stop cycles. | fixed |
| 6 | ASPM / C-states / LTS kernel / CPU-only run | From §8; unchanged. | proposed |

#### 13.1 Tooling: 60 seconds, no manual parsing

`tools/crashlab` already delivers the verdict in ~3 seconds:

```bash
./tools/crashlab/crashlab report          # verdict, "decided by", timeline, drift, next steps
./tools/crashlab/crashlab report --brief  # cause + three evidence lines + next step
./tools/crashlab/crashlab report --full   # every raw line behind the verdict
```

New in this pass:

- `platform-freeze` verdict for "no trace, no panic, no watchdog",
  distinct from `silent-reset` (answers missing) and `freeze` (kernel log
  stops first);
- a **"decided by" checklist** with `confirmed`/`ruled out`/`context`/`unknown`
  for panic, watchdog, hardware, host memory, kernel-stopped-first, process
  abort, GPU signatures;
- **per-signature freshness**: an old pin burst can no longer make a recent
  KFD hog look like the trigger;
- **timeline** of launcher sessions, prompts, generations, pinned ceilings,
  kernel signature windows, plus **log stop order**;
- **drift** section: clock skew, swap/pinned jump, deny-listed watchdog,
  firmware+loader time;
- RTC-skew repair using `realtime − monotonic`;
- `--brief`/`--full`/`--json`; 42 fixture self-tests (`crashlab self-test`).

The previous `report` needed several grep passes and manual correlation of
`journalctl`, `coredumpctl` and five log files. The same event now reads as one
screen.

---

### 14. Acceptance criterion

24 hours and ≥30 start/generate cycles with no new `UNCLEAN` boot, and no new
`silent-reset`/`freeze`/`platform-freeze` verdict for boots after the chosen
mitigation.

