#!/usr/bin/env bash
# Shared helpers for the ComfyUI provisioning scripts.
#
# This file is sourced, not executed. It contains only plumbing: logging,
# privilege handling, the plan/action executor and small filesystem helpers.
# Every decision (what to do, whether it is needed, whether a reboot follows)
# lives in the Python package provision/lib/comfyprov.
set -Eeuo pipefail

: "${DRY_RUN:=0}"
: "${ASSUME_YES:=0}"
: "${FORCE:=0}"

# Keep the documented exit codes in one place (see comfyprov/actions.py).
: "${EXIT_OK:=0}"
: "${EXIT_ERROR:=1}"
: "${EXIT_USAGE:=2}"
: "${EXIT_REBOOT:=3}"
: "${EXIT_PREREQ:=4}"
: "${EXIT_VERIFY:=5}"
# Seam for tests: empty SUDO_CMD runs the command as the current user.
: "${SUDO_CMD=sudo}"

if [[ -t 1 && -z "${NO_COLOR:-}" ]]; then
    C_RESET=$'\033[0m'
    C_GREEN=$'\033[32m'
    C_YELLOW=$'\033[33m'
    C_RED=$'\033[31m'
    C_BLUE=$'\033[34m'
else
    C_RESET=''
    C_GREEN=''
    C_YELLOW=''
    C_RED=''
    C_BLUE=''
fi

log()  { printf '%s==>%s %s\n' "$C_BLUE" "$C_RESET" "$*"; }
ok()   { printf '%s  OK%s %s\n' "$C_GREEN" "$C_RESET" "$*"; }
warn() { printf '%s WARN%s %s\n' "$C_YELLOW" "$C_RESET" "$*" >&2; }
die()  { printf '%sERROR%s %s\n' "$C_RED" "$C_RESET" "$*" >&2; exit "$EXIT_ERROR"; }

on_error() {
    local exit_code=$?
    # Codes 3/4/5 are documented, expected outcomes (reboot, missing
    # prerequisite, failed verification), not unexpected crashes.
    if [[ $exit_code -eq 3 || $exit_code -eq 4 || $exit_code -eq 5 ]]; then
        exit "$exit_code"
    fi
    printf '%sERROR%s command failed at %s:%s (exit %s)\n' \
        "$C_RED" "$C_RESET" "${BASH_SOURCE[1]:-?}" "${BASH_LINENO[0]:-?}" "$exit_code" >&2
    exit "$exit_code"
}
trap on_error ERR

# ---------------------------------------------------------------------------
# Python bridge
# ---------------------------------------------------------------------------
PROVISION_LIB=${PROVISION_LIB:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)}

python_bin() {
    local candidate
    for candidate in python3.12 python3; do
        if command -v "$candidate" >/dev/null 2>&1; then
            printf '%s' "$candidate"
            return 0
        fi
    done
    return 1
}

PYTHON_BIN=${PYTHON_BIN:-$(python_bin || true)}

py() {
    [[ -n "${PYTHON_BIN:-}" ]] || die "python3 is not installed; run './provision.sh system' first."
    PYTHONPATH="$PROVISION_LIB${PYTHONPATH:+:$PYTHONPATH}" PYTHONDONTWRITEBYTECODE=1 \
        "$PYTHON_BIN" -m comfyprov "$@"
}

# ---------------------------------------------------------------------------
# Privileges and prompts
# ---------------------------------------------------------------------------
run() {
    if [[ "$DRY_RUN" == "1" ]]; then
        printf '[dry-run]'
        printf ' %q' "$@"
        printf '\n'
        return 0
    fi
    "$@"
}

run_sudo() {
    if [[ "$DRY_RUN" == "1" ]]; then
        printf '[dry-run] sudo'
        printf ' %q' "$@"
        printf '\n'
        return 0
    fi
    sudo "$@"
}

require_not_root() {
    if [[ "$(id -u)" -eq 0 ]]; then
        die "Run this script as your desktop user, not as root. Root actions are invoked through sudo."
    fi
}

require_sudo() {
    [[ "$DRY_RUN" == "1" ]] && return 0
    command -v sudo >/dev/null 2>&1 || die "sudo is not available."
    sudo -v || die "sudo authentication failed."
}

need_cmd() {
    command -v "$1" >/dev/null 2>&1 || die "Required command not found: $1"
}

