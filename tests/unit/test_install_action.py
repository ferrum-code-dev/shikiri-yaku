from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from shikiri_yaku.domain.classification import InstallProposal, Verdict
from shikiri_yaku.domain.outbound import Purpose
from shikiri_yaku.domain.outcome import Notified
from shikiri_yaku.domain.recipes import InstallOpPayload
from shikiri_yaku.domain.schedule import Destination
from shikiri_yaku.domain.signal import Kind, Signal
from shikiri_yaku.extensions.infrastructure.install import InstallAction
from shikiri_yaku.extensions.infrastructure.preparation import InstallPreparation
from shikiri_yaku.extensions.infrastructure.rules import INFRASTRUCTURE
from shikiri_yaku.journal import (
    Database,
    JournalOutbox,
    MessageStore,
    NewSignal,
    OpStore,
    SignalStore,
)
from shikiri_yaku.journal.ops import OpState
from shikiri_yaku.ports import HostObservation
from tests.support.clock import NapClock
from tests.support.destinations import FixedDestinations
from tests.support.notices import FailingReporter

if TYPE_CHECKING:
    from collections.abc import Generator

NOW = datetime(2026, 10, 29, tzinfo=UTC)
OWNER = Destination("telegram", "1001")
SIGNAL = Signal(
    project="api",
    kind=Kind.ESCALATION,
    source_id="E-1",
    reason="decision_required",
    text="the build needs ffmpeg",
    task_id="T-1",
    escalation_id="E-1",
)
PAYLOAD = InstallOpPayload(
    number=1,
    escalation_id="E-1",
    tool="ffmpeg",
    manager="brew",
    package="ffmpeg",
    why="not on the host PATH",
    argv=("brew", "install", "ffmpeg"),
)


class Probes:
    def __init__(self) -> None:
        self.calls = 0
        self.tools: list[str] = []

    async def collect(self) -> HostObservation:
        self.calls += 1
        return HostObservation(("OS: test",), frozenset({"brew"}))

    async def find(self, tool: str) -> str | None:
        self.tools.append(tool)
        return None


class Harness:
    def __init__(self, database: Database) -> None:
        self.signals = SignalStore(database)
        self.number = self.signals.insert_if_absent(
            NewSignal(
                SIGNAL.id,
                SIGNAL.project,
                SIGNAL.kind.value,
                SIGNAL.reason,
                SIGNAL.text,
                SIGNAL.task_id,
                SIGNAL.escalation_id,
            ),
            NOW,
        ).row_id
        self.messages = MessageStore(database, FailingReporter())
        self.outbox = JournalOutbox(database, self.messages)
        self.ops = OpStore(database)
        _classify(self.signals, self.number, _proposal())
        self.probes = Probes()
        preparation = InstallPreparation(
            proposals=self.signals,
            facts=self.probes,
            check=self.probes,
        )
        self.action = InstallAction(
            preparation=preparation,
            proposals=self.signals,
            numbers=self.signals,
            installs=self.ops,
            outbox=self.outbox,
            clock=NapClock(),
            destinations=FixedDestinations([OWNER]),
        )


@pytest.fixture
def memory() -> Generator[Database]:
    database = Database.open(":memory:")
    yield database
    database.close()


def _proposal(*, package: str = "ffmpeg", why: str = "not on the host PATH") -> InstallProposal:
    return InstallProposal(tool="ffmpeg", manager="brew", package=package, why=why)


def _classify(signals: SignalStore, number: int, proposal: InstallProposal) -> None:
    signals.classify(
        number,
        category=INFRASTRUCTURE,
        classified_by="reasoner",
        verdict=Verdict.HOST_TOOL,
        handler="infrastructure",
        now=NOW,
        install_proposal=proposal,
    )


def _stored_operation(harness: Harness):
    return harness.ops.intend_install(f"install:{SIGNAL.id}", SIGNAL, PAYLOAD, NOW)


def test_repeat_uses_stored_payload_and_one_logical_approval(memory: Database) -> None:
    harness = Harness(memory)

    first = asyncio.run(harness.action.run(SIGNAL))
    _classify(
        harness.signals, harness.number, _proposal(package="other", why="changed after persistence")
    )
    asyncio.run(harness.action.run(SIGNAL))

    assert isinstance(first, Notified)
    operation = harness.ops.get_install(f"install:{SIGNAL.id}")
    assert operation is not None
    assert operation.payload == PAYLOAD.model_copy(update={"number": harness.number})
    [pending] = harness.outbox.pending()
    assert "Command: brew install ffmpeg" in pending.message.text
    assert "Why (model-written): not on the host PATH" in pending.message.text
    assert "Package manager: brew" in pending.message.text
    assert "adds ffmpeg for every component on this host" in pending.message.text
    assert "may also update dependencies" in pending.message.text
    assert [button.label for button in pending.message.buttons] == ["Install", "Decline"]
    assert [button.data for button in pending.message.buttons] == [
        f"i:{harness.number}:{operation.id}",
        f"x:{harness.number}:{operation.id}",
    ]
    [stored] = harness.messages.for_signal(harness.number)
    assert stored.purpose is Purpose.APPROVAL
    assert stored.signal_number == harness.number
    assert harness.probes.calls == 1
    assert harness.probes.tools == ["ffmpeg"]
    assert memory.connection.execute("SELECT count(*) FROM ops").fetchone()[0] == 1


def test_recovery_queues_from_intended_op_without_rereading_proposal(memory: Database) -> None:
    harness = Harness(memory)
    operation = _stored_operation(harness)
    _classify(harness.signals, harness.number, _proposal(package="changed", why="new proposal"))

    outcome = asyncio.run(harness.action.run(SIGNAL))

    assert isinstance(outcome, Notified)
    [pending] = harness.outbox.pending()
    assert "Command: brew install ffmpeg" in pending.message.text
    assert "Why (model-written): not on the host PATH" in pending.message.text
    assert f"i:{harness.number}:{operation.id}" == pending.message.buttons[0].data
    assert harness.probes.calls == 0
    assert harness.probes.tools == []
    assert memory.connection.execute("SELECT count(*) FROM ops").fetchone()[0] == 1


@pytest.mark.parametrize("state", [OpState.APPROVED, OpState.FAILED])
def test_handled_install_op_does_not_reoffer_install(memory: Database, state: OpState) -> None:
    harness = Harness(memory)
    operation = _stored_operation(harness)
    assert harness.ops.transition(operation.id, OpState.INTENDED, state, NOW)

    outcome = asyncio.run(harness.action.run(SIGNAL))

    assert isinstance(outcome, Notified)
    assert harness.outbox.pending() == []
