---
name: crash-forensics
description: Determine why this Linux/ComfyUI machine crashed (hard reset, freeze, OOM, GPU hang, process abort) from its logs. Use when the PC rebooted by itself, froze, lost the ComfyUI server, or when someone asks "what happened in the logs". Prefers parser commands over reading raw logs by hand.
---

# Crash forensics

Goal: name the cause in minutes, with evidence, and never guess. Delegation
rule: **run a parser, read its verdict, and only open raw log lines that the
parser points at.** Do not dump whole logs into the context.

All commands below are read-only and safe to run while ComfyUI is up.

## 0. Safety rules for this box

- **Never run `./provision.sh test`, `pytest -m e2e` or `./start-comfyui.sh`
  while diagnosing.** The end-to-end test starts a real ComfyUI server and
  hard-resets this machine (observed twice). Diagnosis must not create the
  event it is investigating.
- Do not run anything that needs sudo. `crashlog` (the provisioning step) is
  run by the user, not by you.
- Never call it "hardware" from a single event. Only `Hardware Error`, `MCE`,
  `EDAC` or thermal-critical lines justify that.

## 1. First pass: the parser

```bash
./tools/crashlab/crashlab report          # CAUSE + VERDICT + evidence + boot history
./tools/crashlab/crashlab report --json   # same, machine-readable
./tools/crashlab/crashlab boots           # clean / UNCLEAN / running per boot
./tools/crashlab/crashlab timeline        # last lines of every log file
./tools/crashlab/crashlab coredump        # dumps with decoded signals
```

`report` prints the cause, the verdict, the exact evidence lines, the signature
counts of the last three boots, the launcher sessions and the newest log tails.
Start there; only continue when the verdict is `inconclusive` or contradicts
itself.

## 2. Was it a crash or a clean reboot?

```bash
./tools/crashlab/crashlab boots
```

- `UNCLEAN` means the boot ended without
  `Reached target (poweroff|reboot|shutdown).target`; `running` is this boot.
- If the journal of the crashed boot contains only boot-up lines, journald was
  not persistent yet — the missing information is itself the finding. Fix with
  the `system` step (`./provision.sh system`, run by the user).

Second, independent marker — the page cache never reached the disk.
`crashlab timeline` prints it per file as `[N zero bytes at the end -> hard reset]`;
`report` lists the same fact in the evidence section.

## 3. What ran before it died?

The launcher and the generator write their own state to disk, so this needs no
shell archaeology:

| File | Answers |
|---|---|
| `logs/session.log` | RAM/swap/load + VRAM/GTT at every server start |
| `logs/comfyui-start.log` | console of a server started by `generate.sh` |
| `logs/comfyui-server.log` | console of a server started by hand |
| `logs/comfyui.log` / `comfyui-debug.log` | `INFO` / `DEBUG` of ComfyUI itself |
| `logs/generate.log` | generator phases and end state |

`crashlab timeline` prints, newest first, the name, last write time and last
three lines of every `logs/*.log`, plus the parsed launcher sessions
(`reserve-vram`, memory/VRAM at start). Compare the last write of each file with
the moment the machine died: the file that stops first belongs to the process
that died first.

## 4. Kernel signatures

`crashlab report` already counts these and shows the lines; the raw command is
only needed for a signature that the table does not know yet:

```bash
journalctl -b -1 -k --no-pager -o short-iso | grep -aiE \
 'pin failed|Failed to pin framebuffer|amdgpu_job_timedout|GPU reset|ring .* timeout|VM_L2|page fault|soft lockup|hard LOCKUP|hung_task|Out of memory|oom-kill|Hardware Error|Machine check|MCE:|EDAC|critical temperature|AER'
```

Interpretation (each maps to a different fix):

- `amdgpu ... pin failed` + `Failed to pin framebuffer with error -12` —
  GPU memory accounting: the display plane cannot get VRAM/GTT. Software:
  allocations/limits (`COMFYUI_RESERVE_VRAM`, pinned memory, no swap). Not a PSU.
- `amdgpu_job_timedout`, `GPU reset`, `ring gfx .* timeout`, `VM_L2` — driver
  hang. Suspect the ROCm stack/pinned memory, then the driver version.
- `soft lockup`, `hard LOCKUP`, `hung_task` — the box stopped scheduling;
  needs `hardlockup_panic` + pstore to capture the stack next time.
- `oom-kill`, `Out of memory` — host RAM exhausted (check swap: it is mandatory
  on a 32 GB box that pins up to 16 GiB).
- `Hardware Error`, `MCE`, `EDAC ... error`, thermal critical — the only
  evidence that justifies blaming the hardware.
- Nothing at all in the kernel log while userspace kept logging — the kernel
  froze without a trace: the logging chain was not fast enough (see step 6).

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

## 6. pstore and firmware records

```bash
./tools/crashlab/crashlab report   # prints pstore records when present
```

Empty pstore is normal when the firmware backend is off — do not read it as
"no crash".

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

## 8. Report format

Deliver, in this order: cause (one line), evidence (the exact signature lines
with timestamps), what was running (from step 3), and the next experiment.
Store the full text under `logs/forensics/crash-<timestamp>.txt` instead of
pasting raw logs into the conversation.

If the evidence does not support a cause, say **"inconclusive"** and name the
missing source (journal, pstore, coredump) — never invent a hypothesis.
