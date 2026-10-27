"""Integration tests of run and doctor through bootstrap.main (design §3.1, §10)."""

from __future__ import annotations

import multiprocessing
import os
import signal
import sys
from typing import TYPE_CHECKING, Final

import pytest

from shikiri_yaku.bootstrap import main
from shikiri_yaku.instance import JOURNAL_FILE_NAME, instance_lock
from shikiri_yaku.journal import Database
from shikiri_yaku.journal.database import LATEST_VERSION

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

_PRODUCT: Final = "shop"
_ATTEMPTS: Final = 100
_ATTEMPT_WAIT_S: Final = 0.1

type _Breakage = Callable[[Path], None]


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


def _run_doctor(home: Path) -> int:
    return main(["doctor", _PRODUCT, "--home", str(home)])


def test_doctor_passes_a_valid_instance(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _instance(tmp_path)

    assert _run_doctor(tmp_path) == 0
    assert _failed(capsys.readouterr().out) == set()


@pytest.mark.parametrize(
    ("breakage", "failed"),
    [
        (_remove_config, {"config"}),
        (_remove_component, {"components"}),
        (_loosen_token, {"token file"}),
        (_journal_too_new, {"journal"}),
        (_journal_not_a_database, {"journal"}),
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


def test_doctor_names_the_instance_lock_while_another_holder_runs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with instance_lock(_instance(tmp_path)):
        assert _run_doctor(tmp_path) == 1

    assert _failed(capsys.readouterr().out) == {"instance lock"}


def test_run_is_refused_while_another_holder_has_the_instance_lock(tmp_path: Path) -> None:
    with instance_lock(_instance(tmp_path)):
        assert main(["run", _PRODUCT, "--home", str(tmp_path)]) == 1


def _exit_with(argv: list[str]) -> None:
    sys.exit(main(argv))


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
