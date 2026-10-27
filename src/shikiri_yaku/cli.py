from __future__ import annotations

import argparse
import asyncio
import signal
import stat
import sys
from importlib.metadata import version
from pathlib import Path
from typing import TYPE_CHECKING, Final, Protocol

from shikiri_yaku.instance import InstanceLockedError, instance_dir, instance_lock
from shikiri_yaku.ports import CompositionError
from shikiri_yaku.settings import SettingsError, load_settings

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from shikiri_yaku.settings import Settings

_PROGRAM = "shikiri-yaku"
TOKEN_MODE: Final = 0o600


class Lifecycle(Protocol):
    """A composed instance, as the command line sees it: it is closed when the command ends."""

    def close(self) -> None: ...


type Compose = Callable[[Settings, Path], Lifecycle]


def main(argv: Sequence[str] | None, compose: Compose) -> int:
    """Run one command. The caller passes the composition, so this module builds nothing."""
    parser = argparse.ArgumentParser(prog=_PROGRAM)
    parser.add_argument("--version", action="version", version=f"%(prog)s {version(_PROGRAM)}")
    commands = parser.add_subparsers(required=True)
    run = commands.add_parser("run", help="serve one product until SIGTERM")
    _add_product(run)
    run.set_defaults(command=_run)
    doctor = commands.add_parser("doctor", help="check one product's instance")
    _add_product(doctor)
    doctor.set_defaults(command=_doctor)
    arguments = parser.parse_args(argv)
    return arguments.command(instance_dir(arguments.product, arguments.home), compose)


def _add_product(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("product")
    parser.add_argument("--home", type=Path, help="parent of the instance directory")


def _run(directory: Path, compose: Compose) -> int:
    try:
        settings = load_settings(directory)
        with instance_lock(directory):
            composition = compose(settings, directory)
            try:
                asyncio.run(_serve())
            finally:
                composition.close()
    except (SettingsError, InstanceLockedError, CompositionError) as error:
        sys.stderr.write(f"{error}\n")
        return 1
    return 0


async def _serve() -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for number in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(number, stop.set)
    await stop.wait()


def _doctor(directory: Path, compose: Compose) -> int:
    try:
        settings = load_settings(directory)
    except SettingsError as error:
        return _report([("config", str(error))])
    return _report(
        [
            ("components", _components_problem(settings)),
            ("token file", _token_problem(settings.telegram.token_file)),
            ("instance lock", _lock_problem(directory)),
            ("journal", _journal_problem(settings, directory, compose)),
        ]
    )


def _report(results: Sequence[tuple[str, str | None]]) -> int:
    for name, problem in results:
        sys.stdout.write(f"{name}: ok\n" if problem is None else f"{name}: FAILED: {problem}\n")
    return 1 if any(problem is not None for _, problem in results) else 0


def _components_problem(settings: Settings) -> str | None:
    missing = [
        str(component.path) for component in settings.components if not component.path.is_dir()
    ]
    return f"not a directory: {', '.join(missing)}" if missing else None


def _token_problem(token: Path) -> str | None:
    try:
        mode = stat.S_IMODE(token.stat().st_mode)
    except FileNotFoundError:
        return f"{token} does not exist"
    return None if mode == TOKEN_MODE else f"{token} has mode {mode:04o}, must be {TOKEN_MODE:04o}"


def _lock_problem(directory: Path) -> str | None:
    try:
        with instance_lock(directory):
            return None
    except InstanceLockedError as error:
        return str(error)


def _journal_problem(settings: Settings, directory: Path, compose: Compose) -> str | None:
    try:
        composition = compose(settings, directory)
    except CompositionError as error:
        return str(error)
    composition.close()
    return None
