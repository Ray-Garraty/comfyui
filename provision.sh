#!/usr/bin/env bash
# provision.sh - idempotent provisioning for ComfyUI + ROCm on this machine.
#
#   ./provision.sh all          # everything that can be done before a reboot, then verify
#   ./provision.sh status       # read-only: what is done, what is pending, is a reboot needed
#   ./provision.sh preflight    # read-only environment checks
#   ./provision.sh system       # apt packages, kernel tuning, directories
#   ./provision.sh gpu          # render/video groups and udev rules
#   ./provision.sh comfyui      # ComfyUI checkout, venv, ROCm PyTorch
#   ./provision.sh manager      # ComfyUI-Manager
#   ./provision.sh launcher     # start-comfyui.sh permissions and menu entry
#   ./provision.sh verify       # GPU + ComfyUI + Manager smoke test
#   ./provision.sh auth         # store a Civitai API token (chmod 600)
#   ./provision.sh models ...   # download/check models (not part of 'all')
#   ./provision.sh doctor       # print diagnostics
#   ./provision.sh update       # move ComfyUI/Manager to the configured refs
#   ./provision.sh swap         # create a 16 GB swap file (opt-in)
#   ./provision.sh test         # bash -n, shellcheck, ruff, pyright, the
#                               # Python unit + crashlab self-tests and the
#                               # mandatory e2e test
#
# Exit codes: 0 ok, 1 error, 2 usage, 3 reboot/re-login required,
#             4 missing prerequisite, 5 verification failed.
set -Eeuo pipefail

HERE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=provision/lib/common.sh
source "$HERE/provision/lib/common.sh"
# shellcheck source=provision/config/provision.env
source "$HERE/provision/config/provision.env"

COMMAND=""
MODELS_ARGS=()
VERIFY_FULL=0

usage() {
    sed -n '2,23p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    cat <<EOF

Global flags:
  --dry-run     Print the plan and change nothing
  --yes, -y     Do not ask for confirmation
  --force       Replace conflicting refs/files where supported
  --full        With 'verify': run a real MOSS generation (models required)
  --no-color    Disable colored output
  -h, --help    Show this help

Models options (only with the 'models' command):
  --set jibmix|sft|both   Choose the MOSS model set to download
  --check                 Only verify files that are already present
  --verify                Recompute SHA256 even when the size matches

Typical first run:
  ./provision.sh all          # installs everything, then stops with "reboot"
  sudo reboot
  ./provision.sh all          # finishes with the GPU/server verification
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run)  DRY_RUN=1; shift ;;
        --yes|-y)   ASSUME_YES=1; shift ;;
        --force)    FORCE=1; shift ;;
        --full)     VERIFY_FULL=1; shift ;;
        --no-color) NO_COLOR=1; shift ;;
        -h|--help)  usage; exit 0 ;;
        --set)
            [[ $# -ge 2 ]] || die "--set requires a value (jibmix, sft, both)"
            MODELS_ARGS+=("$1" "$2")
            shift 2
            ;;
        --check|--verify)
            MODELS_ARGS+=("$1")
            shift
            ;;
        all|preflight|status|system|gpu|comfyui|manager|launcher|verify|auth|models|doctor|update|swap|test)
            [[ -z "$COMMAND" ]] || die "Only one command can be given (got '$COMMAND' and '$1')."
            COMMAND=$1
            shift
            ;;
        *)
            die "Unknown argument: $1 (try --help)"
            ;;
    esac
done

export DRY_RUN ASSUME_YES FORCE
if [[ -n "${NO_COLOR:-}" ]]; then
    export NO_COLOR
fi
if [[ -z "$COMMAND" ]]; then
    usage
    exit "$EXIT_USAGE"
fi

# ---------------------------------------------------------------------------
# Locking: only mutating commands take the lock
# ---------------------------------------------------------------------------
case "$COMMAND" in
    preflight|status|doctor|verify|auth|test) ;;
    *)
        if command -v flock >/dev/null 2>&1; then
            mkdir -p "$INSTALL_DIR/provision/.state"
            exec 9>"$INSTALL_DIR/provision/.state/provision.lock"
            if ! flock -n 9; then
                die "Another provision run is in progress ($INSTALL_DIR/provision/.state/provision.lock)."
            fi
        fi
        ;;
esac

# ---------------------------------------------------------------------------
# Step runner: a plan is applied here, all decisions come from Python
# ---------------------------------------------------------------------------
run_step() {
    local step=$1
    shift
    printf '\n'
    log "Step: $step"

    local rc=0
    apply_plan "$step" "$@" && rc=0 || rc=$?
    if [[ $rc -ne 0 ]]; then
        warn "Step '$step' cannot run yet (see the explanation above)."
        return "$rc"
    fi
    if [[ "$PLAN_STATUS" == "needs-reboot" ]]; then
        return "$EXIT_REBOOT"
    fi
    return 0
}

# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------
cmd_preflight() {
    log "Preflight checks"
    local rc=0
    py preflight && rc=0 || rc=$?
    if [[ $rc -ne 0 ]]; then
        die "Preflight found fatal problems; nothing was changed."
    fi
    ok "Preflight passed"
}

cmd_status() {
    py status
}

cmd_verify() {
    local rc=0
    run_step verify && rc=0 || rc=$?
    if [[ $rc -eq "$EXIT_REBOOT" ]]; then
        if [[ "$DRY_RUN" == "1" ]]; then
            warn "dry-run: a reboot would be required before verification"
            return 0
        fi
        reboot_banner
        return "$EXIT_REBOOT"
    fi
    if [[ $rc -ne 0 ]]; then
        return "$rc"
    fi

    local extra=()
    if [[ "$VERIFY_FULL" == "1" ]]; then
        extra+=(--full)
    fi

    if [[ "$DRY_RUN" == "1" ]]; then
        printf '[dry-run] %q -m comfyprov.gpucheck\n' "$VENV_DIR/bin/python"
        printf '[dry-run] start ComfyUI on a temporary port, check /system_stats and /manager/version\n'
        if [[ "$VERIFY_FULL" == "1" ]]; then
            printf '[dry-run] generate one MOSS-Image sample\n'
        fi
        return 0
    fi

    log "Checking PyTorch and the GPU"
    PYTHONPATH="$PROVISION_LIB${PYTHONPATH:+:$PYTHONPATH}" PYTHONDONTWRITEBYTECODE=1 \
        "$VENV_DIR/bin/python" -m comfyprov.gpucheck \
        || die "GPU check failed (render group, /dev/kfd permissions or the ROCm wheel)."
    ok "GPU check passed"

    export PYTORCH_HIP_ALLOC_CONF=${PYTORCH_HIP_ALLOC_CONF:-expandable_segments:True}
    export MIOPEN_FIND_MODE=${MIOPEN_FIND_MODE:-FAST}
    export HF_HOME=${HF_HOME:-$MODELS_DIR/.cache/huggingface}
    export HUGGINGFACE_HUB_CACHE=${HUGGINGFACE_HUB_CACHE:-$HF_HOME/hub}
    export TORCH_HOME=${TORCH_HOME:-$MODELS_DIR/.cache/torch}
    export HF_HUB_DISABLE_TELEMETRY=1
    export PYTHONUNBUFFERED=1
    unset HSA_OVERRIDE_GFX_VERSION

    mkdir -p "$LOG_DIR" "$OUTPUT_DIR" "$INPUT_DIR" "$TEMP_DIR" "$USER_DIR"
    local log_file="$LOG_DIR/verify.log"
    local console_log="$LOG_DIR/verify-console.log"
    : >"$log_file"
    : >"$console_log"

    local srv_pid=""
    local srv_started=0
    local url=""

    cleanup() {
        if [[ "${srv_started:-0}" == "1" && -n "${srv_pid:-}" ]] && kill -0 "${srv_pid}" 2>/dev/null; then
            kill -INT "${srv_pid}" 2>/dev/null || true
            for _ in $(seq 1 40); do
                kill -0 "${srv_pid}" 2>/dev/null || break
                sleep 0.5
            done
            kill -KILL "${srv_pid}" 2>/dev/null || true
        fi
    }
    trap cleanup EXIT

    local running="http://127.0.0.1:${COMFYUI_PORT}"
    if curl -fsS --max-time 2 "$running/system_stats" >/dev/null 2>&1; then
        url=$running
        log "A ComfyUI instance is already running at $url; verifying it in place"
    else
        local port=""
        local candidate
        for candidate in $(seq 18188 18200); do
            if ! (exec 3<>"/dev/tcp/127.0.0.1/$candidate") >/dev/null 2>&1; then
                port=$candidate
                break
            fi
        done
        [[ -n "$port" ]] || die "No free port for the temporary ComfyUI instance."
        url="http://127.0.0.1:$port"
        log "Starting a temporary ComfyUI instance on $url"
        (
            cd "$COMFYUI_DIR"
            exec "$VENV_DIR/bin/python" main.py \
                --listen 127.0.0.1 \
                --port "$port" \
                --models-directory "$MODELS_DIR" \
                --output-directory "$OUTPUT_DIR" \
                --input-directory "$INPUT_DIR" \
                --temp-directory "$TEMP_DIR" \
                --user-directory "$USER_DIR" \
                --reserve-vram "${COMFYUI_RESERVE_VRAM}" \
                --disable-auto-launch \
                --verbose INFO "$log_file"
        ) >>"$console_log" 2>&1 &
        srv_pid=$!
        srv_started=1

        log "Waiting for ComfyUI to answer (up to 240 s)"
        local ready=0
        for _ in $(seq 1 240); do
            if ! kill -0 "$srv_pid" 2>/dev/null; then
                break
            fi
            if curl -fsS --max-time 2 "$url/system_stats" >/dev/null 2>&1; then
                ready=1
                break
            fi
            sleep 1
        done
        if [[ "$ready" != "1" ]]; then
            warn "ComfyUI did not start. Last log lines:"
            tail -n 60 "$console_log" >&2 || true
            tail -n 60 "$log_file" >&2 || true
            die "ComfyUI startup check failed."
        fi
    fi

    if grep -q 'IMPORT FAILED' "$console_log" 2>/dev/null; then
        warn "Custom node import failures were reported:"
        grep -B2 -A6 'IMPORT FAILED' "$console_log" || true
    fi

    local verify_rc=0
    py verify --url "$url" ${extra[@]+"${extra[@]}"} && verify_rc=0 || verify_rc=$?
    if [[ "$verify_rc" != "0" ]]; then
        warn "Verification failed. Last log lines:"
        tail -n 40 "$console_log" >&2 || true
        return "$verify_rc"
    fi
    ok "Verification passed"
}

