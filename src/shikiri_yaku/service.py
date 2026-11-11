"""The service unit that keeps one instance running under the OS service manager.

The renderers are pure: they say where a unit belongs and what it contains. write_service_file
touches the disk, and ServiceManager asks the service manager to start, stop, restart or report a
unit through the CommandRunner port. A unit runs the given command line with the PATH the caller
passes in, so shikiru and the agent CLIs are found when the service manager starts the process.
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
NOT_LOADED: Final = "not loaded"
RUNNING: Final = "running"
STOPPED: Final = "stopped"
# keep: launchctl print gives a loaded job's state on its own line; only that line says it runs
_LAUNCHD_RUNNING_LINE: Final = "state = running"
_SYSTEMD_ACTIVE: Final = "active"
_SYSTEMD_INACTIVE: Final = "inactive"
# keep: a unit the manager no longer loads is already off, so unloading it is not a failure
_LAUNCHD_NOT_LOADED: Final = ("No such process", "Could not find service")
_SYSTEMD_NOT_LOADED: Final = ("not loaded", "not enabled", "not running")


class ServiceError(Exception):
    """A service unit cannot be made or written; the message says why."""


@dataclass(frozen=True, slots=True)
class ServiceFile:
    """A unit's place on disk and its content."""

    path: Path
    content: bytes


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
        return ServiceFile(path, _launchd_plist(name, program, login_path))
    return ServiceFile(path, _systemd_unit(product, program, login_path).encode())


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
    """Starts, stops, restarts, reports and removes products' services through the service manager.

    Each action returns None when it finished, else why it did not. status returns None when the
    service runs, else the state it is in or why its state is unknown. uninstall returns
    NOT_INSTALLED when there is no unit to remove. A product whose unit was never written is not
    installed, and no command runs for it.
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

    async def install(self, product: str) -> str | None:
        name = service_name(product)
        if self._platform == LAUNCHD_PLATFORM:
            target = f"gui/{self._uid}/{name}"
            argv = ["launchctl", "print", target]
            try:
                result = await self._commands.run(argv, timeout_s=self._command_timeout_s)
            except CommandError as error:
                return str(error)
            if result.returncode == 0:
                problem = await self._run(product, ["launchctl", "bootout", target])
                if problem is not None:
                    return problem
            actions = [
                [
                    "launchctl",
                    "bootstrap",
                    f"gui/{self._uid}",
                    str(unit_path(self._platform, self._user_home, product)),
                ]
            ]
        else:
            actions = [
                ["systemctl", "--user", "daemon-reload"],
                ["systemctl", "--user", "enable", f"{name}.service"],
                ["systemctl", "--user", "restart", f"{name}.service"],
            ]
        for argv in actions:
            problem = await self._run(product, argv)
            if problem is not None:
                return problem
        return None

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

    async def status(self, product: str) -> str | None:
        if not self._is_installed(product):
            return NOT_INSTALLED
        if self._platform == LAUNCHD_PLATFORM:
            try:
                state = await self._launchd_state(product)
            except CommandError as error:
                return str(error)
            return None if state == RUNNING else state
        return await self._systemd_status(product)

    def _is_installed(self, product: str) -> bool:
        return unit_path(self._platform, self._user_home, product).is_file()

    async def uninstall(self, product: str) -> str | None:
        if not self._is_installed(product):
            return NOT_INSTALLED
        name = service_name(product)
        if self._platform == LAUNCHD_PLATFORM:
            problem = await self._unload(
                ["launchctl", "bootout", f"gui/{self._uid}/{name}"], _LAUNCHD_NOT_LOADED
            )
        else:
            problem = await self._unload(
                ["systemctl", "--user", "disable", "--now", f"{name}.service"],
                _SYSTEMD_NOT_LOADED,
            )
        if problem is not None:
            return problem
        try:
            remove_service_file(unit_path(self._platform, self._user_home, product))
        except ServiceError as error:
            return str(error)
        if self._platform == SYSTEMD_PLATFORM:
            return await self._command(["systemctl", "--user", "daemon-reload"])
        return None

    async def _unload(self, argv: list[str], not_loaded: Sequence[str]) -> str | None:
        try:
            result = await self._commands.run(argv, timeout_s=self._command_timeout_s)
        except CommandError as error:
            return str(error)
        if result.returncode == 0 or _only_says(result.stderr, not_loaded):
            return None
        return _failure_reason(result)

    async def _run(self, product: str, argv: list[str]) -> str | None:
        if not self._is_installed(product):
            return NOT_INSTALLED
        return await self._command(argv)

    async def _command(self, argv: list[str]) -> str | None:
        try:
            result = await self._commands.run(argv, timeout_s=self._command_timeout_s)
        except CommandError as error:
            return str(error)
        if result.returncode != 0:
            return _failure_reason(result)
        return None

    async def _systemd_status(self, product: str) -> str | None:
        # keep: is-active exits non-zero unless the unit is active, so its stdout is the answer
        argv = ["systemctl", "--user", "is-active", f"{service_name(product)}.service"]
        try:
            result = await self._commands.run(argv, timeout_s=self._command_timeout_s)
        except CommandError as error:
            return str(error)
        answer = result.stdout.strip()
        if answer == _SYSTEMD_ACTIVE:
            return None
        if answer == _SYSTEMD_INACTIVE:
            return STOPPED
        return answer or _failure_reason(result)

    async def _launchd_state(self, product: str) -> str:
        """Return the job's state from launchctl print; not loaded if the manager lacks the job."""
        argv = ["launchctl", "print", f"gui/{self._uid}/{service_name(product)}"]
        result = await self._commands.run(argv, timeout_s=self._command_timeout_s)
        if result.returncode != 0:
            return NOT_LOADED
        return RUNNING if _is_running(result.stdout) else STOPPED

    async def _wait_until_stopped(self, product: str) -> str | None:
        # keep: launchd keeps a stopped job loaded, so the job's state decides, not its presence
        deadline = self._clock.now() + timedelta(seconds=self._stop_timeout_s)
        while True:
            try:
                state = await self._launchd_state(product)
            except CommandError as error:
                return str(error)
            if state != RUNNING:
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


def remove_service_file(path: Path) -> None:
    """Delete a unit that install wrote; nothing else of the instance is touched."""
    try:
        path.unlink()
    except OSError as error:
        message = f"cannot remove {path}: {error}"
        raise ServiceError(message) from error


def _unsupported(platform: str) -> ServiceError:
    message = f"shikiri-yaku supports macOS (launchd) and Linux (systemd), not {platform!r}"
    return ServiceError(message)


def _failure_reason(result: CommandResult) -> str:
    lines = [line.strip() for line in result.stderr.splitlines() if line.strip()]
    return "; ".join(lines) or f"exit status {result.returncode}"


def _is_running(launchctl_print: str) -> bool:
    return any(line.strip() == _LAUNCHD_RUNNING_LINE for line in launchctl_print.splitlines())


def _only_says(stderr: str, phrases: Sequence[str]) -> bool:
    lines = [line for line in stderr.splitlines() if line.strip()]
    return bool(lines) and all(any(phrase in line for phrase in phrases) for line in lines)


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
