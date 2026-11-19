# AGENTS.md

Guidance for AI agents working in this repository. Keep this file short and
update it when the project structure or invariants change. It is the only
project documentation: there is no separate user-facing README, so keep the
usage notes below complete enough for a human too.

## What this project is

This is **not** the ComfyUI source tree. It provisions and launches ComfyUI on
one specific Linux machine.

- Target host: Linux Mint 22.3 (Ubuntu 24.04 base), x86_64, Python 3.12.
- GPU: AMD Radeon RX 6700 XT, Navi 22, **gfx1031**, 12 GB VRAM, no matrix cores.
- No NVIDIA/CUDA. Uses AMD multi-arch PyTorch ROCm wheels; no system-wide ROCm.
- The app itself lives in `/home/vlabe/Documents/comfyui`.
- Localhost only (`127.0.0.1`), no systemd unit, no autostart, no swap by default.

Do not hardcode assumptions that conflict with these unless the user asks.

## Project language

The project language is **English**. All code, comments, commit messages,
issue text, documentation, and agent reasoning must be in English. Russian
(or any other language) is not permitted in code comments, commit messages,
or user-facing strings produced by the project.

## Architecture: plan in Python, apply in bash

All decisions live in the pure-stdlib package `provision/lib/comfyprov/`
(works on the system Python, before the venv exists, never mutates the host).
Bash only executes:

- `plan.py` computes an ordered `Plan` of `Action`s from a read-only `Facts`
  snapshot (`facts.py`, `probe.py`). Nothing is decided in shell.
- `actions.py` defines the 10 action kinds: `mkdir`, `write_file`,
  `append_line`, `symlink`, `exec`, `apt_install`, `usermod_groups`,
  `git_checkout`, `pip_install`, `download`.
- `lib/common.sh` (`load_plan`/`apply_plan`/`apply_action`) is the executor.
  Transport is a tab-separated line protocol with base64 values (`PLAN`, `NOTE`,
  `ACT`, `PARAM`, `END`); do not switch it to JSON/jq.
- If a step needs something new, add a planner branch in `plan.py`. Only extend
  the action vocabulary when an existing kind genuinely cannot express it.

Package map:

```
comfyprov/config.py    provision.env parser/validator (env wins, bash-compatible)
comfyprov/layout.py    directories, apt lists, udev/sysctl content, icon, stamps
comfyprov/probe.py     root-aware read-only host access (injectable for tests)
comfyprov/gpu.py       gfx parsing from KFD, group/permission logic
comfyprov/facts.py     single snapshot of the machine
comfyprov/plan.py      all step planners + status/reboot classification
comfyprov/checks.py    preflight + doctor
comfyprov/crashlog.py  crash evidence (pstore, previous boot) + host snapshots
comfyprov/server.py    HTTP helpers (stats, manager, history)
comfyprov/smoke.py     MOSS workflow + PNG validation
comfyprov/gpucheck.py  PyTorch/ROCm matmul check (runs in the venv)
comfyprov/verify.py    server-level verification findings
comfyprov/auth.py      Civitai token storage (mode 600)
comfyprov/report.py    Finding/level rendering shared by checks and verify
```

## Design rule: Python decides, bash executes

**Every business decision ("what should be done") lives in a reusable Python
module under `provision/lib/comfyprov/`. Bash scripts (`provision.sh`,
`start-comfyui.sh`, `provision/lib/common.sh`) only perform execution,
privileges, and IO.**

This means:

- No `if`/`case` logic about *whether* a step is needed lives in bash. All
  conditionality is computed by `plan.py` from `facts.py`.
- No duplicated settings or version checks across `provision.sh` and Python.
  `provision/config/provision.env` is the single source; `comfyprov/config.py`
  parses it with the same precedence (`env wins over default`).
- Every planner branch must have unit tests in `provision/tests/` that use
  `FakeProbe` / `FakeClient` and never touch the real host, network, or sudo.
- Shell may only invoke Python actions (`apply_action`), format output, and
  handle privileges/exit codes.

If a new capability is needed, add a planner branch in `plan.py`, extend the
action vocabulary in `actions.py` only when unavoidable, and add tests before
shipping.

## Layout

```
provision.sh                     # command dispatcher, lock, step runner
start-comfyui.sh                 # launcher (mount, group re-exec, env, exec)
pyproject.toml                   # ruff / pyright / pytest configuration
tools/crashlab/                  # standalone crash forensics (read-only, stdlib only)
.agents/skills/crash-forensics/  # SKILL.md: how to investigate a crash
provision/
├── lib/common.sh                # executor, logging, sudo, downloads
├── lib/comfyprov/               # business logic (see above)
├── config/provision.env         # single source of defaults
├── config/models.md             # generative AI model settings (separate from AGENTS.md)
├── tests/                       # unittest/pytest suite with fake host and HTTP client
└── .state/                      # runtime: lock, constraints, pip/torch stamps
```

