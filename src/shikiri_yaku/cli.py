"""The command line: one Typer app whose global options come before the verb (design §3.1)."""

from __future__ import annotations

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
from typing import TYPE_CHECKING, Annotated, Any, Final, Protocol, cast

import typer
import typer.rich_utils as typer_rich_utils
from typer.main import get_command

from shikiri_yaku.cli_render import (
    CheckResult,
    doctor_document,
    print_doctor,
    print_failure,
    print_json,
    print_stats,
    stats_document,
)
from shikiri_yaku.instance import (
    InstanceLockedError,
    InstanceLockError,
    home_dir,
    instance_dir,
    instance_lock,
)
from shikiri_yaku.ports import (
    ChannelError,
    ChannelRepairError,
    ChannelStartupError,
    CompositionError,
    ReasonerError,
    ShikiruError,
)
from shikiri_yaku.service import ServiceError, service_file, write_service_file
from shikiri_yaku.settings import NAME_PATTERN, SettingsError, load_settings, product_names

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Coroutine, Mapping, Sequence

    from shikiri_yaku.app.stats import Measures
    from shikiri_yaku.service import ServiceFile
    from shikiri_yaku.settings import Settings

_PROGRAM = "shikiri-yaku"
TOKEN_MODE: Final = 0o600
# keep: the effect of allow_other_members as the PO reads it in doctor's output (design §13)
OTHER_MEMBERS_NOTE: Final = "Other members of the group can read the messages; only owners can act"
_THREAD_ID_PATTERN: Final = r"[1-9][0-9]*"
_DESCRIPTION: Final = "An autonomous Operator for product development with Shikiru."
_BOLD: Final = "bold"
_DIM: Final = "dim"
_STEEL_BLUE: Final = "#83bddd"
_PRIMARY_TEXT: Final = "#dce2e9"
_SECONDARY_TEXT: Final = "#9aa5b5"
_BORDER: Final = "#3d4654"
_ALERT: Final = "#e3a0a0"


class Lifecycle(Protocol):
    """A composed instance, as the command line sees it: it is closed when the command ends."""

    @property
    def link_probes(self) -> Mapping[str, Callable[[], Awaitable[None]]]: ...

    @property
    def telegram_probe(self) -> Callable[[], Awaitable[None]]: ...

    @property
    def rung_probes(self) -> Mapping[str, Callable[[], Awaitable[None]]]: ...

    @property
    def jobs(self) -> Sequence[Job]: ...

    @property
    def recover(self) -> Callable[[], None]: ...

    def close(self) -> None: ...


type Job = Callable[[], Coroutine[Any, Any, None]]
type Compose = Callable[[Settings, Path], Lifecycle]
type ReadMeasures = Callable[[Path], Measures]
type AdoptTopic = Callable[[Path, str, int], str]
type ForgetTopic = Callable[[Path, str], str]


@dataclass(frozen=True, slots=True)
class _Check:
    """One line of the doctor report: a failure names its problem; a pass may say more than ok."""

    name: str
    problem: str | None = None
    status: str = "ok"


@dataclass(frozen=True, slots=True)
class _Global:
    """The global options, as every verb reads them from the root context."""

    product: str | None
    home: Path | None
    json_output: bool


@dataclass(frozen=True, slots=True)
class _Target:
    """The instance a verb works on: its product, the home it was given under, and its directory."""

    product: str
    home: Path | None
    directory: Path


def _product_option(value: str | None) -> str | None:
    if value is not None and re.fullmatch(NAME_PATTERN, value) is None:
        message = f"invalid product name {value!r}: use letters, digits, '-' and '_'"
        raise typer.BadParameter(message)
    return value


def _thread_id(value: str) -> str:
    if re.fullmatch(_THREAD_ID_PATTERN, value) is None:
        message = f"invalid thread id {value!r}: use a positive whole number"
        raise typer.BadParameter(message)
    return value


def _show_version(*, value: bool) -> None:
    if value:
        sys.stdout.write(f"{_PROGRAM} {version(_PROGRAM)}\n")
        raise typer.Exit


