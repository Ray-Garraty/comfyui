# Crash report — two resets in nine minutes under ComfyUI (2026-11-17)

**Status:** the machine stopped executing twice while ComfyUI was the only
unusual workload. Both stops are platform-level: the kernel wrote nothing to a
journal, a dedicated fsynced kernel log, or `pstore`. Today's resets differ from
the 2026-11-13 series in one important way — **there was no GPU error of any kind
before either stop**, so the "amdgpu page fault / pin failure" signature does not
explain them.

**Confidence:** high on *what happened* (an abrupt platform stop, not a software
crash, not an OOM kill, not a userspace abort). Low on *which layer stopped it*
(CPU/DRAM/VRM/PSU versus the amdgpu/KFD driver versus the firmware). The evidence
that would decide it was never on disk — this report also describes the black box
that now records it, second by second.

---

## 1. Summary

1. At **06:15:05** the last image of a five-round batch completed. The server
   logged `Prompt executed in 55.69 seconds`, wrote nothing more, and the machine
   was gone. It came back at 06:16:13.
2. At **06:25:37** a freshly started server finished its device initialisation.
   It never reached the model scan. The machine was gone again. It came back at
   06:26:04.
3. In both boots the kernel logged **zero runtime messages**, `pstore` was empty,
   no OOM kill happened, no watchdog was armed and the server left no core dump.
4. Both stops fall inside the two windows where ComfyUI creates or retires a
   HIP/KFD context: **server start** and **end of a round**. Between them the box
   ran 35 minutes and produced 25 images.
5. The machine is stable when ComfyUI is not running: boot `-1` sat idle for
   9 minutes and did not reset, and the current boot has been up since 06:26.

The reproduction of the analysis takes under a second:

```bash
./tools/crashlab/crashlab report --brief
```

---

## 2. Environment

| Item | Value |
|---|---|
| OS | Linux Mint 22.3 (Ubuntu 24.04 base), x86_64 |
| Kernel | `7.0.0-43-generic` (Ubuntu 7.0.0-43.43~24.04.1) |
| CPU / chipset | Intel Core i5-6500 (4C/4T) / Intel B150 LPC/eSPI |
| GPU | AMD Radeon RX 6700 XT, Navi 22, **gfx1031**, 12 272 MB VRAM, 16 027 MB GTT |
| RAM / swap | 32 056 MB / `/swapfile` 16 383 MB, no zram, no zswap |
| ECC | **absent** — `/sys/devices/system/edac/mc` has no memory controller, so a bad DRAM cell cannot be reported by anything |
| Host watchdog | `iTCO_wdt` deny-listed by the distro; `WatchdogDevice` is empty, no `/sys/class/watchdog` |
| PyTorch | `2.13.0+rocm10.1.0`, ROCm `(7, 16)` |
| ComfyUI | `0.45.3`, `comfy-kitchen 0.2.41`, `comfy-aimdo 0.6.0` |
| Launch | `--reserve-vram 1.5`, `INFO` + `DEBUG` log files, pinned memory enabled |

Boot of the second crash: 06:16:13 → 06:25:35 (journal), 9 min 28 s.

---

## 3. Crash inventory

`journalctl --list-boots` still holds 28 boots; `last` still holds 12 crash
records. The two added today:

| Boot | Started | Last record | Next boot | Dead air | Verdict |
|---|---|---|---|---|---|
| -2 | 05:38:45 (RTC skew, see §4.1) | 06:15:05 (app log) | 06:16:13 | 69 s | UNCLEAN |
| -1 | 06:16:13 | 06:25:37 (app log) | 06:26:04 | 27 s | UNCLEAN |

Boot `-2`'s journal stops at 06:11:25 — 3 min 40 s before the machine died —
because nothing was logging, not because journald stalled. The application logs
kept writing until 06:15:05.

The machine had already reset seven times on 2026-11-13 and twice on 2026-11-14
(see `reports/2026-11-13-crash-report.md`). Today's two are the first resets since
that investigation closed.

---

## 4. The two resets, second by second

### 4.1 06:15:05 — end of a generation round

Boot `-2` came up with the RTC three hours fast; `systemd-timesyncd` corrected it
at 05:39:19 (`Initial clock synchronization to Tue 2026-11-17 05:39:19`), so
everything below is real local time.

