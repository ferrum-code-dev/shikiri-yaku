"""Unit tests for the service units: their content and place, per platform (design §3.1).

The renderers are pure, so these tests only build content; nothing is written to a real launchd or
systemd directory.
"""

from __future__ import annotations

import plistlib
from pathlib import Path
from typing import Final

import pytest

from shikiri_yaku.service import ServiceError, service_file

_HOME: Final = Path("/users/op")
_PROGRAM: Final = ["/opt/tools/shikiri-yaku", "run", "shop", "--home", "/srv/op"]
_LOGIN_PATH: Final = "/opt/tools:/usr/bin:/bin"


def _unit_lines(content: bytes) -> list[str]:
    return [line.strip() for line in content.decode("utf-8").splitlines() if line.strip()]


def test_the_launchd_plist_runs_the_command_with_the_login_path() -> None:
    service = service_file("darwin", _HOME, "shop", _PROGRAM, _LOGIN_PATH)

    assert service.path == _HOME / "Library" / "LaunchAgents" / "shikiri-yaku-shop.plist"
    assert plistlib.loads(service.content) == {
        "Label": "shikiri-yaku-shop",
        "ProgramArguments": _PROGRAM,
        "EnvironmentVariables": {"PATH": _LOGIN_PATH},
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
    }


def test_the_systemd_user_unit_runs_the_command_with_the_login_path() -> None:
    service = service_file("linux", _HOME, "shop", _PROGRAM, _LOGIN_PATH)

    assert service.path == _HOME / ".config" / "systemd" / "user" / "shikiri-yaku-shop.service"
    assert _unit_lines(service.content) == [
        "[Unit]",
        "Description=shikiri-yaku for shop",
        "[Service]",
        'ExecStart=:"/opt/tools/shikiri-yaku" "run" "shop" "--home" "/srv/op"',
        'Environment="PATH=/opt/tools:/usr/bin:/bin"',
        "Restart=on-failure",
        "[Install]",
        "WantedBy=default.target",
    ]


@pytest.mark.parametrize(
    ("executable", "quoted"),
    [
        ("/opt/shikiri-yaku", '"/opt/shikiri-yaku"'),
        ("/Apps/My Tools/shikiri-yaku", '"/Apps/My Tools/shikiri-yaku"'),
        ("/opt/$TOOLS/bin/shikiri-yaku", '"/opt/$TOOLS/bin/shikiri-yaku"'),
        ('/a"b\\c%d/shikiri-yaku', '"/a\\"b\\\\c%%d/shikiri-yaku"'),
    ],
)
def test_the_systemd_executable_is_quoted_so_that_it_reads_back_whole(
    executable: str, quoted: str
) -> None:
    service = service_file("linux", _HOME, "shop", [executable, "run", "shop"], _LOGIN_PATH)

    assert f"ExecStart=:{quoted} " in service.content.decode("utf-8")


@pytest.mark.parametrize(
    ("argument", "quoted"),
    [
        ("/srv/${MISSING}/instances", '"/srv/${MISSING}/instances"'),
        ("$NAME", '"$NAME"'),
    ],
)
def test_the_systemd_exec_start_keeps_a_dollar_sign_literal(argument: str, quoted: str) -> None:
    service = service_file(
        "linux", _HOME, "shop", ["/opt/shikiri-yaku", "run", "shop", argument], _LOGIN_PATH
    )

    lines = _unit_lines(service.content)
    assert f'ExecStart=:"/opt/shikiri-yaku" "run" "shop" {quoted}' in lines


def test_the_systemd_environment_path_is_not_dollar_escaped() -> None:
    service = service_file("linux", _HOME, "shop", _PROGRAM, "/opt/$HOME/bin")

    assert 'Environment="PATH=/opt/$HOME/bin"' in _unit_lines(service.content)


def test_a_platform_without_a_service_manager_is_refused() -> None:
    with pytest.raises(ServiceError):
        service_file("win32", _HOME, "shop", _PROGRAM, _LOGIN_PATH)
