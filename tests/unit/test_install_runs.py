from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final

import pytest

from shikiri_yaku.domain.recipes import InstallOpPayload
from shikiri_yaku.domain.signal import Closing, Kind, Resolved, Signal
from shikiri_yaku.journal.cursors import CursorStore
from shikiri_yaku.journal.database import Database
from shikiri_yaku.journal.events import JournalEvents
from shikiri_yaku.journal.install_runs import JournalInstallRuns
from shikiri_yaku.journal.ops import OpState, OpStore
from shikiri_yaku.journal.signals import NewSignal, SignalState, SignalStore
from shikiri_yaku.ports import Ack, AckStatus

if TYPE_CHECKING:
    from collections.abc import Generator

NOW: Final = datetime(2026, 10, 30, tzinfo=UTC)
LATER: Final = datetime(2026, 10, 30, 12, tzinfo=UTC)
IDENTITY: Final = "install:api:escalation:E-1"
OUTPUT: Final = "/instance/output/install-1.log"
FINAL_LINE: Final = "Installed ffmpeg with brew."
DONE: Final = Ack(id=7, status=AckStatus.DONE, result=None, message=None)
QUEUED: Final = Ack(id=8, status=AckStatus.QUEUED, result=None, message=None)


@pytest.fixture
def memory() -> Generator[Database]:
    database = Database.open(":memory:")
    yield database
    database.close()


class _Setup:
    """One forwarded escalation with an install op, approved or still intended."""

    def __init__(self, database: Database, *, approved: bool) -> None:
        self.signals = SignalStore(database)
        self.ops = OpStore(database)
        self.runs = JournalInstallRuns(database, self.signals, self.ops)
        self.number = self.signals.insert_if_absent(
            NewSignal(
                "api:escalation:E-1",
                "api",
                "escalation",
                "decision_required",
                "need ffmpeg",
                escalation_id="E-1",
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
            escalation_id="E-1",
            tool="ffmpeg",
            manager="brew",
            package="ffmpeg",
            why="needed by the build",
            argv=("brew", "install", "ffmpeg"),
        )
        intended = self.ops.intend_install(IDENTITY, _signal(), self.payload, NOW)
        self.op_id = intended.id
        if approved:
            assert self.ops.approve_install(self.op_id, self.number, NOW)

    def state(self) -> str:
        stored = self.ops.install_op(self.op_id)
        assert stored is not None
        return stored.state

    def signal_state(self) -> SignalState:
        stored = self.signals.get(self.number)
        assert stored is not None
        return stored.state


def test_only_an_approved_op_is_claimed_once(memory: Database) -> None:
    intended = _Setup(memory, approved=False)
    assert intended.runs.claim(intended.op_id, NOW) is None
    assert intended.state() == OpState.INTENDED

    approved = _Setup(memory, approved=True)
    claim = approved.runs.claim(approved.op_id, NOW)
    assert claim is not None
    assert claim.op.payload == approved.payload
    assert claim.signal.escalation_id == "E-1"
    assert claim.suggestion == "Install ffmpeg."
    assert approved.state() == OpState.ISSUED
    assert approved.runs.claim(approved.op_id, LATER) is None


def test_a_claim_runs_the_payload_stored_at_approval(memory: Database) -> None:
    setup = _Setup(memory, approved=True)
    replacement = setup.payload.model_copy(
        update={"why": "a changed model reason", "argv": ("sh", "-c", "rm -rf /tmp/x")}
    )
    setup.ops.intend_install("install:api:escalation:E-1", _signal(), replacement, LATER)

    claim = setup.runs.claim(setup.op_id, LATER)

    assert claim is not None
    assert claim.op.payload == setup.payload


@pytest.mark.parametrize("ack", [DONE, QUEUED])
def test_an_acknowledged_run_stays_issued_until_its_event(memory: Database, ack: Ack) -> None:
    setup = _Setup(memory, approved=True)
    assert setup.runs.claim(setup.op_id, NOW) is not None

    setup.runs.acknowledge(setup.op_id, ack, OUTPUT, FINAL_LINE, LATER)

    stored = setup.ops.get(setup.op_id)
    assert stored is not None
    assert stored.state == OpState.ISSUED
    assert stored.command_id == str(ack.id)
    assert stored.output_path == OUTPUT
    assert stored.acknowledgement == ack
    assert setup.signal_state() == SignalState.FORWARDED


def test_the_event_that_names_the_command_confirms_the_op(memory: Database) -> None:
    setup = _Setup(memory, approved=True)
    assert setup.runs.claim(setup.op_id, NOW) is not None
    setup.runs.acknowledge(setup.op_id, DONE, OUTPUT, FINAL_LINE, LATER)

    _observe_resolved(memory, command_id="7", now=LATER)

    stored = setup.ops.get(setup.op_id)
    assert stored is not None
    assert stored.state == OpState.CONFIRMED
    assert setup.signal_state() == SignalState.CLOSED
    assert _final_line(memory, setup.number) == FINAL_LINE


def test_an_event_for_another_command_changes_nothing(memory: Database) -> None:
    setup = _Setup(memory, approved=True)
    assert setup.runs.claim(setup.op_id, NOW) is not None
    setup.runs.acknowledge(setup.op_id, DONE, OUTPUT, FINAL_LINE, LATER)

    _observe_resolved(memory, command_id="99", now=LATER)

    assert setup.state() == OpState.ISSUED


def test_fail_moves_only_an_issued_op_and_keeps_its_output(memory: Database) -> None:
    approved = _Setup(memory, approved=True)
    assert not approved.runs.fail(approved.op_id, OUTPUT, NOW)
    assert approved.state() == OpState.APPROVED

    assert approved.runs.claim(approved.op_id, NOW) is not None
    assert approved.runs.fail(approved.op_id, OUTPUT, LATER)

    stored = approved.ops.get(approved.op_id)
    assert stored is not None
    assert stored.state == OpState.FAILED
    assert stored.output_path == OUTPUT
    assert approved.signal_state() == SignalState.FORWARDED


def _observe_resolved(database: Database, *, command_id: str, now: datetime) -> None:
    """Feed the event journal the `escalation_resolved` event Shikiru sends for the command."""
    events = JournalEvents(
        database, CursorStore(database, SignalStore(database)), OpStore(database)
    )
    events.record_batch(
        "api",
        [Closing("api", escalation_id="E-1"), Resolved("api", "E-1", "answer", command_id)],
        1,
        None,
        now,
    )


def _final_line(database: Database, number: int) -> str | None:
    row = database.connection.execute(
        "SELECT final_line FROM signals WHERE number = ?", (number,)
    ).fetchone()
    return None if row is None else row["final_line"]


def _signal() -> Signal:
    return Signal(
        project="api",
        kind=Kind.ESCALATION,
        source_id="E-1",
        reason="decision_required",
        text="need ffmpeg",
        task_id=None,
        escalation_id="E-1",
    )
