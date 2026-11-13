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
./provision.sh test                # bash -n, shellcheck, python tests
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

## Git and the pre-commit gate

- The repository is initialized (`main`); generated trees (`ComfyUI/`, `venv/`,
  `logs/`, `output/`, `input/`, `temp/`, `user/`, `provision/.state/`) are
  ignored.
- Hooks are versioned in `.githooks/` and enabled with
  `git config core.hooksPath .githooks`.
- `.githooks/pre-commit` calls `./provision.sh test`, which runs `bash -n`,
  `shellcheck`, `ruff format --check`, `ruff check`, `pyright` and `pytest`.
  Keep that single entry point; do not duplicate the check list in the hook.

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
