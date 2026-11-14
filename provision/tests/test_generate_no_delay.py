"""Business requirement: no delay after the user presses Enter at interactive prompts.

The user sees a prompt (``Select [1-1]:``, ``Prompt:`` *), presses Enter, and
expects the next screen / next prompt to appear immediately. Historically
``generate.sh`` piped Python's stdout through a process-substitution loop
(``> >(while read line; do ... "$(date ...)"; done)``) without ``-u``:

* Python buffered stdout whenever it was a pipe, so prompts only appeared
  once the 8 KiB buffer filled or the process exited;
* the consumer loop forked ``date`` for every line and could fall behind, so
  ``print()`` blocked on ``write()`` right after Enter.

The fix is a contract, and this module locks both halves of it:

1. ``generate.sh`` keeps Python's stdout attached to the terminal and passes
   ``-u``; the log file is written from inside Python (``GENERATE_LOG_FILE``),
   never from a shell pipe.
2. ``comfyprov.generate`` tees output to the log itself and flushes the
   terminal stream on every write, so a prompt is visible before ``input()``
   blocks and nothing is lost on a hard crash.

The tests never touch ComfyUI, the network, sudo or the real models directory.
"""

from __future__ import annotations

import io
import os
import re
import select
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from comfyprov import generate
from comfyprov.generate import LogTee, _ask, _ask_yn, _select_from_list, install_log_tee

INSTALL_DIR = Path(__file__).resolve().parents[2]
PROVISION_LIB = INSTALL_DIR / "provision" / "lib"
GENERATE_SH = INSTALL_DIR / "generate.sh"

# Same format as the shell helpers in generate.sh: date '+%Y-%m-%d %H:%M:%S'.
TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} ")


class RecordingStream(io.StringIO):
    """A stream that records how many times it was flushed."""

    def __init__(self) -> None:
        super().__init__()
        self.flushes = 0

    def flush(self) -> None:
        self.flushes += 1
        super().flush()


def _log_lines(content: str) -> list[str]:
    """Split a log by newlines only (progress redraws with \\r inside a line)."""
    return [line for line in content.split("\n") if line]


def _python_block() -> str:
    """The python3 invocation block of generate.sh (from the command to its exit status)."""
    script = GENERATE_SH.read_text(encoding="utf-8")
    start = script.index("PY_ARGS=(")
    end = script.index("PY_EXIT=$?", start)
    return script[start:end]


# ---------------------------------------------------------------------------
# 1. Shell wrapper contract
# ---------------------------------------------------------------------------


class ShellWrapperContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.script = GENERATE_SH.read_text(encoding="utf-8")
        cls.python_block = _python_block()

    def test_python_stdout_is_not_a_process_substitution_pipe(self) -> None:
        """A ``> >(while read ...)`` consumer is asynchronous and can block Python."""
        self.assertNotIn(
            "> >(while",
            self.script,
            "generate.sh must not pipe Python stdout through a process substitution: "
            "the consumer can fall behind and freeze the prompt after Enter.",
        )

    def test_python_runs_unbuffered(self) -> None:
        """Without -u a piped stdout holds prompts back until the buffer fills."""
        self.assertIn("python3 -u -m comfyprov.generate", self.python_block)

    def test_log_file_is_passed_to_python(self) -> None:
        """The log must be written from Python, not from a shell pipe."""
        self.assertIn('export GENERATE_LOG_FILE="$LOG_FILE"', self.python_block)

    def test_no_per_line_date_fork_around_python(self) -> None:
        """Forking ``date`` for every line stalls the prompt on short read bursts."""
        self.assertNotIn("$(date", self.python_block)


# ---------------------------------------------------------------------------
# 2. LogTee: duplicate to terminal + log without adding latency
# ---------------------------------------------------------------------------


class LogTeeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.stream = RecordingStream()
        self.log = io.StringIO()
        self.tee = LogTee(self.stream, self.log)

    def test_writes_go_to_both_terminal_and_log(self) -> None:
        self.tee.write("hello\n")
        self.assertEqual(self.stream.getvalue(), "hello\n")
        self.assertIn("hello", self.log.getvalue())

    def test_terminal_is_flushed_on_every_write(self) -> None:
        """The prompt must reach the terminal before input() blocks on stdin."""
        before = self.stream.flushes
        self.tee.write("Select [1-1]: ")
        self.assertGreater(self.stream.flushes, before, "write() must flush the terminal stream")
        self.assertEqual(self.stream.getvalue(), "Select [1-1]: ")

    def test_log_is_flushed_on_every_write(self) -> None:
        """A crash right after a write must not lose the logged line."""
        self.tee.write("progress\n")
        self.assertEqual(len(_log_lines(self.log.getvalue())), 1)

    def test_each_log_line_gets_a_timestamp(self) -> None:
        self.tee.write("first\nsecond\n")
        lines = _log_lines(self.log.getvalue())
        self.assertEqual(len(lines), 2)
        for line in lines:
            self.assertRegex(line, TIMESTAMP_RE)

    def test_partial_line_is_not_timestamped_twice(self) -> None:
        """input() prompts are written without a newline and completed later."""
        self.tee.write("Select [1-1]: ")
        self.tee.write("\n")
        lines = _log_lines(self.log.getvalue())
        self.assertEqual(len(lines), 1)
        self.assertRegex(lines[0], TIMESTAMP_RE)
        self.assertEqual(lines[0].count("Select [1-1]:"), 1)
        self.assertTrue(lines[0].endswith("Select [1-1]: "))

    def test_carriage_return_progress_stays_one_log_line(self) -> None:
        """Writing with \\r through write() still keeps the log readable."""
        for elapsed in range(3):
            self.tee.write(f"\rstatus {elapsed}")
        self.tee.write("\n")
        lines = _log_lines(self.log.getvalue())
        self.assertEqual(len(lines), 1)
        self.assertRegex(lines[0], TIMESTAMP_RE)

    def test_private_writes_never_reach_the_log(self) -> None:
        """Progress repaints are terminal-only: the log must not tick every second."""
        for elapsed in range(3):
            self.tee.write_private(f"\rGenerating… {elapsed}s elapsed")
        self.tee.write_private("\n")
        self.assertIn("2s elapsed", self.stream.getvalue())
        self.assertEqual(self.log.getvalue(), "", "private output must not be logged")

    def test_write_private_flushes_the_terminal(self) -> None:
        before = self.stream.flushes
        self.assertEqual(self.tee.write_private("secret"), 6)
        self.assertGreater(self.stream.flushes, before)
        self.assertEqual(self.tee.write_private(""), 0)

    def test_write_returns_character_count(self) -> None:
        """print() expects the number of characters written."""
        self.assertEqual(self.tee.write("abc"), 3)
        self.assertEqual(self.tee.write(""), 0)

    def test_delegates_fileno_and_isatty(self) -> None:
        """input() calls sys.stdout.fileno(); losing it breaks interactive mode."""
        with tempfile.TemporaryFile("w+") as handle:
            tee = LogTee(handle, io.StringIO())
            self.assertEqual(tee.fileno(), handle.fileno())
            self.assertEqual(tee.isatty(), handle.isatty())
            self.assertFalse(tee.isatty())

    def test_reports_encoding_and_writability(self) -> None:
        with tempfile.TemporaryFile("w+", encoding="utf-8") as handle:
            tee = LogTee(handle, io.StringIO())
            self.assertEqual(tee.encoding, "utf-8")
            self.assertEqual(tee.errors, handle.errors)
            self.assertTrue(tee.writable())
            self.assertFalse(tee.readable())

    def test_flush_flushes_both_sides(self) -> None:
        typed = mock.Mock()
        tee = LogTee(typed, mock.Mock())
        tee.flush()
        typed.flush.assert_called_once_with()

    def test_close_does_not_close_the_terminal(self) -> None:
        """Closing the wrapper must leave sys.stdout usable by the interpreter."""
        self.tee.close()
        self.assertFalse(self.stream.closed)