def _configure_help_palette() -> None:
    """Colour the help as Shikiru's is; typer reads these settings each time it renders help."""
    typer_rich_utils.STYLE_OPTION = f"{_BOLD} {_STEEL_BLUE}"
    typer_rich_utils.STYLE_SWITCH = f"{_BOLD} {_STEEL_BLUE}"
    typer_rich_utils.STYLE_NEGATIVE_OPTION = f"{_BOLD} {_ALERT}"
    typer_rich_utils.STYLE_NEGATIVE_SWITCH = f"{_BOLD} {_ALERT}"
    typer_rich_utils.STYLE_TYPES = _SECONDARY_TEXT
    typer_rich_utils.STYLE_TYPES_SEPARATOR = f"{_DIM} {_SECONDARY_TEXT}"
    typer_rich_utils.STYLE_USAGE = _STEEL_BLUE
    typer_rich_utils.STYLE_USAGE_COMMAND = f"{_BOLD} {_PRIMARY_TEXT}"
    typer_rich_utils.STYLE_HELPTEXT_FIRST_LINE = _PRIMARY_TEXT
    typer_rich_utils.STYLE_HELPTEXT = f"{_DIM} {_SECONDARY_TEXT}"
    typer_rich_utils.STYLE_OPTION_HELP = _PRIMARY_TEXT
    typer_rich_utils.STYLE_OPTION_DEFAULT = f"{_DIM} {_SECONDARY_TEXT}"
    typer_rich_utils.STYLE_OPTION_ENVVAR = f"{_DIM} {_SECONDARY_TEXT}"
    typer_rich_utils.STYLE_REQUIRED_SHORT = _ALERT
    typer_rich_utils.STYLE_REQUIRED_LONG = f"{_DIM} {_ALERT}"
    typer_rich_utils.STYLE_OPTIONS_PANEL_BORDER = f"{_DIM} {_BORDER}"
    typer_rich_utils.STYLE_COMMANDS_PANEL_BORDER = f"{_DIM} {_BORDER}"
    typer_rich_utils.ARGUMENTS_PANEL_TITLE = f"[{_BOLD} not dim {_PRIMARY_TEXT}]Arguments[/]"
    typer_rich_utils.OPTIONS_PANEL_TITLE = f"[{_BOLD} not dim {_PRIMARY_TEXT}]Options[/]"
    typer_rich_utils.COMMANDS_PANEL_TITLE = f"[{_BOLD} not dim {_PRIMARY_TEXT}]Commands[/]"
    typer_rich_utils.STYLE_COMMANDS_TABLE_FIRST_COLUMN = f"{_BOLD} {_STEEL_BLUE}"
    typer_rich_utils.STYLE_ERRORS_PANEL_BORDER = _ALERT
    typer_rich_utils.STYLE_ERRORS_SUGGESTION = f"{_DIM} {_SECONDARY_TEXT}"
    typer_rich_utils.STYLE_ABORTED = _ALERT


PRODUCT_OPTION = Annotated[
    str | None,
    typer.Option(
        "--product",
        metavar="NAME",
        callback=_product_option,
        help="The product to work on. [bold]Default:[/] the only product under --home",
    ),
]
HOME_OPTION = Annotated[
    Path | None,
    typer.Option(
        "--home",
        metavar="PATH",
        help=(
            "The directory that holds the product directories. [bold]Default:[/] ~/.shikiri-yaku"
        ),
    ),
]
VERSION_OPTION = Annotated[
    bool,
    typer.Option(
        "--version",
        callback=_show_version,
        is_eager=True,
        help="Show the version and exit. [bold]Default:[/] off",
    ),
]
JSON_OPTION = Annotated[
    bool,
    typer.Option(
        "--json",
        help=("Print doctor's and stats' output as one JSON object. [bold]Default:[/] off"),
    ),
]
COMPONENT_ARGUMENT = Annotated[
    str,
    typer.Argument(metavar="COMPONENT", help="A component's name, or general."),
]
THREAD_ID_ARGUMENT = Annotated[
    str,
    typer.Argument(
        metavar="THREAD_ID",
        callback=_thread_id,
        help="The thread of the topic in the forum, a positive whole number.",
    ),
]


