"""Integration tests of run and doctor through bootstrap.main (design §3.1, §10)."""

from __future__ import annotations

import multiprocessing
import os
import signal
import sys
import threading
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from typing import TYPE_CHECKING, Any, Final

import pytest
from mcp.types import CallToolResult

from shikiri_yaku import cli
from shikiri_yaku.adapters.clock import SystemClock
from shikiri_yaku.adapters.shikiru_mcp import stdio_connector
from shikiri_yaku.bootstrap import compose, main
from shikiri_yaku.instance import JOURNAL_FILE_NAME, instance_lock
from shikiri_yaku.journal import Database
from shikiri_yaku.journal.database import LATEST_VERSION
from shikiri_yaku.service import service_file

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable
    from pathlib import Path

    from shikiri_yaku.adapters.shikiru_mcp import ToolSession

_PRODUCT: Final = "shop"
_ATTEMPTS: Final = 100
_ATTEMPT_WAIT_S: Final = 0.1

type _Breakage = Callable[[Path], None]


@dataclass(frozen=True, slots=True)
class _UserEnvironment:
    home: Path
    executable: Path


def _directory(home: Path) -> Path:
    return home / _PRODUCT


def _instance(home: Path) -> Path:
    directory = _directory(home)
    component = home / "api"
    component.mkdir(parents=True)
    (directory / "secrets").mkdir(parents=True)
    (directory / "config.toml").write_text(
        f'product = "{_PRODUCT}"\n\n[[components]]\nname = "api"\npath = "{component}"\n\n'
        "[channels.telegram]\nowners = [123456789]\n",
        encoding="utf-8",
    )
    token = directory / "secrets" / "telegram.token"
    token.write_text("", encoding="utf-8")
    token.chmod(0o600)
    return directory


def _failed(output: str) -> set[str]:
    return {line.partition(": FAILED")[0] for line in output.splitlines() if ": FAILED" in line}


def _remove_config(home: Path) -> None:
    (_directory(home) / "config.toml").unlink()


def _remove_component(home: Path) -> None:
    (home / "api").rmdir()


def _loosen_token(home: Path) -> None:
    (_directory(home) / "secrets" / "telegram.token").chmod(0o644)


def _journal_too_new(home: Path) -> None:
    database = Database.open(_directory(home) / JOURNAL_FILE_NAME)
    database.connection.execute(f"PRAGMA user_version = {LATEST_VERSION + 1}")
    database.close()


def _journal_not_a_database(home: Path) -> None:
    (_directory(home) / JOURNAL_FILE_NAME).write_bytes(b"not a journal" * 100)


def _journal_becomes_directory(home: Path) -> None:
    (_directory(home) / JOURNAL_FILE_NAME).mkdir()


def _config_becomes_directory(home: Path) -> None:
    config = _directory(home) / "config.toml"
    config.unlink()
    config.mkdir()


def _config_not_utf8(home: Path) -> None:
    (_directory(home) / "config.toml").write_bytes(b"\xff" * 10)


def _token_becomes_directory(home: Path) -> None:
    token = _directory(home) / "secrets" / "telegram.token"
    token.unlink()
    token.mkdir()
    token.chmod(0o600)


def _secrets_become_file(home: Path) -> None:
    secrets = _directory(home) / "secrets"
    (secrets / "telegram.token").unlink()
    secrets.rmdir()
    secrets.write_text("", encoding="utf-8")


def _lock_becomes_directory(home: Path) -> None:
    (_directory(home) / "instance.lock").mkdir()


def _token_path_and_lock_broken(home: Path) -> None:
    _secrets_become_file(home)
    _lock_becomes_directory(home)


class _StatusSession:
    async def call_tool(self, name: str, arguments: dict[str, Any], *_ignored: object) -> object:
        if name in {"wait_for_events", "list_escalations"}:
            await SystemClock().sleep(arguments.get("timeout_s", 0))
            return CallToolResult(content=[], structuredContent={"result": []}, isError=False)
        assert name == "status"
        return CallToolResult(
            content=[],
            structuredContent={"engine_up": True, "quotas": {}},
            isError=False,
        )


@asynccontextmanager
async def _answering_link() -> AsyncGenerator[ToolSession]:
    yield _StatusSession()  # pyright: ignore[reportReturnType]


class _unreachable_link:  # noqa: N801 -- used like the link factories next to it
    async def __aenter__(self) -> ToolSession:
        message = "no such executable"
        raise FileNotFoundError(message)

    async def __aexit__(self, *_ignored: object) -> None: ...


