"""The service unit that keeps one instance running under the OS service manager.

The renderers are pure: they say where a unit belongs and what it contains. write_service_file
touches the disk, and ServiceManager asks the service manager to start, stop or restart a unit
through the CommandRunner port. A unit runs the given command line with the PATH the caller passes
in, so shikiru and the agent CLIs are found when the service manager starts the process.
"""

from __future__ import annotations

import plistlib
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Final

from shikiri_yaku.ports import CommandError

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from shikiri_yaku.ports import Clock, CommandResult, CommandRunner

LAUNCHD_PLATFORM: Final = "darwin"
SYSTEMD_PLATFORM: Final = "linux"
SERVICE_PREFIX: Final = "shikiri-yaku-"
NOT_INSTALLED: Final = "not installed"
# keep: launchctl print gives a loaded job's state on its own line; only that line says it runs
_LAUNCHD_RUNNING_LINE: Final = "state = running"


class ServiceError(Exception):
    """A service unit cannot be made or written; the message says why."""


@dataclass(frozen=True, slots=True)
class ServiceFile:
    """A unit's place on disk, its content, and the command that starts it."""

    path: Path
    content: bytes
    start: str


def service_name(product: str) -> str:
    """Return the name the service manager knows the product's instance by."""
    return f"{SERVICE_PREFIX}{product}"


def unit_path(platform: str, user_home: Path, product: str) -> Path:
    """Return where the platform's service manager keeps the product's unit."""
    name = service_name(product)
    if platform == LAUNCHD_PLATFORM:
        return user_home / "Library" / "LaunchAgents" / f"{name}.plist"
    if platform == SYSTEMD_PLATFORM:
        return user_home / ".config" / "systemd" / "user" / f"{name}.service"
    raise _unsupported(platform)


def service_file(
    platform: str,
    user_home: Path,
    product: str,
    program: Sequence[str],
    login_path: str,
) -> ServiceFile:
    """Return the unit of this platform that runs `program` with `login_path` as its PATH."""
    name = service_name(product)
    path = unit_path(platform, user_home, product)
    if platform == LAUNCHD_PLATFORM:
        start = f"launchctl bootstrap gui/$(id -u) {path}"
        return ServiceFile(path, _launchd_plist(name, program, login_path), start)
    start = f"systemctl --user enable --now {name}.service"
    return ServiceFile(path, _systemd_unit(product, program, login_path).encode(), start)


def start_argv(platform: str, product: str, uid: int) -> list[str]:
    """Return the service manager's command that starts the product's unit, if it is not running."""
    name = service_name(product)
    if platform == LAUNCHD_PLATFORM:
        return ["launchctl", "kickstart", f"gui/{uid}/{name}"]
    if platform == SYSTEMD_PLATFORM:
        return ["systemctl", "--user", "start", f"{name}.service"]
    raise _unsupported(platform)


def stop_argv(platform: str, product: str, uid: int) -> list[str]:
    """Return the service manager's command that asks the product's unit to stop."""
    name = service_name(product)
    if platform == LAUNCHD_PLATFORM:
        return ["launchctl", "kill", "SIGTERM", f"gui/{uid}/{name}"]
    if platform == SYSTEMD_PLATFORM:
        return ["systemctl", "--user", "stop", f"{name}.service"]
    raise _unsupported(platform)


def restart_argv(platform: str, product: str, uid: int) -> list[str]:
    """Return the service manager's command that restarts the product's running unit."""
    name = service_name(product)
    if platform == LAUNCHD_PLATFORM:
        return ["launchctl", "kickstart", "-k", f"gui/{uid}/{name}"]
    if platform == SYSTEMD_PLATFORM:
        return ["systemctl", "--user", "restart", f"{name}.service"]
    raise _unsupported(platform)


