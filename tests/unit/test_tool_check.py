"""The existence check: `command -v` in the login shell, the name as an argument."""

from __future__ import annotations

import asyncio
from stat import S_IRWXU
from typing import TYPE_CHECKING, Final, override

import pytest

from shikiri_yaku.adapters.commands import ProcessCommandRunner
from shikiri_yaku.app.tool_check import ToolCheck, ToolNameError
from shikiri_yaku.ports import CommandError, CommandResult, CommandRunner

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

SHELL: Final = "/bin/zsh"
TIMEOUT_S: Final = 5
HOSTILE_NAMES: Final = ("ffmpeg;echo", "$(echo)", "ff mpeg", "", "-ffmpeg", "ffmpeg\necho")
ABNORMAL_EXITS: Final = (
    (1, ""),
    (2, "missing\n"),
    (127, ""),
    (-9, "missing\n"),
    (-15, "found=/bin/x\n"),
)
UNEXPECTED_ANSWERS: Final = ("", "garbage\n", "found=\n", "/usr/bin/git\n")


class _Shell(CommandRunner):
    """A login shell that prints `stdout` and exits with `returncode`, or cannot start at all."""

    def __init__(
        self, *, stdout: str = "", returncode: int = 0, error: CommandError | None = None
    ) -> None:
        self.stdout = stdout
        self.returncode = returncode
        self.error = error
        self.argv: list[str] = []

    @override
    async def run(
        self,
        argv: Sequence[str],
        *,
        timeout_s: float,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        del timeout_s, cwd, env
        self.argv = list(argv)
        if self.error is not None:
            raise self.error
        return CommandResult(returncode=self.returncode, stdout=self.stdout, stderr="")


def _find(runner: CommandRunner, tool: str, interpreter: str = SHELL) -> str | None:
    return asyncio.run(ToolCheck(runner, interpreter, TIMEOUT_S).find(tool))


def test_a_found_tool_gives_the_path_the_shell_printed() -> None:
    assert (
        _find(_Shell(stdout="found=/opt/homebrew/bin/ffmpeg\n"), "ffmpeg")
        == "/opt/homebrew/bin/ffmpeg"
    )


def test_a_missing_tool_gives_none() -> None:
    assert _find(_Shell(stdout="missing\n"), "ffmpeg") is None


def test_output_the_profile_prints_before_the_answer_is_skipped() -> None:
    assert _find(_Shell(stdout="Welcome back\nfound=/usr/bin/git\n"), "git") == "/usr/bin/git"


def test_the_name_is_the_last_argument_and_never_part_of_the_script() -> None:
    runner = _Shell(stdout="found=/bin/ffmpeg\n")

    _find(runner, "ffmpeg")

    assert runner.argv[:3] == [SHELL, "-l", "-c"]
    assert runner.argv[-1] == "ffmpeg"
    assert "ffmpeg" not in runner.argv[3]


@pytest.mark.parametrize("name", HOSTILE_NAMES)
def test_a_hostile_name_is_refused_before_the_shell_starts(name: str) -> None:
    runner = _Shell(stdout="found=/bin/x\n")

    with pytest.raises(ToolNameError):
        _find(runner, name)

    assert runner.argv == []


def test_a_runner_error_crosses_to_the_caller() -> None:
    runner = _Shell(error=CommandError("the shell did not start"))

    with pytest.raises(CommandError, match="did not start"):
        _find(runner, "ffmpeg")


@pytest.mark.parametrize(("returncode", "stdout"), ABNORMAL_EXITS)
def test_an_abnormal_shell_exit_is_an_error_whatever_it_printed(
    returncode: int, stdout: str
) -> None:
    with pytest.raises(CommandError):
        _find(_Shell(stdout=stdout, returncode=returncode), "ffmpeg")


@pytest.mark.parametrize("stdout", UNEXPECTED_ANSWERS)
def test_an_output_without_the_answer_is_an_error(stdout: str) -> None:
    with pytest.raises(CommandError):
        _find(_Shell(stdout=stdout), "ffmpeg")


def test_a_real_shell_that_exits_with_an_error_is_an_error() -> None:
    with pytest.raises(CommandError):
        _find(ProcessCommandRunner(), "ffmpeg", interpreter="/usr/bin/false")


def test_a_real_shell_killed_by_a_signal_is_an_error(tmp_path: Path) -> None:
    shell = tmp_path / "killed-shell"
    shell.write_text("#!/bin/sh\nkill -9 $$\n")
    shell.chmod(S_IRWXU)

    with pytest.raises(CommandError):
        _find(ProcessCommandRunner(), "ffmpeg", interpreter=str(shell))


def test_the_real_login_shell_finds_sh_and_reports_a_missing_tool() -> None:
    found = _find(ProcessCommandRunner(), "sh", interpreter="/bin/sh")
    missing = _find(ProcessCommandRunner(), "shikiri-no-such-tool-xyz", interpreter="/bin/sh")

    assert found is not None
    assert found.endswith("/sh")
    assert missing is None