confirm() {
    local prompt=$1
    if [[ "$ASSUME_YES" == "1" ]]; then
        return 0
    fi
    if [[ ! -t 0 ]]; then
        die "$prompt (no interactive terminal; re-run with --yes to accept)"
    fi
    local answer
    read -r -p "$prompt [y/N] " answer
    case "$answer" in
        y|Y|yes|YES) return 0 ;;
        *) die "Cancelled by user." ;;
    esac
}

reboot_banner() {
    printf '\n'
    warn "A reboot is required before ComfyUI can use the GPU."
    printf '  Group changes only apply to new login sessions.\n'
    printf '  Reboot (recommended), then run:\n\n'
    printf '    %s/provision.sh all\n\n' "${INSTALL_DIR:-.}"
}

# ---------------------------------------------------------------------------
# Plan transport (see comfyprov/actions.py for the producer side)
# ---------------------------------------------------------------------------
PLAN_STATUS="ok"
PLAN_EXIT=0
PLAN_NOTES=()
PLAN_KIND=()
PLAN_SUDO=()
PLAN_EFFECT=()
PLAN_ID=()
PLAN_SUMMARY=()
PLAN_SAT=()
declare -A PLAN_PARAM=()

_b64d() {
    [[ -n "$1" ]] || { printf ''; return 0; }
    printf '%s' "$1" | base64 -d
}

# Decode into _DECODED without losing trailing newlines. "$(base64 -d)" would
# strip them, and every file written from a plan would lose its final "\n" -
# content comparisons then never match and the doctor keeps reporting "differs".
_DECODED=""
_decode_param() {
    local out
    out=$(_b64d "$1"; printf '\001')
    _DECODED=${out%$'\001'}
}

load_plan() {
    local step=$1
    shift
    PLAN_STATUS="ok"
    PLAN_EXIT=0
    PLAN_NOTES=()
    PLAN_KIND=()
    PLAN_SUDO=()
    PLAN_EFFECT=()
    PLAN_ID=()
    PLAN_SUMMARY=()
    PLAN_SAT=()
    PLAN_PARAM=()

    local stream
    if ! stream=$(py plan "$step" --stream "$@"); then
        die "Cannot compute the plan for '$step' (see the message above)."
    fi

    local tag f1 f2 f3 f4 f5 f6 f7
    while IFS=$'\t' read -r tag f1 f2 f3 f4 f5 f6 f7; do
        case "$tag" in
            PLAN)  PLAN_STATUS=$f1; PLAN_EXIT=$f2 ;;
            NOTE)  _decode_param "$f1"; PLAN_NOTES+=("$_DECODED") ;;
            ACT)
                PLAN_KIND+=("$f2")
                PLAN_SUDO+=("$f3")
                PLAN_EFFECT+=("$f4")
                PLAN_ID+=("$f5")
                _decode_param "$f6"
                PLAN_SUMMARY+=("$_DECODED")
                PLAN_SAT+=("$f7")
                ;;
            PARAM) _decode_param "$f3"; PLAN_PARAM["$f1:$f2"]="$_DECODED" ;;
            END)   break ;;
        esac
    done <<<"$stream"
}

_param() {
    printf '%s' "${PLAN_PARAM[${1}:${2}]:-}"
}

# _param_list IDX NAME ARRAYNAME
_param_list() {
    local index=$1 name=$2
    local -n _target=$3
    local value=${PLAN_PARAM[${index}:${name}]:-}
    _target=()
    [[ -n "$value" ]] || return 0
    mapfile -t _target < <(printf '%s' "$value" | tr '\037' '\n')
}

# ---------------------------------------------------------------------------
# Action executor: one branch per action kind, no decisions
# ---------------------------------------------------------------------------
apply_action() {
    local index=$1
    local kind=${PLAN_KIND[$index]}
    local sudo=${PLAN_SUDO[$index]}

    case "$kind" in
        mkdir)          _apply_mkdir "$index" ;;
        write_file)     _apply_write_file "$index" "$sudo" ;;
        append_line)    _apply_append_line "$index" "$sudo" ;;
        symlink)        _apply_symlink "$index" ;;
        exec)           _apply_exec "$index" "$sudo" ;;
        apt_install)    _apply_apt "$index" ;;
        usermod_groups) _apply_usermod "$index" ;;
        git_checkout)   _apply_git_checkout "$index" ;;
        pip_install)    _apply_pip_install "$index" ;;
        download)       _apply_download "$index" ;;
        *)              die "Unknown action kind: $kind" ;;
    esac
}