def _run_doctor(home: Path, link: Callable[[], Any] = _answering_link) -> int:
    composed = partial(compose, link=lambda _executable, _project: link)
    return cli.main(["doctor", _PRODUCT, "--home", str(home)], composed)


def test_doctor_passes_a_valid_instance(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _instance(tmp_path)

    assert _run_doctor(tmp_path) == 0
    assert _failed(capsys.readouterr().out) == set()


@pytest.mark.parametrize(
    ("breakage", "failed"),
    [
        (_remove_config, {"config"}),
        (_config_becomes_directory, {"config"}),
        (_config_not_utf8, {"config"}),
        (_remove_component, {"components"}),
        (_loosen_token, {"token file"}),
        (_token_becomes_directory, {"token file"}),
        (_secrets_become_file, {"token file"}),
        (_lock_becomes_directory, {"instance lock"}),
        (_token_path_and_lock_broken, {"token file", "instance lock"}),
        (_journal_too_new, {"journal"}),
        (_journal_not_a_database, {"journal"}),
        (_journal_becomes_directory, {"journal"}),
    ],
)
def test_doctor_names_each_failed_check_and_exits_non_zero(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    breakage: _Breakage,
    failed: set[str],
) -> None:
    _instance(tmp_path)
    breakage(tmp_path)

    assert _run_doctor(tmp_path) == 1
    assert _failed(capsys.readouterr().out) == failed


def test_doctor_names_a_component_whose_mcp_link_does_not_come_up(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _instance(tmp_path)

    assert _run_doctor(tmp_path, _unreachable_link) == 1
    assert _failed(capsys.readouterr().out) == {"link api"}


def test_doctor_reports_every_component_whose_real_mcp_transport_dies_on_start(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    directory = _instance(tmp_path)
    second = tmp_path / "web"
    second.mkdir()
    with (directory / "config.toml").open("a", encoding="utf-8") as config:
        config.write(f'\n[[components]]\nname = "web"\npath = "{second}"\n')
    composed = partial(
        compose, link=lambda _executable, project: stdio_connector(sys.executable, project)
    )

    assert cli.main(["doctor", _PRODUCT, "--home", str(tmp_path)], composed) == 1
    assert _failed(capsys.readouterr().out) == {"link api", "link web"}


def test_doctor_shows_a_held_instance_lock_as_running_and_passes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with instance_lock(_instance(tmp_path)):
        assert _run_doctor(tmp_path) == 0

    output = capsys.readouterr().out
    assert _failed(output) == set()
    assert "instance lock: running" in output


def test_run_is_refused_while_another_holder_has_the_instance_lock(tmp_path: Path) -> None:
    with instance_lock(_instance(tmp_path)):
        assert main(["run", _PRODUCT, "--home", str(tmp_path)]) == 1


def test_run_is_refused_when_the_lock_file_cannot_be_opened(tmp_path: Path) -> None:
    _instance(tmp_path)
    _lock_becomes_directory(tmp_path)

    assert main(["run", _PRODUCT, "--home", str(tmp_path)]) == 1


def _exit_with(argv: list[str]) -> None:
    sys.exit(cli.main(argv, partial(compose, link=lambda _executable, _project: _answering_link)))


def test_run_stops_with_exit_code_zero_on_sigterm_and_releases_the_lock(tmp_path: Path) -> None:
    """The child inherits an ignored SIGTERM from the parent, so a signal that arrives before
    the child installs its handler is dropped, and the parent sends it again until the child stops.
    """
    directory = _instance(tmp_path)
    process = multiprocessing.get_context("spawn").Process(
        target=_exit_with, args=(["run", _PRODUCT, "--home", str(tmp_path)],)
    )
    inherited = signal.signal(signal.SIGTERM, signal.SIG_IGN)
    try:
        process.start()
    finally:
        signal.signal(signal.SIGTERM, inherited)
    assert process.pid is not None
    try:
        for _ in range(_ATTEMPTS):
            os.kill(process.pid, signal.SIGTERM)
            process.join(_ATTEMPT_WAIT_S)
            if not process.is_alive():
                break
    finally:
        if process.is_alive():
            process.kill()
        process.join()

    assert process.exitcode == 0
    with instance_lock(directory):
        pass


class _DownSession(_StatusSession):
    async def call_tool(self, name: str, arguments: dict[str, Any], *_ignored: object) -> object:
        if name == "wait_for_events" and arguments["since_id"] == 0:
            return CallToolResult(
                content=[],
                structuredContent={
                    "result": [
                        {
                            "id": 1,
                            "kind": "engine_started",
                            "ts": datetime(2026, 1, 1, tzinfo=UTC).isoformat(),
                            "task_id": None,
                            "message": "started",
                            "data": {},
                        }
                    ]
                },
                isError=False,
            )
        if name != "status":
            return await super().call_tool(name, arguments)
        return CallToolResult(
            content=[], structuredContent={"engine_up": False, "quotas": {}}, isError=False
        )


@asynccontextmanager
async def _engine_down_link() -> AsyncGenerator[ToolSession]:
    yield _DownSession()  # pyright: ignore[reportReturnType]


def _stop_when_signaled(journal_file: Path) -> None:
    pause = threading.Event()
    for _ in range(_ATTEMPTS):
        pause.wait(_ATTEMPT_WAIT_S)
        database = Database.open(journal_file)
        try:
            found = database.connection.execute("SELECT 1 FROM signals").fetchone()
        finally:
            database.close()
        if found:
            break
    os.kill(os.getpid(), signal.SIGTERM)


def test_run_checks_liveness_and_signals_an_engine_that_is_down(tmp_path: Path) -> None:
    directory = _instance(tmp_path)
    journal_file = directory / JOURNAL_FILE_NAME
    stopper = threading.Thread(target=_stop_when_signaled, args=(journal_file,))
    stopper.start()
    composed = partial(compose, link=lambda _executable, _project: _engine_down_link)

    assert cli.main(["run", _PRODUCT, "--home", str(tmp_path)], composed) == 0

    stopper.join()
    database = Database.open(journal_file)
    ids = [row["id"] for row in database.connection.execute("SELECT id FROM signals")]
    database.close()
    assert ids == ["api:engine_down:1"]


@pytest.fixture
def user_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _UserEnvironment:
    home = tmp_path / "user"
    home.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    executable = bin_dir / "shikiri-yaku"
    executable.write_text("", encoding="utf-8")
    executable.chmod(0o755)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", str(bin_dir))
    return _UserEnvironment(home, executable)


def test_install_service_writes_the_unit_of_the_run_command_under_home(
    tmp_path: Path, user_environment: _UserEnvironment
) -> None:
    instances = tmp_path / "instances"
    _instance(instances)

    assert main(["install-service", _PRODUCT, "--home", str(instances)]) == 0

    program = [str(user_environment.executable), "run", _PRODUCT, "--home", str(instances)]
    expected = service_file(
        sys.platform,
        user_environment.home,
        _PRODUCT,
        program,
        str(user_environment.executable.parent),
    )
    assert expected.path.is_relative_to(user_environment.home)
    assert expected.path.read_bytes() == expected.content


def test_install_service_writes_an_absolute_executable_for_a_relative_path_entry(
    tmp_path: Path, user_environment: _UserEnvironment, monkeypatch: pytest.MonkeyPatch
) -> None:
    instances = tmp_path / "instances"
    _instance(instances)
    relative_bin = tmp_path / "local-bin"
    relative_bin.mkdir()
    executable = relative_bin / "shikiri-yaku"
    executable.write_text("", encoding="utf-8")
    executable.chmod(0o755)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", "local-bin")

    assert main(["install-service", _PRODUCT, "--home", str(instances)]) == 0

    program = [str(executable), "run", _PRODUCT, "--home", str(instances)]
    expected = service_file(sys.platform, user_environment.home, _PRODUCT, program, "local-bin")
    assert expected.path.read_bytes() == expected.content


def test_install_service_refuses_an_instance_that_cannot_run(
    tmp_path: Path, user_environment: _UserEnvironment
) -> None:
    assert main(["install-service", _PRODUCT, "--home", str(tmp_path)]) == 1
    assert list(user_environment.home.iterdir()) == []


def test_install_service_refuses_when_the_login_path_has_no_shikiri_yaku(
    tmp_path: Path,
    user_environment: _UserEnvironment,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instances = tmp_path / "instances"
    _instance(instances)
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))

    assert main(["install-service", _PRODUCT, "--home", str(instances)]) == 1
    assert list(user_environment.home.iterdir()) == []


@pytest.mark.parametrize("name", ["../escape", "a/b", "-shop", "sh op", ""])
def test_a_product_name_that_is_not_a_plain_name_is_refused_before_anything_is_written(
    tmp_path: Path, user_environment: _UserEnvironment, name: str
) -> None:
    with pytest.raises(SystemExit):
        main(["install-service", name, "--home", str(tmp_path)])

    assert list(user_environment.home.iterdir()) == []
