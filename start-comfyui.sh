#!/usr/bin/env bash
# start-comfyui.sh - launch ComfyUI with the settings prepared by provision.sh.
#
# Examples:
#   ./start-comfyui.sh                 # localhost:8188, opens the browser
#   ./start-comfyui.sh --no-browser    # keep the terminal free of browser windows
#   ./start-comfyui.sh --lowvram       # if 12 GB VRAM is not enough
#   ./start-comfyui.sh --port 8189     # explicit port
#   ./start-comfyui.sh --dry-run       # print the command and environment
#   ./start-comfyui.sh -- --use-sage-attention   # extra ComfyUI flags
set -Eeuo pipefail

SCRIPT_PATH=$(readlink -f -- "${BASH_SOURCE[0]}")
SCRIPT_DIR=$(cd -- "$(dirname -- "$SCRIPT_PATH")" && pwd)

# shellcheck source=provision/lib/common.sh
source "$SCRIPT_DIR/provision/lib/common.sh"
# shellcheck source=provision/config/provision.env
source "$SCRIPT_DIR/provision/config/provision.env"

PORT=${COMFYUI_PORT}
PORT_EXPLICIT=0
OPEN_BROWSER=1
DO_MOUNT=1
ALLOW_SG=1
LOWVRAM=${COMFYUI_LOWVRAM}
CPU_ONLY=0
DRY_RUN=0
RESERVE_VRAM=${COMFYUI_RESERVE_VRAM}
LOG_LEVEL=${COMFYUI_LOG_LEVEL}
EXTRA_ARGS=()
ORIGINAL_ARGS=("$@")

usage() {
    cat <<EOF
Usage: $(basename "$0") [options] [-- comfyui args...]

Options:
  --port N          Listen port (default: $PORT)
  --lowvram         Enable ComfyUI --lowvram (low VRAM mode)
  --cpu             Force CPU mode (useful for debugging only)
  --no-browser      Do not open the web UI in a browser
  --no-mount        Do not try to mount the BIG disk automatically
  --no-sg           Do not re-exec through 'sg render'
  --reserve-vram N  VRAM in GB reserved for the OS (default: $RESERVE_VRAM)
  --log-level LVL   ComfyUI log level for the log file (default: $LOG_LEVEL)
  --dry-run         Show the final command instead of running it
  -h, --help        Show this help

Environment overrides: COMFYUI_PORT, COMFYUI_LOWVRAM, COMFYUI_RESERVE_VRAM,
                      COMFYUI_DISABLE_METADATA (1 keeps prompts out of the files).
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --port)          [[ $# -ge 2 ]] || die "--port requires a value"; PORT=$2; PORT_EXPLICIT=1; shift 2 ;;
        --lowvram)       LOWVRAM=1; shift ;;
        --cpu)           CPU_ONLY=1; shift ;;
        --no-browser)    OPEN_BROWSER=0; shift ;;
        --no-mount)      DO_MOUNT=0; shift ;;
        --no-sg)         ALLOW_SG=0; shift ;;
        --reserve-vram)  [[ $# -ge 2 ]] || die "--reserve-vram requires a value"; RESERVE_VRAM=$2; shift 2 ;;
        --log-level)     [[ $# -ge 2 ]] || die "--log-level requires a value"; LOG_LEVEL=$2; shift 2 ;;
        --dry-run)       DRY_RUN=1; shift ;;
        -h|--help)       usage; exit 0 ;;
        --)              shift; EXTRA_ARGS=("$@"); break ;;
        *)               die "Unknown option: $1 (use -- to pass extra ComfyUI flags)" ;;
    esac
done

export DRY_RUN

