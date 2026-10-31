"""The host facts of the Reasoner's prompt: read in the login shell, and never raising (design §5).

The shell is a fake except in one test, which runs the script in `sh` with read-only commands.
"""

from __future__ import annotations

import asyncio
import platform
from typing import TYPE_CHECKING, Final, override

from shikiri_yaku.adapters.commands import ProcessCommandRunner
from shikiri_yaku.app.host_facts import PACKAGE_MANAGERS, HostFacts
from shikiri_yaku.ports import CommandError, CommandResult, CommandRunner, HostObservation

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

SHELL: Final = "/bin/zsh"
TIMEOUT_S: Final = 5
OUTPUT: Final = "os=Darwin 25.3.0\nmanager=brew\nmanager=uv\npath=/opt/homebrew/bin:/usr/bin\n"


class _Shell(CommandRunner):
    """A login shell that prints `stdout`, exits with `returncode`, or cannot start at all."""

    def __init__(
        self, *, stdout: str = OUTPUT, returncode: int = 0, error: CommandError | None = None
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


def _collect(runner: CommandRunner, interpreter: str = SHELL) -> HostObservation:
    return asyncio.run(HostFacts(runner, interpreter, TIMEOUT_S).collect())


def test_the_script_runs_in_the_login_shell_with_the_package_managers_as_arguments() -> None:
    runner = _Shell()

    _collect(runner)

    assert runner.argv[:3] == [SHELL, "-l", "-c"]
    assert runner.argv[-len(PACKAGE_MANAGERS) :] == list(PACKAGE_MANAGERS)


def test_the_facts_give_the_os_the_installed_managers_and_the_path() -> None:
    facts = _collect(_Shell())
    joined = "\n".join(facts.prompt)

    assert "Darwin 25.3.0" in joined
    assert "/opt/homebrew/bin:/usr/bin" in joined
    assert "brew" in joined
    assert "uv" in joined
    assert "npm" not in joined
    assert facts.managers == frozenset({"brew", "uv"})


def test_a_shell_that_cannot_start_gives_one_line_that_names_the_error() -> None:
    facts = _collect(_Shell(error=CommandError("the shell did not start")))

    assert len(facts.prompt) == 1
    assert "did not start" in facts.prompt[0]
    assert facts.managers == frozenset()


def test_a_shell_that_exits_with_an_error_gives_one_line_and_does_not_raise() -> None:
    facts = _collect(_Shell(returncode=2))

    assert len(facts.prompt) == 1


def test_the_script_reports_the_os_when_a_real_sh_runs_it() -> None:
    facts = _collect(ProcessCommandRunner(), interpreter="/bin/sh")

    assert f"{platform.system()} {platform.release()}" in facts.prompt[0]