| Time | Event (source) |
|---|---|
| 05:39:37 | session snapshot: available **29 828 MiB**, swap **0/16 383**, VRAM 538/12 272, GTT 102/16 027 (`logs/session.log`) |
| 05:39:41 | server start: `Total VRAM 12272 MB, total RAM 32056 MB`, `Enabled pinned memory 27959` (`logs/comfyui.log`) |
| 05:39:47 | `aimdo: comfy-aimdo inited for GPU`, `DynamicVRAM support detected and enabled` |
| 05:44–06:07 | 20 images (`ComfyUI_00065` … `00084`), ~57 s per image, `RAM cache evictions … active=False full=False` throughout |
| 06:11–06:15 | 5 more images (`00085` … `00089`) |
| 06:15:03.892 | `Model AutoencoderKL prepared for dynamic VRAM loading. 118MB Staged` |
| 06:15:05.216 | `Prompt executed in 55.69 seconds` — **last line ever written** |
| 06:16:13.969 | next kernel boots |

Dead air: 69 s. Nothing in between: not one kernel line, not one rsyslog line.

### 4.2 06:25:37 — start of the server

`generate.sh` was started at 06:25:20. The server it launched printed its usual
banner, and then stopped mid-initialisation.

| Time | Event (source) |
|---|---|
| 06:25:20 | `=== generate.sh started ===`, `ComfyUI is not running … Starting it (--no-browser)` (`logs/generate.log`) |
| 06:25:35.939 | last journal record in that boot: `irqbalance[773]: Cannot change IRQ 132 affinity: Permission denied` |
| 06:25:37.317 | `Total VRAM 12272 MB, total RAM 32056 MB` |
| 06:25:37.729 | `Set vram state to: NORMAL_VRAM`, `Device: cuda:0 AMD Radeon RX 6700 XT : native`, `Reserving 1536.0MB vram` |
| 06:25:37.731 | `Enabled pinned memory 27959` |
| 06:25:37.808 | `Using sub quadratic optimization for attention` — **last line ever written** |
| 06:26:04.809 | next kernel boots |

Dead air: 27 s.

The next thing a healthy start does is create the ROCm context through
`comfy-aimdo`. In the 05:39 start the same gap took **5.5 s**
(`Using sub quadratic optimization` 05:39:41.683 → `aimdo: comfy-aimdo inited for
GPU` 05:39:47.182). The machine died inside that window: **no model was ever
loaded, no prompt was ever queued, and the GPU was idle** (on this box that reads
≈20 W, 47–50 °C, `gpu` 15 %).

---

## 5. Evidence

### 5.1 What exists

| Source | State at both crashes |
|---|---|
| `logs/comfyui.log`, `logs/comfyui-debug.log` | end mid-operation, no exception, no traceback |
| `logs/generate.log` | ends with the phase in progress |
| `logs/comfyui-server.log` | console mirror ends on the same line |
| `journalctl -b -2`, `-b -1` | 1152 kernel lines, all of them from boot; nothing at runtime |
| `/var/log/comfyui-kernel.log` (rsyslog, `$ActionFileEnableSync on`) | no line between 06:20 and 06:26:05 |
| `/var/log/syslog` (rsyslog, fsync) | no line between 06:14:45 and 06:16:00; last line 06:25:35.94 |
| `coredumpctl` | only `Xorg` `SIGABRT` entries created at boot; nothing from python |
| `output/` | `ComfyUI_00089_.png` is a complete, valid 1 152 635-byte image |

Every user-space log front writes to a file and flushes at least once a second,
so a userspace stall would have been visible as a gap. There is no gap: the last
line is a normal line, and then the machine is gone.

### 5.2 What does not exist, and why that is itself evidence

| Absent | Meaning |
|---|---|
| `Out of memory: Killed process` / `oom_kill` | the kernel did not run out of memory in a context where it could reclaim |
| `page allocation failure`, `allocstall` | the page allocator never reported a failed order |
| `amdgpu … page fault`, `pin failed`, `Failed to pin framebuffer` | **no GPU fault at all** — unlike 2026-11-13, where every reset was preceded by these |
| `mce`, `EDAC`, `AER`, `thermal` | no machine check, no PCIe error, no thermal trip reported |
| `hung_task`, `softlockup`, `hardlockup` | the watchdogs configured in `/etc/sysctl.d/99-comfyui-crash.conf` never fired |
| `/sys/fs/pstore` content | no panic record reached the firmware |
| `/sys/class/watchdog`, `WatchdogDevice=` | no hardware watchdog was armed, so nothing restarted the box on its own |

