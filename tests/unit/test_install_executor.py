from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Final, cast

import pytest

from shikiri_yaku.adapters.host_lock import FileHostLock
from shikiri_yaku.adapters.install_files import SCRATCH_DIR_NAME, InstanceInstallFiles
from shikiri_yaku.app.install_executor import InstallExecutor
from shikiri_yaku.domain.recipes import InstallOpPayload
from shikiri_yaku.domain.signal import Kind, Signal
from shikiri_yaku.journal.database import Database
from shikiri_yaku.journal.install_runs import JournalInstallRuns
from shikiri_yaku.journal.ops import OpState, OpStore
from shikiri_yaku.journal.signals import NewSignal, SignalState, SignalStore
from shikiri_yaku.ports import (
    Ack,
    AckStatus,
    CommandError,
    CommandResult,
    ResolutionKind,
    ShikiruError,
    ShikiruLinkDownError,
    ShikiruRejectedError,
)
from tests.support.clock import NapClock

if TYPE_CHECKING:
    from collections.abc import Generator, Mapping, Sequence

    from shikiri_yaku.ports import Clock, ShikiruClient

NOW: Final = datetime(2026, 10, 30, tzinfo=UTC)
LATER: Final = datetime(2026, 10, 30, 12, tzinfo=UTC)
SHELL: Final = "/bin/zsh"
TIMEOUT_S: Final = 900.0
TAIL_LINES: Final = 2
POLL_S: Final = 1
PAUSE_S: Final = 1
FOUND: Final = "/opt/homebrew/bin/ffmpeg"
ANSWER_TEXT: Final = (
    "Installed ffmpeg with brew (found at /opt/homebrew/bin/ffmpeg). Please continue."
)
DONE: Final = Ack(id=7, status=AckStatus.DONE, result=None, message=None)
QUEUED: Final = Ack(id=8, status=AckStatus.QUEUED, result=None, message=None)


@pytest.fixture
def memory() -> Generator[Database]:
    database = Database.open(":memory:")
    yield database
    database.close()


