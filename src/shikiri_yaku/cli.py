from __future__ import annotations

import argparse
import asyncio
import os
import re
import shutil
import signal
import stat
import sys
from dataclasses import dataclass
from functools import partial
from importlib.metadata import version
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Protocol

from shikiri_yaku.instance import (
    InstanceLockedError,
    InstanceLockError,
    instance_dir,
    instance_lock,
)
from shikiri_yaku.ports import CompositionError, ShikiruError
from shikiri_yaku.service import ServiceError, service_file, write_service_file
from shikiri_yaku.settings import NAME_PATTERN, SettingsError, load_settings

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Coroutine, Mapping, Sequence

    from shikiri_yaku.service import ServiceFile
    from shikiri_yaku.settings import Settings

_PROGRAM = "shikiri-yaku"
TOKEN_MODE: Final = 0o600


class Lifecycle(Protocol):
    """A composed instance, as the command line sees it: it is closed when the command ends."""

    @property
    def link_probes(self) -> Mapping[str, Callable[[], Awaitable[None]]]: ...

    @property
    def jobs(self) -> Sequence[Job]: ...

    def close(self) -> None: ...


type Job = Callable[[], Coroutine[Any, Any, None]]
type Compose = Callable[[Settings, Path], Lifecycle]


@dataclass(frozen=True, slots=True)
class _Check:
    """One line of the doctor report: a failure names its problem; a pass may say more than ok."""

    name: str
    problem: str | None = None
    status: str = "ok"


def main(argv: Sequence[str] | None, compose: Compose) -> int:
    """Run one command. The caller passes the composition, so this module builds nothing."""
    parser = argparse.ArgumentParser(prog=_PROGRAM)
    parser.add_argument("--version", action="version", version=f"%(prog)s {version(_PROGRAM)}")
    commands = parser.add_subparsers(required=True)
    run = commands.add_parser("run", help="serve one product until SIGTERM")
    _add_product(run)
    run.set_defaults(command=partial(_run, compose=compose))
    doctor = commands.add_parser("doctor", help="check one product's instance")
    _add_product(doctor)
    doctor.set_defaults(command=partial(_doctor, compose=compose))
    install = commands.add_parser("install-service", help="write the service unit of one product")
    _add_product(install)
    install.set_defaults(command=_install_service)
    arguments = parser.parse_args(argv)
    return arguments.command(arguments)


def _add_product(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("product", type=_product_name)
    parser.add_argument("--home", type=Path, help="parent of the instance directory")


def _product_name(value: str) -> str:
    if re.fullmatch(NAME_PATTERN, value) is None:
        message = f"invalid product name {value!r}: use letters, digits, '-' and '_'"
        raise argparse.ArgumentTypeError(message)
    return value


def _run(arguments: argparse.Namespace, compose: Compose) -> int:
    directory = instance_dir(arguments.product, arguments.home)
    try:
        settings = load_settings(directory)
        with instance_lock(directory):
            composition = compose(settings, directory)
            try:
                asyncio.run(_serve(composition.jobs))
            finally:
                composition.close()
    except (SettingsError, InstanceLockError, CompositionError, ShikiruError) as error:
        sys.stderr.write(f"{error}\n")
        return 1
    return 0


async def _wait_for(event: asyncio.Event) -> None:
    await event.wait()


async def _serve(jobs: Sequence[Job]) -> None:
    """Run the jobs until a stop signal; a job that fails ends the service with its error."""
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for number in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(number, stop.set)
    running = [asyncio.create_task(job()) for job in jobs]
    waiting = asyncio.create_task(_wait_for(stop))
    try:
        await asyncio.wait([waiting, *running], return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in (waiting, *running):
            task.cancel()
        outcomes = await asyncio.gather(*running, waiting, return_exceptions=True)
    for outcome in outcomes:
        if isinstance(outcome, Exception):
            raise outcome


def _doctor(arguments: argparse.Namespace, compose: Compose) -> int:
    directory = instance_dir(arguments.product, arguments.home)
    try:
        settings = load_settings(directory)
    except SettingsError as error:
        return _report([_Check("config", str(error))])
    return _report(
        [
            _Check("components", _components_problem(settings)),
            _Check("token file", _token_problem(settings.telegram.token_file)),
            _lock_check(directory),
            *_composed_checks(settings, directory, compose),
        ]
    )


def _report(checks: Sequence[_Check]) -> int:
    for check in checks:
        if check.problem is None:
            sys.stdout.write(f"{check.name}: {check.status}\n")
        else:
            sys.stdout.write(f"{check.name}: FAILED: {check.problem}\n")
    return 1 if any(check.problem is not None for check in checks) else 0


def _components_problem(settings: Settings) -> str | None:
    missing = [
        str(component.path)
        for component in settings.components
        if not _is_directory(component.path)
    ]
    return f"not a directory: {', '.join(missing)}" if missing else None


def _is_directory(path: Path) -> bool:
    try:
        return path.is_dir()
    except OSError:
        return False


def _token_problem(token: Path) -> str | None:
    try:
        status = token.stat()
    except FileNotFoundError:
        return f"{token} does not exist"
    except OSError as error:
        return f"{token} cannot be read: {error}"
    if not stat.S_ISREG(status.st_mode):
        return f"{token} is not a regular file"
    mode = stat.S_IMODE(status.st_mode)
    return None if mode == TOKEN_MODE else f"{token} has mode {mode:04o}, must be {TOKEN_MODE:04o}"


def _lock_check(directory: Path) -> _Check:
    try:
        with instance_lock(directory):
            return _Check("instance lock")
    except InstanceLockedError:
        return _Check("instance lock", status="running")
    except InstanceLockError as error:
        return _Check("instance lock", str(error))


def _composed_checks(settings: Settings, directory: Path, compose: Compose) -> list[_Check]:
    try:
        composition = compose(settings, directory)
    except CompositionError as error:
        return [_Check("journal", str(error))]
    try:
        links = [
            _Check(f"link {name}", _link_problem(probe))
            for name, probe in composition.link_probes.items()
        ]
    finally:
        composition.close()
    return [*links, _Check("journal")]


def _link_problem(probe: Callable[[], Awaitable[None]]) -> str | None:
    try:
        asyncio.run(probe())
    except ShikiruError as error:
        return str(error) or type(error).__name__
    return None


def _install_service(arguments: argparse.Namespace) -> int:
    directory = instance_dir(arguments.product, arguments.home)
    login_path = os.environ.get("PATH", "")
    try:
        load_settings(directory)
        service = _service(arguments, login_path)
        write_service_file(service)
    except (SettingsError, ServiceError) as error:
        sys.stderr.write(f"{error}\n")
        return 1
    sys.stdout.write(f"wrote {service.path}\nstart it with: {service.start}\n")
    return 0


def _service(arguments: argparse.Namespace, login_path: str) -> ServiceFile:
    executable = shutil.which(_PROGRAM, path=login_path)
    if executable is None:
        message = f"{_PROGRAM} is not on PATH, so a service could not find it"
        raise ServiceError(message)
    home_arguments = [] if arguments.home is None else ["--home", str(arguments.home.absolute())]
    program = [str(Path(executable).absolute()), "run", arguments.product, *home_arguments]
    return service_file(sys.platform, Path.home(), arguments.product, program, login_path)