### 5.3 The A/B test the day already provides

| Interval | ComfyUI | Outcome |
|---|---|---|
| 06:16:13 → 06:25:20 | no | 9 min 7 s, stable, then a normal boot-time session |
| 06:25:20 → 06:25:37 | 18 s of start-up | **reset** |
| 06:26:04 → now | no | stable |
| 05:39:41 → 06:15:05 | 35 min of generation | **reset at the end of a round** |

The trigger is loading ComfyUI, not the passage of time.

---

## 6. Analysis

### 6.1 The one thing both resets share

| | 06:15 crash | 06:25 crash |
|---|---|---|
| Phase | end of a round, `Prompt executed` | start-up, device init |
| Model loaded | yes (MOSSImage + Qwen35TE + VAE) | **no** |
| GPU work in flight | VAE staged, then idle | context creation |
| GPU power / temp | ~20 W (idle after the round) | 20 W, 47 °C |
| Host memory | up to 25 GB of staging buffers in play | nothing staged yet |
| Kernel messages | none | none |

What is common is not "heavy compute" and not "high temperature" and not "VRAM
full". It is the **lifecycle of a HIP/KFD context and the host/GPU mappings behind
it**: created at start-up, torn down when the round ends and ComfyUI re-stages its
weights. Those are exactly the two moments the driver allocates or frees the
GTT/VRAM maps and the pinned host buffers, and they are the same two moments the
2026-11-13 report identified — but that report could point at page faults and
pin failures, and today there are none.

### 6.2 The memory picture

`ComfyUI/comfy/model_management.py` computes the pinned-memory allowance from RAM
**and swap**:

```python
MAX_PINNED_MEMORY = max(ram * 0.40, min(ram * 0.90, ram - 4 GiB, ram + swap - 16 GiB))
pinned_hostbuf_size(size) = max(0, int(min(size, MAX_PINNED_MEMORY) * 2))
```

The append-only `logs/comfyui.log` records the value on every start, which dates
the change exactly:

| Starts | `Enabled pinned memory` | Why |
|---|---|---|
| 2026-11-13 08:37 … 22:16 | **15 671 MiB** (15.3 GiB) | no swap active: `ram + 0 − 16 GiB` binds |
| 2026-11-13 22:17 … today | **27 959 MiB** (27.3 GiB) | swap enabled at 22:16:33: `ram − 4 GiB` binds |

Enabling swap did not add headroom that ComfyUI can swap into — pinned memory is
unswappable by definition. It **raised the pinning ceiling by 12 GiB**, from 49 %
to 87 % of physical RAM. With a 5 783 MB diffusion model and a 7 002 MB text
encoder staged at the same time, the staging buffers alone can ask for
2 × (5 783 + 7 002) = **25 570 MiB, ≈ 25 GiB**, plus GTT, plus the desktop.

That leaves roughly 4 GiB for the kernel, Xorg, the browser and the editor, and
the kernel has to service every allocation from it — including the allocations the
amdgpu driver makes inside its own paths. This is the largest single behavioural
change on the machine between the earlier investigations and today, and it was
made by us, in `provision.sh swap`.

It is not a sufficient explanation on its own (the 22:16–23:06 resets on
2026-11-13 happened under both ceilings), but it is the amplifier that makes the
difference between "a latent fault that never shows" and "a fault that shows every
session".

### 6.3 Candidate mechanisms, ranked

