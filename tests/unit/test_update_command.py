"""The update command line: which arguments reach the update, and where the home comes from."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Final

from shikiri_yaku import cli
from shikiri_yaku.adapters.commands import ProcessCommandRunner
from shikiri_yaku.bootstrap import adopt_topic, compose, forget_topic, stats_measures
from shikiri_yaku.settings import CliSettings

if TYPE_CHECKING:
    from collections.abc import Sequence

WHEEL: Final = "/downloads/shikiri_yaku-0.2.0-py3-none-any.whl"


def _update(argv: Sequence[str], recorded: list[tuple[Path | None, bool, Path]]) -> int:
    def update_host(wheel: Path | None, rollback: bool, home: Path) -> int:  # noqa: FBT001
        recorded.append((wheel, rollback, home))
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
    recorded: list[tuple[Path | None, bool, Path]] = []

    code = _update(["--home", str(tmp_path), "update", "--wheel", WHEEL], recorded)

    assert code == 0
    assert recorded == [(Path(WHEEL), False, tmp_path)]


def test_update_rollback_passes_no_wheel_and_the_rollback_flag(tmp_path: Path) -> None:
    recorded: list[tuple[Path | None, bool, Path]] = []

    code = _update(["--home", str(tmp_path), "update", "--rollback"], recorded)

    assert code == 0
    assert recorded == [(None, True, tmp_path)]


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
