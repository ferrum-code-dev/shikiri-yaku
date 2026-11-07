from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Final

import pytest
from scripts import deploy

from shikiri_yaku.journal import Database
from shikiri_yaku.settings import product_names

if TYPE_CHECKING:
    from collections.abc import Sequence

RELEASE: Final = "0.2.0"
PREVIOUS: Final = "0.1.0"
PRODUCT: Final = "shop"


def wheel_name(version: str) -> str:
    return f"shikiri_yaku-{version}-py3-none-any.whl"


def _ok(stdout: str = "") -> deploy.CommandResult:
    return deploy.CommandResult(0, stdout, "")


def _failed() -> deploy.CommandResult:
    return deploy.CommandResult(1, "", "refused")


def _no_wait(_seconds: float) -> None:
    """Stand in for the pause between doctor runs, so the tests do not wait."""


def _version_in(name: str) -> str:
    return name.split("-")[1]


@dataclass
class FakeHost:
    """Answers the commands the script runs and records them in order.

    A fault applies to the release being deployed, so the previous release stays healthy and a
    rollback to it succeeds. `doctor_refusals` makes the first that many doctor runs fail on any
    release, to model a service that is still starting.
    """

    running: str = PREVIOUS
    faults: frozenset[str] = frozenset()
    doctor_refusals: int = 0
    status: str = ""
    tags: str = f"v{RELEASE}"
    calls: list[tuple[str, ...]] = field(default_factory=list)

    def __call__(self, argv: Sequence[str], _cwd: Path) -> deploy.CommandResult:
        command = tuple(argv)
        self.calls.append(command)
        if command[:2] == ("uv", "build"):
            (Path(command[-1]) / wheel_name(RELEASE)).write_bytes(b"wheel")
            return _ok()
        if command[:3] == ("uv", "tool", "install"):
            return self._install(Path(command[-1]))
        return self._answer(command)

    def _answer(self, command: tuple[str, ...]) -> deploy.CommandResult:
        if command == deploy.GIT_STATUS:
            return _ok(self.status)
        if command == deploy.GIT_TAGS_AT_HEAD:
            return _ok(self.tags)
        if command == deploy.GATE:
            return _failed() if "gate" in self.faults else _ok()
        if command == deploy.RESTART:
            return self._restart()
        if command == deploy.VERSION:
            shown = "0.0.0" if self._new_release_fails("version") else self.running
            return _ok(f"shikiri-yaku {shown}\n")
        if command[-1] == "doctor":
            return self._doctor()
        message = f"unexpected command: {command}"
        raise AssertionError(message)

    def _doctor(self) -> deploy.CommandResult:
        if self.doctor_refusals > 0:
            self.doctor_refusals -= 1
            return _failed()
        return _failed() if self._new_release_fails("doctor") else _ok()

    def _new_release_fails(self, check: str) -> bool:
        return check in self.faults and self.running == RELEASE

    def _install(self, wheel: Path) -> deploy.CommandResult:
        version = _version_in(wheel.name)
        if version == RELEASE and "install" in self.faults:
            return _failed()
        self.running = version
        return _ok()

    def _restart(self) -> deploy.CommandResult:
        broken = self._new_release_fails("restart")
        entries = [
            {"product": PRODUCT, "passed": not broken, "result": "stuck" if broken else "restarted"}
        ]
        return deploy.CommandResult(1 if broken else 0, json.dumps(entries), "")


def _layout(root: Path) -> deploy.Layout:
    checkout = root / "checkout"
    checkout.mkdir()
    (checkout / "pyproject.toml").write_text(
        f'[project]\nname = "shikiri-yaku"\nversion = "{RELEASE}"\n', encoding="utf-8"
    )
    return deploy.Layout(checkout, root / "releases", root / "home")


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


def _previous_release(layout: deploy.Layout) -> Path:
    layout.store.mkdir(parents=True)
    wheel = layout.store / wheel_name(PREVIOUS)
    wheel.write_bytes(b"previous")
    (layout.store / "current").write_text(f"{wheel.name}\n", encoding="utf-8")
    return wheel


@pytest.fixture
def layout(tmp_path: Path) -> deploy.Layout:
    return _layout(tmp_path)