| # | Mechanism | For | Against |
|---|---|---|---|
| 1 | **Platform hardware**: host DRAM without ECC, VRM, or PSU. A bit flip in a kernel structure or a rail droop is a triple fault or a power-good drop: instant, silent, no trace, and only reachable when all 32 GB are exercised — which only ComfyUI does | explains the total absence of evidence, in every boot, for eight days; explains why idle sessions never fail | cannot be confirmed without `memtest86+`; GPU was at 20 W at the second reset, which weakens a PSU explanation but not a DRAM one |
| 2 | **amdgpu/KFD defect** in context creation/teardown under host-memory pressure | both stops land in exactly those windows; the machine is otherwise stable | the driver logged nothing, where it logged page faults and pin failures on 2026-11-13; a pure driver bug usually leaves at least one line |
| 3 | **Host memory exhaustion** | the pinning ceiling is 87 % of RAM; exhaustion is one allocation away | no OOM kill and no allocation failure; ComfyUI's own contract keeps 4 GiB free — but nothing measured `MemAvailable` at the moment of death, so this cannot yet be excluded |
| 4 | **Thermal shutdown (CPU/board EC)** | silent by nature; a firmware trip leaves no kernel record | the GPU was idle at ~47 °C and the CPU had nothing to do at the second reset |

### 6.4 "No trace" does not yet mean "no panic"

`panic_on_oops=1` and `panic=10` are configured. A panic would print on the
console, but journald and rsyslog are user-space processes: their last second is
exactly what a panic destroys. `efi_pstore` is registered but this firmware does
not persist a record to it, and **`ramoops` is not configured**, so there is no
memory region that survives a reset with the tail of `dmesg` in it.

The observed dead air (69 s and 27 s) does not fit a single timer either: a
10 s `panic=` plus firmware POST could be ~27 s, but not ~69 s. So the reset
source is still unknown, and the report keeps it that way instead of guessing.

---

## 7. Why the blind spot exists

Everything the project built so far records the *aftermath*: `pstore`, the
previous boot's journal, `coredumpctl`, signature counts. Those are only useful
when the kernel had a chance to complain. This machine's failure mode is the one
that gives no chance at all, so the only evidence that can survive is state that
was **already on the platter before the stop**.

That is now recorded.

---

## 8. Instrumentation delivered with this report

### 8.1 `vitals` — a one-second black box (`provision/lib/comfyprov/vitals.py`)

One line per second, flushed and `fsync`ed, rotated at `VITALS_MAX_MB` (8 MB,
≈ 7 hours; the `.1` file adds as much again). It covers the three things that can
plausibly take this platform down:

| Group | Keys |
|---|---|
| Host memory | `total avail free swap mlock unevict dirty writeback anonhuge` (MiB) |
| Pressure | `psi_some psi_full` (avg10), `psi_some_total psi_full_total` (seconds), `allocstall compactstall oom pgscandirect pgstealdirect pgmajfault` |
| Scheduler | `load run blocked dstate` — the name of every task stuck in `D` |
| GPU | `vram gtt gpu temp power sclk mclk`, `kfd` (open compute contexts) |
| The server | `pid mono rss pss pssanon swappss privatedirty`, plus `lag` (how late the sample was) |

A real sample from this machine:

```
2026-11-17T06:39:11.848+03:00 mono=792.91 total=32055 avail=28059 free=18353 swap=0/16383
  mlock=0 unevict=0 psi_some=0.00 allocstall=0 pgmajfault=16117 load=2.60/2.37/1.41
  run=7 blocked=0 dstate=- vram=980/12272 gtt=57/16027 gpu=15 temp=49 power=20.0
  sclk=84 mclk=96 kfd=0 lag=0.00
```

`MLocked`/`Unevictable` are the numbers that have never been measured on this
box: they will show whether the 27 959 MiB ceiling is actually reached, and
`lag` will show whether the *kernel* stopped scheduling before the reset (a stretch
of samples arriving late) or whether the machine simply stopped (no further line).

### 8.2 The recorder is wired into the launcher

`start-comfyui.sh` starts it as soon as the command line is known, watching the
launcher's own PID — which becomes the server through `exec`, so the recorder
stops exactly when the server does. Enabled with `COMFYUI_VITALS=1` (default).

### 8.3 A tripwire, not just a witness

`VITALS_GUARD_AVAIL_MB` (default 1024): when `MemAvailable` falls below the floor,
the recorder appends one `event=guard avail=… floor=… action=stop` line and sends
`SIGTERM` to the server. A run that ends that way is an explained shutdown, not an
unexplained reset — and if resets stop happening under the guard, hypothesis 3
(§6.3) is confirmed by experiment.

### 8.4 The largest memory consumer can be removed

