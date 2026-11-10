"""The CommandRunner adapter against real harmless commands."""

from __future__ import annotations

import asyncio
import errno
import os
import signal
import sys
import threading
from pathlib import Path
from typing import Final

import pytest

from shikiri_yaku.adapters.commands import ProcessCommandRunner
from shikiri_yaku.ports import CommandError

_TIMEOUT_S: Final = 10
_START_WAIT_S: Final = 3
_SHORT_TIMEOUT_S: Final = 2
_GROUP_TIMEOUT_S: Final = 0.2
_CHILD_PID_ATTEMPTS: Final = 100
_CHILD_PID_WAIT_S: Final = 0.05

_STUBBORN_CHILD: Final = """
import os, signal, sys, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
open(sys.argv[1] + '.tmp', 'w').write(str(os.getpid()))
os.replace(sys.argv[1] + '.tmp', sys.argv[1])
time.sleep(60)
"""


def _run(
    argv: list[str],
    timeout_s: float = _TIMEOUT_S,
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
):
    return asyncio.run(ProcessCommandRunner().run(argv, timeout_s=timeout_s, cwd=cwd, env=env))


def test_a_successful_command_returns_its_output() -> None:
    result = _run(["echo", "hello"])

    assert (result.returncode, result.stdout) == (0, "hello\n")


def test_a_command_starts_in_the_given_directory(tmp_path: Path) -> None:
    result = _run([sys.executable, "-c", "import os; print(os.getcwd())"], cwd=tmp_path)

    assert Path(result.stdout.strip()).resolve() == tmp_path.resolve()


def test_extra_environment_is_added_for_the_command_only() -> None:
    result = _run(
        [sys.executable, "-c", "import os; print(os.environ['SHIKIRI_TEST_VALUE'])"],
        env={"SHIKIRI_TEST_VALUE": "set"},
    )

    assert (result.returncode, result.stdout) == (0, "set\n")
    assert "SHIKIRI_TEST_VALUE" not in os.environ


def test_a_nonzero_exit_code_and_stderr_are_returned_not_raised() -> None:
    result = _run([sys.executable, "-c", "import sys; sys.stderr.write('bad'); sys.exit(3)"])

    assert (result.returncode, result.stderr) == (3, "bad")


def test_stdin_is_closed() -> None:
    result = _run([sys.executable, "-c", "import sys; print(repr(sys.stdin.read()))"])

    assert result.stdout == "''\n"


def test_a_command_that_cannot_start_is_a_command_error() -> None:
    with pytest.raises(CommandError):
        _run(["/no/such/executable"])


def test_output_printed_before_a_timeout_is_kept() -> None:
    with pytest.raises(CommandError) as error:
        _run(["/bin/sh", "-c", "echo diagnostic-before-timeout; exec sleep 60"], _SHORT_TIMEOUT_S)

    assert "diagnostic-before-timeout" in error.value.output


def test_a_child_outliving_its_leader_is_killed_at_the_timeout() -> None:
    with pytest.raises(CommandError, match="killed") as error:
        _run(["/bin/sh", "-c", "sleep 60 & child=$!; printf '%s\\n' \"$child\""], _GROUP_TIMEOUT_S)

    child = int(error.value.output.strip())
    assert not _alive(child)


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


def _wait_for_file(path: Path) -> None:
    for _ in range(_CHILD_PID_ATTEMPTS):
        if path.exists():
            return
        threading.Event().wait(_CHILD_PID_WAIT_S)


async def _cancel_once_started(pid_file: Path) -> None:
    command = asyncio.create_task(
        ProcessCommandRunner().run(
            [sys.executable, "-c", _STUBBORN_CHILD, str(pid_file)], timeout_s=_TIMEOUT_S
        )
    )
    await asyncio.to_thread(_wait_for_file, pid_file)
    command.cancel()
    with pytest.raises(asyncio.CancelledError):
        await command


def test_cancelling_a_command_kills_and_reaps_its_process(tmp_path: Path) -> None:
    pid_file = tmp_path / "child.pid"

    asyncio.run(_cancel_once_started(pid_file))

    assert not _alive(int(pid_file.read_text(encoding="utf-8")))


def _macos_probe_of_exited_group(monkeypatch: pytest.MonkeyPatch) -> None:
    real_killpg = os.killpg

    def killpg(group_id: int, sig: int) -> None:
        if sig == 0:
            raise PermissionError(errno.EPERM, "Operation not permitted")
        real_killpg(group_id, sig)

    monkeypatch.setattr(os, "killpg", killpg)


def test_a_command_whose_group_reports_not_permitted_returns_its_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _macos_probe_of_exited_group(monkeypatch)

    result = _run(["echo", "hello"])

    assert (result.returncode, result.stdout) == (0, "hello\n")
