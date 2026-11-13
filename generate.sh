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

die()  { echo "ERROR: $*" | tee -a "$LOG_FILE" >&2; exit 1; }
ok()   { echo "OK: $*" | tee -a "$LOG_FILE"; }
warn() { echo "WARN: $*" | tee -a "$LOG_FILE" >&2; }
log()  { echo "$(date '+%Y-%m-%d %H:%M:%S') $*" | tee -a "$LOG_FILE"; }

LOG_FILE="$LOG_DIR/generate.log"
mkdir -p "$LOG_DIR"
if [[ -f "$LOG_FILE" ]]; then
    log_size=$(stat -c %s "$LOG_FILE" 2>/dev/null || echo 0)
    if (( log_size > 20 * 1024 * 1024 )); then
        mv -f "$LOG_FILE" "$LOG_FILE.1" 2>/dev/null || true
    fi
fi

exec > >(tee -a "$LOG_FILE") 2>&1

log "=== generate.sh started ==="
log "Install dir: $INSTALL_DIR"
log "API: $API"
log "Models dir: $DIFF_DIR"
log "Output dir: $OUTPUT_DIR"
log "Log file: $LOG_FILE"

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
    "$INSTALL_DIR/start-comfyui.sh" --no-browser >/tmp/comfyui-generate.log 2>&1 &
    for _ in $(seq 1 180); do
        if comfyui_ready; then
            ok "ComfyUI is ready at $API"
            break
        fi
        sleep 1
    done
    comfyui_ready || die "ComfyUI did not become ready. Check /tmp/comfyui-generate.log"
else
    ok "ComfyUI already running at $API"
fi

log "ComfyUI is ready at $API"

# ---------------------------------------------------------------------------
# Execute the Python business logic
# ---------------------------------------------------------------------------
mapfile -t MODELS < <(find "$DIFF_DIR" -maxdepth 1 -type f -name '*.safetensors' -printf '%f\n' 2>/dev/null | sort)
[[ ${#MODELS[@]} -gt 0 ]] || die "No .safetensors models found in $DIFF_DIR"

export PYTHONPATH="$INSTALL_DIR/provision/lib:$INSTALL_DIR/comfy_kitchen_config${PYTHONPATH:+:$PYTHONPATH}"
mkdir -p "$OUTPUT_DIR" "$LOG_DIR"

exec python3 -m comfyprov.generate "$API" "$OUTPUT_DIR" "$DIFF_DIR" "$MODELS_DIR/text_encoders" "$MODELS_DIR/vae" "${MODELS[@]}"
