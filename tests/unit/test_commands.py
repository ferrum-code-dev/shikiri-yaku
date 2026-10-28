"""The CommandRunner adapter against real harmless commands."""

from __future__ import annotations

import asyncio
import os
import signal
import sys
import threading
from typing import TYPE_CHECKING, Final

import pytest

from shikiri_yaku.adapters.commands import ProcessCommandRunner
from shikiri_yaku.ports import CommandError

if TYPE_CHECKING:
    from pathlib import Path

_TIMEOUT_S: Final = 10
_START_WAIT_S: Final = 3
_CHILD_PID_ATTEMPTS: Final = 100
_CHILD_PID_WAIT_S: Final = 0.05

_STUBBORN_CHILD: Final = """
import os, signal, sys, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
open(sys.argv[1] + '.tmp', 'w').write(str(os.getpid()))
os.replace(sys.argv[1] + '.tmp', sys.argv[1])
time.sleep(60)
"""


def _run(argv: list[str], timeout_s: float = _TIMEOUT_S):
    return asyncio.run(ProcessCommandRunner().run(argv, timeout_s=timeout_s))


def test_a_successful_command_returns_its_output() -> None:
    result = _run(["echo", "hello"])

    assert (result.returncode, result.stdout) == (0, "hello\n")


def test_a_nonzero_exit_code_and_stderr_are_returned_not_raised() -> None:
    result = _run([sys.executable, "-c", "import sys; sys.stderr.write('bad'); sys.exit(3)"])

    assert (result.returncode, result.stderr) == (3, "bad")


def test_stdin_is_closed() -> None:
    result = _run([sys.executable, "-c", "import sys; print(repr(sys.stdin.read()))"])

    assert result.stdout == "''\n"


def test_a_command_that_cannot_start_is_a_command_error() -> None:
    with pytest.raises(CommandError):
        _run(["/no/such/executable"])


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_a_timeout_kills_a_child_that_ignores_sigterm(tmp_path: Path) -> None:
    pid_file = tmp_path / "child.pid"
    parent = (
        "import os, subprocess, sys, time\n"
        f"subprocess.Popen([sys.executable, '-c', {_STUBBORN_CHILD!r}, {str(pid_file)!r}])\n"
        f"while not os.path.exists({str(pid_file)!r}): time.sleep(0.01)\n"
        "print('ready', flush=True)\n"
        "time.sleep(60)\n"
    )
    with pytest.raises(CommandError, match="killed"):
        _run([sys.executable, "-c", parent], _START_WAIT_S)

    child = int(pid_file.read_text(encoding="utf-8"))
    for _ in range(_CHILD_PID_ATTEMPTS):
        if not _alive(child):
            break
        threading.Event().wait(_CHILD_PID_WAIT_S)
    else:
        os.kill(child, signal.SIGKILL)
        pytest.fail("the child of the killed command is still running")