Generated at provisioning time (do not edit by hand):

- `ComfyUI/` — git checkout pinned to `COMFYUI_REF`.
- `venv/` — Python 3.12 virtualenv with ROCm PyTorch.
- `logs/`, `output/`, `input/`, `temp/`, `user/` — runtime directories.
- `ComfyUI/models` — symlink to `MODELS_DIR`.
- `~/.config/comfyui/` — tokens and configs, mode 700/600.
- `~/.local/share/applications/comfyui.desktop`, optional `~/Desktop` copy.
- `/etc/systemd/journald.conf.d/99-comfyui.conf`,
  `/etc/rsyslog.d/49-comfyui-crash.conf`,
  `/etc/sysctl.d/99-comfyui-crash.conf` — durable logging and crash capture,
  written by the `system` step (see "Logging and crash capture" below).

## Commands

```bash
./provision.sh all                 # everything possible before a reboot, then verify
./provision.sh status              # read-only: per-step status, pending work, reboot flag
./provision.sh preflight
./provision.sh system|gpu|comfyui|manager|launcher|swap
./provision.sh verify [--full]
./provision.sh auth
./provision.sh doctor
./provision.sh update [--force]
./provision.sh test                # bash -n, shellcheck, ruff, pyright, tests + e2e
```

Global flags: `--dry-run`, `--yes`, `--force`, `--full`, `--no-color`.

`all` never runs sudo as root:
steps prompt for sudo only when they need it.

Exit codes (documented in `comfyprov/actions.py`): `0` ok, `1` error, `2` usage,
`3` reboot/re-login required, `4` missing prerequisite, `5` verification failed.
Bash `on_error` does not print a crash banner for 3/4/5: they are expected
outcomes.

`start-comfyui.sh` flags: `--port`, `--lowvram`, `--cpu`, `--no-browser`,
`--no-mount`, `--no-sg`, `--reserve-vram`, `--log-level`, `--dry-run`,
`-- <extra ComfyUI args>`.

`generate.sh` is interactive. After a successful round it asks `Generate more
images with the same settings? (y/n) [n]`; answering `y` keeps the running
ComfyUI and asks only for the image count and the positive/negative prompts,
defaulting to the previous round's values (Enter repeats them). A round started
from seed `-1` draws a new seed, a fixed seed is kept. The server started by
`generate.sh` is stopped only after the generator exits (after `n`/Ctrl-D), so
further rounds reuse the loaded model and VAE.

## Reboot flow (important)

`usermod -aG render,video` changes the database, not the running session. The
gpu planner therefore reports `needs-reboot` (exit 3) while `/dev/kfd` is not
readable. `all` continues through the rest of the steps, prints a reboot
banner, and stops with exit 3; the second `./provision.sh all` after the reboot
runs verification. The launcher can also re-exec through `/usr/bin/sg render`:

- `sg` in `PATH` may be ast-grep (`/usr/local/bin/sg`). Always use
  `/usr/bin/sg`, which is the `newgrp` symlink.

`verify` also returns 3 (with a banner) instead of failing when the GPU is not
reachable from the current session.

## Shell contract

- Every script keeps `set -Eeuo pipefail`; mutating commands are idempotent.
- Dry-run: `apply_plan` prints planned actions and runs nothing. It must not
  create directories, write files, mount disks or call sudo. Plans must not
  depend on artefacts produced earlier in the same run.
- `external` state (pip, torch, requirements, manager) is guarded by stamps in
  `provision/.state/`; torch versions are compared via `pip show` output.
- Fail with `die`; use `ok`/`warn` for status. Never run the whole script as
  root; call `require_sudo` for root work.
- Bash pitfall: `[[ cond ]] && cmd` as the **last** command of a function
  returns 1 and aborts under `set -e`. Use an explicit `if`.

## Configuration

All tunables live in `provision/config/provision.env` with `: "${VAR:=default}"`
so the environment can override them. `comfyprov/config.py` parses the same
file with the same precedence; never duplicate defaults in Python or bash.

Important values:

- `COMFYUI_REF=v0.45.3`, `MANAGER_REF=4.3` (ComfyUI ≥ 0.42 has native MOSS-Image).
- `COMFYUI_RESERVE_VRAM=1.5` — 12 GB VRAM is shared with the desktop; without
  the reserve the kernel logs `Failed to pin framebuffer with error -12`.
- `ENABLE_SWAP=1`, `SWAP_SIZE=16G`, `SWAP_FILE=/swapfile` — swap is part of
  every `all` run; ComfyUI may pin up to 16 GiB of host memory.