_apply_mkdir() {
    local index=$1 path mode
    path=$(_param "$index" path)
    mode=$(_param "$index" mode)
    run mkdir -p "$path"
    if [[ -n "$mode" && "$DRY_RUN" != "1" ]]; then
        chmod "$mode" "$path"
    fi
}

_apply_write_file() {
    local index=$1 sudo=$2 path content mode
    path=$(_param "$index" path)
    content=$(_param "$index" content)
    mode=$(_param "$index" mode)
    mode=${mode:-644}
    if [[ "$sudo" == "1" ]]; then
        if [[ "$DRY_RUN" == "1" ]]; then
            printf '[dry-run] %s install -m %s -D /dev/stdin %q\n' "$SUDO_CMD" "$mode" "$path"
            return 0
        fi
        # install -D creates the parent directory. "tee" cannot, and a root
        # write into a missing drop-in directory (/etc/systemd/journald.conf.d
        # does not exist on Ubuntu) must never be attempted silently.
        local sudo_cmd=()
        if [[ -n "$SUDO_CMD" ]]; then
            sudo_cmd=("$SUDO_CMD")
        fi
        printf '%s' "$content" | "${sudo_cmd[@]}" install -D -m "$mode" /dev/stdin "$path"
    else
        if [[ "$DRY_RUN" == "1" ]]; then
            printf '[dry-run] write %s (mode %s)\n' "$path" "$mode"
            return 0
        fi
        mkdir -p "$(dirname -- "$path")"
        printf '%s' "$content" >"$path"
        chmod "$mode" "$path"
    fi
}

_apply_append_line() {
    local index=$1 sudo=$2 path line
    path=$(_param "$index" path)
    line=$(_param "$index" line)
    if [[ "$DRY_RUN" == "1" ]]; then
        printf '[dry-run] append %q to %s (unless present)\n' "$line" "$path"
        return 0
    fi
    if grep -qxF "$line" "$path" 2>/dev/null; then
        return 0
    fi
    if [[ "$sudo" == "1" ]]; then
        printf '%s\n' "$line" | sudo tee -a "$path" >/dev/null
    else
        printf '%s\n' "$line" >>"$path"
    fi
}

_apply_symlink() {
    local index=$1 link target
    link=$(_param "$index" link)
    target=$(_param "$index" target)
    run ln -sfn "$target" "$link"
}