def choose_product(given: str | None, found: Sequence[str], home: Path) -> str:
    """Return the product a verb works on: the one given, else the only one that home holds."""
    if given is not None:
        return given
    if len(found) == 1:
        return found[0]
    if not found:
        message = f"no product under {home}"
    else:
        message = f"several products under {home}: {', '.join(found)}"
    raise typer.BadParameter(message, param_hint="'--product'")


def _target(ctx: typer.Context) -> _Target:
    options = _options(ctx)
    home = home_dir(options.home)
    product = choose_product(options.product, product_names(home), home)
    return _Target(product, options.home, instance_dir(product, options.home))


def _options(ctx: typer.Context) -> _Global:
    return cast("_Global", ctx.find_root().obj)


def build_app(
    compose: Compose,
    read_measures: ReadMeasures,
    adopt_topic: AdoptTopic,
    forget_topic: ForgetTopic,
) -> typer.Typer:
    """Build the command line over the callables it is given; it builds no concrete object."""
    _configure_help_palette()
    app = typer.Typer(
        name=_PROGRAM,
        help=_DESCRIPTION,
        no_args_is_help=True,
        add_completion=False,
        rich_markup_mode="rich",
    )
    telegram = typer.Typer(no_args_is_help=True, rich_markup_mode="rich")
    app.add_typer(
        telegram, name="telegram", help="Repair the Telegram topic of a component or general."
    )

    @app.callback()
    def global_options(
        ctx: typer.Context,
        product: PRODUCT_OPTION = None,
        home: HOME_OPTION = None,
        *,
        _version: VERSION_OPTION = False,
        json_output: JSON_OPTION = False,
    ) -> None:
        ctx.obj = _Global(product, home, json_output)

    @app.command("run", help="Serve one product until SIGTERM.")
    def run(ctx: typer.Context) -> None:
        raise typer.Exit(_run(_target(ctx).directory, compose))

    @app.command(help="Check one product's instance.")
    def doctor(ctx: typer.Context) -> None:
        raise typer.Exit(
            _doctor(_target(ctx).directory, compose, as_json=_options(ctx).json_output)
        )

    @app.command("install-service", help="Write the service unit of one product.")
    def install_service(ctx: typer.Context) -> None:
        raise typer.Exit(_install_service(_target(ctx)))

    @app.command(help="Print the figures of one product's journal.")
    def stats(ctx: typer.Context) -> None:
        raise typer.Exit(
            _stats(_target(ctx).directory, read_measures, as_json=_options(ctx).json_output)
        )

    @telegram.command(
        "adopt",
        help="Mark a topic open with the thread it has: an uncertain one, or one made by hand.",
    )
    def adopt(
        ctx: typer.Context, component: COMPONENT_ARGUMENT, thread_id: THREAD_ID_ARGUMENT
    ) -> None:
        directory = _target(ctx).directory
        change = partial(adopt_topic, directory, component, int(thread_id))
        raise typer.Exit(_changed(directory, change))

    @telegram.command("forget", help="Delete an uncertain topic's row, so start-up creates one.")
    def forget(ctx: typer.Context, component: COMPONENT_ARGUMENT) -> None:
        directory = _target(ctx).directory
        raise typer.Exit(_changed(directory, partial(forget_topic, directory, component)))

    return app


def main(
    argv: Sequence[str] | None,
    compose: Compose,
    read_measures: ReadMeasures,
    adopt_topic: AdoptTopic,
    forget_topic: ForgetTopic,
) -> int:
    """Run one command line and return its exit code; the caller supplies what it needs."""
    command = get_command(build_app(compose, read_measures, adopt_topic, forget_topic))
    try:
        command.main(args=None if argv is None else list(argv), prog_name=_PROGRAM)
    except SystemExit as exit_:
        return exit_.code if isinstance(exit_.code, int) else 0
    return 0