class InstallLogTeeTests(unittest.TestCase):
    def test_installs_a_tee_on_stdout_and_stderr(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "generate.log"
            stdout, stderr = sys.stdout, sys.stderr
            try:
                tee = install_log_tee(log_path)
                self.assertIsNotNone(tee)
                self.assertIs(sys.stdout, tee)
                self.assertIs(sys.stderr, tee)
                print("Select [1-1]: ")
            finally:
                sys.stdout, sys.stderr = stdout, stderr
            self.assertIn("Select [1-1]:", log_path.read_text(encoding="utf-8"))

    def test_unwritable_log_falls_back_to_plain_terminal(self) -> None:
        """A broken log path must never break generation or the prompts."""
        stdout, stderr = sys.stdout, sys.stderr
        out, err = RecordingStream(), RecordingStream()
        try:
            sys.stdout, sys.stderr = out, err
            with mock.patch("sys.stderr", err):
                self.assertIsNone(install_log_tee(Path("/nonexistent/for/sure/generate.log")))
            self.assertIs(sys.stdout, out, "stdout must stay untouched when the log cannot be opened")
        finally:
            sys.stdout, sys.stderr = stdout, stderr

    def test_no_op_without_a_path(self) -> None:
        self.assertIsNone(install_log_tee(None))

    def test_appends_to_an_existing_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "generate.log"
            log_path.write_text("shell: started\n", encoding="utf-8")
            stdout, stderr = sys.stdout, sys.stderr
            try:
                install_log_tee(log_path)
                print("python: ready")
            finally:
                sys.stdout, sys.stderr = stdout, stderr
            content = log_path.read_text(encoding="utf-8")
            self.assertIn("shell: started", content)
            self.assertIn("python: ready", content)


# ---------------------------------------------------------------------------
# 3. Prompts flush before blocking on stdin
# ---------------------------------------------------------------------------


class PromptFlushTests(unittest.TestCase):
    def test_ask_flushes_stdout_before_reading(self) -> None:
        stream = RecordingStream()
        with mock.patch("sys.stdout", stream), mock.patch("builtins.input", return_value="a cat") as ask:
            self.assertEqual(_ask("Prompt"), "a cat")
        self.assertGreater(stream.flushes, 0)
        ask.assert_called_once_with("Prompt: ")

    def test_select_from_list_flushes_options_before_reading(self) -> None:
        """The freeze reported by the user: after Enter the next list never appeared."""
        stream = RecordingStream()
        flushes_at_input: list[int] = []

        def fake_input(_prompt: str) -> str:
            flushes_at_input.append(stream.flushes)
            return "2"

        with mock.patch("sys.stdout", stream), mock.patch("builtins.input", fake_input):
            self.assertEqual(_select_from_list("Samplers", ["euler", "heun"]), "heun")

        self.assertTrue(flushes_at_input, "input() was never called")
        self.assertGreater(flushes_at_input[0], 0, "options must be flushed before blocking on input()")


# ---------------------------------------------------------------------------
# 4. End-to-end: output reaches a pipe instantly (no TTY, no -u)
# ---------------------------------------------------------------------------


_BOOTSTRAP = """
import os
from comfyprov.generate import _ask, install_log_tee

install_log_tee(os.environ["GENERATE_LOG_FILE"])
answer = _ask("Prompt")
print("ANSWER=" + answer)
"""


# No input() here: it flushes sys.stdout itself, which would hide a wrapper
# that buffers.  readline() only blocks, so the only thing that can put the
# prompt on the wire is the tee's own flush.
_RAW_PROMPT_BOOTSTRAP = """
import os, sys
from comfyprov.generate import install_log_tee

install_log_tee(os.environ["GENERATE_LOG_FILE"])
sys.stdout.write("Select [1-1]: ")
answer = sys.stdin.readline().strip()
print("ANSWER=" + answer)
"""


class PromptLatencyTests(unittest.TestCase):
    """Runs the real module in a subprocess whose stdout is a pipe.

    A buffered write would only arrive when the process exits; here it must be
    readable while the process is still blocked on stdin.
    """

    def _spawn(self, code: str, log_path: Path) -> subprocess.Popen[bytes]:
        env = os.environ.copy()
        env["PYTHONPATH"] = str(PROVISION_LIB) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        env["GENERATE_LOG_FILE"] = str(log_path)
        env.pop("PYTHONUNBUFFERED", None)
        return subprocess.Popen(
            [sys.executable, "-c", code],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
            env=env,
        )

    def _run(self, code: str, expected: bytes, answer: bytes) -> tuple[bytes, str]:
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "generate.log"
            proc = self._spawn(code, log_path)
            try:
                assert proc.stdout is not None
                ready, _, _ = select.select([proc.stdout], [], [], 15)
                self.assertTrue(ready, "no output before stdin was answered: the prompt was buffered")
                chunk = os.read(proc.stdout.fileno(), 4096)
                self.assertIn(expected, chunk)
                self.assertIsNone(proc.poll(), "the process must be blocked on stdin at this point")

                assert proc.stdin is not None
                proc.stdin.write(answer + b"\n")
                proc.stdin.close()
                rest = proc.stdout.read()
                proc.wait(timeout=15)
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()

            self.assertEqual(proc.returncode, 0)
            return chunk + rest, log_path.read_text(encoding="utf-8")

    def test_write_is_flushed_to_the_pipe_immediately(self) -> None:
        """The exact failure mode: a prompt stuck in a buffer after Enter."""
        stdout, log = self._run(_RAW_PROMPT_BOOTSTRAP, b"Select [1-1]: ", b"2")
        self.assertIn(b"ANSWER=2", stdout)
        self.assertIn("Select [1-1]: ", log)

    def test_ask_prompt_reaches_both_pipe_and_log(self) -> None:
        stdout, log = self._run(_BOOTSTRAP, b"Prompt: ", b"a cat")
        self.assertIn(b"ANSWER=a cat", stdout)
        self.assertIn("Prompt: ", log)
        self.assertIn("ANSWER=a cat", log)

    def test_ask_yn_flushes_stdout_before_reading(self) -> None:
        """The "generate more?" question is a prompt like any other."""
        stream = RecordingStream()
        with mock.patch("sys.stdout", stream), mock.patch("builtins.input", return_value="y") as ask:
            self.assertTrue(_ask_yn("Generate more images with the same settings?"))
        self.assertGreater(stream.flushes, 0)
        ask.assert_called_once_with("Generate more images with the same settings? (y/n) [n]: ")


_TTY_BOOTSTRAP = """
import os
from comfyprov.generate import _ask, _select_from_list, install_log_tee

install_log_tee(os.environ["GENERATE_LOG_FILE"])
name = _ask("Prompt")
choice = _select_from_list("Samplers", ["euler", "heun"])
print("ANSWER=" + name + "/" + choice)
"""


@unittest.skipUnless(hasattr(os, "openpty"), "requires a POSIX pty")
class TtyPromptTests(unittest.TestCase):
    """Same flow on a real terminal: the tee must not break readline/input().

    input() calls sys.stdout.fileno() and sys.stdout.isatty(); a wrapper that
    does not delegate them silently breaks every prompt.
    """

    def test_prompts_still_work_on_a_tty(self) -> None:
        import pty

        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "generate.log"
            env = os.environ.copy()
            env["PYTHONPATH"] = str(PROVISION_LIB) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
            env["GENERATE_LOG_FILE"] = str(log_path)
            master, slave = pty.openpty()
            proc = subprocess.Popen(
                [sys.executable, "-c", _TTY_BOOTSTRAP],
                stdin=slave,
                stdout=slave,
                stderr=slave,
                env=env,
            )
            os.close(slave)
            try:
                self.assertIn(
                    b"Prompt: ", self._read_until(master, b"Prompt: "), "the prompt must be visible before input()"
                )
                os.write(master, b"a cat\n")
                self.assertIn(b"Select [1-2]: ", self._read_until(master, b"Select [1-2]: "))
                os.write(master, b"2\n")
                proc.wait(timeout=15)
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()
                os.close(master)

            self.assertEqual(proc.returncode, 0)
            log = log_path.read_text(encoding="utf-8")
            self.assertIn("1) euler (default)", log)
            self.assertIn("ANSWER=a cat/heun", log)

    @staticmethod
    def _read_until(master: int, marker: bytes, timeout: float = 15.0) -> bytes:
        """Read terminal output until ``marker`` appears or the timeout expires."""
        buffer = b""
        deadline = time.monotonic() + timeout
        while marker not in buffer and time.monotonic() < deadline:
            ready, _, _ = select.select([master], [], [], 0.2)
            if ready:
                buffer += os.read(master, 4096)
        return buffer


class MainWiringTests(unittest.TestCase):
    def test_main_installs_the_tee_from_the_environment(self) -> None:
        with (
            mock.patch.dict(os.environ, {"GENERATE_LOG_FILE": "/tmp/generate-test.log"}),
            mock.patch("comfyprov.generate.install_log_tee") as install,
            mock.patch("comfyprov.generate.discover_models", return_value=[]),
            mock.patch("comfyprov.generate.interactive", side_effect=RuntimeError("stop here")),
            self.assertRaises(RuntimeError),
        ):
            generate.main(["http://127.0.0.1:8188", "/tmp/out", "", "", ""])
        install.assert_called_once_with("/tmp/generate-test.log")

    def test_main_without_log_env_keeps_stdout(self) -> None:
        env = {key: value for key, value in os.environ.items() if key != "GENERATE_LOG_FILE"}
        with (
            mock.patch.dict(os.environ, env, clear=True),
            mock.patch("comfyprov.generate.install_log_tee") as install,
            mock.patch("comfyprov.generate.discover_models", return_value=[]),
            mock.patch("comfyprov.generate.interactive", side_effect=RuntimeError("stop here")),
            self.assertRaises(RuntimeError),
        ):
            generate.main(["http://127.0.0.1:8188", "/tmp/out", "", "", ""])
        install.assert_not_called()


if __name__ == "__main__":
    unittest.main()