_apply_exec() {
    local index=$1 sudo=$2 cwd allow_fail
    local -a argv=()
    _param_list "$index" argv argv
    cwd=$(_param "$index" cwd)
    allow_fail=$(_param "$index" allow_fail)
    [[ ${#argv[@]} -gt 0 ]] || die "_apply_exec: empty argv for ${PLAN_ID[$index]}"

    local status=0
    if [[ "$sudo" == "1" ]]; then
        if [[ -n "$cwd" ]]; then
            ( cd "$cwd" && run_sudo "${argv[@]}" ) || status=$?
        else
            run_sudo "${argv[@]}" || status=$?
        fi
    else
        if [[ -n "$cwd" ]]; then
            ( cd "$cwd" && run "${argv[@]}" ) || status=$?
        else
            run "${argv[@]}" || status=$?
        fi
    fi
    if [[ $status -ne 0 ]]; then
        if [[ "$allow_fail" == "1" ]]; then
            warn "${PLAN_SUMMARY[$index]} failed (exit $status); continuing"
        else
            die "${PLAN_SUMMARY[$index]} failed (exit $status)"
        fi
    fi
}

_apply_apt() {
    local index=$1 update
    local -a packages=()
    _param_list "$index" packages packages
    update=$(_param "$index" update)
    [[ ${#packages[@]} -gt 0 ]] || return 0
    if [[ "$update" == "1" ]]; then
        run_sudo apt-get update
    fi
    run_sudo apt-get install -y --no-install-recommends "${packages[@]}"
}

_apply_usermod() {
    local index=$1 user
    local -a groups=()
    _param_list "$index" groups groups
    user=$(_param "$index" user)
    local joined
    joined=$(IFS=,; printf '%s' "${groups[*]}")
    run_sudo usermod -aG "$joined" "$user"
}

_apply_git_checkout() {
    local index=$1 dir url ref allow_missing
    dir=$(_param "$index" dir)
    url=$(_param "$index" url)
    ref=$(_param "$index" ref)
    allow_missing=$(_param "$index" allow_missing)

    if [[ ! -d "$dir/.git" ]]; then
        run mkdir -p "$(dirname -- "$dir")"
        run git clone "$url" "$dir"
    fi
    run git -C "$dir" fetch --tags --force

    if [[ "$ref" == "latest" ]]; then
        ref=$(git -C "$dir" tag --sort=-v:refname | head -n 1 || true)
        [[ -n "$ref" ]] || die "Could not determine the latest tag in $dir"
    fi

    if git -C "$dir" rev-parse --verify --quiet "${ref}^{commit}" >/dev/null; then
        run git -C "$dir" checkout --detach "$ref"
    elif [[ "$allow_missing" == "1" ]]; then
        warn "Ref '$ref' not found in $dir; keeping the default branch"
    else
        die "Ref '$ref' not found in $dir"
    fi
}

_apply_pip_install() {
    local index=$1 python index_url constraints requirements stamp_path stamp_content check_after
    local -a args=() specs=() fallback=()
    python=$(_param "$index" python)
    index_url=$(_param "$index" index_url)
    constraints=$(_param "$index" constraints)
    requirements=$(_param "$index" requirements)
    stamp_path=$(_param "$index" stamp_path)
    stamp_content=$(_param "$index" stamp_content)
    check_after=$(_param "$index" check_after)
    _param_list "$index" args args
    _param_list "$index" specs specs
    _param_list "$index" fallback_specs fallback

    _pip_build() {
        local -n _cmd=$1
        local mode=$2
        _cmd=("$python" -m pip install)
        if [[ -n "$index_url" ]]; then
            _cmd+=(--index-url "$index_url")
        fi
        if [[ -n "$constraints" && -f "$constraints" ]]; then
            _cmd+=(--constraint "$constraints")
        fi
        if [[ ${#args[@]} -gt 0 ]]; then
            _cmd+=("${args[@]}")
        fi
        if [[ -n "$requirements" ]]; then
            _cmd+=(-r "$requirements")
        fi
        if [[ "$mode" == "fallback" ]]; then
            _cmd+=("${fallback[@]}")
        else
            _cmd+=("${specs[@]}")
        fi
    }

    local -a command=()
    _pip_build command primary
    if ! run "${command[@]}"; then
        if [[ ${#fallback[@]} -eq 0 ]]; then
            die "pip install failed: ${PLAN_SUMMARY[$index]}"
        fi
        warn "Combined install failed; retrying with explicit device packages"
        _pip_build command fallback
        run "${command[@]}" || die "pip install failed: ${PLAN_SUMMARY[$index]}"
    fi

    if [[ "$check_after" == "1" && "$DRY_RUN" != "1" ]]; then
        "$python" -m pip check || warn "pip check reported dependency conflicts"
    fi

    if [[ -n "$stamp_path" && "$DRY_RUN" != "1" ]]; then
        mkdir -p "$(dirname -- "$stamp_path")"
        printf '%s' "$stamp_content" >"$stamp_path"
    fi
}

_apply_download() {
    local index=$1 url dest size sha label source token_file
    url=$(_param "$index" url)
    dest=$(_param "$index" dest)
    size=$(_param "$index" size)
    sha=$(_param "$index" sha256)
    label=$(_param "$index" label)
    source=$(_param "$index" source)
    token_file=$(_param "$index" token_file)

    local auth_config=""
    if [[ "$source" == "civitai" && "$DRY_RUN" != "1" ]]; then
        [[ -r "$token_file" ]] || die "Civitai token not found: $token_file (run './provision.sh auth')"
        local token
        token=$(<"$token_file")
        [[ -n "$token" ]] || die "Civitai token file is empty: $token_file"
        auth_config=$(mktemp)
        chmod 600 "$auth_config"
        local separator='?'
        if [[ "$url" == *\?* ]]; then
            separator='&'
        fi
        printf 'url = "%s%stoken=%s"\n' "$url" "$separator" "$token" >"$auth_config"
    fi

    download_file "$url" "$dest" "$sha" "$size" "$label" "$auth_config"
    [[ -z "$auth_config" ]] || rm -f "$auth_config"
}

apply_plan() {
    local step=$1
    shift
    load_plan "$step" "$@"

    local note
    if [[ "$PLAN_STATUS" != "blocked" ]]; then
        for note in ${PLAN_NOTES[@]+"${PLAN_NOTES[@]}"}; do
            log "$note"
        done
    fi

    if [[ "$PLAN_STATUS" == "blocked" ]]; then
        for note in ${PLAN_NOTES[@]+"${PLAN_NOTES[@]}"}; do
            printf '%sERROR%s %s\n' "$C_RED" "$C_RESET" "$note" >&2
        done
        return "$PLAN_EXIT"
    fi

    local index skipped=0
    for index in "${!PLAN_KIND[@]}"; do
        if [[ "${PLAN_SAT[$index]}" == "1" ]]; then
            skipped=$((skipped + 1))
            continue
        fi
        if [[ "$DRY_RUN" == "1" ]]; then
            printf '[dry-run] %s\n' "${PLAN_SUMMARY[$index]}"
            continue
        fi
        log "${PLAN_SUMMARY[$index]}"
        if ! apply_action "$index"; then
            die "action '${PLAN_ID[$index]}' failed: ${PLAN_SUMMARY[$index]}"
        fi
    done

    if [[ "$DRY_RUN" != "1" && $skipped -eq ${#PLAN_KIND[@]} && ${#PLAN_KIND[@]} -gt 0 ]]; then
        ok "already up to date"
    fi
    return 0
}

# ---------------------------------------------------------------------------
# Filesystem helpers used by the launcher and by downloads
# ---------------------------------------------------------------------------
ensure_big_mount() {
    local mount=${BIG_MOUNT:?BIG_MOUNT is not set}
    local device=${BIG_DEVICE:-}

    if mountpoint -q "$mount" 2>/dev/null; then
        return 0
    fi

    log "Disk $mount is not mounted; trying to mount it."
    if [[ "$DRY_RUN" == "1" ]]; then
        printf '[dry-run] udisksctl mount -b %q\n' "$device"
        return 0
    fi
    if [[ -n "$device" ]] && command -v udisksctl >/dev/null 2>&1; then
        if udisksctl mount -b "$device"; then
            sleep 1
            if mountpoint -q "$mount"; then
                ok "Mounted $mount"
                return 0
            fi
        fi
    fi
    die "Disk $mount is not mounted. Mount it with:
  udisksctl mount -b ${device:-/dev/disk/by-uuid/$BIG_UUID}"
}

file_size() {
    stat -c '%s' "$1" 2>/dev/null || echo 0
}

# download_file URL DEST SHA256 SIZE LABEL [AUTH_CONFIG]
# Resumable, atomic and verified. Verification is delegated to Python so there
# is exactly one implementation of "file matches the manifest".
download_file() {
    local url=$1 dest=$2 sha=$3 size=$4 label=$5 auth=${6:-}
    local part="${dest}.part"

    [[ "$DRY_RUN" == "1" ]] || mkdir -p "$(dirname -- "$dest")"

    if py verify-file --path "$dest" --size "$size" --sha256 "$sha"; then
        ok "$label already present and verified"
        return 0
    fi

    if [[ -f "$dest" ]]; then
        warn "$label exists but does not match the manifest; moving it aside"
        if [[ "$DRY_RUN" != "1" ]]; then
            mv -f "$dest" "${dest}.bad.$(date +%s)"
        fi
    fi

    if [[ "$DRY_RUN" == "1" ]]; then
        printf '[dry-run] download %s -> %s\n' "$label" "$dest"
        return 0
    fi

    local -a args=(
        --fail --location --show-error
        --retry 5 --retry-delay 5 --retry-all-errors
        --connect-timeout 30
        --speed-time 120 --speed-limit 1024
        --continue-at -
        --output "$part"
    )
    if [[ -t 2 ]]; then
        args+=(--progress-bar)
    else
        args+=(--silent)
    fi
    if [[ -n "$auth" ]]; then
        # The URL (with its token) lives in the config file, not in argv.
        args+=(--config "$auth")
    else
        args+=("$url")
    fi

    log "Downloading $label"
    if ! curl "${args[@]}"; then
        die "Download failed: $label. Partial file kept at $part"
    fi
    if ! py verify-file --path "$part" --size "$size" --sha256 "$sha"; then
        rm -f "$part"
        die "Verification failed for $label"
    fi
    mv -f "$part" "$dest"
    chmod 664 "$dest" 2>/dev/null || true
    ok "$label downloaded and verified"
}