class _Commands:
    """Answers each run with the configured outcome and records what it was asked to run."""

    def __init__(
        self,
        *,
        returncode: int = 0,
        stdout: str = "",
        stderr: str = "",
        error: CommandError | None = None,
        pause: Clock | None = None,
    ) -> None:
        self._returncode = returncode
        self._stdout = stdout
        self._stderr = stderr
        self._error = error
        self._pause = pause
        self._active = 0
        self.peak = 0
        self.calls: list[tuple[list[str], float, Path | None, Mapping[str, str] | None]] = []

    async def run(
        self,
        argv: Sequence[str],
        *,
        timeout_s: float,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        self.calls.append((list(argv), timeout_s, cwd, env))
        self._active += 1
        self.peak = max(self.peak, self._active)
        try:
            if self._pause is not None:
                await self._pause.sleep(PAUSE_S)
            if self._error is not None:
                raise self._error
            return CommandResult(self._returncode, self._stdout, self._stderr)
        finally:
            self._active -= 1


class _Check:
    """Finds the tool at `found` after a run, or reports it missing when `found` is None."""

    def __init__(self, found: str | None) -> None:
        self._found = found
        self.asked: list[str] = []

    async def find(self, tool: str) -> str | None:
        self.asked.append(tool)
        return self._found


class _Forwarder:
    def __init__(self) -> None:
        self.forwarded: list[tuple[int, Signal, str, str]] = []

    async def forward(self, number: int, signal: Signal, note: str, suggestion: str) -> None:
        self.forwarded.append((number, signal, note, suggestion))


class _Shikiru:
    """Answers `resolve_escalation` with `ack`, or raises `error` instead when it is set."""

    def __init__(self, *, ack: Ack = DONE, error: ShikiruError | None = None) -> None:
        self._ack = ack
        self._error = error
        self.calls: list[tuple[str, str, str | None]] = []

    async def resolve_escalation(
        self, escalation_id: str, kind: str, text: str | None = None
    ) -> Ack:
        self.calls.append((escalation_id, kind, text))
        if self._error is not None:
            raise self._error
        return self._ack


class _World:
    """The executor over one journal and one instance directory, with fakes for outside calls."""

    def __init__(
        self,
        database: Database,
        directory: Path,
        *,
        commands: _Commands,
        check: _Check,
        shikiru: _Shikiru,
    ) -> None:
        self.forwarder = _Forwarder()
        clock = NapClock()
        runs = JournalInstallRuns(database, SignalStore(database), OpStore(database))
        self.executor = InstallExecutor(
            runs,
            {"api": cast("ShikiruClient", shikiru)},
            self.forwarder,
            check,
            commands,
            host=FileHostLock(directory / "host.lock", clock, POLL_S),
            files=InstanceInstallFiles(directory),
            clock=clock,
            shell=SHELL,
            timeout_s=TIMEOUT_S,
            tail_lines=TAIL_LINES,
        )

    async def run(self, *op_ids: int) -> None:
        for op_id in op_ids:
            self.executor.start(op_id)
        await self.executor.join()


class _Install:
    """One forwarded escalation with an install op the PO approved for it."""

    def __init__(self, database: Database, escalation: str) -> None:
        self.signals = SignalStore(database)
        self.ops = OpStore(database)
        self.number = self.signals.insert_if_absent(
            NewSignal(
                f"api:escalation:{escalation}",
                "api",
                "escalation",
                "decision_required",
                "need ffmpeg",
                escalation_id=escalation,
            ),
            NOW,
        ).row_id
        self.signals.classify(
            self.number,
            category="infrastructure",
            classified_by="rule",
            verdict="host_tool",
            handler="install",
            now=NOW,
            note="Install ffmpeg.",
        )
        self.signals.transition(self.number, SignalState.NEW, SignalState.HANDLING, NOW)
        self.signals.transition(self.number, SignalState.HANDLING, SignalState.FORWARDED, NOW)
        self.payload = InstallOpPayload(
            number=self.number,
            escalation_id=escalation,
            tool="ffmpeg",
            manager="brew",
            package="ffmpeg",
            why="needed by the build",
            argv=("brew", "install", "ffmpeg"),
        )
        identity = f"install:api:escalation:{escalation}"
        self.op_id = self.ops.intend_install(identity, _signal(escalation), self.payload, NOW).id
        assert self.ops.approve_install(self.op_id, self.number, NOW)

    def state(self) -> str:
        stored = self.ops.get(self.op_id)
        assert stored is not None
        return stored.state

    def signal_state(self) -> SignalState:
        stored = self.signals.get(self.number)
        assert stored is not None
        return stored.state


def _signal(escalation: str) -> Signal:
    return Signal(
        project="api",
        kind=Kind.ESCALATION,
        source_id=escalation,
        reason="decision_required",
        text="need ffmpeg",
        task_id=None,
        escalation_id=escalation,
    )


def _world(
    database: Database,
    directory: Path,
    *,
    commands: _Commands | None = None,
    check: _Check | None = None,
    shikiru: _Shikiru | None = None,
) -> _World:
    return _World(
        database,
        directory,
        commands=_Commands() if commands is None else commands,
        check=_Check(FOUND) if check is None else check,
        shikiru=_Shikiru() if shikiru is None else shikiru,
    )


def test_a_passing_check_after_a_clean_exit_answers_and_confirms(
    memory: Database, tmp_path: Path
) -> None:
    shikiru = _Shikiru()
    world = _world(memory, tmp_path, shikiru=shikiru)
    install = _Install(memory, "E-1")

    asyncio.run(world.run(install.op_id))

    assert shikiru.calls == [("E-1", ResolutionKind.ANSWER, ANSWER_TEXT)]
    assert install.state() == OpState.CONFIRMED
    assert install.signal_state() == SignalState.RESOLVED
    assert world.forwarder.forwarded == []


def test_a_clean_exit_with_a_failing_check_forwards_and_fails(
    memory: Database, tmp_path: Path
) -> None:
    shikiru = _Shikiru()
    world = _world(memory, tmp_path, check=_Check(None), shikiru=shikiru)
    install = _Install(memory, "E-1")

    asyncio.run(world.run(install.op_id))

    assert install.state() == OpState.FAILED
    assert shikiru.calls == []
    [(number, _, note, suggestion)] = world.forwarder.forwarded
    assert number == install.number
    assert "did not find ffmpeg" in note
    assert suggestion == "Install ffmpeg."
    assert install.signal_state() == SignalState.FORWARDED


def test_a_non_zero_exit_forwards_the_output_tail_and_fails(
    memory: Database, tmp_path: Path
) -> None:
    commands = _Commands(returncode=1, stdout="a\nb\nc\n", stderr="boom\n")
    check = _Check(FOUND)
    world = _world(memory, tmp_path, commands=commands, check=check)
    install = _Install(memory, "E-1")

    asyncio.run(world.run(install.op_id))

    assert install.state() == OpState.FAILED
    assert check.asked == []
    [(_, _, note, _)] = world.forwarder.forwarded
    assert note.endswith("c\nboom")
    stored = install.ops.get(install.op_id)
    assert stored is not None
    assert stored.output_path is not None
    assert Path(stored.output_path).read_text(encoding="utf-8") == "a\nb\nc\nboom\n"


def test_a_timeout_forwards_the_error_and_fails(memory: Database, tmp_path: Path) -> None:
    commands = _Commands(error=CommandError("timed out after 900 s"))
    check = _Check(FOUND)
    world = _world(memory, tmp_path, commands=commands, check=check)
    install = _Install(memory, "E-1")

    asyncio.run(world.run(install.op_id))

    assert install.state() == OpState.FAILED
    assert check.asked == []
    [(_, _, note, _)] = world.forwarder.forwarded
    assert "timed out after 900 s" in note


def test_the_command_is_the_stored_argv_run_in_scratch_non_interactively(
    memory: Database, tmp_path: Path
) -> None:
    commands = _Commands()
    world = _world(memory, tmp_path, commands=commands)
    install = _Install(memory, "E-1")

    asyncio.run(world.run(install.op_id))

    [(argv, timeout_s, cwd, env)] = commands.calls
    assert argv == [SHELL, "-lc", "brew install ffmpeg"]
    assert timeout_s == TIMEOUT_S
    assert cwd == tmp_path / SCRATCH_DIR_NAME
    assert env is not None
    assert dict(env) == {"CI": "1", "NONINTERACTIVE": "1"}


def test_a_replacement_offered_after_approval_is_not_run(memory: Database, tmp_path: Path) -> None:
    commands = _Commands()
    world = _world(memory, tmp_path, commands=commands)
    install = _Install(memory, "E-1")
    replacement = install.payload.model_copy(update={"argv": ("echo", "changed")})
    install.ops.intend_install("install:api:escalation:E-1", _signal("E-1"), replacement, LATER)

    asyncio.run(world.run(install.op_id))

    [(argv, _, _, _)] = commands.calls
    assert argv == [SHELL, "-lc", "brew install ffmpeg"]


def test_two_executors_started_for_one_op_run_one_command(memory: Database, tmp_path: Path) -> None:
    commands = _Commands()
    first = _world(memory, tmp_path, commands=commands)
    second = _world(memory, tmp_path, commands=commands)
    install = _Install(memory, "E-1")

    async def both() -> None:
        first.executor.start(install.op_id)
        second.executor.start(install.op_id)
        await first.executor.join()
        await second.executor.join()

    asyncio.run(both())

    assert len(commands.calls) == 1
    assert install.state() == OpState.CONFIRMED


def test_two_ops_never_run_their_commands_at_once(memory: Database, tmp_path: Path) -> None:
    commands = _Commands(pause=NapClock())
    world = _world(memory, tmp_path, commands=commands)
    first = _Install(memory, "E-1")
    second = _Install(memory, "E-2")

    asyncio.run(world.run(first.op_id, second.op_id))

    assert len(commands.calls) == 2
    assert commands.peak == 1
    assert first.state() == OpState.CONFIRMED
    assert second.state() == OpState.CONFIRMED


def test_a_rejected_answer_fails_the_op_and_forwards_shikiru_s_message(
    memory: Database, tmp_path: Path
) -> None:
    shikiru = _Shikiru(error=ShikiruRejectedError("escalation already resolved"))
    world = _world(memory, tmp_path, shikiru=shikiru)
    install = _Install(memory, "E-1")

    asyncio.run(world.run(install.op_id))

    assert install.state() == OpState.FAILED
    assert install.signal_state() == SignalState.FORWARDED
    [(_, _, note, _)] = world.forwarder.forwarded
    assert "escalation already resolved" in note


def test_a_queued_answer_leaves_the_op_issued(memory: Database, tmp_path: Path) -> None:
    world = _world(memory, tmp_path, shikiru=_Shikiru(ack=QUEUED))
    install = _Install(memory, "E-1")

    asyncio.run(world.run(install.op_id))

    stored = install.ops.get(install.op_id)
    assert stored is not None
    assert stored.state == OpState.ISSUED
    assert stored.command_id == "8"
    assert install.signal_state() == SignalState.FORWARDED
    assert world.forwarder.forwarded == []


def test_a_lost_link_leaves_the_op_issued_for_the_reconcile(
    memory: Database, tmp_path: Path
) -> None:
    world = _world(memory, tmp_path, shikiru=_Shikiru(error=ShikiruLinkDownError("link lost")))
    install = _Install(memory, "E-1")

    asyncio.run(world.run(install.op_id))

    assert install.state() == OpState.ISSUED
    assert world.forwarder.forwarded == []