async def _wait_for(event: asyncio.Event) -> None:
    await event.wait()


def _run(directory: Path, compose: Compose) -> int:
    try:
        settings = load_settings(directory)
        with instance_lock(directory):
            composition = compose(settings, directory)
            try:
                composition.recover()
                asyncio.run(_serve(composition.jobs))
            finally:
                composition.close()
    except (
        SettingsError,
        InstanceLockError,
        CompositionError,
        ShikiruError,
        ChannelError,
        ChannelStartupError,
    ) as error:
        print_failure(str(error))
        return 1
    return 0


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


def _doctor(directory: Path, compose: Compose, *, as_json: bool) -> int:
    try:
        settings = load_settings(directory)
    except SettingsError as error:
        return _report([_Check("config", str(error))], as_json=as_json)
    return _report(
        [
            _Check("components", _components_problem(settings)),
            _Check("token file", _token_problem(settings.telegram.token_file)),
            _lock_check(directory),
            *_composed_checks(settings, directory, compose),
            *_group_checks(settings),
        ],
        as_json=as_json,
    )


def _group_checks(settings: Settings) -> list[_Check]:
    """Name what other group members can see, when the config admits them."""
    if not settings.telegram.allow_other_members:
        return []
    return [_Check("group", status=OTHER_MEMBERS_NOTE)]


def _report(checks: Sequence[_Check], *, as_json: bool) -> int:
    results = [
        CheckResult(
            check.name,
            check.status if check.problem is None else check.problem,
            passed=check.problem is None,
        )
        for check in checks
    ]
    if as_json:
        print_json(doctor_document(results))
    else:
        print_doctor(results)
    return 0 if all(result.passed for result in results) else 1


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
            _Check(f"link {name}", _probe_problem(probe))
            for name, probe in composition.link_probes.items()
        ]
        rungs = [
            _Check(f"reasoner {name}", _probe_problem(probe))
            for name, probe in composition.rung_probes.items()
        ]
        telegram = _Check("telegram", _probe_problem(composition.telegram_probe))
    finally:
        composition.close()
    return [*links, *rungs, telegram, _Check("journal")]


def _probe_problem(probe: Callable[[], Awaitable[None]]) -> str | None:
    try:
        asyncio.run(probe())
    except (ShikiruError, ChannelError, ReasonerError) as error:
        return str(error) or type(error).__name__
    return None


def _stats(directory: Path, read_measures: ReadMeasures, *, as_json: bool) -> int:
    try:
        measures = read_measures(directory)
    except CompositionError as error:
        print_failure(str(error))
        return 1
    if as_json:
        print_json(stats_document(measures))
    else:
        print_stats(measures)
    return 0


def _changed(directory: Path, change: Callable[[], str]) -> int:
    """Make one topic change while holding the instance lock, so a running instance refuses it."""
    try:
        with instance_lock(directory):
            message = change()
    except (InstanceLockError, CompositionError, ChannelRepairError) as error:
        print_failure(str(error))
        return 1
    sys.stdout.write(f"{message}\n")
    return 0


def _install_service(target: _Target) -> int:
    login_path = os.environ.get("PATH", "")
    try:
        load_settings(target.directory)
        service = _service(target, login_path)
        write_service_file(service)
    except (SettingsError, ServiceError) as error:
        print_failure(str(error))
        return 1
    sys.stdout.write(f"wrote {service.path}\nstart it with: {service.start}\n")
    return 0


def _service(target: _Target, login_path: str) -> ServiceFile:
    executable = shutil.which(_PROGRAM, path=login_path)
    if executable is None:
        message = f"{_PROGRAM} is not on PATH, so a service could not find it"
        raise ServiceError(message)
    home_arguments = [] if target.home is None else ["--home", str(target.home.absolute())]
    program = [
        str(Path(executable).absolute()),
        "--product",
        target.product,
        *home_arguments,
        "run",
    ]
    return service_file(sys.platform, Path.home(), target.product, program, login_path)
