"""The service unit that keeps one instance running under the OS service manager.

The renderers are pure: they say where a unit belongs and what it contains. write_service_file
touches the disk, and restart_product asks the service manager to restart a unit through the
CommandRunner port. A unit runs the given command line with the PATH the caller passes in, so
shikiru and the agent CLIs are found when the service manager starts the process.
"""

from __future__ import annotations

import plistlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from shikiri_yaku.ports import CommandError

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from shikiri_yaku.ports import CommandResult, CommandRunner

LAUNCHD_PLATFORM: Final = "darwin"
SYSTEMD_PLATFORM: Final = "linux"
SERVICE_PREFIX: Final = "shikiri-yaku-"


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


def restart_argv(platform: str, product: str, uid: int) -> list[str]:
    """Return the service manager's command that restarts the product's running unit."""
    name = service_name(product)
    if platform == LAUNCHD_PLATFORM:
        return ["launchctl", "kickstart", "-k", f"gui/{uid}/{name}"]
    if platform == SYSTEMD_PLATFORM:
        return ["systemctl", "--user", "restart", f"{name}.service"]
    raise _unsupported(platform)


async def restart_product(
    commands: CommandRunner,
    platform: str,
    user_home: Path,
    uid: int,
    product: str,
    *,
    timeout_s: float,
) -> str | None:
    """Restart the product's service; return None when it restarted, else why it did not.

    A product whose unit was never written is not installed, and no command runs for it.
    """
    if not unit_path(platform, user_home, product).is_file():
        return "not installed"
    argv = restart_argv(platform, product, uid)
    try:
        result = await commands.run(argv, timeout_s=timeout_s)
    except CommandError as error:
        return str(error)
    if result.returncode != 0:
        return _failure_reason(result)
    return None


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