`COMFYUI_DISABLE_PINNED_MEMORY=1` passes `--disable-pinned-memory` to ComfyUI.
It costs throughput and it is the cleanest A/B test available for hypothesis 2.

### 8.5 Tests

`provision/tests/test_vitals.py` — 17 cases on a fake host: meminfo/PSI/vmstat
parsing, the `D`-state scan, hwmon scaling, `smaps_rollup` versus `status`,
sample shape, log rotation, the guard actually terminating a process, and exit on
the watched process disappearing.

### 8.6 Still missing (next)

- `crashlab` does not read `logs/vitals.log` yet: no `vitals` section in the
  report, no "last sample before the stop" verdict row. The format is stable and
  deliberately independent of `comfyprov`, so this is a parser addition only.
- `ramoops` / serial console / `netconsole`: the only way to read a panic whose
  user-space log fronts never got to write.
- Hardware watchdog arming (needs `sudo` and a `./provision.sh` step; blocklisted
  by the distro today).

---

## 9. Experiment plan

1. **Let the black box run.** The next `generate.sh` session writes
   `logs/vitals.log`. After the next reset, read the last sample and match it
   against this table:

   | Last samples say | Verdict |
   |---|---|
   | `avail` ramps down to ~0, `psi_full` ≈ 100, `allocstall` jumps, `swap` fills | host memory exhaustion (hypothesis 3) |
   | `gpu` = 100 %, `power`/`temp` spike before the stop | power or thermal (hypothesis 1/4) |
   | `vram`/`gtt` saturate, `kfd` churns, then the file stops | GPU driver path (hypothesis 2) |
   | everything normal, then no further line | platform stop (hypothesis 1) |
   | timestamps stretch, `lag` grows before the end | the kernel stalled first — a different bug |

2. **Test the RAM.** `memtest86+`, two full passes, overnight. There is no ECC, so
   this is the only way to rule hypothesis 1 in or out. Do not skip it because the
   software story is more interesting.
3. **A/B the pinned memory.** One session with `COMFYUI_DISABLE_PINNED_MEMORY=1`.
   If the resets stop, the pinned path is implicated; if they continue, the
   pinning ceiling is not the trigger.
4. **Arm the watchdog** (separate `sudo` step): a 30 s hardware timer converts
   "frozen forever" into "rebooted in 30 s with the event still in the journal".
5. **Keep the guard on** (default) while testing, so a memory cliff is caught
   instead of investigated.

---

## 10. Acceptance criteria

- Ten consecutive `generate.sh` rounds complete without a reset, **and**
- if a reset does happen, `logs/vitals.log` contains the second before it, and the
  `crashlab` report names the mechanism instead of "platform-freeze".

Until then the verdict stays **platform stop, cause not yet proven**, and that is
the honest state of the evidence: the machine is not rebooting because something
threw an exception — it is rebooting because, at the moment ComfyUI creates or
retires its GPU context, the platform stops executing and leaves nothing behind.

---

## 11. Open questions

1. Who reset the machine? 69 s and 27 s of dead air fit no single timer. A person
   pressing reset would look exactly like this.
2. Does the pinned-memory ceiling actually get reached? `mlock` was never
   measured. The recorder answers this within one session.
3. Is the amdgpu driver silent because nothing happened, or because it panicked in
   a context that cannot print? Only `ramoops`/serial can answer.

---

## 12. Appendix — commands used

```bash
# one-second answer
./tools/crashlab/crashlab report --brief
./tools/crashlab/crashlab report --full
./tools/crashlab/crashlab boots

# the two boots
journalctl --list-boots --no-pager | tail -4
journalctl -b -1 -k -o short-precise | tail -40
last -x --time-format=iso | head -20

# application-side evidence
grep -a "Enabled pinned memory" logs/comfyui.log
tail -25 logs/comfyui.log
grep -av STREAM logs/comfyui-debug.log | tail -15
cat logs/generate.log

# the logging chain that should have caught it
tail -30 /var/log/comfyui-kernel.log
awk '$1 >= "2026-11-17T06:25:15" && $1 <= "2026-11-17T06:26:05"' /var/log/syslog
systemctl show -p WatchdogDevice
coredumpctl list --no-pager | tail -5

# the black box
python3 -m comfyprov vitals --once          # with PYTHONPATH=provision/lib
tail -3 logs/vitals.log
```
