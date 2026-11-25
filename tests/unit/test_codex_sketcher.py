"""The Codex sketcher: one fixed run in an empty directory, and only a picture its thread saved."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import TYPE_CHECKING, Final

import pytest

from shikiri_yaku.adapters.agents.codex import GENERATED_IMAGES, CodexSketcher, codex_store
from shikiri_yaku.ports import CommandError, CommandResult, SketchError
from tests.support.attachments import PNG

if TYPE_CHECKING:
    from collections.abc import Sequence

THREAD: Final = "thread-1"
STARTED: Final = json.dumps({"type": "thread.started", "thread_id": THREAD})
NOT_A_PNG: Final = b"not an image"
START_FAILURE: Final = "codex could not be started"


class _Runner:
    """Answers one run as Codex does: its output, and the pictures it leaves in the store."""

    def __init__(
        self,
        store: Path,
        *,
        stdout: str = STARTED,
        returncode: int = 0,
        fail: bool = False,
        saved: dict[str, dict[str, bytes]] | None = None,
    ) -> None:
        self.store = store
        self.stdout = stdout
        self.returncode = returncode
        self.fail = fail
        self.saved = {} if saved is None else saved
        self.argv: list[str] = []
        self.stdin: str | None = None
        self.empty_directory = False

    async def run(
        self,
        argv: Sequence[str],
        *,
        timeout_s: float,
        cwd: Path | None = None,
        env: object = None,
        stdin: str | None = None,
    ) -> CommandResult:
        del timeout_s, env
        self.argv = list(argv)
        self.stdin = stdin
        self.empty_directory = _is_empty(cwd)
        if self.fail:
            raise CommandError(START_FAILURE)
        for thread, pictures in self.saved.items():
            folder = self.store / GENERATED_IMAGES / thread
            folder.mkdir(parents=True, exist_ok=True)
            for name, content in pictures.items():
                (folder / name).write_bytes(content)
        return CommandResult(self.returncode, self.stdout, "")


def _is_empty(directory: Path | None) -> bool:
    return directory is not None and not any(directory.iterdir())


def _draw(sketcher: CodexSketcher) -> Path:
    return asyncio.run(sketcher.draw("a graph"))


def test_the_picture_of_the_run_thread_is_returned_from_a_fixed_read_only_run(
    tmp_path: Path,
) -> None:
    store = tmp_path / "codex"
    runner = _Runner(store, saved={THREAD: {"ig_1.png": PNG}})
    sketcher = CodexSketcher("gpt-test", runner, effort="low", timeout_s=30, store=store)

    picture = _draw(sketcher)

    assert picture == store / GENERATED_IMAGES / THREAD / "ig_1.png"
    assert runner.stdin == "a graph"
    assert runner.empty_directory
    assert runner.argv[runner.argv.index("-m") + 1] == "gpt-test"
    assert "--json" in runner.argv
    assert 'sandbox_mode="read-only"' in runner.argv
    assert 'web_search="disabled"' in runner.argv
    assert 'model_reasoning_effort="low"' in runner.argv
    assert not any("mcp" in part for part in runner.argv)


def test_the_run_names_no_effort_when_the_rung_gives_none(tmp_path: Path) -> None:
    store = tmp_path / "codex"
    runner = _Runner(store, saved={THREAD: {"ig_1.png": PNG}})

    _draw(CodexSketcher("gpt-test", runner, effort=None, timeout_s=30, store=store))

    assert not any("model_reasoning_effort" in part for part in runner.argv)


@pytest.mark.parametrize(
    "case",
    [
        "run-exits-with-an-error",
        "run-cannot-start",
        "no-thread-is-named",
        "thread-name-climbs-out",
        "no-picture-is-saved",
        "saved-file-is-not-a-png",
    ],
)
def test_a_run_that_gives_no_checked_picture_is_a_sketch_error(tmp_path: Path, case: str) -> None:
    store = tmp_path / "codex"
    runner = {
        "run-exits-with-an-error": _Runner(store, returncode=1, saved={THREAD: {"a.png": PNG}}),
        "run-cannot-start": _Runner(store, fail=True),
        "no-thread-is-named": _Runner(store, stdout="", saved={THREAD: {"a.png": PNG}}),
        "thread-name-climbs-out": _Runner(
            store,
            stdout=json.dumps({"type": "thread.started", "thread_id": "../elsewhere"}),
            saved={THREAD: {"a.png": PNG}},
        ),
        "no-picture-is-saved": _Runner(store),
        "saved-file-is-not-a-png": _Runner(store, saved={THREAD: {"a.png": NOT_A_PNG}}),
    }[case]

    with pytest.raises(SketchError):
        _draw(CodexSketcher("gpt-test", runner, effort=None, timeout_s=30, store=store))


def test_a_picture_that_is_a_link_is_not_taken(tmp_path: Path) -> None:
    store = tmp_path / "codex"
    folder = store / GENERATED_IMAGES / THREAD
    folder.mkdir(parents=True)
    real = tmp_path / "elsewhere.png"
    real.write_bytes(PNG)
    (folder / "ig.png").symlink_to(real)
    runner = _Runner(store)

    with pytest.raises(SketchError):
        _draw(CodexSketcher("gpt-test", runner, effort=None, timeout_s=30, store=store))


@pytest.mark.parametrize(
    ("variable", "expected"),
    [("/custom/codex", Path("/custom/codex")), (None, Path.home() / ".codex")],
    ids=["codex-home-set", "default-under-home"],
)
def test_codex_keeps_its_pictures_under_codex_home_or_the_default(
    monkeypatch: pytest.MonkeyPatch, variable: str | None, expected: Path
) -> None:
    if variable is None:
        monkeypatch.delenv("CODEX_HOME", raising=False)
    else:
        monkeypatch.setenv("CODEX_HOME", variable)

    assert codex_store() == expected
