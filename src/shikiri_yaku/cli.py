"""The command line: one Typer app whose global options come before the verb."""

from __future__ import annotations

import asyncio
import io
import os
import re
import shutil
import signal
import sys
from contextlib import redirect_stderr
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
    ServiceResult,
    doctor_document,
    print_doctor,
    print_failure,
    print_json,
    print_services,
    print_stats,
    service_document,
    stats_document,
)
from shikiri_yaku.domain.releases import release_numbers
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
    ChannelUnavailableError,
    CommandError,
    CompositionError,
    ExpertError,
    ReasonerError,
    ShikiruError,
    ShikiruLinkDownError,
)
from shikiri_yaku.service import (
    NOT_INSTALLED,
    ServiceError,
    ServiceManager,
    service_file,
    write_service_file,
)
from shikiri_yaku.settings import (
    NAME_PATTERN,
    SHIKIRU_EXECUTABLE,
    SHIKIRU_MINIMUM,
    CliSettings,
    SettingsError,
    load_settings,
    product_names,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Coroutine, Mapping, Sequence

    from shikiri_yaku.app.stats import Measures
    from shikiri_yaku.ports import ChannelCheck, Clock, CommandRunner
    from shikiri_yaku.service import ServiceFile
    from shikiri_yaku.settings import Settings

_PROGRAM = "shikiri-yaku"
_THREAD_ID_PATTERN: Final = r"[1-9][0-9]*"
_DESCRIPTION: Final = "An autonomous Operator for product development with Shikiru."
_BOLD: Final = "bold"
_DIM: Final = "dim"
_STEEL_BLUE: Final = "#83bddd"
_PRIMARY_TEXT: Final = "#dce2e9"
_SECONDARY_TEXT: Final = "#9aa5b5"
_BORDER: Final = "#3d4654"
_ALERT: Final = "#e3a0a0"
_SHIKIRU_CHECK: Final = "Shikiru version"
_SHIKIRU_VERSION_FLAG: Final = "--version"
_MINIMUM: Final = tuple(int(part) for part in SHIKIRU_MINIMUM.split("."))


class Lifecycle(Protocol):
    """A composed instance, as the command line sees it: it is closed when the command ends."""

    @property
    def link_probes(self) -> Mapping[str, Callable[[], Awaitable[None]]]: ...

    @property
    def channel_check(self) -> ChannelCheck: ...

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
type ServiceAction = Callable[[ServiceManager, str], Awaitable[str | None]]
type ServiceOperation = Callable[[ServiceManager, str], Awaitable[ServiceResult]]


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
        help=(
            "Print doctor's and stats' output as one JSON object, and each service command's as "
            "a JSON list. [bold]Default:[/] off"
        ),
    ),
]
COMPONENT_ARGUMENT = Annotated[
    str,
    typer.Argument(metavar="COMPONENT", help="A component's name."),
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


def _add_service_commands(
    service: typer.Typer,
    *,
    commands: CommandRunner,
    clock: Clock,
    cli_settings: CliSettings,
) -> None:
    run_service = partial(_service, commands=commands, clock=clock, cli_settings=cli_settings)

    @service.command("install", help="Write and start the service unit of one product.")
    def service_install(ctx: typer.Context) -> None:
        raise typer.Exit(_service_install(_target(ctx), commands, clock, cli_settings))

    @service.command(
        "start", help="Start the service of one product, or of every product under home."
    )
    def service_start(ctx: typer.Context) -> None:
        action = _reporting(ServiceManager.start, "started")
        raise typer.Exit(run_service(_options(ctx), action))

    @service.command(
        "stop",
        help="Stop the service of one product, or of every product under home, and wait for it.",
    )
    def service_stop(ctx: typer.Context) -> None:
        raise typer.Exit(run_service(_options(ctx), _reporting(ServiceManager.stop, "stopped")))

    @service.command(
        "restart", help="Restart the service of one product, or of every product under home."
    )
    def service_restart(ctx: typer.Context) -> None:
        action = _reporting(ServiceManager.restart, "restarted")
        raise typer.Exit(run_service(_options(ctx), action))

    @service.command(
        "status",
        help="Show whether the service of one product, or of every product under home, runs.",
    )
    def service_status(ctx: typer.Context) -> None:
        raise typer.Exit(run_service(_options(ctx), _reporting(ServiceManager.status, "running")))

    @service.command(
        "uninstall",
        help="Stop and remove the service of one product, or of every product under home.",
    )
    def service_uninstall(ctx: typer.Context) -> None:
        raise typer.Exit(run_service(_options(ctx), _uninstall))


def build_app(
    compose: Compose,
    read_measures: ReadMeasures,
    adopt_topic: AdoptTopic,
    forget_topic: ForgetTopic,
    *,
    commands: CommandRunner,
    clock: Clock,
    cli_settings: CliSettings,
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
    service = typer.Typer(no_args_is_help=True, rich_markup_mode="rich")
    app.add_typer(
        service,
        name="service",
        help="Install, start, stop, restart, check and uninstall the service of a product.",
    )
    _add_service_commands(service, commands=commands, clock=clock, cli_settings=cli_settings)
    telegram = typer.Typer(no_args_is_help=True, rich_markup_mode="rich")
    app.add_typer(telegram, name="telegram", help="Repair the Telegram topic of a component.")

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
            _doctor(
                _target(ctx).directory,
                compose,
                commands,
                as_json=_options(ctx).json_output,
            )
        )

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
    *,
    commands: CommandRunner,
    clock: Clock,
    cli_settings: CliSettings,
) -> int:
    """Run one command line and return its exit code; the caller supplies what it needs."""
    app = build_app(
        compose,
        read_measures,
        adopt_topic,
        forget_topic,
        commands=commands,
        clock=clock,
        cli_settings=cli_settings,
    )
    command = get_command(app)
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
                _verify_channel(composition.channel_check)
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


