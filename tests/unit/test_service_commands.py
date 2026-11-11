"""The service commands: start, stop, restart and status of each product, via CommandRunner."""

from __future__ import annotations

import errno
import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Final, override

import pytest

from shikiri_yaku import cli
from shikiri_yaku.bootstrap import adopt_topic, compose, forget_topic, stats_measures
from shikiri_yaku.ports import CommandError, CommandResult, CommandRunner
from shikiri_yaku.service import SERVICE_PREFIX, unit_path
from shikiri_yaku.settings import CliSettings

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

_UID: Final = 501
_SHOP: Final = "shop"
_FERRUMCODE: Final = "ferrumcode"
_USAGE_ERROR: Final = 2
_PASSED: Final = "✔ "
_FAILED: Final = "✘ "
_VERBS: Final = ("start", "stop", "restart")
_EVERY_VERB: Final = (*_VERBS, "status")
_ALL_VERBS: Final = (*_EVERY_VERB, "uninstall")
_CLOCK_START: Final = datetime(2026, 1, 1, tzinfo=UTC)
_OK: Final = CommandResult(0, "", "")
_RUNNING: Final = CommandResult(0, "\tstate = running\n", "")
_NOT_RUNNING: Final = CommandResult(0, "\tstate = not running\n", "")
_ACTIVE: Final = CommandResult(0, "active\n", "")
_INACTIVE: Final = CommandResult(3, "inactive\n", "")


class _Clock:
    """A clock whose waits move it forward at once, so a timeout is reached without waiting."""

    def __init__(self) -> None:
        self._now = _CLOCK_START
        self.slept: list[float] = []

    def now(self) -> datetime:
        return self._now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self._now += timedelta(seconds=seconds)


