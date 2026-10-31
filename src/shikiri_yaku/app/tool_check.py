"""The existence check of design §6.3, step 2: `command -v` in the user's login shell.

The script is fixed and the tool name reaches it as an argument, never as part of the script text.
A name that is not a valid executable name is refused before the shell starts.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from shikiri_yaku.domain.recipes import is_tool_name

if TYPE_CHECKING:
    from shikiri_yaku.ports import CommandRunner

_SCRIPT: Final = 'command -v "$1"'
_SCRIPT_NAME: Final = "sh"


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

        A CommandError from the runner (the shell did not start, or it timed out) is not caught.
        """
        if not is_tool_name(tool):
            message = "the tool name is not a valid executable name; no check runs for it"
            raise ToolNameError(message)
        argv = [self._shell, "-l", "-c", _SCRIPT, _SCRIPT_NAME, tool]
        result = await self._runner.run(argv, timeout_s=self._timeout_s)
        if result.returncode != 0:
            return None
        return _last_line(result.stdout)


def _last_line(stdout: str) -> str | None:
    lines = stdout.strip().splitlines()
    return lines[-1] if lines else None
