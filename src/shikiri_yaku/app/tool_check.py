"""The existence check of design §6.3, step 2: `command -v` in the user's login shell.

The script is fixed and the tool name reaches it as an argument, never as part of the script text.
A name that is not a valid executable name is refused before the shell starts. The script always
prints one of two marked answers, so a shell that fails for any other reason is an error and is
never read as a missing tool.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from shikiri_yaku.domain.recipes import is_tool_name
from shikiri_yaku.ports import CommandError

if TYPE_CHECKING:
    from shikiri_yaku.ports import CommandRunner

# keep: zsh ties the name `path` to PATH, so the script must not assign it
_SCRIPT: Final = (
    'if location=$(command -v "$1"); then echo "found=$location"; else echo missing; fi'
)
_SCRIPT_NAME: Final = "sh"
_FOUND: Final = "found="
_MISSING: Final = "missing"


class ToolNameError(ValueError):
    """The tool name is not a valid executable name, so no check runs for it."""


class ToolCheck:
    """Finds a tool on the PATH of the login shell `shell`, through the CommandRunner."""

    def __init__(self, runner: CommandRunner, shell: str, timeout_s: float) -> None:
        self._runner = runner
        self._shell = shell
        self._timeout_s = timeout_s

    async def find(self, tool: str) -> str | None:
        """Return the path the shell finds for `tool`, or None when the tool is missing.

        Raises CommandError when the runner fails (the shell did not start, or it timed out), when
        the shell exits abnormally, or when it prints no answer. None of those says the tool is
        missing, so none of them is read as one.
        """
        if not is_tool_name(tool):
            message = "the tool name is not a valid executable name; no check runs for it"
            raise ToolNameError(message)
        argv = [self._shell, "-l", "-c", _SCRIPT, _SCRIPT_NAME, tool]
        result = await self._runner.run(argv, timeout_s=self._timeout_s)
        if result.returncode != 0:
            message = f"the login shell exited with status {result.returncode} during the check"
            raise CommandError(message)
        return _answer(result.stdout)


def _answer(stdout: str) -> str | None:
    lines = stdout.strip().splitlines()
    last = lines[-1] if lines else ""
    if last == _MISSING:
        return None
    if last.startswith(_FOUND) and len(last) > len(_FOUND):
        return last.removeprefix(_FOUND)
    message = "the login shell printed no answer for the check"
    raise CommandError(message)
