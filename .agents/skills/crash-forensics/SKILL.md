---
name: crash-forensics
description: Determine why this Linux/ComfyUI machine crashed (hard reset, freeze, OOM, GPU hang, process abort) from its logs. Use when the PC rebooted by itself, froze, lost the ComfyUI server, or when someone asks "what happened in the logs". Prefers parser commands over reading raw logs by hand.
---

# Crash forensics

Goal: name the cause in **under 60 seconds**, with evidence, and never guess.
`crashlab report` itself runs in about three seconds; the rest of the minute is
reading its output. Delegation rule: **run the parser, read the verdict, and
only open raw log lines that it points at** (`--full`). Do not dump whole logs
into the context, and do not grep the journal by hand before running it.

All commands below are read-only and safe to run while ComfyUI is up.

## 0. Safety rules for this box

- **Never run `./provision.sh test`, `pytest -m e2e` or `./start-comfyui.sh`
  while diagnosing.** The end-to-end test starts a real ComfyUI server and
  hard-resets this machine (observed twice). Diagnosis must not create the
  event it is investigating.
- Do not run anything that needs sudo. The `system` step that installs the
  logging chain is run by the user, not by you.
- Never call it "hardware" from a single event. Only `Hardware Error`, `MCE`,
  `EDAC` or thermal-critical lines justify that.

## 1. The 60-second pass: one command

```bash
./tools/crashlab/crashlab report
```

Read the output in this order:

1. **boot** — true start (from the monotonic clock), runtime, end, and whether
   the boot was clean. A wrong RTC is repaired here; the boot-list timestamp
   appears as `(clock list: ...)`.
2. **silence** — seconds between the last write on disk and power-on. Eight
   seconds is a power button; minutes means the machine stopped writing long
   before it stopped running.
3. **CAUSE / VERDICT** — the finding and one sentence that qualifies it.
4. **decided by** — the checklist. Each row is a question with an answer:
   `confirmed`, `ruled out`, `context` (real but too old to be the trigger) or
   `unknown`. This is what separates a finding from a guess.
5. **drift** — what changed since the previous boot: clock skew, swapfile
   activation and the pinned-memory jump, a deny-listed watchdog, firmware
   time. A new crash usually follows a drift row.
6. **next steps** — the experiment that can close the still-open question.

Use the shorter and longer forms only when needed:

```bash
./tools/crashlab/crashlab report --brief   # cause, three evidence lines, next step
./tools/crashlab/crashlab report --full    # every raw line behind the verdict
./tools/crashlab/crashlab report --json    # machine-readable
```

If the verdict is `unlogged` or `silent-reset`, the missing information *is*
the finding: the logging chain is not applied yet (step 6). Continue there
instead of guessing.

## 2. Was it a crash or a clean reboot?

```bash
./tools/crashlab/crashlab boots
```

- `UNCLEAN` means the boot ended without
  `Reached target (poweroff|reboot|shutdown).target`; `running` is this boot.
- A row with `(clock list: ...)` had a wrong RTC at boot; the first timestamp
  is the real start computed from `realtime - monotonic`, not from the clock.
- If the journal of the crashed boot contains only boot-up lines, journald was
  not persistent yet — the missing information is itself the finding. Fix with
  the `system` step (`./provision.sh system`, run by the user).

Second, independent marker — the page cache never reached the disk.
`crashlab timeline` prints it per file as `[N zero bytes at the end -> hard reset]`;
`report` lists the same fact in the evidence section.

## 3. What ran before it died?

The launcher and the generator write their own state to disk, so this needs no
shell archaeology. `report` already builds the timeline from these files:

| File | Answers |
|---|---|
| `logs/session.log` | RAM/swap/load + VRAM/GTT at every server start |
| `logs/comfyui-start.log` | console of a server started by `generate.sh` |
| `logs/comfyui-server.log` | console of a server started by hand |
| `logs/comfyui.log` / `comfyui-debug.log` | `INFO` / `DEBUG` of ComfyUI itself |
| `logs/generate.log` | generator phases and end state |

The timeline flags two things worth acting on:

- **programmatic prompts** — `got prompt` less than a few seconds after
  `Starting server` was queued by a script, not typed by a human, so the operator
  was not sitting in the session that crashed.
- **slow generations** — a prompt that took more than 3x the median duration of
  the boot is a memory-pressure symptom, not a model problem.

`log stop order` names the file that stopped last: that process was still alive
when everything else froze.

`crashlab timeline` prints the last three lines of every `logs/*.log` if you
need them without the full report.

## 4. Kernel signatures

`crashlab report` counts these and shows their windows; the raw command is only
needed for a signature that the table does not know yet:

```bash
journalctl -b -1 -k --no-pager -o short-iso | grep -aiE \
 'pin failed|Failed to pin framebuffer|amdgpu_job_timedout|GPU reset|ring .* timeout|VM_L2|page fault|soft lockup|hard LOCKUP|hung_task|Out of memory|oom-kill|Hardware Error|Machine check|MCE:|EDAC|critical temperature|AER|hogged CPU'
```

Interpretation (each maps to a different fix):

- `amdgpu ... pin failed` + `Failed to pin framebuffer with error -12` —
  GPU memory accounting: the display plane cannot get VRAM/GTT. Software:
  allocations/limits (`COMFYUI_RESERVE_VRAM`, pinned memory, no swap). Not a PSU.
  Check **when** it happened: a burst 8 minutes before the kill is `context`.
- `amdgpu_job_timedout`, `GPU reset`, `ring gfx .* timeout`, `VM_L2` — driver
  hang. Suspect the ROCm stack/pinned memory, then the driver version.