# ---------------------------------------------------------------------------
# Installation checks
# ---------------------------------------------------------------------------
[[ -x "$VENV_DIR/bin/python" ]] || {
    if [[ "$DRY_RUN" == "1" ]]; then
        warn "Virtual environment not found at $VENV_DIR (run ./provision.sh all first)."
    else
        die "Virtual environment not found at $VENV_DIR. Run ./provision.sh all first."
    fi
}
[[ -f "$COMFYUI_DIR/main.py" ]] || {
    if [[ "$DRY_RUN" == "1" ]]; then
        warn "ComfyUI not found at $COMFYUI_DIR (run ./provision.sh comfyui first)."
    else
        die "ComfyUI not found at $COMFYUI_DIR. Run ./provision.sh comfyui first."
    fi
}

# ---------------------------------------------------------------------------
# Disk
# ---------------------------------------------------------------------------
if [[ "$DO_MOUNT" == "1" ]]; then
    ensure_big_mount
elif ! mountpoint -q "$BIG_MOUNT" 2>/dev/null; then
    die "BIG is not mounted at $BIG_MOUNT (started with --no-mount)."
fi
if [[ "$DRY_RUN" != "1" ]]; then
    [[ -d "$MODELS_DIR" ]] || die "Models directory is missing: $MODELS_DIR. Run ./provision.sh all first."
    [[ -w "$MODELS_DIR" ]] || die "Models directory is not writable: $MODELS_DIR"
    mkdir -p "$LOG_DIR" "$OUTPUT_DIR" "$INPUT_DIR" "$TEMP_DIR" "$USER_DIR" 2>/dev/null || true
fi

# ---------------------------------------------------------------------------
# GPU access: the decision comes from the Python facts, the re-exec from bash
# ---------------------------------------------------------------------------
if [[ "$CPU_ONLY" == "0" ]]; then
    [[ -e /dev/kfd ]] || die "/dev/kfd not found. The amdgpu kernel driver is not loaded."
    [[ "$(id -u)" -ne 0 ]] || die "Do not run ComfyUI as root. Run it as $USER."

    eval "$(py start-check)"
    if [[ "${KFD_ACCESS:-0}" != "1" ]]; then
        if [[ "$DRY_RUN" == "1" || "$ALLOW_SG" == "0" || "${COMFYUI_NEWSESSION:-0}" == "1" ]]; then
            warn "No access to /dev/kfd in this session."
        elif [[ "${IN_DB:-0}" == "1" && -x /usr/bin/sg ]]; then
            # /usr/bin/sg is the newgrp symlink. /usr/local/bin/sg may be
            # ast-grep, so never rely on PATH here.
            log "Re-executing through '/usr/bin/sg render' to pick up the render group..."
            shell_quote() {
                printf "'%s'" "${1//\'/\'\\\'\'}"
            }
            quoted_cmd=$(shell_quote "$SCRIPT_PATH")
            for arg in "${ORIGINAL_ARGS[@]}"; do
                quoted_cmd+=" $(shell_quote "$arg")"
            done
            export COMFYUI_NEWSESSION=1
            exec /usr/bin/sg render -c "exec $quoted_cmd"
        else
            die "You are not in the 'render' group and cannot access /dev/kfd.
Run: sudo usermod -aG render,video $USER
Reboot (or log out and back in), then start ComfyUI again."
        fi
    fi

    if [[ "$DRY_RUN" != "1" ]] && ! [[ -r /dev/kfd && -w /dev/kfd ]]; then
        die "Still no read/write access to /dev/kfd.
Reboot (or log out and back in) so the new group membership applies."
    fi
fi

# ---------------------------------------------------------------------------
# Port handling
# ---------------------------------------------------------------------------
port_in_use() {
    (exec 3<>"/dev/tcp/127.0.0.1/$1") >/dev/null 2>&1
}

comfyui_ready() {
    curl -fsS --max-time 2 "http://127.0.0.1:$1/system_stats" >/dev/null 2>&1
}

