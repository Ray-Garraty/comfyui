# crashlab — crash forensics for this machine

Read-only, dependency-free (system `python3`, stdlib only). It does **not**
import `provision/`, does not need the venv, does not use sudo, and does not
start ComfyUI. That is deliberate: it must work when the things it investigates
are broken.

## Usage

```bash
./tools/crashlab/crashlab report            # verdict + evidence for the previous boot
./tools/crashlab/crashlab report --boot -3  # an older boot
./tools/crashlab/crashlab report --json     # machine-readable (for scripts/agents)
./tools/crashlab/crashlab report --out logs/forensics/crash.txt
./tools/crashlab/crashlab boots             # boot inventory: clean / UNCLEAN / running
./tools/crashlab/crashlab timeline          # last lines of every log file
./tools/crashlab/crashlab coredump          # list dumps with a decoded signal
./tools/crashlab/crashlab coredump 9614     # signal, command, stack, blame
./tools/crashlab/crashlab coredump 9614 --bt  # run gdb on the stored core (slow)
./tools/crashlab/crashlab snapshot          # RAM/swap/VRAM/GTT right now
./tools/crashlab/crashlab self-test         # parser self-tests
```

## What the report answers

1. `CAUSE` — one of `hardware`, `kernel-lockup`, `gpu-hang`, `oom`,
   `gpu-memory`, `process-abort`, `silent-reset`, `unlogged`.
2. `VERDICT` — one sentence.
3. evidence — the exact lines that produced the verdict.
4. boot history — signature counts of the last three boots, so a pattern is
   visible without reading anything.
5. what was running — launcher sessions (`logs/session.log`), newest log files,
   and whether a log tail ends in NUL bytes (proof of a hard reset).

## Interpretation notes

- `silent-reset` + "kernel log is truncated" means journald lost the tail:
  the durable-logging configuration (`./provision.sh system`, run by the user)
  is not applied yet. Nothing else can be concluded from that boot.
- `gpu-memory` = `amdgpu ... pin failed` / `Failed to pin framebuffer with
  error -12`: the display plane cannot get VRAM/GTT. It is an allocation
  problem, not a power problem.
- `process-abort` with `blame: PyTorch ... (c10)` and an origin frame like
  `c10::cuda::SetDevice` = the HIP call failed and `std::terminate` aborted the
  process. The machine survived that process; treat it as a separate bug.
- Xorg/VS Code dumps are shutdown noise and are excluded from attribution.

## Layout

```
crashlab                     launcher (bash, sets PYTHONPATH)
lib/crashlab/host.py         read-only IO (injectable runner)
lib/crashlab/parsers.py      pure text parsers (journal, coredump, logs)
lib/crashlab/verdict.py      evidence -> cause -> report
lib/crashlab/__main__.py     CLI
lib/crashlab/selftest.py     canned-fixture tests
```