- `soft lockup`, `hard LOCKUP`, `hung_task` — the box stopped scheduling;
  needs `hardlockup_panic` + pstore to capture the stack next time.
- `kfd_process_wq_release [amdgpu] hogged CPU` — GPU context teardown is
  spinning; one is normal, a pattern means context churn (repeated ComfyUI
  start/stop cycles).
- `oom-kill`, `Out of memory` — host RAM exhausted (check swap: it is mandatory
  on a 32 GB box that pins up to 16 GiB).
- `Hardware Error`, `MCE`, `EDAC ... error`, thermal critical — the only
  evidence that justifies blaming the hardware.
- Nothing at all in the kernel log while userspace kept logging — the kernel
  froze without a trace: the logging chain was not fast enough (see step 6).
- Nothing at all anywhere (`platform-freeze`) — the machine stopped executing
  in one step. This is a platform/power event, not an OS bug.

## 5. Core dumps: do not read them, parse them

```bash
./tools/crashlab/crashlab coredump          # list: pid, signal, decoded meaning
./tools/crashlab/crashlab coredump <PID>    # signal, command, frames, blame
./tools/crashlab/crashlab coredump <PID> --bt   # gdb backtrace, ~15 s
```

`blame` names the subsystem (`PyTorch HIP/CUDA call check (c10)`, `HIP runtime`,
`rocmBLAS`, ...) and `origin:` gives the failing frame, e.g.
`c10::cuda::SetDevice`. Xorg and VS Code dumps are shutdown noise and are
excluded from attribution.

Signals mean different things: `SIGABRT` = `abort()`/assert in native code
(ROCm, comfy-kitchen), `SIGSEGV`/`SIGBUS` = bad memory access or bad mapping,
`SIGKILL` = the kernel killed it (OOM). Attribute the abort by the *first
frames*: `libamdhip64` → HIP runtime, `librocblas`/`libMIOpen` → math library,
`comfy_aimdo` → the dynamic-VRAM allocator, `comfy_kitchen` → quantized ops,
`libtorch` → PyTorch.

A ComfyUI python abort at server *shutdown* means the machine survived the
process: treat it as a separate bug, not as the reset.

## 6. Panic, pstore and the watchdog: what "no evidence" means here

`report` answers these under **decided by**; you do not need to run them by hand.

- **Kernel panic.** `efi_pstore` is registered on every boot of this machine,
  so a panic in the previous boot leaves a record that `systemd-pstore` archives.
  An empty `/sys/fs/pstore` plus `systemd-pstore.service ... skipped
  (ConditionDirectoryNotEmpty=/sys/fs/pstore)` on the next boot is therefore
  **positive evidence that no panic happened**, not a blind spot. A non-empty
  pstore or a `Kernel panic` line overrides everything else.
- **Hardware watchdog.** `/sys/class/watchdog` and the journal's
  `Using hardware watchdog` line tell you whether one was armed. On this machine
  `iTCO_wdt` is deny-listed by Ubuntu's kernel blacklist, so no watchdog runs:
  if the box had hung, it could not reboot itself. An unarmed watchdog plus no
  panic means the reset came from outside the OS.
- **Host memory.** `no oom-kill in this boot` plus the last snapshot's available
  RAM and swap answers the OOM question.

If `report` says `unreadable ... run sudo ls /sys/fs/pstore`, the directory is
root-only and the journal of the next boot could not be read. Ask the user to
run that one command; do not run sudo yourself.

## 7. Gaps in the logging chain and how to close them

`./provision.sh doctor` reports it; the `system` step installs it. Missing pieces:

- journald not durable → `./provision.sh system` writes
  `/etc/systemd/journald.conf.d/99-comfyui.conf` (persistent,
  `SyncIntervalSec=1`, no rate limiting);
- no fsync'd kernel copy → same step writes `/etc/rsyslog.d/49-comfyui-crash.conf`
  and `/var/log/comfyui-kernel.log`;
- lockups not panicking → same step sets `hardlockup_panic`, `panic=10`
  through `/etc/sysctl.d/99-comfyui-crash.conf`;
- no state before the crash → the launcher and the generator append snapshots
  to `logs/session.log` and `logs/generate.log`; `crashlab` parses them.

## 8. The verdict vocabulary

| CAUSE | Means | First move |
|---|---|---|
| `kernel-panic` | a panic record or `Kernel panic` line | read the pstore/panic block |
| `hardware` | MCE/EDAC/AER/thermal-critical | stress test outside ComfyUI, check sensors |
| `kernel-lockup` | a watchdog fired / the box stopped scheduling | keep `hardlockup_panic` + pstore, capture the stack |
| `gpu-hang` | amdgpu timeout, reset or page fault | retry with `--disable-pinned-memory --lowvram` |
| `oom` | host RAM exhausted | verify swap, lower pinned memory |
| `gpu-memory` | framebuffer pin failed (ENOMEM) | raise `COMFYUI_RESERVE_VRAM`, watch session.log |
| `process-abort` | a process crashed, the machine survived | `crashlab coredump <pid> --bt` |
| `platform-freeze` | no trace, no panic, no watchdog, one-step stop | netconsole, hardware watchdog, fewer GPU contexts |
| `silent-reset` | died with no fresh signature and no panic/watchdog answer | apply the logging chain, rerun |
| `unlogged` | the journal of that boot is gone | apply the logging chain, rerun |

## 9. Report format

Deliver, in this order: cause (one line), the "decided by" answers, the evidence
lines with timestamps, what was running, and the next experiment. Store the
full text under `logs/forensics/crash-<timestamp>.txt` (`report --out`) instead
of pasting raw logs into the conversation.

If the evidence does not support a cause, say **"inconclusive"** and name the
missing source (journal, pstore, coredump) — never invent a hypothesis.
