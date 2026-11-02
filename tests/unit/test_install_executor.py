from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Final, cast

import pytest

from shikiri_yaku.adapters.host_lock import FileHostLock
from shikiri_yaku.adapters.install_files import SCRATCH_DIR_NAME, InstanceInstallFiles
from shikiri_yaku.app.install_executor import REFUSED, InstallExecutor
from shikiri_yaku.domain.recipes import InstallOpPayload
from shikiri_yaku.domain.signal import Closing, Kind, Signal
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
    from collections.abc import AsyncGenerator, Callable, Generator, Mapping, Sequence

    from shikiri_yaku.ports import Clock, HostLock, ShikiruClient

NOW: Final = datetime(2026, 10, 30, tzinfo=UTC)
LATER: Final = datetime(2026, 10, 30, 12, tzinfo=UTC)
SHELL: Final = "/bin/zsh"
TIMEOUT_S: Final = 900.0
TAIL_LINES: Final = 2
POLL_S: Final = 1
PAUSE_S: Final = 1
RETRY_S: Final = 60.0
FOREVER: Final = 1_000_000
INTERRUPT_S: Final = 0.2
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
    """Records its forwards; its first `failures` calls raise, as a down channel does."""

    def __init__(
        self, *, failures: int = 0, while_forwarding: Callable[[], None] | None = None
    ) -> None:
        self._failures = failures
        self._while_forwarding = while_forwarding
        self.attempts = 0
        self.forwarded: list[tuple[int, Signal, str, str]] = []

    async def forward(
        self, number: int, signal: Signal, note: str, suggestion: str, *, standing: bool
    ) -> bool:
        del standing
        self.attempts += 1
        if self._while_forwarding is not None:
            self._while_forwarding()
        if self.attempts <= self._failures:
            message = "the channel is down"
            raise OSError(message)
        self.forwarded.append((number, signal, note, suggestion))
        return False


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


class _ContendedLock:
    """A host lock another run holds: `while_waiting` runs while this run waits for it."""

    def __init__(self, while_waiting: Callable[[], None]) -> None:
        self._while_waiting = while_waiting

    @asynccontextmanager
    async def hold(self) -> AsyncGenerator[None]:
        self._while_waiting()
        yield


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
        host: HostLock | None = None,
        forwarder: _Forwarder | None = None,
    ) -> None:
        self.forwarder = _Forwarder() if forwarder is None else forwarder
        clock = NapClock()
        runs = JournalInstallRuns(database, SignalStore(database), OpStore(database))
        self.executor = InstallExecutor(
            runs,
            {"api": cast("ShikiruClient", shikiru)},
            self.forwarder,
            check,
            commands,
            host=FileHostLock(directory / "host.lock", clock, POLL_S) if host is None else host,
            files=InstanceInstallFiles(directory),
            clock=clock,
            shell=SHELL,
            timeout_s=TIMEOUT_S,
            tail_lines=TAIL_LINES,
            forward_retry_s=RETRY_S,
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
    host: HostLock | None = None,
    forwarder: _Forwarder | None = None,
) -> _World:
    return _World(
        database,
        directory,
        commands=_Commands() if commands is None else commands,
        check=_Check(FOUND) if check is None else check,
        shikiru=_Shikiru() if shikiru is None else shikiru,
        host=host,
        forwarder=forwarder,
    )


def _close_elsewhere(database: Database) -> None:
    """Close the escalation as Shikiru does when the PO resolves it in another channel."""
    SignalStore(database).close(Closing("api", escalation_id="E-1"), LATER)


def test_a_passing_check_after_a_clean_exit_answers_and_waits_for_its_event(
    memory: Database, tmp_path: Path
) -> None:
    shikiru = _Shikiru()
    world = _world(memory, tmp_path, shikiru=shikiru)
    install = _Install(memory, "E-1")

    asyncio.run(world.run(install.op_id))

    assert shikiru.calls == [("E-1", ResolutionKind.ANSWER, ANSWER_TEXT)]
    assert install.state() == OpState.ISSUED
    assert install.signal_state() == SignalState.FORWARDED
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


def test_a_non_zero_exit_with_a_passing_check_answers_the_escalation(
    memory: Database, tmp_path: Path
) -> None:
    commands = _Commands(returncode=1, stdout="a\nb\nc\n", stderr="boom\n")
    check = _Check(FOUND)
    shikiru = _Shikiru()
    world = _world(memory, tmp_path, commands=commands, check=check, shikiru=shikiru)
    install = _Install(memory, "E-1")

    asyncio.run(world.run(install.op_id))

    assert install.state() == OpState.ISSUED
    assert check.asked == ["ffmpeg"]
    assert shikiru.calls == [("E-1", ResolutionKind.ANSWER, ANSWER_TEXT)]
    assert world.forwarder.forwarded == []
    stored = install.ops.get(install.op_id)
    assert stored is not None
    assert stored.output_path is not None
    assert Path(stored.output_path).read_text(encoding="utf-8") == "a\nb\nc\nboom\n"


def test_a_non_zero_exit_with_a_missing_tool_forwards_the_output_tail_and_fails(
    memory: Database, tmp_path: Path
) -> None:
    commands = _Commands(returncode=1, stdout="a\nb\nc\n", stderr="boom\n")
    world = _world(memory, tmp_path, commands=commands, check=_Check(None))
    install = _Install(memory, "E-1")

    asyncio.run(world.run(install.op_id))

    assert install.state() == OpState.FAILED
    [(_, _, note, _)] = world.forwarder.forwarded
    assert "exited with status 1" in note
    assert note.endswith("c\nboom")