cmd_auth() {
    if [[ "$DRY_RUN" == "1" ]]; then
        printf '[dry-run] prompt for a Civitai token and store it in %s (mode 600)\n' "$CIVITAI_TOKEN_FILE"
        return 0
    fi
    py auth
}

cmd_models() {
    local rc=0
    run_step models ${MODELS_ARGS[@]+"${MODELS_ARGS[@]}"} && rc=0 || rc=$?
    if [[ $rc -ne 0 ]]; then
        return "$rc"
    fi
    if [[ "$PLAN_EXIT" != "0" ]]; then
        return "$PLAN_EXIT"
    fi
    return 0
}

cmd_doctor() {
    py doctor
}

cmd_swap() {
    run_step swap
}

cmd_test() {
    local failed=0
    # Options must not be injectable: nothing may deselect, skip or silence a
    # test through the environment.
    unset PYTEST_ADDOPTS PYTEST_PLUGINS
    local script
    for script in "$HERE/provision.sh" "$HERE/start-comfyui.sh" "$HERE"/provision/lib/*.sh; do
        if bash -n "$script"; then
            ok "syntax: ${script#"$HERE"/}"
        else
            failed=1
        fi
    done
    if command -v shellcheck >/dev/null 2>&1; then
        if shellcheck -x -S warning "$HERE/provision.sh" "$HERE/start-comfyui.sh" "$HERE"/provision/lib/*.sh; then
            ok "shellcheck"
        else
            failed=1
        fi
    else
        warn "shellcheck is not installed; skipping the lint"
    fi
    local py_targets=("$HERE/provision/lib/comfyprov" "$HERE/provision/tests")
    if command -v ruff >/dev/null 2>&1; then
        if ruff format --check "${py_targets[@]}" && ruff check "${py_targets[@]}"; then
            ok "ruff format + lint"
        else
            failed=1
        fi
    else
        warn "ruff is not installed; skipping the Python lint"
    fi
    if command -v pyright >/dev/null 2>&1; then
        if (cd "$HERE" && pyright); then
            ok "pyright"
        else
            failed=1
        fi
    else
        warn "pyright is not installed; skipping the type check"
    fi
    # The e2e test starts a real ComfyUI; it must not run twice per commit
    # (reports/2026-11-13-crash-report.md §4.5: each extra start/stop cycle is
    # a GPU context churn on an unstable amdgpu stack). The unit pass deselects
    # it here, the mandatory e2e pass below runs it exactly once.
    if PYTHONPATH="$PROVISION_LIB" PYTHONDONTWRITEBYTECODE=1 "$PYTHON_BIN" \
        -m pytest -q -m "not e2e" -p no:cacheprovider "$HERE/provision/tests" 2>/dev/null; then
        ok "python tests"
    elif PYTHONPATH="$PROVISION_LIB" PYTHONDONTWRITEBYTECODE=1 "$PYTHON_BIN" -m unittest discover \
        -s "$HERE/provision/tests" -t "$HERE/provision/tests"; then
        ok "python tests (unittest fallback)"
    else
        failed=1
    fi

    # crashlab is stdlib-only and read-only; its self-test takes milliseconds
    # and protects the forensics path that has to work when provision/ is broken.
    if "$HERE/tools/crashlab/crashlab" self-test; then
        ok "crashlab self-test"
    else
        failed=1
    fi

    # Business requirement: the e2e test is mandatory for every commit.
    # No option, no condition, no excuse skips it: a failure, a skip, an empty
    # collection or an injected pytest option all block the commit. It runs
    # even when the GPU is dead or the model is missing — that is exactly what
    # a red e2e run has to report. `git commit --no-verify` and
    # `-c core.hooksPath=...` are forbidden for the same reason.
    # Violation is punished with a ban: permanent loss of write access.
    unset PYTEST_ADDOPTS PYTEST_PLUGINS
    local e2e_output="" e2e_rc=0
    e2e_output=$(PYTHONPATH="$PROVISION_LIB" PYTHONDONTWRITEBYTECODE=1 "$PYTHON_BIN" \
        -m pytest -q -m e2e --strict-markers --no-header -rA -p no:cacheprovider \
        "$HERE/provision/tests" 2>&1) || e2e_rc=$?
    if [[ $e2e_rc -ne 0 ]]; then
        printf '%s\n' "$e2e_output"
        failed=1
    elif ! grep -qE '^PASSED ' <<<"$e2e_output" || grep -qE '^(SKIPPED|XFAIL|XPASS) ' <<<"$e2e_output"; then
        # pytest exits 0 for a skipped test; the requirement does not.
        printf '%s\n' "$e2e_output"
        warn "e2e tests did not run to completion; a skip is a failure"
        failed=1
    else
        ok "e2e tests"
    fi

    if bash -n "$HERE/provision/tests/verify_trap_test.sh" && \
       bash "$HERE/provision/tests/verify_trap_test.sh" >/dev/null 2>&1; then
        ok "trap cleanup regression test"
    else
        warn "trap cleanup regression test failed"
        failed=1
    fi

    return "$failed"
}

cmd_update() {
    local ref=$COMFYUI_REF
    if [[ "$FORCE" == "1" ]]; then
        ref=latest
        warn "--force: updating ComfyUI to the newest tag"
    fi
    run_step comfyui --ref "$ref" || return $?
    run_step manager || return $?
    cmd_verify
}

cmd_all() {
    cmd_preflight

    local rc=0
    run_step system && rc=0 || rc=$?
    [[ $rc -eq 0 ]] || return "$rc"

    local gpu_rc=0
    run_step gpu && gpu_rc=0 || gpu_rc=$?
    if [[ $gpu_rc -ne 0 && $gpu_rc -ne "$EXIT_REBOOT" ]]; then
        return "$gpu_rc"
    fi

    run_step comfyui && rc=0 || rc=$?
    [[ $rc -eq 0 ]] || return "$rc"
    run_step manager && rc=0 || rc=$?
    [[ $rc -eq 0 ]] || return "$rc"
    run_step launcher && rc=0 || rc=$?
    [[ $rc -eq 0 ]] || return "$rc"

    if [[ "$gpu_rc" -eq "$EXIT_REBOOT" ]]; then
        if [[ "$DRY_RUN" == "1" ]]; then
            warn "dry-run: a reboot would be required before the verify step"
            return 0
        fi
        reboot_banner
        printf 'Then finish with:\n  %s/provision.sh all\n\n' "$INSTALL_DIR"
        return "$EXIT_REBOOT"
    fi

    cmd_verify || return $?

    if [[ "$DRY_RUN" == "1" ]]; then
        printf '\n'
        ok "Dry-run complete; nothing was changed."
        return 0
    fi

    printf '\n'
    ok "Provisioning complete."
    printf 'Start ComfyUI with: %s/start-comfyui.sh\n' "$INSTALL_DIR"
    printf 'Models were not downloaded. When ready: ./provision.sh models --set jibmix\n'
}

case "$COMMAND" in
    all)       cmd_all ;;
    preflight) cmd_preflight ;;
    status)    cmd_status ;;
    system)    run_step system ;;
    gpu)       run_step gpu ;;
    comfyui)   run_step comfyui ;;
    manager)   run_step manager ;;
    launcher)  run_step launcher ;;
    verify)    cmd_verify ;;
    auth)      cmd_auth ;;
    models)    cmd_models ;;
    doctor)    cmd_doctor ;;
    update)    cmd_update ;;
    swap)      cmd_swap ;;
    test)      cmd_test ;;
    *)         die "Unknown command: $COMMAND" ;;
esac
