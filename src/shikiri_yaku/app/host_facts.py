"""The facts about this host that the Reasoner's prompt carries: OS, package managers, PATH.

One script runs in the user's login shell, so PATH and the tools match the PO's terminal. A fact
that cannot be read becomes a line that says so: collecting the facts never raises.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from shikiri_yaku.domain.recipes import RECIPES
from shikiri_yaku.ports import CommandError, HostObservation

if TYPE_CHECKING:
    from shikiri_yaku.ports import CommandRunner

PACKAGE_MANAGERS: Final = tuple(RECIPES)
_OS_TAG: Final = "os="
_MANAGER_TAG: Final = "manager="
_PATH_TAG: Final = "path="
_UNKNOWN: Final = "unknown"
_NONE: Final = "none"
_SCRIPT: Final = (
    f'echo "{_OS_TAG}$(uname -sr)"; '
    'for tool in "$@"; do '
    f'command -v "$tool" >/dev/null 2>&1 && echo "{_MANAGER_TAG}$tool"; '
    "done; "
    f'echo "{_PATH_TAG}$PATH"'
)


class HostFacts:
    """Collects the host facts through the CommandRunner, in the login shell `shell`."""

    def __init__(self, runner: CommandRunner, shell: str, timeout_s: float) -> None:
        self._runner = runner
        self._shell = shell
        self._timeout_s = timeout_s

    async def collect(self) -> HostObservation:
        argv = [self._shell, "-l", "-c", _SCRIPT, "sh", *PACKAGE_MANAGERS]
        try:
            result = await self._runner.run(argv, timeout_s=self._timeout_s)
        except CommandError as error:
            return HostObservation((f"host facts could not be read: {error}",), frozenset())
        if result.returncode != 0:
            code = result.returncode
            return HostObservation(
                (f"host facts could not be read: the login shell exited with code {code}",),
                frozenset(),
            )
        return _facts(result.stdout)


def _facts(output: str) -> HostObservation:
    systems = _tagged(output, _OS_TAG)
    managers = _tagged(output, _MANAGER_TAG)
    paths = _tagged(output, _PATH_TAG)
    return HostObservation(
        (
            f"OS: {systems[-1] if systems else _UNKNOWN}",
            f"package managers on this host: {', '.join(managers) or _NONE}",
            f"PATH: {paths[-1] if paths else _UNKNOWN}",
        ),
        frozenset(manager for manager in managers if manager in PACKAGE_MANAGERS),
    )


def _tagged(output: str, tag: str) -> list[str]:
    return [line[len(tag) :] for line in output.splitlines() if line.startswith(tag)]
