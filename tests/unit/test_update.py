"""The update steps: install, verify, roll back on failure, and keep the store and backups tidy."""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Final

import pytest

from shikiri_yaku.journal import Database
from shikiri_yaku.settings import UpdateSettings, product_names
from shikiri_yaku.update import Host, Layout, process, run_update, store, verify
from shikiri_yaku.update import backup as update_backup
from shikiri_yaku.update.host import CommandResult, UpdateError

if TYPE_CHECKING:
    from collections.abc import Sequence

    from shikiri_yaku.update.host import Pause, Runner

RELEASE: Final = "0.2.0"
PREVIOUS: Final = "0.1.0"
PRODUCT: Final = "shop"
NUMBERS: Final = UpdateSettings()


def wheel_name(version: str) -> str:
    return f"shikiri_yaku-{version}-py3-none-any.whl"


def _ok(stdout: str = "") -> CommandResult:
    return CommandResult(0, stdout, "")


def _failed() -> CommandResult:
    return CommandResult(1, "", "refused")


def _no_wait(_seconds: float) -> None:
    """Stand in for the pause between doctor runs, so the tests do not wait."""


def _version_in(name: str) -> str:
    return name.split("-")[1]


@dataclass
class FakeHost:
    """Answers the commands the update runs and records them in order.

    A fault applies to the release being installed, so the previous release stays healthy and a
    rollback to it succeeds. `doctor_refusals` makes the first that many doctor runs fail on any
    release, to model a service that is still starting.
    """

    running: str = PREVIOUS
    faults: frozenset[str] = frozenset()
    doctor_refusals: int = 0
    calls: list[tuple[str, ...]] = field(default_factory=list)

    def __call__(self, argv: Sequence[str]) -> CommandResult:
        command = tuple(argv)
        self.calls.append(command)
        if command[:3] == ("uv", "tool", "install"):
            return self._install(Path(command[-1]))
        return self._answer(command)

    def _answer(self, command: tuple[str, ...]) -> CommandResult:
        if command == verify.RESTART:
            return self._restart()
        if command == verify.VERSION:
            shown = "0.0.0" if self._new_release_fails("version") else self.running
            return _ok(f"shikiri-yaku {shown}\n")
        if command[-1] == "doctor":
            return self._doctor()
        message = f"unexpected command: {command}"
        raise AssertionError(message)

    def _doctor(self) -> CommandResult:
        if self.doctor_refusals > 0:
            self.doctor_refusals -= 1
            return _failed()
        return _failed() if self._new_release_fails("doctor") else _ok()

    def _new_release_fails(self, check: str) -> bool:
        return check in self.faults and self.running == RELEASE

    def _install(self, wheel: Path) -> CommandResult:
        version = _version_in(wheel.name)
        if version == RELEASE and "install" in self.faults:
            return _failed()
        self.running = version
        return _ok()

    def _restart(self) -> CommandResult:
        broken = self._new_release_fails("restart")
        entries = [
            {"product": PRODUCT, "passed": not broken, "result": "stuck" if broken else "restarted"}
        ]
        return CommandResult(1 if broken else 0, json.dumps(entries), "")


def _host(layout: Layout, run: Runner, pause: Pause = _no_wait) -> Host:
    return Host(run, pause, layout, NUMBERS)


def _layout(root: Path) -> Layout:
    return Layout(root / "releases", root / "home")


def _journal(product: Path) -> int:
    """Create a product with a config and a journal; return the journal's schema version."""
    product.mkdir(parents=True, exist_ok=True)
    (product / "config.toml").write_text("", encoding="utf-8")
    database = Database.open(product / "journal.db")
    try:
        (version,) = database.connection.execute("PRAGMA user_version").fetchone()
    finally:
        database.close()
    return int(version)


def _previous_release(layout: Layout) -> Path:
    layout.store.mkdir(parents=True)
    wheel = layout.store / wheel_name(PREVIOUS)
    wheel.write_bytes(b"previous")
    (layout.store / store.CURRENT_NAME).write_text(f"{wheel.name}\n", encoding="utf-8")
    return wheel


def _new_wheel(root: Path) -> Path:
    """A wheel to install, as a user downloads it: outside the store, under its release name."""
    folder = root / "downloads"
    folder.mkdir(exist_ok=True)
    wheel = folder / wheel_name(RELEASE)
    wheel.write_bytes(b"wheel")
    return wheel


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture
def layout(root: Path) -> Layout:
    return _layout(root)


def _current(layout: Layout) -> str:
    return (layout.store / store.CURRENT_NAME).read_text(encoding="utf-8")