- `COMFYUI_LOG_LEVEL=INFO`, `COMFYUI_DEBUG_LOG=1` (second DEBUG log file),
  `LOG_SYNC_INTERVAL=5` (seconds between `sync -f` calls on the log files),
  `ENABLE_CRASH_CAPTURE=1` (journald/rsyslog/watchdog/pstore step),
  `COMFYUI_DISABLE_METADATA=1` (no prompt/workflow in saved files).
- `TORCH_VERSION=2.13.0`, `TORCHVISION_VERSION=0.28.0`,
  `TORCHAUDIO_VERSION=2.11.0.2`, `ROCM_VERSION=10.1.0`, index
  `https://stable.repo.amd.com/rocm/whl-next/`.
- Device extras are `torch[device-gfx1031]` etc.; `gfx1031` is auto-detected
  from `/sys/class/kfd/kfd/topology`. Do **not** set
  `HSA_OVERRIDE_GFX_VERSION`; the launcher unsets it intentionally.

## Verification behavior

`cmd_verify` in `provision.sh`:

- Runs `comfyprov.gpucheck` in the venv first (fails if `torch.cuda` or FP16
  matmul fails).
- Reuses a running instance on `COMFYUI_PORT` if present; otherwise starts a
  temporary server on ports `18188–18200` and shuts it down via a trap.
- `comfyprov verify --url ...` checks `/system_stats` and `/manager/version`
  (Manager returns plain text).

`doctor` and `preflight` are read-only and safe at any time.

## Logging and crash capture

This machine hard-reset four times in one day with nothing useful on disk. The
rule now: every log front writes to a file and reaches the platter quickly.

Runtime (user level, no sudo):

- `logs/comfyui.log` — ComfyUI `INFO`, `logs/comfyui-debug.log` — `DEBUG`
  (`--verbose` is passed twice), `logs/comfyui-server.log` — console mirror of
  the launcher, `logs/session.log` — memory/VRAM snapshots, `logs/generate.log`
  — generator, `logs/comfyui-start.log` — output of a server started by
  `generate.sh` (never `/tmp`: tmpfs loses it on a reset).
- `py snapshot` (`comfyprov.crashlog.snapshot`) prints RSS/swap/load plus
  `mem_info_vram_*` and `mem_info_gtt_*`; the launcher and the generator append
  it to their logs at start and at exit.
- User prompts are private. They never reach a log or a generated file:
  prompts and the once-per-second `Generating…` repaint are terminal-only
  (`comfyprov.generate.write_private`); echoes of raw user input (a prompt
  typed into "Number of images") are shown on the terminal and logged without
  the value (`write_split`); every prompt is registered with
  `LogTee.add_sensitive`, so an error body that quotes the workflow is
  `[redacted]` before it is written. `logs/generate.log` records phases,
  timings, produced paths and host state only.
- Images carry no metadata: `start-comfyui.sh` and the temporary server in
  `verify` pass `--disable-metadata` (ComfyUI otherwise writes the API prompt
  and the whole workflow into PNG tEXt chunks), and the generator strips
  `tEXt`/`zTXt`/`iTXt`/`eXIf` chunks before saving. `smoke.validate_outputs`
  rejects any PNG that still has them, and `test_generate_e2e.py` asserts the
  prompt is absent from the files and from every log line of the run.
- `start-comfyui.sh` and `generate.sh` run a `sync -f <log files>` loop every
  `LOG_SYNC_INTERVAL` seconds; the loop exits when its parent process is gone.
- `PYTHONFAULTHANDLER=1` is exported so a native crash prints a traceback.

System level (part of the `system` step, needs sudo):

- `/etc/systemd/journald.conf.d/99-comfyui.conf` — persistent journal,
  `SyncIntervalSec=1`, no rate limiting (the default 5 min flush is what lost
  the last lines before every reset).
- `/etc/rsyslog.d/49-comfyui-crash.conf` — `$ActionFileEnableSync on` plus a
  dedicated `/var/log/comfyui-kernel.log`.
- `/etc/sysctl.d/99-comfyui-crash.conf` — `kernel.hardlockup_panic=1`,
  `softlockup_panic=1`, `hung_task_panic=1`, `panic_on_oops=1`, `panic=10`,
  watchdogs on, `printk_devkmsg=on`. A frozen desktop then panics and reboots
  instead of hanging silently; the tail lands in the journal.
- `systemd-pstore` collects firmware/ramoops records left by a reset.