@pytest.mark.parametrize(
    ("status", "tags", "faults", "expected"),
    [
        pytest.param(
            " M README.md", f"v{RELEASE}", frozenset[str](), [deploy.GIT_STATUS], id="dirty-tree"
        ),
        pytest.param(
            "",
            "",
            frozenset[str](),
            [deploy.GIT_STATUS, deploy.GIT_TAGS_AT_HEAD],
            id="untagged-head",
        ),
        pytest.param(
            "",
            f"v{PREVIOUS}",
            frozenset[str](),
            [deploy.GIT_STATUS, deploy.GIT_TAGS_AT_HEAD],
            id="tag-differs-from-version",
        ),
        pytest.param(
            "",
            f"v{RELEASE}",
            frozenset({"gate"}),
            [deploy.GIT_STATUS, deploy.GIT_TAGS_AT_HEAD, deploy.GATE],
            id="failing-gate",
        ),
    ],
)
def test_a_refused_release_stops_before_anything_is_built(
    layout: deploy.Layout,
    status: str,
    tags: str,
    faults: frozenset[str],
    expected: list[tuple[str, ...]],
) -> None:
    host = FakeHost(status=status, tags=tags, faults=faults)

    assert deploy.deploy(host, _no_wait, layout) == 1
    assert host.calls == expected
    assert not layout.store.exists()


def test_a_successful_deploy_runs_the_documented_commands_in_order(layout: deploy.Layout) -> None:
    _journal(layout.home / PRODUCT)
    host = FakeHost()

    assert deploy.deploy(host, _no_wait, layout) == 0

    installed = layout.store / wheel_name(RELEASE)
    assert host.calls == [
        deploy.GIT_STATUS,
        deploy.GIT_TAGS_AT_HEAD,
        deploy.GATE,
        deploy.build_argv(layout.store),
        deploy.install_argv(installed),
        deploy.RESTART,
        deploy.VERSION,
        deploy.doctor_argv(PRODUCT),
    ]
    assert (layout.store / "current").read_text(encoding="utf-8") == f"{installed.name}\n"


@pytest.mark.parametrize("fault", ["install", "restart", "version", "doctor"])
def test_a_failed_verification_reinstalls_the_release_current_names(
    layout: deploy.Layout, fault: str
) -> None:
    previous = _previous_release(layout)
    _journal(layout.home / PRODUCT)
    host = FakeHost(faults=frozenset({fault}))

    assert deploy.deploy(host, _no_wait, layout) == 1

    new_install = deploy.install_argv(layout.store / wheel_name(RELEASE))
    rollback_install = deploy.install_argv(previous)
    assert host.running == PREVIOUS
    assert rollback_install in host.calls[host.calls.index(new_install) + 1 :]
    assert deploy.RESTART in host.calls[host.calls.index(rollback_install) :]
    assert (layout.store / "current").read_text(encoding="utf-8") == f"{previous.name}\n"


def test_with_no_current_a_failed_deploy_installs_only_the_new_release(
    layout: deploy.Layout,
) -> None:
    _journal(layout.home / PRODUCT)
    host = FakeHost(faults=frozenset({"doctor"}))

    assert deploy.deploy(host, _no_wait, layout) == 1

    installs = [command for command in host.calls if command[:3] == ("uv", "tool", "install")]
    assert installs == [deploy.install_argv(layout.store / wheel_name(RELEASE))]
    assert deploy.read_current(layout.store) is None


def test_a_doctor_that_fails_twice_then_passes_does_not_roll_back(layout: deploy.Layout) -> None:
    _journal(layout.home / PRODUCT)
    host = FakeHost(doctor_refusals=2)
    pauses: list[float] = []

    assert deploy.deploy(host, pauses.append, layout) == 0

    assert pauses == [deploy.PAUSE_S, deploy.PAUSE_S]
    assert host.calls.count(deploy.doctor_argv(PRODUCT)) == 3
    assert host.running == RELEASE


def test_a_doctor_that_never_passes_rolls_back_after_exactly_attempts_runs(
    layout: deploy.Layout,
) -> None:
    previous = _previous_release(layout)
    _journal(layout.home / PRODUCT)
    host = FakeHost(faults=frozenset({"doctor"}))
    pauses: list[float] = []

    assert deploy.deploy(host, pauses.append, layout) == 1

    new_install = host.calls.index(deploy.install_argv(layout.store / wheel_name(RELEASE)))
    rollback_install = host.calls.index(deploy.install_argv(previous))
    tried = host.calls[new_install:rollback_install]
    assert tried.count(deploy.doctor_argv(PRODUCT)) == deploy.ATTEMPTS
    assert len(pauses) == deploy.ATTEMPTS - 1
    assert host.running == PREVIOUS