def test_a_successful_update_stores_the_wheel_installs_it_and_writes_current(
    root: Path, layout: Layout
) -> None:
    _journal(layout.home / PRODUCT)
    fake = FakeHost()
    wheel = _new_wheel(root)

    assert run_update(_host(layout, fake), wheel, rollback=False) == 0

    installed = layout.store / wheel_name(RELEASE)
    assert fake.calls == [
        verify.install_argv(installed),
        verify.RESTART,
        verify.VERSION,
        verify.doctor_argv(PRODUCT),
    ]
    assert _current(layout) == f"{installed.name}\n"


@pytest.mark.parametrize("fault", ["install", "restart", "version", "doctor"])
def test_a_failed_verification_reinstalls_the_release_current_names(
    root: Path, layout: Layout, fault: str
) -> None:
    previous = _previous_release(layout)
    _journal(layout.home / PRODUCT)
    fake = FakeHost(faults=frozenset({fault}))

    assert run_update(_host(layout, fake), _new_wheel(root), rollback=False) == 1

    new_install = verify.install_argv(layout.store / wheel_name(RELEASE))
    rollback_install = verify.install_argv(previous)
    assert fake.running == PREVIOUS
    assert rollback_install in fake.calls[fake.calls.index(new_install) + 1 :]
    assert verify.RESTART in fake.calls[fake.calls.index(rollback_install) :]
    assert _current(layout) == f"{previous.name}\n"


def test_with_no_current_a_failed_update_installs_only_the_new_release(
    root: Path, layout: Layout
) -> None:
    _journal(layout.home / PRODUCT)
    fake = FakeHost(faults=frozenset({"doctor"}))

    assert run_update(_host(layout, fake), _new_wheel(root), rollback=False) == 1

    installs = [command for command in fake.calls if command[:3] == ("uv", "tool", "install")]
    assert installs == [verify.install_argv(layout.store / wheel_name(RELEASE))]
    assert store.read_current(layout.store) is None


def test_a_doctor_that_fails_twice_then_passes_does_not_roll_back(
    root: Path, layout: Layout
) -> None:
    _journal(layout.home / PRODUCT)
    fake = FakeHost(doctor_refusals=2)
    pauses: list[float] = []

    assert run_update(_host(layout, fake, pauses.append), _new_wheel(root), rollback=False) == 0

    assert pauses == [NUMBERS.verify_pause_s, NUMBERS.verify_pause_s]
    assert fake.calls.count(verify.doctor_argv(PRODUCT)) == 3
    assert fake.running == RELEASE


def test_a_doctor_that_never_passes_rolls_back_after_exactly_attempts_runs(
    root: Path, layout: Layout
) -> None:
    previous = _previous_release(layout)
    _journal(layout.home / PRODUCT)
    fake = FakeHost(faults=frozenset({"doctor"}))
    pauses: list[float] = []

    assert run_update(_host(layout, fake, pauses.append), _new_wheel(root), rollback=False) == 1

    new_install = fake.calls.index(verify.install_argv(layout.store / wheel_name(RELEASE)))
    rollback_install = fake.calls.index(verify.install_argv(previous))
    tried = fake.calls[new_install:rollback_install]
    assert tried.count(verify.doctor_argv(PRODUCT)) == NUMBERS.verify_attempts
    assert len(pauses) == NUMBERS.verify_attempts - 1
    assert fake.running == PREVIOUS


def test_a_failing_doctor_is_reported_with_the_last_lines_of_its_output(
    layout: Layout,
) -> None:
    output = [f"check {number}" for number in range(30)]
    calls: list[tuple[str, ...]] = []

    def run(argv: Sequence[str]) -> CommandResult:
        calls.append(tuple(argv))
        if argv[-1] == "doctor":
            return CommandResult(1, "\n".join(output) + "\n", "")
        return _ok(f"shikiri-yaku {RELEASE}\n")

    problems = verify.verify(_host(layout, run), RELEASE, [PRODUCT])

    assert len(problems) == 1
    assert problems[0].splitlines()[1:] == output[-NUMBERS.output_tail_lines :]
    assert calls.count(verify.doctor_argv(PRODUCT)) == NUMBERS.verify_attempts


def test_a_wrong_version_rolls_back_at_once_without_retrying_doctor(
    root: Path, layout: Layout
) -> None:
    previous = _previous_release(layout)
    _journal(layout.home / PRODUCT)
    fake = FakeHost(faults=frozenset({"version"}))
    pauses: list[float] = []

    assert run_update(_host(layout, fake, pauses.append), _new_wheel(root), rollback=False) == 1

    new_install = fake.calls.index(verify.install_argv(layout.store / wheel_name(RELEASE)))
    rollback_install = fake.calls.index(verify.install_argv(previous))
    tried = fake.calls[new_install:rollback_install]
    assert tried.count(verify.doctor_argv(PRODUCT)) == 1
    assert pauses == []
    assert fake.running == PREVIOUS


