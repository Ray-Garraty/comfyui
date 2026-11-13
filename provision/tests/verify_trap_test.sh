#!/usr/bin/env bash
# Regression test for the trap/cleanup unbound-variable bug in
# ``provision.sh cmd_verify``.
#
# ``cleanup`` is a trap handler fired at script EXIT, long after ``cmd_verify``
# returns. Its ``local`` variables ``srv_started``/``srv_pid`` are destroyed
# when the function returns, so under ``set -u`` they read as "unbound
# variable" and abort the script with exit 1.
#
# The fix: reference them with default expansion ``${var:-0}`` / ``${var:-}``
# so the trap never fails on a missing local.
set -Eeuo pipefail

run_buggy() {
    bash -Eeuo pipefail <<'EOF'
set -Eeuo pipefail
cmd() {
    local started=0
    local pid=""
    cleanup() {
        if [[ "$started" == "1" && -n "$pid" ]]; then
            echo "cleanup ran"
        fi
    }
    trap cleanup EXIT
    echo "done"
}
cmd
EOF
}

run_fixed() {
    bash -Eeuo pipefail <<'EOF'
set -Eeuo pipefail
cmd() {
    local started=0
    local pid=""
    cleanup() {
        if [[ "${started:-0}" == "1" && -n "${pid:-}" ]]; then
            echo "cleanup ran"
        fi
    }
    trap cleanup EXIT
    echo "done"
}
cmd
EOF
}

failures=0

echo "=== buggy trap pattern (expect non-zero exit) ==="
if run_buggy >/dev/null 2>&1; then
    echo "FAIL: buggy pattern exited 0 (bug not reproduced)"
    failures=$((failures + 1))
else
    echo "OK: buggy pattern fails as expected"
fi

echo "=== fixed trap pattern (expect zero exit) ==="
if run_fixed >/dev/null 2>&1; then
    echo "OK: fixed pattern passes"
else
    echo "FAIL: fixed pattern exited non-zero (fix broken)"
    failures=$((failures + 1))
fi

if [[ $failures -eq 0 ]]; then
    echo "ALL TRAP TESTS PASSED"
    exit 0
else
    echo "$failures test(s) failed"
    exit 1
fi
