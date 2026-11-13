"""Pins the commit gate: the e2e test cannot be skipped for a commit.

Business requirement: every commit runs the full `./provision.sh test` matrix
including the e2e test. There is no option, no condition and no excuse to skip
it — a missing model, a dead GPU or a deadline all produce a red gate, not a
green one. These tests fail when someone edits that rule away:

* the hook must call `./provision.sh test` unconditionally;
* the hook must not mention `--no-verify`, an `exit 0` shortcut or a
  `if`/`case` guard around the call;
* `provision.sh test` must run the e2e marker exactly once and without a
  fallback; the plain unit run must deselect it, otherwise every commit starts
  ComfyUI twice (see reports/2026-11-13-crash-report.md §4.5);
* `provision.sh test` must treat a skipped run as a failure;
* the e2e test itself must not call `pytest.skip`.
"""

from __future__ import annotations

import re
import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
HOOK = REPO_ROOT / ".githooks" / "pre-commit"
PROVISION_SH = REPO_ROOT / "provision.sh"
E2E_TEST = REPO_ROOT / "provision" / "tests" / "test_generate_e2e.py"


def _hook_lines() -> list[str]:
    return [
        line.strip()
        for line in HOOK.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def _cmd_test_body() -> str:
    text = PROVISION_SH.read_text(encoding="utf-8")
    start = text.index("cmd_test() {")
    end = text.index("\ncmd_", start + 1)
    return text[start:end]


class CommitGateTests(unittest.TestCase):
    def test_hook_calls_the_full_test_matrix(self) -> None:
        self.assertTrue(HOOK.is_file(), f"{HOOK} is missing; commits would be ungated")
        self.assertIn("./provision.sh test", _hook_lines())

    def test_hook_has_no_bypass(self) -> None:
        for line in _hook_lines():
            self.assertNotIn("no-verify", line)
            self.assertFalse(
                re.match(r"(if|case|while)\b", line),
                f"the hook must not conditionally run the gate: {line!r}",
            )
            self.assertNotRegex(line, r"\|\|\s*(true|:)")
            self.assertNotRegex(line, r"^\s*exit 0\s*$")

    def test_provision_test_runs_e2e_unconditionally(self) -> None:
        body = _cmd_test_body()
        self.assertIn("-m e2e", body)
        # No fallback that turns a red e2e run green.
        self.assertNotRegex(body, r"-m e2e[^\n]*\|\|\s*(true|:)")
        # A skipped run must be treated as a failure.
        self.assertIn("SKIPPED", body)
        self.assertIn("PYTEST_ADDOPTS", body)

    def test_e2e_test_never_skips(self) -> None:
        body = E2E_TEST.read_text(encoding="utf-8")
        self.assertNotIn("pytest.skip", body)
        self.assertNotRegex(body, r"skip[Ii]f|skipUnless|skipIf")
        self.assertIn("pytest.fail", body)

    def test_unit_run_deselects_e2e(self) -> None:
        """The plain pytest pass must not start ComfyUI: e2e runs once, later.

        reports/2026-11-13-crash-report.md §4.5: running the full suite (which
        contains the e2e test) and then the e2e marker again gave every commit
        two ComfyUI start/stop cycles, and 4 of 7 platform resets landed inside
        that cycle. One commit, one cycle.
        """
        body = _cmd_test_body()
        self.assertIn('-m "not e2e"', body, f"the unit run must deselect e2e:\n{body}")

    def test_e2e_marker_selected_exactly_once(self) -> None:
        body = _cmd_test_body()
        self.assertEqual(body.count("-m e2e"), 1, f"e2e must run once per test run:\n{body}")
        self.assertEqual(body.count('-m "not e2e"'), 1, f"the unit pass must deselect e2e once:\n{body}")

    def test_marker_filters_partition_the_suite(self) -> None:
        """Behavioural proof: the two runs do not overlap and together cover all."""
        cmd = [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider"]
        tests = str(REPO_ROOT / "provision" / "tests")

        def collected(*marker: str) -> set[str]:
            result = subprocess.run(
                [*cmd, *marker, tests],
                capture_output=True,
                text=True,
                timeout=180,
                check=False,
                cwd=REPO_ROOT,
            )
            self.assertEqual(result.returncode, 0, f"collection failed: {result.stdout}{result.stderr}")
            return {line.strip() for line in result.stdout.splitlines() if "::" in line}

        unit = collected("-m", "not e2e")
        e2e = collected("-m", "e2e")
        self.assertTrue(e2e, "the e2e marker collected nothing")
        self.assertTrue(unit, "the unit marker collected nothing")
        self.assertFalse(unit & e2e, f"both runs execute the same tests: {sorted(unit & e2e)}")
        self.assertIn("test_generate_e2e.py", "\n".join(e2e))


if __name__ == "__main__":
    unittest.main()