def test_a_failing_doctor_is_reported_with_the_last_lines_of_its_output(
    layout: deploy.Layout,
) -> None:
    output = [f"check {number}" for number in range(30)]
    calls: list[tuple[str, ...]] = []

    def run(argv: Sequence[str], _cwd: Path) -> deploy.CommandResult:
        calls.append(tuple(argv))
        if argv[-1] == "doctor":
            return deploy.CommandResult(1, "\n".join(output) + "\n", "")
        return _ok(f"{deploy.PROGRAM} {RELEASE}\n")

    problems = deploy.verify(run, _no_wait, layout.checkout, RELEASE, [PRODUCT])

    assert len(problems) == 1
    assert problems[0].splitlines()[1:] == output[-deploy.TAIL_LINES :]
    assert calls.count(deploy.doctor_argv(PRODUCT)) == deploy.ATTEMPTS


def test_a_wrong_version_rolls_back_at_once_without_retrying_doctor(
    layout: deploy.Layout,
) -> None:
    previous = _previous_release(layout)
    _journal(layout.home / PRODUCT)
    host = FakeHost(faults=frozenset({"version"}))
    pauses: list[float] = []

    assert deploy.deploy(host, pauses.append, layout) == 1

    new_install = host.calls.index(deploy.install_argv(layout.store / wheel_name(RELEASE)))
    rollback_install = host.calls.index(deploy.install_argv(previous))
    tried = host.calls[new_install:rollback_install]
    assert tried.count(deploy.doctor_argv(PRODUCT)) == 1
    assert pauses == []
    assert host.running == PREVIOUS


def test_rollback_reinstalls_current_and_verifies_the_old_release(layout: deploy.Layout) -> None:
    previous = _previous_release(layout)
    host = FakeHost(running=RELEASE)

    assert deploy.rollback_only(host, _no_wait, layout) == 0

    assert host.calls == [
        deploy.install_argv(previous),
        deploy.RESTART,
        deploy.VERSION,
        deploy.doctor_argv(PRODUCT),
    ]


def test_a_journal_copy_is_openable_and_named_by_its_schema_version(layout: deploy.Layout) -> None:
    version = _journal(layout.home / PRODUCT)

    (backup,) = deploy.backup_journals(layout.home)

    assert backup.copy == layout.home / PRODUCT / "backups" / f"journal-schema{version}.db"
    reopened = Database.open(backup.copy)
    try:
        (copied,) = reopened.connection.execute("PRAGMA user_version").fetchone()
        (integrity,) = reopened.connection.execute("PRAGMA integrity_check").fetchone()
    finally:
        reopened.close()
    assert copied == version
    assert integrity == "ok"


def test_the_last_three_backups_of_a_product_are_kept(layout: deploy.Layout) -> None:
    version = _journal(layout.home / PRODUCT)
    folder = layout.home / PRODUCT / "backups"
    folder.mkdir()
    older = [folder / f"journal-schema{number}.db" for number in (1, 2, 3)]
    for stamp, path in enumerate(older, start=1000):
        path.write_bytes(b"old")
        os.utime(path, (stamp, stamp))

    deploy.backup_journals(layout.home)

    remaining = sorted(path.name for path in folder.glob(deploy.BACKUP_GLOB))
    assert remaining == sorted([older[1].name, older[2].name, f"journal-schema{version}.db"])


def test_the_last_five_wheels_are_kept_and_the_current_one_is_never_removed(
    layout: deploy.Layout,
) -> None:
    layout.store.mkdir(parents=True)
    wheels = [layout.store / wheel_name(f"0.{number}.0") for number in range(1, 8)]
    for stamp, path in enumerate(wheels, start=1000):
        path.write_bytes(b"wheel")
        os.utime(path, (stamp, stamp))
    current = layout.store / "current"
    current.write_text(f"{wheels[0].name}\n", encoding="utf-8")

    deploy.prune_releases(layout.store, wheels[0])

    assert sorted(path.name for path in layout.store.glob("*.whl")) == sorted(
        path.name for path in [wheels[0], *wheels[2:]]
    )
    assert current.is_file()


def test_products_are_found_as_the_command_line_finds_them(layout: deploy.Layout) -> None:
    for name in ("shop", "ferrum-code"):
        _journal(layout.home / name)
    (layout.home / "no-config").mkdir()
    (layout.home / "bad name").mkdir()

    assert deploy.products(layout.home) == product_names(layout.home)


def test_the_checkout_virtualenv_is_off_the_path_of_the_commands() -> None:
    environment = deploy.command_environment(
        {"VIRTUAL_ENV": "/checkout/.venv", "PATH": "/checkout/.venv/bin:/usr/bin:/bin"}
    )

    assert environment["PATH"] == "/usr/bin:/bin"