def test_a_timeout_forwards_the_error_and_the_output_printed_before_it(
    memory: Database, tmp_path: Path
) -> None:
    error = CommandError("timed out after 900 s", output="step one\nstep two started")
    commands = _Commands(error=error)
    check = _Check(None)
    world = _world(memory, tmp_path, commands=commands, check=check)
    install = _Install(memory, "E-1")

    asyncio.run(world.run(install.op_id))

    assert install.state() == OpState.FAILED
    assert check.asked == ["ffmpeg"]
    [(_, _, note, _)] = world.forwarder.forwarded
    assert "timed out after 900 s" in note
    assert "step two started" in note


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


TAMPERED_PLANS: Final = [
    pytest.param({"argv": ("brew", "install", "ffmpeg", "--force")}, id="extra-word"),
    pytest.param({"argv": ("uv", "tool", "install", "ffmpeg")}, id="another-managers-recipe"),
    pytest.param({"argv": ("brew", "install", "ffmpeg;echo")}, id="semicolon-in-the-list"),
    pytest.param(
        {"package": "ffmpeg;echo", "argv": ("brew", "install", "ffmpeg;echo")},
        id="semicolon-in-the-package",
    ),
]


@pytest.mark.parametrize("changes", TAMPERED_PLANS)
def test_a_stored_list_changed_after_approval_is_not_run_and_fails_with_a_reason(
    memory: Database, tmp_path: Path, changes: dict[str, object]
) -> None:
    commands = _Commands()
    check = _Check(FOUND)
    world = _world(memory, tmp_path, commands=commands, check=check)
    install = _Install(memory, "E-1")
    _store_payload(memory, install.op_id, install.payload.model_copy(update=changes))

    asyncio.run(world.run(install.op_id))

    assert commands.calls == []
    assert check.asked == []
    assert install.state() == OpState.FAILED
    assert install.signal_state() == SignalState.FORWARDED
    [(number, _, note, _)] = world.forwarder.forwarded
    assert number == install.number
    assert note == REFUSED
    stored = install.ops.get(install.op_id)
    assert stored is not None
    assert stored.output_path is None


def _store_payload(database: Database, op_id: int, payload: InstallOpPayload) -> None:
    """Rewrite the stored plan the way someone with access to the journal could."""
    database.connection.execute(
        "UPDATE ops SET payload = ? WHERE id = ?", (payload.model_dump_json(), op_id)
    )


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
    assert install.state() == OpState.ISSUED


def test_running_holds_from_the_start_until_the_run_settles(
    memory: Database, tmp_path: Path
) -> None:
    world = _world(memory, tmp_path, commands=_Commands(pause=NapClock()))
    install = _Install(memory, "E-1")
    seen: list[bool] = []

    async def watch() -> None:
        world.executor.start(install.op_id)
        seen.append(world.executor.running(install.op_id))
        await world.executor.join()
        seen.append(world.executor.running(install.op_id))

    asyncio.run(watch())

    assert seen == [True, False]


def test_two_ops_never_run_their_commands_at_once(memory: Database, tmp_path: Path) -> None:
    commands = _Commands(pause=NapClock())
    world = _world(memory, tmp_path, commands=commands)
    first = _Install(memory, "E-1")
    second = _Install(memory, "E-2")

    asyncio.run(world.run(first.op_id, second.op_id))

    assert len(commands.calls) == 2
    assert commands.peak == 1
    assert first.state() == OpState.ISSUED
    assert second.state() == OpState.ISSUED


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


def test_an_escalation_closed_before_the_run_voids_the_approval(
    memory: Database, tmp_path: Path
) -> None:
    commands = _Commands()
    world = _world(memory, tmp_path, commands=commands)
    install = _Install(memory, "E-1")
    _close_elsewhere(memory)

    asyncio.run(world.run(install.op_id))

    assert install.state() == OpState.VOID
    assert commands.calls == []
    assert world.forwarder.forwarded == []


def test_a_failed_forward_is_tried_again_while_the_op_stays_issued(
    memory: Database, tmp_path: Path
) -> None:
    install = _Install(memory, "E-1")
    seen: list[str] = []
    forwarder = _Forwarder(failures=2, while_forwarding=lambda: seen.append(install.state()))
    world = _world(
        memory,
        tmp_path,
        commands=_Commands(returncode=1),
        check=_Check(None),
        forwarder=forwarder,
    )

    asyncio.run(world.run(install.op_id))

    assert seen == [OpState.ISSUED] * 3
    assert install.state() == OpState.FAILED
    [(number, _, note, _)] = forwarder.forwarded
    assert number == install.number
    assert "exited with status 1" in note


def test_a_run_ended_while_its_failure_is_forwarded_leaves_the_op_issued(
    memory: Database, tmp_path: Path
) -> None:
    install = _Install(memory, "E-1")
    forwarder = _Forwarder(failures=FOREVER)
    world = _world(
        memory,
        tmp_path,
        commands=_Commands(returncode=1),
        check=_Check(None),
        forwarder=forwarder,
    )

    with pytest.raises(TimeoutError):
        asyncio.run(asyncio.wait_for(world.run(install.op_id), INTERRUPT_S))

    assert install.state() == OpState.ISSUED
    assert forwarder.forwarded == []


def test_an_escalation_closed_while_the_run_waits_for_the_host_lock_voids_the_approval(
    memory: Database, tmp_path: Path
) -> None:
    commands = _Commands()
    host = _ContendedLock(lambda: _close_elsewhere(memory))
    world = _world(memory, tmp_path, commands=commands, host=host)
    install = _Install(memory, "E-1")

    asyncio.run(world.run(install.op_id))

    assert install.state() == OpState.VOID
    assert commands.calls == []
    assert world.forwarder.forwarded == []