def _doctor(directory: Path, compose: Compose, commands: CommandRunner, *, as_json: bool) -> int:
    try:
        settings = load_settings(directory)
    except SettingsError as error:
        return _report([_Check("config", str(error))], as_json=as_json)
    version = asyncio.run(_shikiru_version_check(commands, settings.operator.link_call_timeout_s))
    return _report(
        [
            _Check("components", _components_problem(settings)),
            version,
            _lock_check(directory),
            *_composed_checks(settings, directory, compose),
        ],
        as_json=as_json,
    )


async def _shikiru_version_check(commands: CommandRunner, timeout_s: float) -> _Check:
    needed = f"needs Shikiru {SHIKIRU_MINIMUM} or later"
    try:
        result = await commands.run(
            [SHIKIRU_EXECUTABLE, _SHIKIRU_VERSION_FLAG], timeout_s=timeout_s
        )
    except CommandError as error:
        return _Check(_SHIKIRU_CHECK, f"{error}; {needed}")
    if result.returncode != 0:
        exited = f"{SHIKIRU_EXECUTABLE} {_SHIKIRU_VERSION_FLAG} exited with {result.returncode}"
        return _Check(_SHIKIRU_CHECK, f"{exited}; {needed}")
    found = result.stdout.strip()
    number = release_numbers(found)
    if number is None:
        return _Check(_SHIKIRU_CHECK, f"found no version in its output; {needed}")
    if number >= _MINIMUM:
        return _Check(_SHIKIRU_CHECK, status=found)
    return _Check(_SHIKIRU_CHECK, f"found {found}; {needed}")


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
    except SettingsError as error:
        return [_Check("config", str(error))]
    except CompositionError as error:
        return [_Check("journal", str(error))]
    try:
        # keep: diagnostics are not the report; a failed check names its cause on its own line
        with redirect_stderr(io.StringIO()):
            channel_lines = asyncio.run(composition.channel_check.report())
        links = [
            _Check(f"link {name}", _probe_problem(probe))
            for name, probe in composition.link_probes.items()
        ]
        rungs = [
            _Check(f"reasoner {name}", _probe_problem(probe))
            for name, probe in composition.rung_probes.items()
        ]
        experts = [
            _Check(f"expert {name}", _probe_problem(probe))
            for name, probe in getattr(composition, "expert_probes", {}).items()
        ]
        channel = _Check("channel", _probe_problem(composition.channel_check.verify))
    finally:
        composition.close()
    reported = [_Check(line.name, line.detail, line.status) for line in channel_lines]
    return [*reported, *links, *rungs, *experts, channel, _Check("journal")]


def _verify_channel(channel_check: ChannelCheck) -> None:
    # keep: a temporary outage is reported by the channel's existing health source
    try:
        asyncio.run(channel_check.verify())
    except ChannelUnavailableError:
        return


