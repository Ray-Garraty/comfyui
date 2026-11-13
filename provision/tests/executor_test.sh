#!/usr/bin/env bash
# Regression tests for the plan executor in provision/lib/common.sh.
#
#  1. A root write into a directory that does not exist yet must create it.
#     This is the bug that left /etc/systemd/journald.conf.d missing while the
#     system step reported success: "sudo tee path" cannot create parents.
#  2. A failing action must abort the plan. Swallowing the exit code is how the
#     missing journald drop-in went unnoticed.
#
# SUDO_CMD is the seam: empty means "run as the current user", so the test needs
# no sudo and no root-owned paths.
# PLAN_* and the SUDO_CMD/DRY_RUN switches are consumed by the sourced library.
# shellcheck disable=SC2034
set -Eeuo pipefail

HERE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
LIB="$HERE/../lib/common.sh"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }

DRY_RUN=0
NO_COLOR=1
SUDO_CMD=""
# shellcheck source=../lib/common.sh
source "$LIB"

test_write_creates_parent_directory() {
    local target="$TMP/etc/systemd/journald.conf.d/99-comfyui.conf"
    PLAN_KIND=("write_file")
    PLAN_SUDO=("1")
    PLAN_EFFECT=("now")
    PLAN_ID=("write:$target")
    PLAN_SUMMARY=("write")
    PLAN_SAT=("0")
    PLAN_PARAM=()
    PLAN_PARAM["0:path"]="$target"
    PLAN_PARAM["0:content"]="[Journal]
SyncIntervalSec=1
"
    PLAN_PARAM["0:mode"]="644"

    apply_action 0 || fail "write_file returned non-zero"

    [[ -f "$target" ]] || fail "file was not created: $target"
    grep -q 'SyncIntervalSec=1' "$target" || fail "content was not written"
    local mode
    mode=$(stat -c "%a" "$target")
    [[ "$mode" == "644" ]] || fail "mode is $mode, expected 644"
    printf 'ok: root write creates the parent directory\n'
}

test_failing_action_aborts_apply_plan() {
    # Replace the transport with a fixed plan: an exec action that always fails.
    load_plan() {
        PLAN_STATUS="ok"
        PLAN_EXIT=0
        PLAN_NOTES=()
        PLAN_KIND=("exec")
        PLAN_SUDO=("0")
        PLAN_EFFECT=("now")
        PLAN_ID=("boom")
        PLAN_SUMMARY=("failing action")
        PLAN_SAT=("0")
        PLAN_PARAM=()
        PLAN_PARAM["0:argv"]="false"
        PLAN_PARAM["0:allow_fail"]="0"
        PLAN_PARAM["0:cwd"]=""
    }
    local rc=0
    ( set +e; apply_plan fake >/dev/null 2>&1 ) || rc=$?
    [[ $rc -ne 0 ]] || fail "apply_plan ignored a failing action"
    printf 'ok: a failing action aborts the plan (exit %s)\n' "$rc"
}

test_params_keep_the_trailing_newline() {
    # Command substitution strips trailing newlines: every file written from a
    # plan used to lose its final "\n", so content comparisons never matched
    # and the doctor kept reporting "differs".
    local stream_file="$TMP/stream.txt"
    printf 'PLAN\tok\t0\ttest\nACT\t0\twrite_file\t0\tnow\twrite:x\t%s\t0\nPARAM\t0\tpath\t%s\nPARAM\t0\tcontent\t%s\nEND\n' \
        "$(printf 'x' | base64 -w0)" \
        "$(printf '/tmp/x' | base64 -w0)" \
        "$(printf 'line one\nline two\n' | base64 -w0)" >"$stream_file"
    py() { cat "$stream_file"; }

    load_plan test
    local content
    content=$(_param 0 content; printf x)
    content=${content%x}
    [[ "$content" == "line one
line two
" ]] || fail "content lost its trailing newline: $(printf '%q' "$content")"
    printf 'ok: params keep the trailing newline\n'
}

test_write_creates_parent_directory
test_params_keep_the_trailing_newline
test_failing_action_aborts_apply_plan
printf 'all executor tests passed\n'
