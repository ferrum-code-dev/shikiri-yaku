"""The service restart command: each product's service is restarted through CommandRunner."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import TYPE_CHECKING, Final, override

import pytest

from shikiri_yaku import cli
from shikiri_yaku.bootstrap import adopt_topic, compose, forget_topic, stats_measures
from shikiri_yaku.ports import CommandError, CommandResult, CommandRunner
from shikiri_yaku.service import SERVICE_PREFIX, restart_product, unit_path
from shikiri_yaku.settings import RESTART_TIMEOUT_S, CliSettings

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

_UID: Final = 501
_SHOP: Final = "shop"
_FERRUMCODE: Final = "ferrumcode"
_USAGE_ERROR: Final = 2
_PASSED: Final = "✔ "
_FAILED: Final = "✘ "
_HOST_SUPPORTED: Final = sys.platform in ("darwin", "linux")
_host_only = pytest.mark.skipif(not _HOST_SUPPORTED, reason="restart runs on macOS and Linux")


class _ServiceManager(CommandRunner):
    """Answers each restart with its product's outcome, and succeeds for every other product."""

    def __init__(self, outcomes: Mapping[str, CommandResult | CommandError] | None = None) -> None:
        self.commands: list[list[str]] = []
        self.timeouts: list[float] = []
        self._outcomes = dict(outcomes or {})

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
        outcome = self._outcomes.get(_product_of(argv), CommandResult(0, "", ""))
        if isinstance(outcome, CommandError):
            raise outcome
        return outcome


def _product_of(argv: Sequence[str]) -> str:
    """Return the product a restart command names: the unit name after the shikiri-yaku prefix."""
    return argv[-1].rsplit(SERVICE_PREFIX, 1)[-1].removesuffix(".service")


def _restarted(commands: _ServiceManager) -> list[str]:
    return [_product_of(argv) for argv in commands.commands]


def _unit(user_home: Path, platform: str, product: str) -> None:
    path = unit_path(platform, user_home, product)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"unit")


def _product(instances: Path, name: str) -> None:
    """Make a product under home: a directory with a config.toml, as product discovery needs."""
    directory = instances / name
    directory.mkdir(parents=True)
    (directory / "config.toml").write_text("", encoding="utf-8")


def _restart(
    argv: Sequence[str], commands: CommandRunner, cli_settings: CliSettings | None = None
) -> int:
    return cli.main(
        argv,
        compose,
        stats_measures,
        adopt_topic,
        forget_topic,
        commands=commands,
        cli_settings=CliSettings() if cli_settings is None else cli_settings,
    )


@pytest.fixture
def user_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The user's home directory, under which the service units are kept."""
    home = tmp_path / "user"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


@pytest.mark.parametrize(
    ("platform", "argv"),
    [
        ("darwin", ["launchctl", "kickstart", "-k", f"gui/{_UID}/shikiri-yaku-shop"]),
        ("linux", ["systemctl", "--user", "restart", "shikiri-yaku-shop.service"]),
    ],
)
def test_an_installed_unit_is_restarted_by_its_platforms_service_manager(
    tmp_path: Path, platform: str, argv: list[str]
) -> None:
    _unit(tmp_path, platform, _SHOP)
    commands = _ServiceManager()

    problem = asyncio.run(
        restart_product(commands, platform, tmp_path, _UID, _SHOP, timeout_s=RESTART_TIMEOUT_S)
    )

    assert problem is None
    assert commands.commands == [argv]


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_a_product_without_an_installed_unit_is_not_installed_and_no_command_runs(
    tmp_path: Path, platform: str
) -> None:
    commands = _ServiceManager()

    problem = asyncio.run(
        restart_product(commands, platform, tmp_path, _UID, _SHOP, timeout_s=RESTART_TIMEOUT_S)
    )

    assert problem == "not installed"
    assert commands.commands == []


@pytest.mark.parametrize(
    ("stderr", "reason"),
    [
        ("Could not find service\n", "Could not find service"),
        ("Boot-out failed\n\n  No such process \n", "Boot-out failed; No such process"),
        ("", "exit status 3"),
    ],
)
@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_a_failed_restart_gives_the_service_managers_reason(
    tmp_path: Path, platform: str, stderr: str, reason: str
) -> None:
    _unit(tmp_path, platform, _SHOP)
    commands = _ServiceManager({_SHOP: CommandResult(3, "", stderr)})

    problem = asyncio.run(
        restart_product(commands, platform, tmp_path, _UID, _SHOP, timeout_s=RESTART_TIMEOUT_S)
    )

    assert problem == reason


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_a_service_manager_that_cannot_run_gives_its_error(tmp_path: Path, platform: str) -> None:
    _unit(tmp_path, platform, _SHOP)
    commands = _ServiceManager({_SHOP: CommandError("launchctl: command not found")})

    problem = asyncio.run(
        restart_product(commands, platform, tmp_path, _UID, _SHOP, timeout_s=RESTART_TIMEOUT_S)
    )

    assert problem == "launchctl: command not found"