def test_rollback_reinstalls_current_and_verifies_the_old_release(layout: Layout) -> None:
    previous = _previous_release(layout)
    fake = FakeHost(running=RELEASE)

    assert run_update(_host(layout, fake), None, rollback=True) == 0

    assert fake.calls == [
        verify.install_argv(previous),
        verify.RESTART,
        verify.VERSION,
        verify.doctor_argv(PRODUCT),
    ]


def test_the_module_set_after_the_install_is_the_set_before_it(root: Path, layout: Layout) -> None:
    """The install replaces this package, so the running process must not import another module."""
    _journal(layout.home / PRODUCT)
    before = _loaded_modules()
    fake = FakeHost()

    assert run_update(_host(layout, fake), _new_wheel(root), rollback=False) == 0

    assert fake.calls[0][:3] == ("uv", "tool", "install")
    assert _loaded_modules() == before


def _loaded_modules() -> frozenset[str]:
    return frozenset(
        name for name in sys.modules if name == "shikiri_yaku" or name.startswith("shikiri_yaku.")
    )


def test_a_journal_copy_is_openable_and_named_by_its_schema_version(layout: Layout) -> None:
    version = _journal(layout.home / PRODUCT)

    update_backup.backup_journals(layout.home, NUMBERS.keep_backups)

    (backup,) = update_backup.existing_backups(layout.home)
    assert backup.copy == layout.home / PRODUCT / "backups" / f"journal-schema{version}.db"
    reopened = Database.open(backup.copy)
    try:
        (copied,) = reopened.connection.execute("PRAGMA user_version").fetchone()
        (integrity,) = reopened.connection.execute("PRAGMA integrity_check").fetchone()
    finally:
        reopened.close()
    assert copied == version
    assert integrity == "ok"


def test_the_last_three_backups_of_a_product_are_kept(layout: Layout) -> None:
    version = _journal(layout.home / PRODUCT)
    folder = layout.home / PRODUCT / "backups"
    folder.mkdir()
    older = [folder / f"journal-schema{number}.db" for number in (1, 2, 3)]
    for stamp, path in enumerate(older, start=1000):
        path.write_bytes(b"old")
        os.utime(path, (stamp, stamp))

    update_backup.backup_journals(layout.home, NUMBERS.keep_backups)

    remaining = sorted(path.name for path in folder.glob(update_backup.BACKUP_GLOB))
    assert remaining == sorted([older[1].name, older[2].name, f"journal-schema{version}.db"])


def test_only_products_with_a_config_are_backed_up(layout: Layout) -> None:
    _journal(layout.home / PRODUCT)
    _journal(layout.home / "ferrum-code")
    (layout.home / "no-config").mkdir()
    (layout.home / "bad name").mkdir()

    update_backup.backup_journals(layout.home, NUMBERS.keep_backups)

    found = sorted(backup.product for backup in update_backup.existing_backups(layout.home))
    assert found == sorted(product_names(layout.home))


def test_the_last_five_wheels_are_kept_and_the_current_one_is_never_removed(
    layout: Layout,
) -> None:
    layout.store.mkdir(parents=True)
    wheels = [layout.store / wheel_name(f"0.{number}.0") for number in range(1, 8)]
    for stamp, path in enumerate(wheels, start=1000):
        path.write_bytes(b"wheel")
        os.utime(path, (stamp, stamp))
    current = layout.store / store.CURRENT_NAME
    current.write_text(f"{wheels[0].name}\n", encoding="utf-8")

    store.prune_releases(layout.store, wheels[0], NUMBERS.keep_wheels)

    assert sorted(path.name for path in layout.store.glob("*.whl")) == sorted(
        path.name for path in [wheels[0], *wheels[2:]]
    )
    assert current.is_file()


def test_the_checkout_virtualenv_is_off_the_path_of_the_commands() -> None:
    environment = process.command_environment(
        {"VIRTUAL_ENV": "/checkout/.venv", "PATH": "/checkout/.venv/bin:/usr/bin:/bin"}
    )

    assert environment["PATH"] == "/usr/bin:/bin"


def test_a_file_that_is_not_a_shikiri_yaku_wheel_is_refused(root: Path, layout: Layout) -> None:
    other = root / "other-0.1.0-py3-none-any.whl"
    other.write_bytes(b"wheel")

    with pytest.raises(UpdateError, match="is not a shikiri_yaku wheel"):
        store.store_wheel(layout.store, other)
    assert not layout.store.exists()


def test_update_needs_exactly_one_of_a_wheel_or_rollback(
    root: Path, layout: Layout, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeHost()

    assert run_update(_host(layout, fake), None, rollback=False) == 1
    assert run_update(_host(layout, fake), _new_wheel(root), rollback=True) == 1

    assert "--wheel <path>" in capsys.readouterr().err
    assert fake.calls == []
