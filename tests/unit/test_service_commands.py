"""The service commands: start, stop and restart each product's service through CommandRunner."""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final, override

import pytest

from shikiri_yaku import cli
from shikiri_yaku.bootstrap import adopt_topic, compose, forget_topic, stats_measures
from shikiri_yaku.ports import CommandError, CommandResult, CommandRunner
from shikiri_yaku.service import SERVICE_PREFIX, unit_path
from shikiri_yaku.settings import CliSettings

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

_UID: Final = 501
_SHOP: Final = "shop"
_FERRUMCODE: Final = "ferrumcode"
_USAGE_ERROR: Final = 2
_PASSED: Final = "✔ "
_FAILED: Final = "✘ "
_VERBS: Final = ("start", "stop", "restart")
_CLOCK_START: Final = datetime(2026, 1, 1, tzinfo=UTC)
_OK: Final = CommandResult(0, "", "")
_RUNNING: Final = CommandResult(0, "\tstate = running\n", "")
_NOT_RUNNING: Final = CommandResult(0, "\tstate = not running\n", "")


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
        if argv[1] == "print":
            return self._printed.pop(0) if len(self._printed) > 1 else self._printed[0]
        outcome = self._outcomes.get(_product_of(argv), _OK)
        if isinstance(outcome, CommandError):
            raise outcome
        return outcome


def _product_of(argv: Sequence[str]) -> str:
    """Return the product a command names: the unit name after the shikiri-yaku prefix."""
    return argv[-1].rsplit(SERVICE_PREFIX, 1)[-1].removesuffix(".service")


def _print(product: str) -> list[str]:
    return ["launchctl", "print", f"gui/{_UID}/{SERVICE_PREFIX}{product}"]


def _documented(platform: str, verb: str, product: str) -> list[list[str]]:
    """Return the commands one verb runs for one product, as the documented recipes give them."""
    name = f"{SERVICE_PREFIX}{product}"
    if platform == "linux":
        return [["systemctl", "--user", verb, f"{name}.service"]]
    target = f"gui/{_UID}/{name}"
    recipes = {
        "start": [["launchctl", "kickstart", target]],
        "restart": [["launchctl", "kickstart", "-k", target]],
        "stop": [["launchctl", "kill", "SIGTERM", target], _print(product)],
    }
    return recipes[verb]


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
def test_a_service_manager_that_cannot_run_gives_its_error(
    tmp_path: Path,
    user_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    platform: str,
) -> None:
    _pretend(monkeypatch, platform)
    _products(tmp_path, _SHOP)
    _install(user_home, platform, _SHOP)
    commands = _Launchctl({_SHOP: CommandError("launchctl: command not found")})

    argv = ["--json", "--home", str(tmp_path / "instances"), "service", "start"]
    assert _service(argv, commands, _Clock()) == 1

    assert json.loads(capsys.readouterr().out) == [
        {"product": _SHOP, "passed": False, "result": "launchctl: command not found"}
    ]


@pytest.mark.parametrize("platform", ["darwin", "linux"])
@pytest.mark.parametrize("verb", _VERBS)
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


@pytest.mark.parametrize("verb", _VERBS)
def test_an_unsupported_platform_fails_and_no_command_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verb: str
) -> None:
    _pretend(monkeypatch, "win32")
    instances = _products(tmp_path, _SHOP)
    commands = _Launchctl()

    assert _service(["--home", str(instances), "service", verb], commands, _Clock()) == 1

    assert commands.commands == []


@pytest.mark.parametrize("verb", _VERBS)
def test_no_product_under_home_is_a_usage_error_and_no_command_runs(
    tmp_path: Path, verb: str
) -> None:
    instances = tmp_path / "instances"
    instances.mkdir()
    commands = _Launchctl()

    assert _service(["--home", str(instances), "service", verb], commands, _Clock()) == _USAGE_ERROR
    assert commands.commands == []