Reading the evidence: `./tools/crashlab/crashlab report` (see
`.agents/skills/crash-forensics/SKILL.md`) is the single 60-second entry point.
It prints the verdict, a "decided by" checklist (panic, watchdog, hardware, host
memory, kernel-stopped-first, process abort, GPU signatures), the timeline of the
boot, configuration drift, the signature counts of the last three boots and the
next experiment; `--brief` and `--full` shrink or expand it, `--json` is for
scripts. `crashlab boots` repairs RTC-skewed boot timestamps with the monotonic
clock. `crashlab` is deliberately independent of `provision/` and the venv: it
must work when the things it investigates are broken. It never writes to the
machine beyond an optional `--out FILE`.

## Git and the pre-commit gate

- The repository is initialized (`main`); generated trees (`ComfyUI/`, `venv/`,
  `logs/`, `output/`, `input/`, `temp/`, `user/`, `provision/.state/`) are
  ignored.
- Hooks are versioned in `.githooks/` and enabled with
  `git config core.hooksPath .githooks`.
- `.githooks/pre-commit` calls `./provision.sh test`, which runs `bash -n`,
  `shellcheck`, `ruff format --check`, `ruff check`, `pyright`, `pytest`,
  `crashlab self-test` and the mandatory e2e test. Keep that single entry point;
  do not duplicate the check list in the hook.
- The unit pytest pass runs with `-m "not e2e"`; the e2e marker is run exactly
  once, afterwards. Never merge them back into one full-suite run: every extra
  ComfyUI start/stop cycle is GPU context churn on this amdgpu stack
  (`reports/2026-11-13-crash-report.md` §4.5 — 4 of 7 resets landed inside the
  double cycle). Pinned by `provision/tests/test_commit_gate.py`.
- **Business requirement: the e2e test cannot be skipped for a commit.** No
  option, no condition, no environment variable, no "later", no excuse.
  A missing model, a missing server, a broken or burnt GPU, a tight deadline —
  all of them make the commit fail, because that is the state of the machine
  and hiding it is the bug. `provision.sh test` counts a skipped, deselected,
  empty or injected-option e2e run as a failure, and
  `provision/tests/test_generate_e2e.py` fails instead of skipping on a
  missing precondition.
- **Bypassing the gate is forbidden:** `git commit --no-verify`,
  `git -c core.hooksPath=/dev/null commit`, temporarily disabling the hook,
  commenting out the failing step or committing on another branch "for now".
  If the gate is red, fix the tree or do not commit.
  `provision/tests/test_commit_gate.py` pins the hook contents so the rule
  cannot be edited away silently.
- **Penalty: a ban.** Anyone who skips the e2e test, disables the gate or
  pushes a commit that did not pass it loses write access to the repository.
  There is no warning level, no first time, no appeal: the rule is absolute.

**Commit only when the user explicitly asks for a commit.** Never create
commits on your own initiative, and never split work into a stream of small
commits to "save progress". A request to change files is not a request to
commit them: leave the changes staged/unstaged and wait for the user. When a
commit is requested, make one coherent commit unless the user asks otherwise.

### Git history safety

- **Forbidden:** resetting commits in a way that deletes change history
  (e.g. `git reset --hard` discarding commits, force-push that removes history).
- **Allowed:** resetting commits **without losing change history**
  (e.g. `git reset --soft` / `--mixed` that keep commits reachable).

## Safe workflow for agents

1. Read this file and `provision/config/provision.env`; avoid reading generated
   `ComfyUI/` or `venv/` trees unless the task needs them.
2. After Python changes: `ruff format provision/lib/comfyprov provision/tests`,
   `ruff check ...`, `pyright`, `pytest -q`.
3. After shell changes: `bash -n <file>` and
   `shellcheck -x -S warning provision.sh start-comfyui.sh provision/lib/common.sh`.
4. Prefer `./provision.sh --dry-run all` as the integration check (no sudo, no
   writes). `./provision.sh status`, `preflight`, `doctor`, `verify-file`,
   `facts` are safe real runs.
5. Do **not** run `./provision.sh all`, `system`, `gpu`, `swap`, `update` unless
   the user explicitly asks: they use sudo, modify groups/udev/sysctl, or transfer
   many gigabytes.
## Privilege boundaries

`./provision.sh` is for environment setup and dependency installation only.
`generate.sh` is for image generation only — never add provisioning logic there.

- **Never ask the user for a sudo password.** If a step needs root privileges,
  add it to the provision script (`provision.sh`, `provision/lib/common.sh`,
  `comfyprov/plan.py`) and tell the user to run `./provision.sh <step>`
  themselves; the script will prompt for sudo when needed.
- **Non-root operations** may be run by the agent directly, **except** downloads
  of single files larger than **500 MB** — those require explicit user request.
- Never run the whole provision script as root; use `require_sudo` for root
  work inside scripts.