def _probe_problem(probe: Callable[[], Awaitable[None]]) -> str | None:
    # keep: a probe's stderr is read only when it fails, so a passing check stays silent
    diagnostics = io.StringIO()
    with redirect_stderr(diagnostics):
        try:
            asyncio.run(probe())
        except (ShikiruError, ChannelError, ReasonerError, ExpertError) as error:
            return _problem(error, diagnostics.getvalue())
    return None


def _problem(error: Exception, diagnostics: str) -> str:
    problem = str(error) or type(error).__name__
    cause = _stderr_title(diagnostics)
    if not isinstance(error, ShikiruLinkDownError) or cause is None:
        return problem
    return f"{problem}; {cause}"


# keep: Shikiru titles each failure with the same ✕ its doctor uses; logs and tracebacks have none
_FAILURE_TITLE: Final = re.compile(r"^\s*✕\s+(\S.*?)\s*$")


def _stderr_title(diagnostics: str) -> str | None:
    # keep: the last title is the failure written just before the server exited
    cause = None
    for line in diagnostics.splitlines():
        match = _FAILURE_TITLE.match(line)
        if match is not None:
            cause = match.group(1)
    return cause


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


def _service_install(
    target: _Target, commands: CommandRunner, clock: Clock, cli_settings: CliSettings
) -> int:
    login_path = os.environ.get("PATH", "")
    try:
        load_settings(target.directory)
        service = _service_unit(target, login_path)
        write_service_file(service)
    except (SettingsError, ServiceError) as error:
        print_failure(str(error))
        return 1
    manager = ServiceManager(
        commands,
        clock,
        sys.platform,
        Path.home(),
        os.getuid(),
        command_timeout_s=cli_settings.service_timeout_s,
        stop_timeout_s=cli_settings.stop_timeout_s,
        stop_poll_s=cli_settings.stop_poll_s,
    )
    problem = asyncio.run(manager.install(target.product))
    if problem is not None:
        print_failure(problem)
        return 1
    sys.stdout.write(f"wrote {service.path}\nservice started\n")
    return 0


def _service(
    options: _Global,
    operation: ServiceOperation,
    *,
    commands: CommandRunner,
    clock: Clock,
    cli_settings: CliSettings,
) -> int:
    """Run one service action on each product the options name and print what became of each."""
    products = _service_products(options)
    manager = ServiceManager(
        commands,
        clock,
        sys.platform,
        Path.home(),
        os.getuid(),
        command_timeout_s=cli_settings.service_timeout_s,
        stop_timeout_s=cli_settings.stop_timeout_s,
        stop_poll_s=cli_settings.stop_poll_s,
    )
    try:
        results = asyncio.run(_each_product(manager, operation, products))
    except ServiceError as error:
        print_failure(str(error))
        return 1
    if options.json_output:
        print_json(service_document(results))
    else:
        print_services(results)
    return 0 if all(result.passed for result in results) else 1


def _service_products(options: _Global) -> list[str]:
    if options.product is not None:
        return [options.product]
    home = home_dir(options.home)
    found = product_names(home)
    if not found:
        message = f"no product under {home}"
        raise typer.BadParameter(message, param_hint="'--product'")
    return found


async def _each_product(
    manager: ServiceManager, operation: ServiceOperation, products: Sequence[str]
) -> list[ServiceResult]:
    return [await operation(manager, product) for product in products]


def _reporting(action: ServiceAction, done: str) -> ServiceOperation:
    async def operation(manager: ServiceManager, product: str) -> ServiceResult:
        return _settled(product, await action(manager, product), done)

    return operation


async def _uninstall(manager: ServiceManager, product: str) -> ServiceResult:
    problem = await manager.uninstall(product)
    # keep: a product with no unit has nothing to remove, which is what uninstall asks for
    if problem == NOT_INSTALLED:
        return ServiceResult(product, passed=True, result=NOT_INSTALLED)
    return _settled(product, problem, "uninstalled")


def _settled(product: str, problem: str | None, done: str) -> ServiceResult:
    if problem is None:
        return ServiceResult(product, passed=True, result=done)
    return ServiceResult(product, passed=False, result=problem)


def _service_unit(target: _Target, login_path: str) -> ServiceFile:
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
