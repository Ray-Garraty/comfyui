"""Runs the shell-level executor regressions from ``executor_test.sh``.

``provision.sh test`` is a shell entry point; keeping the regression as a bash
test keeps it close to the code it guards, while this Python wrapper makes sure
pytest (and therefore the commit gate) never skips it.
"""

from __future__ import annotations

import subprocess
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "executor_test.sh"


class ExecutorTests(unittest.TestCase):
    def test_executor_regressions(self) -> None:
        result = subprocess.run(
            ["bash", str(SCRIPT)],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        self.assertEqual(result.returncode, 0, f"{result.stdout}\n{result.stderr}")


if __name__ == "__main__":
    unittest.main()