open_url() {
    local url=$1
    [[ "$OPEN_BROWSER" == "1" ]] || return 0
    if [[ -z "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]]; then
        warn "No graphical session detected; open $url manually."
        return 0
    fi
    if command -v xdg-open >/dev/null 2>&1; then
        xdg-open "$url" >/dev/null 2>&1 &
    else
        warn "xdg-open not found; open $url manually."
    fi
}

if port_in_use "$PORT"; then
    if comfyui_ready "$PORT"; then
        ok "ComfyUI is already running at http://127.0.0.1:$PORT"
        open_url "http://127.0.0.1:$PORT"
        exit 0
    fi
    if [[ "$PORT_EXPLICIT" == "1" ]]; then
        die "Port $PORT is already in use by another program."
    fi
    found=0
    for candidate in $(seq $((PORT + 1)) $((PORT + 10))); do
        if ! port_in_use "$candidate"; then
            warn "Port $PORT is busy; using $candidate instead."
            PORT=$candidate
            found=1
            break
        fi
    done
    [[ "$found" == "1" ]] || die "No free port found in range $PORT-$((PORT + 10))."
fi

# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------
if [[ -n "${HSA_OVERRIDE_GFX_VERSION:-}" ]]; then
    warn "Ignoring HSA_OVERRIDE_GFX_VERSION=${HSA_OVERRIDE_GFX_VERSION} (device wheels are used)."
    unset HSA_OVERRIDE_GFX_VERSION
fi

export PYTORCH_HIP_ALLOC_CONF=${PYTORCH_HIP_ALLOC_CONF:-expandable_segments:True}
export MIOPEN_FIND_MODE=${MIOPEN_FIND_MODE:-FAST}
export HF_HOME=${HF_HOME:-$MODELS_DIR/.cache/huggingface}
export HUGGINGFACE_HUB_CACHE=${HUGGINGFACE_HUB_CACHE:-$HF_HOME/hub}
export TORCH_HOME=${TORCH_HOME:-$MODELS_DIR/.cache/torch}
export HF_HUB_DISABLE_TELEMETRY=1
export DO_NOT_TRACK=1
export PYTHONUNBUFFERED=1
# comfy_kitchen_config/sitecustomize.py patches torch._int_mm for RDNA2 (no
# matrix cores: hipblasLt answers HIPBLAS_STATUS_INVALID_VALUE) and filters
# comfy-kitchen's int8 linear handler. Python imports sitecustomize only when
# its directory is on sys.path, so the launcher exports it: without this the
# server looks healthy and every int8 model fails at the first matmul.
export PYTHONPATH="$SCRIPT_DIR/comfy_kitchen_config:$SCRIPT_DIR/provision/lib${PYTHONPATH:+:$PYTHONPATH}"
# A Python-level segfault/abort then prints the C traceback to stderr (and thus
# into the log files) instead of dying silently.
export PYTHONFAULTHANDLER=1

if [[ "$DRY_RUN" != "1" ]]; then
    mkdir -p "$HF_HOME" "$TORCH_HOME"
fi

# ---------------------------------------------------------------------------
# Log rotation
# ---------------------------------------------------------------------------
LOG_FILE="$LOG_DIR/comfyui.log"
DEBUG_LOG="$LOG_DIR/comfyui-debug.log"
SERVER_LOG="$LOG_DIR/comfyui-server.log"
SESSION_LOG="$LOG_DIR/session.log"
if [[ "$DRY_RUN" != "1" ]]; then
    mkdir -p "$LOG_DIR"
    for log_file in "$LOG_FILE" "$DEBUG_LOG"; do
        if [[ -f "$log_file" ]]; then
            log_size=$(file_size "$log_file")
            if (( log_size > 20 * 1024 * 1024 )); then
                mv -f "$log_file" "$log_file.1" 2>/dev/null || true
            fi
        fi
    done
fi

# ---------------------------------------------------------------------------
# Model summary
# ---------------------------------------------------------------------------
if [[ "$DRY_RUN" != "1" ]]; then
    log "Models: $(py models-summary)"
    if [[ ! -d "$MODELS_DIR/diffusion_models" ]] || [[ -z "$(find "$MODELS_DIR/diffusion_models" -maxdepth 1 -name '*.safetensors' -print -quit 2>/dev/null)" ]]; then
        warn "No diffusion models found. Download them later with: ./provision.sh models --set jibmix (or --set sft)"
    fi
fi

# ---------------------------------------------------------------------------
# Command
# ---------------------------------------------------------------------------
CMD=(
    "$VENV_DIR/bin/python" "main.py"
    --listen "$COMFYUI_LISTEN"
    --port "$PORT"
    --models-directory "$MODELS_DIR"
    --output-directory "$OUTPUT_DIR"
    --input-directory "$INPUT_DIR"
    --temp-directory "$TEMP_DIR"
    --user-directory "$USER_DIR"
    --reserve-vram "$RESERVE_VRAM"
    --disable-auto-launch
    --verbose "$LOG_LEVEL" "$LOG_FILE"
)

# A second, more verbose log file: DEBUG goes only there, so the main log stays
# readable while a crash still has the detailed decisions around it.
if [[ "${COMFYUI_DEBUG_LOG:-1}" == "1" ]]; then
    CMD+=(--verbose DEBUG "$DEBUG_LOG")
fi

if [[ "$LOWVRAM" == "1" ]]; then
    CMD+=(--lowvram)
fi
# ComfyUI is allowed to pin ~27 GiB of this 32 GiB box as staging buffers for
# the async weight offload (comfy/model_management.py: ram - 4 GiB). Turning it
# off costs throughput and removes the largest single host-memory consumer.
if [[ "${COMFYUI_DISABLE_PINNED_MEMORY:-0}" == "1" ]]; then
    CMD+=(--disable-pinned-memory)
fi
# Prompts are private. Without this ComfyUI writes the API prompt and the whole
# workflow into the PNG tEXt chunks of every saved image.
if [[ "${COMFYUI_DISABLE_METADATA:-1}" == "1" ]]; then
    CMD+=(--disable-metadata)
fi
if [[ "$CPU_ONLY" == "1" ]]; then
    CMD+=(--cpu)
fi
if (( ${#EXTRA_ARGS[@]} > 0 )); then
    CMD+=("${EXTRA_ARGS[@]}")
fi

PORT_URL="http://127.0.0.1:$PORT"

printf '\n'
log "ComfyUI"
printf '  URL:        %s\n' "$PORT_URL"
printf '  Python:     %s\n' "$VENV_DIR/bin/python"
printf '  ComfyUI:    %s (%s)\n' "$COMFYUI_DIR" "$(git -C "$COMFYUI_DIR" describe --tags --always 2>/dev/null || echo unknown)"
printf '  Models:     %s\n' "$MODELS_DIR"
printf '  Output:     %s\n' "$OUTPUT_DIR"
printf '  Log file:   %s\n' "$LOG_FILE"
if [[ "$LOWVRAM" == "1" ]]; then printf '  Mode:       lowvram\n'; fi
if [[ "$CPU_ONLY" == "1" ]]; then printf '  Mode:       CPU only\n'; fi
if [[ "${COMFYUI_DISABLE_PINNED_MEMORY:-0}" == "1" ]]; then printf '  Pinning:    disabled\n'; fi
if [[ "${COMFYUI_DISABLE_METADATA:-1}" == "1" ]]; then printf '  Metadata:   disabled (no prompt in generated files)\n'; fi
printf '\n'

if [[ "$DRY_RUN" == "1" ]]; then
    printf '[dry-run] environment:\n'
    printf '  PYTORCH_HIP_ALLOC_CONF=%q\n' "$PYTORCH_HIP_ALLOC_CONF"
    printf '  MIOPEN_FIND_MODE=%q\n' "$MIOPEN_FIND_MODE"
    printf '  HF_HOME=%q\n' "$HF_HOME"
    printf '  TORCH_HOME=%q\n' "$TORCH_HOME"
    printf '  PYTHONPATH=%q\n' "$PYTHONPATH"
    printf '[dry-run] cd %q && ' "$COMFYUI_DIR"
    printf '%q ' "${CMD[@]}"
    printf '\n'
    exit 0
fi

# Open the browser once the server is actually answering.
if [[ "$OPEN_BROWSER" == "1" ]]; then
    (
        for _ in $(seq 1 300); do
            if curl -fsS --max-time 1 "$PORT_URL/system_stats" >/dev/null 2>&1; then
                open_url "$PORT_URL"
                exit 0
            fi
            sleep 1
        done
    ) >/dev/null 2>&1 &
    disown 2>/dev/null || true
else
    log "Browser auto-open disabled. Open $PORT_URL manually."
fi

# ---------------------------------------------------------------------------
# Durable logging: every front writes to a file that is flushed as it goes
# ---------------------------------------------------------------------------
# Host state at launch (memory, VRAM/GTT) so a crash is not a black box.
{
    printf '\n--- session %s (pid %s) ---\n' "$(date --iso-8601=seconds)" "$$"
    py snapshot 2>/dev/null || true
    printf '  reserve-vram=%s lowvram=%s log-level=%s\n' "$RESERVE_VRAM" "$LOWVRAM" "$LOG_LEVEL"
} >>"$SESSION_LOG" 2>/dev/null || true

# The console output used to live only in the terminal (or in /tmp, which is
# tmpfs and loses everything on a hard reset). Mirror it into the log dir.
if [[ ! -t 1 ]]; then
    exec > >(tee -a "$SERVER_LOG") 2>&1
fi

# ---------------------------------------------------------------------------
# Black box: one line per second until this process exits
# ---------------------------------------------------------------------------
# The machine has stopped executing with no kernel message at all, so the only
# evidence that can survive is state that was already on the platter. The
# recorder watches this PID (the launcher becomes the server via exec) and
# writes host memory, PSI, allocator stalls, GPU VRAM/GTT/clocks/power and the
# server's own PSS/swap. It also trips the guard below when memory runs out.
VITALS_LOG="$LOG_DIR/vitals.log"
if [[ "${COMFYUI_VITALS:-1}" == "1" ]]; then
    VITALS_ARGS=(--out "$VITALS_LOG" --pid "$$")
    if [[ -n "${VITALS_INTERVAL:-}" ]]; then VITALS_ARGS+=(--interval "$VITALS_INTERVAL"); fi
    if [[ -n "${VITALS_MAX_MB:-}" ]]; then VITALS_ARGS+=(--max-mb "$VITALS_MAX_MB"); fi
    if [[ -n "${VITALS_GUARD_AVAIL_MB:-}" ]]; then VITALS_ARGS+=(--guard-avail-mb "$VITALS_GUARD_AVAIL_MB"); fi
    log "Vitals: $VITALS_LOG (${VITALS_INTERVAL:-1}s, guard ${VITALS_GUARD_AVAIL_MB:-0} MiB)"
    py vitals "${VITALS_ARGS[@]}" >/dev/null 2>&1 &
    disown 2>/dev/null || true
fi

# Force the log files to disk every few seconds: the page cache is exactly what
# the last crash lost. The loop exits as soon as the server process is gone.
SYNC_INTERVAL=${LOG_SYNC_INTERVAL:-5}
if (( SYNC_INTERVAL > 0 )) && command -v sync >/dev/null 2>&1; then
    (
        # Do not inherit unrelated descriptors (for example the generator's
        # instance lock) into a long-lived sync loop.
        exec 9>&- 2>/dev/null || true
        while sleep "$SYNC_INTERVAL"; do
            kill -0 "$$" 2>/dev/null || exit 0
            sync -f "$LOG_FILE" "$DEBUG_LOG" "$SERVER_LOG" "$SESSION_LOG" 2>/dev/null || true
        done
    ) &
    disown 2>/dev/null || true
fi

cd "$COMFYUI_DIR"
if command -v stdbuf >/dev/null 2>&1 && [[ ! -t 1 ]]; then
    exec stdbuf -oL -eL "${CMD[@]}"
fi
exec "${CMD[@]}"