class _Launchctl(CommandRunner):
    """Answers each command with its product's outcome, and `launchctl print` in turn.

    The last print answer repeats. Products without an outcome succeed.
    """

    def __init__(
        self,
        outcomes: Mapping[str, CommandResult | CommandError] | None = None,
        printed: Sequence[CommandResult] = (_NOT_RUNNING,),
    ) -> None:
        self.commands: list[list[str]] = []
        self.timeouts: list[float] = []
        self._outcomes = dict(outcomes or {})
        self._printed = list(printed)

    @override
    async def run(
        self,
        argv: Sequence[str],
        *,
        timeout_s: float,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        del cwd, env
        self.commands.append(list(argv))
        self.timeouts.append(timeout_s)
        outcome = self._outcomes.get(_product_of(argv), _OK)
        if isinstance(outcome, CommandError):
            raise outcome
        if argv[1] == "print":
            return self._printed.pop(0) if len(self._printed) > 1 else self._printed[0]
        return outcome


class _Recording(_Launchctl):
    """Records whether one unit file is still present when each command runs."""

    def __init__(self, unit: Path) -> None:
        super().__init__()
        self._unit = unit
        self.unit_present: list[bool] = []

    @override
    async def run(
        self,
        argv: Sequence[str],
        *,
        timeout_s: float,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        self.unit_present.append(self._unit.is_file())
        return await super().run(argv, timeout_s=timeout_s, cwd=cwd, env=env)


def _product_of(argv: Sequence[str]) -> str:
    """Return the product a command names: the unit name after the shikiri-yaku prefix."""
    return argv[-1].rsplit(SERVICE_PREFIX, 1)[-1].removesuffix(".service")


def _print(product: str) -> list[str]:
    return ["launchctl", "print", f"gui/{_UID}/{SERVICE_PREFIX}{product}"]


def _documented(platform: str, verb: str, product: str) -> list[list[str]]:
    """Return the commands one verb runs for one product, as the documented recipes give them."""
    name = f"{SERVICE_PREFIX}{product}"
    if platform == "linux":
        action = "is-active" if verb == "status" else verb
        return [["systemctl", "--user", action, f"{name}.service"]]
    target = f"gui/{_UID}/{name}"
    recipes = {
        "start": [["launchctl", "kickstart", target]],
        "restart": [["launchctl", "kickstart", "-k", target]],
        "stop": [["launchctl", "kill", "SIGTERM", target], _print(product)],
        "status": [_print(product)],
    }
    return recipes[verb]


def _uninstalled(platform: str, product: str) -> list[list[str]]:
    """Return the commands that uninstalling one product runs, in order."""
    name = f"{SERVICE_PREFIX}{product}"
    if platform == "linux":
        return [
            ["systemctl", "--user", "disable", "--now", f"{name}.service"],
            ["systemctl", "--user", "daemon-reload"],
        ]
    return [["launchctl", "bootout", f"gui/{_UID}/{name}"]]


def _pretend(monkeypatch: pytest.MonkeyPatch, platform: str) -> None:
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(os, "getuid", lambda: _UID)


def _products(tmp_path: Path, *names: str) -> Path:
    """Make products under a home: a directory with a config.toml each, for product discovery."""
    instances = tmp_path / "instances"
    for name in names:
        directory = instances / name
        directory.mkdir(parents=True)
        (directory / "config.toml").write_text("", encoding="utf-8")
    return instances


def _install(user_home: Path, platform: str, *names: str) -> None:
    for name in names:
        path = unit_path(platform, user_home, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"unit")


def _service(
    argv: Sequence[str],
    commands: CommandRunner,
    clock: _Clock,
    cli_settings: CliSettings | None = None,
) -> int:
    return cli.main(
        argv,
        compose,
        stats_measures,
        adopt_topic,
        forget_topic,
        commands=commands,
        clock=clock,
        cli_settings=CliSettings() if cli_settings is None else cli_settings,
    )


@pytest.fixture
def user_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The user's home directory, under which the service units are kept."""
    home = tmp_path / "user"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


@pytest.mark.parametrize("platform", ["darwin", "linux"])
@pytest.mark.parametrize("verb", _VERBS)
def test_each_verb_runs_its_platforms_commands_for_every_product_under_home(
    tmp_path: Path,
    user_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
    verb: str,
) -> None:
    _pretend(monkeypatch, platform)
    instances = _products(tmp_path, _SHOP, _FERRUMCODE)
    _install(user_home, platform, _SHOP, _FERRUMCODE)
    commands = _Launchctl()

    assert _service(["--home", str(instances), "service", verb], commands, _Clock()) == 0

    assert commands.commands == [
        *_documented(platform, verb, _FERRUMCODE),
        *_documented(platform, verb, _SHOP),
    ]


@pytest.mark.parametrize("verb", _VERBS)
def test_the_product_option_acts_on_only_that_product(
    tmp_path: Path, user_home: Path, monkeypatch: pytest.MonkeyPatch, verb: str
) -> None:
    _pretend(monkeypatch, "linux")
    instances = _products(tmp_path, _SHOP, _FERRUMCODE)
    _install(user_home, "linux", _SHOP, _FERRUMCODE)
    commands = _Launchctl()

    argv = ["--home", str(instances), "--product", _SHOP, "service", verb]
    assert _service(argv, commands, _Clock()) == 0

    assert commands.commands == _documented("linux", verb, _SHOP)


def test_a_stop_on_macos_waits_until_the_job_is_no_longer_running(
    tmp_path: Path, user_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _pretend(monkeypatch, "darwin")
    _products(tmp_path, _SHOP)
    _install(user_home, "darwin", _SHOP)
    commands = _Launchctl(printed=[_RUNNING, _RUNNING, _NOT_RUNNING])
    clock = _Clock()

    argv = ["--home", str(tmp_path / "instances"), "service", "stop"]
    assert _service(argv, commands, clock) == 0

    assert commands.commands == [_documented("darwin", "stop", _SHOP)[0], *[_print(_SHOP)] * 3]
    assert clock.slept == [1, 1]


def test_a_stop_on_macos_that_outlasts_the_timeout_fails_after_waiting_that_long(
    tmp_path: Path,
    user_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _pretend(monkeypatch, "darwin")
    _products(tmp_path, _SHOP)
    _install(user_home, "darwin", _SHOP)
    clock = _Clock()

    argv = ["--json", "--home", str(tmp_path / "instances"), "service", "stop"]
    assert _service(argv, _Launchctl(printed=[_RUNNING]), clock, CliSettings(stop_timeout_s=3)) == 1

    assert sum(clock.slept) == 3
    assert json.loads(capsys.readouterr().out)[0]["passed"] is False


@pytest.mark.parametrize("final", [_NOT_RUNNING, CommandResult(113, "", "No such job")])
def test_a_stop_on_macos_finishes_once_launchctl_no_longer_reports_the_job_running(
    tmp_path: Path, user_home: Path, monkeypatch: pytest.MonkeyPatch, final: CommandResult
) -> None:
    _pretend(monkeypatch, "darwin")
    _products(tmp_path, _SHOP)
    _install(user_home, "darwin", _SHOP)

    argv = ["--home", str(tmp_path / "instances"), "service", "stop"]
    assert _service(argv, _Launchctl(printed=[_RUNNING, final]), _Clock()) == 0


def test_a_failed_stop_on_macos_is_not_followed_by_a_wait(
    tmp_path: Path, user_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _pretend(monkeypatch, "darwin")
    _products(tmp_path, _SHOP)
    _install(user_home, "darwin", _SHOP)
    commands = _Launchctl({_SHOP: CommandResult(3, "", "Boot-out failed\n")})

    argv = ["--home", str(tmp_path / "instances"), "service", "stop"]
    assert _service(argv, commands, _Clock()) == 1

    assert commands.commands == [_documented("darwin", "stop", _SHOP)[0]]


@pytest.mark.parametrize(
    ("stderr", "reason"),
    [
        ("Could not find service\n", "Could not find service"),
        ("Boot-out failed\n\n  No such process \n", "Boot-out failed; No such process"),
        ("", "exit status 3"),
    ],
)
@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_a_failed_action_gives_the_service_managers_reason(
    *,
    tmp_path: Path,
    user_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    platform: str,
    stderr: str,
    reason: str,
) -> None:
    _pretend(monkeypatch, platform)
    _products(tmp_path, _SHOP)
    _install(user_home, platform, _SHOP)
    commands = _Launchctl({_SHOP: CommandResult(3, "", stderr)})

    argv = ["--json", "--home", str(tmp_path / "instances"), "service", "restart"]
    assert _service(argv, commands, _Clock()) == 1

    assert json.loads(capsys.readouterr().out) == [
        {"product": _SHOP, "passed": False, "result": reason}
    ]


@pytest.mark.parametrize("platform", ["darwin", "linux"])
@pytest.mark.parametrize("verb", ["start", "status"])
def test_a_service_manager_that_cannot_run_gives_its_error(
    *,
    tmp_path: Path,
    user_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    platform: str,
    verb: str,
) -> None:
    _pretend(monkeypatch, platform)
    _products(tmp_path, _SHOP)
    _install(user_home, platform, _SHOP)
    commands = _Launchctl({_SHOP: CommandError("launchctl: command not found")})

    argv = ["--json", "--home", str(tmp_path / "instances"), "service", verb]
    assert _service(argv, commands, _Clock()) == 1

    assert json.loads(capsys.readouterr().out) == [
        {"product": _SHOP, "passed": False, "result": "launchctl: command not found"}
    ]


@pytest.mark.parametrize(
    ("platform", "answer", "result"),
    [
        ("darwin", _RUNNING, "running"),
        ("darwin", _NOT_RUNNING, "stopped"),
        ("darwin", CommandResult(113, "", "Could not find service\n"), "not loaded"),
        ("linux", _ACTIVE, "running"),
        ("linux", _INACTIVE, "stopped"),
        ("linux", CommandResult(3, "failed\n", ""), "failed"),
        ("linux", CommandResult(1, "", "Failed to connect to bus\n"), "Failed to connect to bus"),
    ],
)
def test_status_maps_the_service_managers_answer_to_a_result(
    *,
    tmp_path: Path,
    user_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    platform: str,
    answer: CommandResult,
    result: str,
) -> None:
    _pretend(monkeypatch, platform)
    _products(tmp_path, _SHOP)
    _install(user_home, platform, _SHOP)
    commands = _Launchctl({_SHOP: answer}, printed=[answer])

    argv = ["--json", "--home", str(tmp_path / "instances"), "service", "status"]
    assert _service(argv, commands, _Clock()) == (0 if result == "running" else 1)

    assert commands.commands == _documented(platform, "status", _SHOP)
    assert json.loads(capsys.readouterr().out) == [
        {"product": _SHOP, "passed": result == "running", "result": result}
    ]


def test_status_exits_one_when_any_product_is_not_running(
    tmp_path: Path,
    user_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _pretend(monkeypatch, "linux")
    _products(tmp_path, _SHOP, _FERRUMCODE)
    _install(user_home, "linux", _SHOP, _FERRUMCODE)
    commands = _Launchctl({_SHOP: _INACTIVE, _FERRUMCODE: _ACTIVE})

    argv = ["--home", str(tmp_path / "instances"), "service", "status"]
    assert _service(argv, commands, _Clock()) == 1

    assert capsys.readouterr().out.splitlines() == [
        f"{_PASSED}ferrumcode running",
        f"{_FAILED}shop: stopped",
    ]


@pytest.mark.parametrize("platform", ["darwin", "linux"])
@pytest.mark.parametrize("verb", _EVERY_VERB)
@pytest.mark.usefixtures("user_home")
def test_a_product_without_an_installed_unit_is_not_installed_and_no_command_runs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    platform: str,
    verb: str,
) -> None:
    _pretend(monkeypatch, platform)
    instances = _products(tmp_path, _SHOP)
    commands = _Launchctl()

    assert _service(["--json", "--home", str(instances), "service", verb], commands, _Clock()) == 1

    assert commands.commands == []
    assert json.loads(capsys.readouterr().out) == [
        {"product": _SHOP, "passed": False, "result": "not installed"}
    ]


@pytest.mark.parametrize(
    ("verb", "done"),
    [("start", "started"), ("stop", "stopped"), ("restart", "restarted")],
)
def test_json_gives_a_list_with_the_word_each_verb_finishes_with(
    *,
    tmp_path: Path,
    user_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    verb: str,
    done: str,
) -> None:
    _pretend(monkeypatch, "darwin")
    _products(tmp_path, _SHOP)
    _install(user_home, "darwin", _SHOP)

    argv = ["--json", "--home", str(tmp_path / "instances"), "service", verb]
    assert _service(argv, _Launchctl(), _Clock()) == 0

    assert json.loads(capsys.readouterr().out) == [
        {"product": _SHOP, "passed": True, "result": done}
    ]


def test_a_failed_action_is_reported_and_the_other_products_still_act(
    tmp_path: Path,
    user_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _pretend(monkeypatch, "darwin")
    _products(tmp_path, _SHOP, _FERRUMCODE)
    _install(user_home, "darwin", _SHOP, _FERRUMCODE)
    commands = _Launchctl({_SHOP: CommandResult(3, "", "Could not find service\n")})

    assert (
        _service(["--home", str(tmp_path / "instances"), "service", "restart"], commands, _Clock())
        == 1
    )

    assert commands.commands == [
        *_documented("darwin", "restart", _FERRUMCODE),
        *_documented("darwin", "restart", _SHOP),
    ]
    assert capsys.readouterr().out.splitlines() == [
        f"{_PASSED}ferrumcode restarted",
        f"{_FAILED}shop: Could not find service",
    ]


@pytest.mark.parametrize("platform", ["darwin", "linux"])
@pytest.mark.parametrize("verb", _VERBS)
def test_every_verb_uses_the_composed_command_timeout(
    tmp_path: Path,
    user_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
    verb: str,
) -> None:
    _pretend(monkeypatch, platform)
    _products(tmp_path, _SHOP)
    _install(user_home, platform, _SHOP)
    commands = _Launchctl()

    argv = ["--product", _SHOP, "service", verb]
    assert _service(argv, commands, _Clock(), CliSettings(service_timeout_s=73)) == 0

    assert commands.timeouts
    assert set(commands.timeouts) == {73}


@pytest.mark.parametrize("verb", _ALL_VERBS)
def test_an_unsupported_platform_fails_and_no_command_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verb: str
) -> None:
    _pretend(monkeypatch, "win32")
    instances = _products(tmp_path, _SHOP)
    commands = _Launchctl()

    assert _service(["--home", str(instances), "service", verb], commands, _Clock()) == 1

    assert commands.commands == []


@pytest.mark.parametrize("verb", _ALL_VERBS)
def test_no_product_under_home_is_a_usage_error_and_no_command_runs(
    tmp_path: Path, verb: str
) -> None:
    instances = tmp_path / "instances"
    instances.mkdir()
    commands = _Launchctl()

    assert _service(["--home", str(instances), "service", verb], commands, _Clock()) == _USAGE_ERROR
    assert commands.commands == []


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_uninstall_runs_its_commands_in_order_and_deletes_the_unit_before_the_reload(
    tmp_path: Path, user_home: Path, monkeypatch: pytest.MonkeyPatch, platform: str
) -> None:
    _pretend(monkeypatch, platform)
    instances = _products(tmp_path, _SHOP)
    _install(user_home, platform, _SHOP)
    unit = unit_path(platform, user_home, _SHOP)
    commands = _Recording(unit)

    argv = ["--home", str(instances), "service", "uninstall"]
    assert _service(argv, commands, _Clock()) == 0

    assert commands.commands == _uninstalled(platform, _SHOP)
    assert commands.unit_present == ([True, False] if platform == "linux" else [True])
    assert not unit.exists()
    assert (instances / _SHOP / "config.toml").is_file()


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_uninstall_removes_the_unit_of_every_product_under_home(
    tmp_path: Path, user_home: Path, monkeypatch: pytest.MonkeyPatch, platform: str
) -> None:
    _pretend(monkeypatch, platform)
    instances = _products(tmp_path, _SHOP, _FERRUMCODE)
    _install(user_home, platform, _SHOP, _FERRUMCODE)
    commands = _Launchctl()

    argv = ["--home", str(instances), "service", "uninstall"]
    assert _service(argv, commands, _Clock()) == 0

    assert commands.commands == [
        *_uninstalled(platform, _FERRUMCODE),
        *_uninstalled(platform, _SHOP),
    ]
    assert not unit_path(platform, user_home, _SHOP).exists()
    assert not unit_path(platform, user_home, _FERRUMCODE).exists()


@pytest.mark.parametrize(
    ("platform", "answer"),
    [
        ("darwin", CommandResult(3, "", "Boot-out failed: 3: No such process\n")),
        ("darwin", CommandResult(113, "", "Could not find service\n")),
        ("linux", CommandResult(1, "", "Unit shikiri-yaku-shop.service not loaded.\n")),
        ("linux", CommandResult(1, "", "Unit shikiri-yaku-shop.service is not enabled.\n")),
        ("linux", CommandResult(1, "", "Unit shikiri-yaku-shop.service not running.\n")),
    ],
    ids=[
        "launchd-no-such-process",
        "launchd-no-service",
        "not-loaded",
        "not-enabled",
        "not-running",
    ],
)
def test_a_unit_the_manager_no_longer_loads_is_still_removed(
    *,
    tmp_path: Path,
    user_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    platform: str,
    answer: CommandResult,
) -> None:
    _pretend(monkeypatch, platform)
    instances = _products(tmp_path, _SHOP)
    _install(user_home, platform, _SHOP)

    argv = ["--json", "--home", str(instances), "service", "uninstall"]
    assert _service(argv, _Launchctl({_SHOP: answer}), _Clock()) == 0

    assert not unit_path(platform, user_home, _SHOP).exists()
    assert json.loads(capsys.readouterr().out) == [
        {"product": _SHOP, "passed": True, "result": "uninstalled"}
    ]


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_a_refused_unload_is_reported_and_keeps_the_unit(
    *,
    tmp_path: Path,
    user_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    platform: str,
) -> None:
    _pretend(monkeypatch, platform)
    instances = _products(tmp_path, _SHOP)
    _install(user_home, platform, _SHOP)
    commands = _Launchctl({_SHOP: CommandResult(1, "", "Operation not permitted\n")})

    argv = ["--json", "--home", str(instances), "service", "uninstall"]
    assert _service(argv, commands, _Clock()) == 1

    assert unit_path(platform, user_home, _SHOP).is_file()
    assert commands.commands == _uninstalled(platform, _SHOP)[:1]
    assert json.loads(capsys.readouterr().out) == [
        {"product": _SHOP, "passed": False, "result": "Operation not permitted"}
    ]


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_a_unit_that_cannot_be_deleted_is_reported_with_its_path_and_no_reload_runs(
    *,
    tmp_path: Path,
    user_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    platform: str,
) -> None:
    _pretend(monkeypatch, platform)
    instances = _products(tmp_path, _SHOP)
    _install(user_home, platform, _SHOP)
    unit = unit_path(platform, user_home, _SHOP)
    real_unlink = Path.unlink

    def refuse_the_unit(path: Path, **options: bool) -> None:
        if path == unit:
            raise PermissionError(errno.EACCES, os.strerror(errno.EACCES))
        real_unlink(path, **options)

    monkeypatch.setattr(Path, "unlink", refuse_the_unit)
    commands = _Launchctl()

    argv = ["--json", "--home", str(instances), "service", "uninstall"]
    assert _service(argv, commands, _Clock()) == 1

    assert commands.commands == _uninstalled(platform, _SHOP)[:1]
    [entry] = json.loads(capsys.readouterr().out)
    assert entry["passed"] is False
    assert str(unit) in entry["result"]


@pytest.mark.parametrize("platform", ["darwin", "linux"])
@pytest.mark.usefixtures("user_home")
def test_a_product_without_a_unit_passes_as_not_installed_and_no_command_runs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    platform: str,
) -> None:
    _pretend(monkeypatch, platform)
    instances = _products(tmp_path, _SHOP)
    commands = _Launchctl()

    argv = ["--json", "--home", str(instances), "service", "uninstall"]
    assert _service(argv, commands, _Clock()) == 0

    assert commands.commands == []
    assert json.loads(capsys.readouterr().out) == [
        {"product": _SHOP, "passed": True, "result": "not installed"}
    ]


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_a_repeated_uninstall_passes_because_nothing_is_left_to_remove(
    tmp_path: Path,
    user_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    platform: str,
) -> None:
    _pretend(monkeypatch, platform)
    instances = _products(tmp_path, _SHOP)
    _install(user_home, platform, _SHOP)
    commands = _Launchctl()
    argv = ["--json", "--home", str(instances), "service", "uninstall"]

    assert _service(argv, commands, _Clock()) == 0
    capsys.readouterr()
    assert _service(argv, commands, _Clock()) == 0

    assert commands.commands == _uninstalled(platform, _SHOP)
    assert json.loads(capsys.readouterr().out) == [
        {"product": _SHOP, "passed": True, "result": "not installed"}
    ]


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_uninstall_uses_the_composed_command_timeout(
    tmp_path: Path, user_home: Path, monkeypatch: pytest.MonkeyPatch, platform: str
) -> None:
    _pretend(monkeypatch, platform)
    _products(tmp_path, _SHOP)
    _install(user_home, platform, _SHOP)
    commands = _Launchctl()

    argv = ["--product", _SHOP, "service", "uninstall"]
    assert _service(argv, commands, _Clock(), CliSettings(service_timeout_s=73)) == 0

    assert commands.timeouts
    assert set(commands.timeouts) == {73}
