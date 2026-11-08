"""The update command line and the download path: which arguments reach the update, where the home
comes from, and what happens to a newest release before anything is installed."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import TYPE_CHECKING, Final

import pytest

from shikiri_yaku import cli
from shikiri_yaku.adapters.commands import ProcessCommandRunner
from shikiri_yaku.bootstrap import (
    adopt_topic,
    compose,
    forget_topic,
    stats_measures,
    update_to_newest,
)
from shikiri_yaku.settings import CliSettings, UpdateSettings
from shikiri_yaku.update import Host, Layout
from shikiri_yaku.update.host import CommandResult
from tests.support.releases import FakeReleaseSource, asset_url, release

if TYPE_CHECKING:
    from collections.abc import Sequence

WHEEL: Final = "/downloads/shikiri_yaku-0.2.0-py3-none-any.whl"
NAME: Final = "shikiri_yaku-0.2.0-py3-none-any.whl"
BYTES: Final = b"the wheel"
SUMS_URL: Final = asset_url("v0.2.0", "SHA256SUMS")
WHEEL_URL: Final = asset_url("v0.2.0", NAME)


def _update(argv: Sequence[str], recorded: list[tuple[Path | None, bool, bool, Path]]) -> int:
    def update_host(wheel: Path | None, rollback: bool, check: bool, home: Path) -> int:  # noqa: FBT001
        recorded.append((wheel, rollback, check, home))
        return 0

    return cli.main(
        argv,
        compose,
        stats_measures,
        adopt_topic,
        forget_topic,
        commands=ProcessCommandRunner(),
        cli_settings=CliSettings(),
        update_host=update_host,
    )


def test_update_passes_the_wheel_and_the_home_on(tmp_path: Path) -> None:
    recorded: list[tuple[Path | None, bool, bool, Path]] = []

    code = _update(["--home", str(tmp_path), "update", "--wheel", WHEEL], recorded)

    assert code == 0
    assert recorded == [(Path(WHEEL), False, False, tmp_path)]


def test_update_rollback_passes_no_wheel_and_the_rollback_flag(tmp_path: Path) -> None:
    recorded: list[tuple[Path | None, bool, bool, Path]] = []

    code = _update(["--home", str(tmp_path), "update", "--rollback"], recorded)

    assert code == 0
    assert recorded == [(None, True, False, tmp_path)]


def test_update_check_passes_the_check_flag_alone(tmp_path: Path) -> None:
    recorded: list[tuple[Path | None, bool, bool, Path]] = []

    code = _update(["--home", str(tmp_path), "update", "--check"], recorded)

    assert code == 0
    assert recorded == [(None, False, True, tmp_path)]


@pytest.mark.parametrize("extra", [["--wheel", WHEEL], ["--rollback"]], ids=["wheel", "rollback"])
def test_check_cannot_be_combined_with_a_wheel_or_a_rollback(
    tmp_path: Path, extra: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    recorded: list[tuple[Path | None, bool, bool, Path]] = []

    code = _update(["--home", str(tmp_path), "update", "--check", *extra], recorded)

    assert code == 1
    assert recorded == []
    assert "--check cannot be combined" in capsys.readouterr().err


def test_update_is_reported_as_failing_when_no_update_is_wired() -> None:
    code = cli.main(
        ["update"],
        compose,
        stats_measures,
        adopt_topic,
        forget_topic,
        commands=ProcessCommandRunner(),
        cli_settings=CliSettings(),
    )

    assert code == 1


class _Runner:
    """Records each command; the install fails, so no release is ever marked current."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.seen: list[bytes] = []

    def __call__(self, argv: Sequence[str]) -> CommandResult:
        self.calls.append(tuple(argv))
        self.seen.append(Path(argv[-1]).read_bytes())
        return CommandResult(1, "", "install refused")


def _host(tmp_path: Path, runner: _Runner) -> Host:
    return Host(
        runner,
        lambda _seconds: None,
        Layout(tmp_path / "store", tmp_path / "home"),
        UpdateSettings(),
    )


def _source(*, damaged: bool = False) -> FakeReleaseSource:
    sums = f"{hashlib.sha256(BYTES).hexdigest()}  {NAME}\n".encode()
    return FakeReleaseSource(
        listing=[release("v0.2.0", names=(NAME, "SHA256SUMS"))],
        files={SUMS_URL: sums, WHEEL_URL: b"damaged" if damaged else BYTES},
    )


def test_check_reports_both_versions_and_installs_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runner = _Runner()
    source = _source()

    code = update_to_newest(_host(tmp_path, runner), source, "0.1.4", check=True)

    assert code == 0
    assert "installed 0.1.4\nnewest 0.2.0\nan update is available" in capsys.readouterr().out
    assert runner.calls == []
    assert source.downloads == []


def test_an_installed_newest_release_is_said_so_and_nothing_is_fetched(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runner = _Runner()
    source = _source()

    code = update_to_newest(_host(tmp_path, runner), source, "0.2.0", check=False)

    assert code == 0
    assert "0.2.0 is the newest release; nothing to update" in capsys.readouterr().out
    assert runner.calls == []
    assert source.downloads == []


def test_a_wheel_that_fails_its_checksum_is_never_installed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runner = _Runner()

    code = update_to_newest(_host(tmp_path, runner), _source(damaged=True), "0.1.4", check=False)

    assert code == 1
    assert "does not match" in capsys.readouterr().err
    assert runner.calls == []


def test_a_newer_release_is_handed_to_the_install_as_the_checked_bytes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runner = _Runner()

    code = update_to_newest(_host(tmp_path, runner), _source(), "0.1.4", check=False)

    assert code == 1
    assert "updating 0.1.4 to 0.2.0" in capsys.readouterr().out
    ((*_, installed),) = runner.calls
    assert Path(installed).name == NAME
    assert runner.seen == [BYTES]


def test_no_usable_release_fails_and_says_so(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runner = _Runner()
    source = FakeReleaseSource(listing=[release("v0.2.0", draft=True)])

    code = update_to_newest(_host(tmp_path, runner), source, "0.1.4", check=True)

    assert code == 1
    assert "no release is published" in capsys.readouterr().err
    assert runner.calls == []