class ServiceManager:
    """Starts, stops and restarts products' services through the platform's service manager.

    Each method returns None when the action finished, else why it did not. A product whose unit
    was never written is not installed, and no command runs for it.
    """

    def __init__(
        self,
        commands: CommandRunner,
        clock: Clock,
        platform: str,
        user_home: Path,
        uid: int,
        *,
        command_timeout_s: float,
        stop_timeout_s: float,
        stop_poll_s: float,
    ) -> None:
        self._commands = commands
        self._clock = clock
        self._platform = platform
        self._user_home = user_home
        self._uid = uid
        self._command_timeout_s = command_timeout_s
        self._stop_timeout_s = stop_timeout_s
        self._stop_poll_s = stop_poll_s

    async def start(self, product: str) -> str | None:
        return await self._run(product, start_argv(self._platform, product, self._uid))

    async def restart(self, product: str) -> str | None:
        return await self._run(product, restart_argv(self._platform, product, self._uid))

    async def stop(self, product: str) -> str | None:
        problem = await self._run(product, stop_argv(self._platform, product, self._uid))
        # keep: systemctl stop returns only once the unit has stopped
        if problem is not None or self._platform == SYSTEMD_PLATFORM:
            return problem
        return await self._wait_until_stopped(product)

    async def _run(self, product: str, argv: list[str]) -> str | None:
        if not unit_path(self._platform, self._user_home, product).is_file():
            return NOT_INSTALLED
        try:
            result = await self._commands.run(argv, timeout_s=self._command_timeout_s)
        except CommandError as error:
            return str(error)
        if result.returncode != 0:
            return _failure_reason(result)
        return None

    async def _wait_until_stopped(self, product: str) -> str | None:
        # keep: launchd keeps a stopped job loaded, so the job's state decides, not its presence
        argv = ["launchctl", "print", f"gui/{self._uid}/{service_name(product)}"]
        deadline = self._clock.now() + timedelta(seconds=self._stop_timeout_s)
        while True:
            try:
                result = await self._commands.run(argv, timeout_s=self._command_timeout_s)
            except CommandError as error:
                return str(error)
            if result.returncode != 0 or not _is_running(result.stdout):
                return None
            remaining = deadline - self._clock.now()
            if remaining <= timedelta():
                return f"still running {self._stop_timeout_s:g} seconds after the stop"
            await self._clock.sleep(min(self._stop_poll_s, remaining.total_seconds()))


def write_service_file(service: ServiceFile) -> None:
    """Write the unit, creating its directory and replacing an earlier unit of the same name."""
    try:
        service.path.parent.mkdir(parents=True, exist_ok=True)
        service.path.write_bytes(service.content)
    except OSError as error:
        message = f"cannot write {service.path}: {error}"
        raise ServiceError(message) from error


def _unsupported(platform: str) -> ServiceError:
    message = f"shikiri-yaku supports macOS (launchd) and Linux (systemd), not {platform!r}"
    return ServiceError(message)


def _failure_reason(result: CommandResult) -> str:
    lines = [line.strip() for line in result.stderr.splitlines() if line.strip()]
    return "; ".join(lines) or f"exit status {result.returncode}"


def _is_running(launchctl_print: str) -> bool:
    return any(line.strip() == _LAUNCHD_RUNNING_LINE for line in launchctl_print.splitlines())


def _launchd_plist(name: str, program: Sequence[str], login_path: str) -> bytes:
    return plistlib.dumps(
        {
            "Label": name,
            "ProgramArguments": list(program),
            "EnvironmentVariables": {"PATH": login_path},
            "RunAtLoad": True,
            "KeepAlive": {"SuccessfulExit": False},
        }
    )


def _systemd_unit(product: str, program: Sequence[str], login_path: str) -> str:
    command = f":{_quoted(program[0])} " + " ".join(_quoted(word) for word in program[1:])
    path_setting = _quoted(f"PATH={login_path}")
    return (
        "[Unit]\n"
        f"Description=shikiri-yaku for {product}\n"
        "\n"
        "[Service]\n"
        f"ExecStart={command}\n"
        f"Environment={path_setting}\n"
        "Restart=on-failure\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def _quoted(word: str) -> str:
    escaped = word.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    return f'"{escaped}"'
