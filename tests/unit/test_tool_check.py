"""The existence check: `command -v` in the login shell, the name as an argument (design §6.3)."""

from __future__ import annotations

import asyncio
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
        _find(_Shell(stdout="/opt/homebrew/bin/ffmpeg\n"), "ffmpeg") == "/opt/homebrew/bin/ffmpeg"
    )


def test_a_missing_tool_gives_none() -> None:
    assert _find(_Shell(returncode=1), "ffmpeg") is None


def test_output_the_profile_prints_before_the_path_is_skipped() -> None:
    assert _find(_Shell(stdout="Welcome back\n/usr/bin/git\n"), "git") == "/usr/bin/git"


def test_the_name_is_the_last_argument_and_never_part_of_the_script() -> None:
    runner = _Shell(stdout="/bin/ffmpeg\n")

    _find(runner, "ffmpeg")

    assert runner.argv[:3] == [SHELL, "-l", "-c"]
    assert runner.argv[-1] == "ffmpeg"
    assert "ffmpeg" not in runner.argv[3]


@pytest.mark.parametrize("name", HOSTILE_NAMES)
def test_a_hostile_name_is_refused_before_the_shell_starts(name: str) -> None:
    runner = _Shell(stdout="/bin/x\n")

    with pytest.raises(ToolNameError):
        _find(runner, name)

    assert runner.argv == []


def test_a_runner_error_crosses_to_the_caller() -> None:
    runner = _Shell(error=CommandError("the shell did not start"))

    with pytest.raises(CommandError, match="did not start"):
        _find(runner, "ffmpeg")


def test_the_real_login_shell_finds_sh_and_reports_a_missing_tool() -> None:
    found = _find(ProcessCommandRunner(), "sh", interpreter="/bin/sh")
    missing = _find(ProcessCommandRunner(), "shikiri-no-such-tool-xyz", interpreter="/bin/sh")

    assert found is not None
    assert found.endswith("/sh")
    assert missing is None
