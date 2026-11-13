#!/usr/bin/env bash
# generate.sh — Thin shell wrapper around comfyprov.generate.
#
# All business logic (model discovery, metadata import, workflow construction,
# ComfyUI API queue/poll/download) lives in provision/lib/comfyprov/generate.py
# and is covered by provision/tests/test_generate.py.
#
# This script only: sources configuration, checks prerequisites, ensures
# ComfyUI is running, and executes the Python module.
#
# Usage: ./generate.sh
#
set -Eeuo pipefail

INSTALL_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=provision/config/provision.env
source "$INSTALL_DIR/provision/config/provision.env"

: "${COMFYUI_PORT:=8188}"
: "${MODELS_DIR:=$BIG_MOUNT/comfyui/models}"
: "${OUTPUT_DIR:=$INSTALL_DIR/output}"
: "${LOG_DIR:=$INSTALL_DIR/logs}"
API="http://127.0.0.1:${COMFYUI_PORT}"
DIFF_DIR="$MODELS_DIR/diffusion_models"

LOG_FILE="$LOG_DIR/generate.log"
mkdir -p "$LOG_DIR"
: > "$LOG_FILE"

# Log file opened directly on fd 3 — without pipe/tee, so output isn't
# lost on hard crash or reboot.
exec 3>>"$LOG_FILE"

# Prevent concurrent generate.sh instances.
LOCK_FILE="$LOG_DIR/generate.lock"
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    die "Another generate.sh instance is already running."
fi

# Each function writes to terminal (stdout/stderr) and directly to file (fd 3),
# with its own timestamp. No duplicates.
die()  { local ts; ts="$(date '+%Y-%m-%d %H:%M:%S')"; printf 'ERROR: %s %s\n' "$ts" "$*"; printf 'ERROR: %s %s\n' "$ts" "$*" >&3; exit 1; }
ok()   { local ts; ts="$(date '+%Y-%m-%d %H:%M:%S')"; printf 'OK: %s %s\n' "$ts" "$*"; printf 'OK: %s %s\n' "$ts" "$*" >&3; }
warn() { local ts; ts="$(date '+%Y-%m-%d %H:%M:%S')"; printf 'WARN: %s %s\n' "$ts" "$*" >&2; printf 'WARN: %s %s\n' "$ts" "$*" >&3; }
log()  { local ts; ts="$(date '+%Y-%m-%d %H:%M:%S')"; printf '%s %s\n' "$ts" "$*"; printf '%s %s\n' "$ts" "$*" >&3; }

log "=== generate.sh started ==="
log "Install dir: $INSTALL_DIR"
log "API: $API"
log "Models dir: $DIFF_DIR"
log "Output dir: $OUTPUT_DIR"
log "Log file: $LOG_FILE"

# Host state (memory, VRAM/GTT) before the server starts and after it stops:
# written by Python so this script stays plumbing only.
{
    py snapshot 2>/dev/null || true
} >>"$LOG_FILE" 2>/dev/null || true

# The log file is the only place where a crash before the first prompt leaves
# traces, so flush it to disk while we run instead of waiting for exit.
SYNC_INTERVAL=${LOG_SYNC_INTERVAL:-5}
SYNC_LOOP_PID=""
if (( SYNC_INTERVAL > 0 )) && command -v sync >/dev/null 2>&1; then
    (
        # The subshell must not keep the instance lock (fd 9) or the log fd (3)
        # alive after this script exits.
        exec 9>&- 3>&-
        while sleep "$SYNC_INTERVAL"; do
            kill -0 "$$" 2>/dev/null || exit 0
            sync -f "$LOG_FILE" 2>/dev/null || true
        done
    ) &
    SYNC_LOOP_PID=$!
fi
stop_sync_loop() {
    if [[ -n "$SYNC_LOOP_PID" ]]; then
        kill "$SYNC_LOOP_PID" 2>/dev/null || true
    fi
}
trap stop_sync_loop EXIT