@_host_only
def test_restart_restarts_every_product_under_home_in_name_order(
    tmp_path: Path, user_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    instances = tmp_path / "instances"
    for name in (_SHOP, _FERRUMCODE):
        _product(instances, name)
        _unit(user_home, sys.platform, name)
    commands = _ServiceManager()

    assert _restart(["--home", str(instances), "service", "restart"], commands) == 0

    assert _restarted(commands) == [_FERRUMCODE, _SHOP]
    assert capsys.readouterr().out.splitlines() == [
        f"{_PASSED}ferrumcode restarted",
        f"{_PASSED}shop restarted",
    ]


@pytest.mark.parametrize(
    ("platform", "expected"),
    [
        (
            "darwin",
            [
                ["launchctl", "kickstart", "-k", f"gui/{_UID}/shikiri-yaku-ferrumcode"],
                ["launchctl", "kickstart", "-k", f"gui/{_UID}/shikiri-yaku-shop"],
            ],
        ),
        (
            "linux",
            [
                ["systemctl", "--user", "restart", "shikiri-yaku-ferrumcode.service"],
                ["systemctl", "--user", "restart", "shikiri-yaku-shop.service"],
            ],
        ),
    ],
)
def test_each_product_under_home_gets_the_documented_command_of_its_platform(
    tmp_path: Path,
    user_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
    expected: list[list[str]],
) -> None:
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(os, "getuid", lambda: _UID)
    instances = tmp_path / "instances"
    for name in (_SHOP, _FERRUMCODE):
        _product(instances, name)
        _unit(user_home, platform, name)
    commands = _ServiceManager()

    assert _restart(["--home", str(instances), "service", "restart"], commands) == 0

    assert commands.commands == expected


@_host_only
def test_the_product_option_restarts_only_that_product(tmp_path: Path, user_home: Path) -> None:
    instances = tmp_path / "instances"
    for name in (_SHOP, _FERRUMCODE):
        _product(instances, name)
        _unit(user_home, sys.platform, name)
    commands = _ServiceManager()

    assert (
        _restart(["--home", str(instances), "--product", _SHOP, "service", "restart"], commands)
        == 0
    )

    assert _restarted(commands) == [_SHOP]


@_host_only
def test_restart_uses_the_composed_cli_timeout(user_home: Path) -> None:
    _unit(user_home, sys.platform, _SHOP)
    commands = _ServiceManager()

    assert (
        _restart(
            ["--product", _SHOP, "service", "restart"], commands, CliSettings(restart_timeout_s=73)
        )
        == 0
    )

    assert commands.timeouts == [73]


@_host_only
def test_a_failed_restart_is_reported_and_the_other_products_still_restart(
    tmp_path: Path, user_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    instances = tmp_path / "instances"
    for name in (_SHOP, _FERRUMCODE):
        _product(instances, name)
        _unit(user_home, sys.platform, name)
    commands = _ServiceManager({_SHOP: CommandResult(3, "", "Could not find service\n")})

    assert _restart(["--home", str(instances), "service", "restart"], commands) == 1

    assert _restarted(commands) == [_FERRUMCODE, _SHOP]
    assert capsys.readouterr().out.splitlines() == [
        f"{_PASSED}ferrumcode restarted",
        f"{_FAILED}shop: Could not find service",
    ]


@_host_only
def test_a_product_without_an_installed_unit_is_reported_as_not_installed(
    tmp_path: Path, user_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    instances = tmp_path / "instances"
    for name in (_SHOP, _FERRUMCODE):
        _product(instances, name)
    _unit(user_home, sys.platform, _SHOP)
    commands = _ServiceManager()

    assert _restart(["--home", str(instances), "service", "restart"], commands) == 1

    assert _restarted(commands) == [_SHOP]
    assert capsys.readouterr().out.splitlines() == [
        f"{_FAILED}ferrumcode: not installed",
        f"{_PASSED}shop restarted",
    ]


@_host_only
def test_json_gives_a_list_with_each_products_result(
    tmp_path: Path, user_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    instances = tmp_path / "instances"
    for name in (_SHOP, _FERRUMCODE):
        _product(instances, name)
    _unit(user_home, sys.platform, _SHOP)

    assert (
        _restart(["--home", str(instances), "--json", "service", "restart"], _ServiceManager()) == 1
    )

    assert json.loads(capsys.readouterr().out) == [
        {"product": _FERRUMCODE, "passed": False, "result": "not installed"},
        {"product": _SHOP, "passed": True, "result": "restarted"},
    ]


@_host_only
def test_no_product_under_home_is_a_usage_error_and_no_command_runs(tmp_path: Path) -> None:
    instances = tmp_path / "instances"
    instances.mkdir()
    commands = _ServiceManager()

    assert _restart(["--home", str(instances), "service", "restart"], commands) == _USAGE_ERROR
    assert commands.commands == []
