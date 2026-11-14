# crashlab — crash forensics for this machine

Read-only, dependency-free (system `python3`, stdlib only). It does **not**
import `provision/`, does not need the venv, does not use sudo, and does not
start ComfyUI. That is deliberate: it must work when the things it investigates
are broken.

Goal: an exact root-cause answer in **under 60 seconds**, without reading a
single raw log by hand. `crashlab report` runs in about three seconds; the rest
of the minute is for reading its output.

## Usage

```bash
./tools/crashlab/crashlab report            # verdict + "decided by" + timeline + drift
./tools/crashlab/crashlab report --brief    # cause, three evidence lines, next step
./tools/crashlab/crashlab report --full     # every raw line behind the verdict
./tools/crashlab/crashlab report --boot -3  # an older boot
./tools/crashlab/crashlab report --json     # machine-readable (for scripts/agents)
./tools/crashlab/crashlab report --out logs/forensics/crash.txt
./tools/crashlab/crashlab boots             # boot inventory: clean / UNCLEAN / running, with clock-skew repair
./tools/crashlab/crashlab timeline          # last lines of every log file
./tools/crashlab/crashlab coredump          # list dumps with a decoded signal
./tools/crashlab/crashlab coredump 9614     # signal, command, stack, blame
./tools/crashlab/crashlab coredump 9614 --bt  # run gdb on the stored core (slow)
./tools/crashlab/crashlab snapshot          # RAM/swap/VRAM/GTT right now
./tools/crashlab/crashlab self-test         # parser self-tests
```

## What the report answers

The output is decision-first, in this order:

1. **boot** — true start (monotonic clock, not the RTC), runtime, end, clean or
   not; plus the next boot's power-on estimate and the **silence** between the
   last write and power-on.
2. `CAUSE` — one of `hardware`, `kernel-panic`, `kernel-lockup`, `gpu-hang`,
   `oom`, `gpu-memory`, `process-abort`, `platform-freeze`, `silent-reset`,
   `unlogged`.
3. `VERDICT` — one sentence, including whether the reset was self-triggered.
4. **decided by** — every question asked, with `confirmed`, `ruled out`,
   `context` or `unknown`: kernel panic, hardware watchdog, hardware (MCE/EDAC/
   thermal), host memory, kernel-stopped-first, process abort, GPU signatures.
   This is the checklist that turns a guess into a finding.
5. **evidence** — the exact signature windows (`x101, 08:20:02..08:22:35`), not
   a wall of raw lines.
6. **what ran** — timeline of launcher sessions, prompts, generations,
   pinned-memory ceilings and kernel signatures for the boot.
7. **log stop order** — which log file stopped last.
8. **drift** — what changed since the crash series began (wall-clock skew,
   swapfile activation and the pinned-memory jump, deny-listed watchdog,
   firmware+loader time).
9. **boot history** — signature counts of the last three boots.
10. **next steps** — the concrete experiment that can close the open question.

`--full` appends every raw kernel tail, log tail, pstore record and logging-chain
state. `--brief` is the three-line form for scripts.

## Interpretation notes

- `platform-freeze` — the machine stopped executing with **no kernel message**,
  **no panic record** (efi_pstore registered, `/sys/fs/pstore` empty,
  `systemd-pstore` skipped on the next boot) and **no hardware watchdog armed**
  (no `/sys/class/watchdog`, `iTCO_wdt` deny-listed). Nothing on the machine
  rebooted it, so the reset was external (reset button or power event). Only
  netconsole, ramoops or a serial console can name the trigger.
- `silent-reset` — the machine died without a fresh kernel signature **and**
  without the panic/watchdog answers. Close the logging chain first.
- `freeze` — the kernel log stops **before** journald: the box stopped
  scheduling, but userspace logged a little longer. Different from
  `platform-freeze`, which leaves no gap because everything stops together.
- `gpu-memory` = `amdgpu ... pin failed` / `Failed to pin framebuffer with
  error -12`: the display plane cannot get VRAM/GTT. It is an allocation
  problem, not a power problem. Freshness is per signature: a burst eight
  minutes before the kill is `context`, not the trigger.
- `gpu-hang` = `amdgpu_job_timedout`, `GPU reset`, `VM_L2 page fault`.
- `oom` = `oom-kill`/`Out of memory`: host RAM exhausted.
- `process-abort` with `blame: PyTorch ... (c10)` and an origin frame like
  `c10::cuda::SetDevice` = the HIP call failed and `std::terminate` aborted the
  process. The machine survived that process; treat it as a separate bug.
- `kernel-panic` — a pstore record or `Kernel panic` in the journal wins over
  every other signature, because it is the only self-reported cause.
- Xorg/VS Code dumps are shutdown noise and are excluded from attribution.
- A wrong RTC at boot is repaired with `realtime - monotonic` from the last
  journal record; the boot list timestamp is shown as `(clock list: ...)` so a
  three-hour skew cannot be mistaken for a three-hour session.

## Layout

```
crashlab                     launcher (bash, sets PYTHONPATH)
lib/crashlab/host.py         read-only IO (injectable runner)
lib/crashlab/parsers.py      pure text parsers (journal, signatures, coredump, logs, app events)
lib/crashlab/verdict.py      evidence -> cause -> checklist -> timeline -> drift -> report
lib/crashlab/__main__.py     CLI
lib/crashlab/selftest.py     canned-fixture tests
```