# ---------------------------------------------------------------------------
# Prerequisites
# ---------------------------------------------------------------------------
command -v curl >/dev/null 2>&1 || die "curl is required."
command -v python3 >/dev/null 2>&1 || die "python3 is required."
mountpoint -q "$BIG_MOUNT" 2>/dev/null || die "BIG disk not mounted at $BIG_MOUNT."
[[ -d "$DIFF_DIR" ]] || die "Diffusion models directory not found: $DIFF_DIR"

# ---------------------------------------------------------------------------
# Ensure ComfyUI is running
# ---------------------------------------------------------------------------
comfyui_ready() {
    curl -fsS --max-time 2 "$API/system_stats" >/dev/null 2>&1
}

if ! comfyui_ready; then
    warn "ComfyUI is not running at $API. Starting it (--no-browser)..."
    # Not /tmp: tmpfs loses the whole startup log on a hard reset.
    # fd 9 is this script's instance lock; the child must not keep it alive.
    "$INSTALL_DIR/start-comfyui.sh" --no-browser >"$LOG_DIR/comfyui-start.log" 2>&1 9>&- &
    COMFYUI_PID=$!
    COMFYUI_WAS_STARTED=1
    for _ in $(seq 1 180); do
        if comfyui_ready; then
            ok "ComfyUI is ready at $API"
            break
        fi
        sleep 1
    done
    comfyui_ready || die "ComfyUI did not become ready. Check $LOG_DIR/comfyui-start.log"
else
    ok "ComfyUI already running at $API"
    COMFYUI_WAS_STARTED=0
fi

log "ComfyUI is ready at $API"

# ---------------------------------------------------------------------------
# Execute the Python business logic
# ---------------------------------------------------------------------------
mapfile -t MODELS < <(find "$DIFF_DIR" -maxdepth 1 -type f -name '*.safetensors' -printf '%f\n' 2>/dev/null | sort)
[[ ${#MODELS[@]} -gt 0 ]] || die "No .safetensors models found in $DIFF_DIR"

export PYTHONPATH="$INSTALL_DIR/provision/lib:$INSTALL_DIR/comfy_kitchen_config${PYTHONPATH:+:$PYTHONPATH}"
mkdir -p "$OUTPUT_DIR" "$LOG_DIR"

PY_ARGS=(
    "$API"
    "$OUTPUT_DIR"
    "$DIFF_DIR"
    "$MODELS_DIR/text_encoders"
    "$MODELS_DIR/vae"
    "${MODELS[@]}"
)

# Business requirement: no delay after the user presses Enter at interactive
# prompts (Select [1-1]:, Prompt: ...).  Python stdout stays connected to the
# terminal (no process-substitution pipe), so prompts appear instantly.
# python3 -u = unbuffered output.  The log file is written from inside Python
# (GENERATE_LOG_FILE), not from a shell pipe.
set +e
export GENERATE_LOG_FILE="$LOG_FILE"
python3 -u -m comfyprov.generate "${PY_ARGS[@]}" 2>&1
PY_EXIT=$?
set -e

{
    printf '%s generate.sh finished with exit %s\n' "$(date --iso-8601=seconds)" "$PY_EXIT"
    py snapshot 2>/dev/null || true
} >>"$LOG_FILE" 2>/dev/null || true

# ---------------------------------------------------------------------------
# Graceful exit of ComfyUI if we started it
# ---------------------------------------------------------------------------
if [[ "$COMFYUI_WAS_STARTED" == "1" && -n "${COMFYUI_PID:-}" ]]; then
    log "Stopping ComfyUI (PID $COMFYUI_PID) after generation completes..."
    kill "$COMFYUI_PID" 2>/dev/null || true
    wait "$COMFYUI_PID" 2>/dev/null || true
    log "ComfyUI stopped."
fi

exit "$PY_EXIT"
