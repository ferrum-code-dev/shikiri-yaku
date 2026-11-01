from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from shikiri_yaku.domain.recipes import InstallOpPayload
from shikiri_yaku.domain.signal import Kind, Signal
from shikiri_yaku.journal.database import Database
from shikiri_yaku.journal.ops import OpState, OpStore

if TYPE_CHECKING:
    from collections.abc import Generator

NOW = datetime(2026, 10, 29, tzinfo=UTC)
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
    number=8,
    escalation_id="E-1",
    tool="ffmpeg",
    manager="brew",
    package="ffmpeg",
    why="needed by the build",
    argv=("brew", "install", "ffmpeg"),
)


@pytest.fixture
def memory() -> Generator[Database]:
    database = Database.open(":memory:")
    yield database
    database.close()


def test_install_op_round_trips_as_intended_and_keeps_first_argv(memory: Database) -> None:
    store = OpStore(memory)
    first = store.intend_install("install:api:escalation:E-1", SIGNAL, PAYLOAD, NOW)
    replacement = PAYLOAD.model_copy(
        update={"why": "a changed proposal", "argv": ("brew", "install", "other")}
    )

    repeated = store.intend_install("install:api:escalation:E-1", SIGNAL, replacement, NOW)

    assert first.id == repeated.id
    assert first.state == "intended"
    assert repeated.payload == PAYLOAD
    assert store.install_op(first.id) == first


def test_install_decisions_are_conditional_signal_bound_and_final(memory: Database) -> None:
    store = OpStore(memory)
    decisions = (
        (store.approve_install, OpState.APPROVED),
        (store.decline_install, OpState.DECLINED),
        (store.expire_install, OpState.EXPIRED),
        (store.void_install, OpState.VOID),
    )

    for decision, expected in decisions:
        operation = store.intend_install(f"install:{expected}", SIGNAL, PAYLOAD, NOW)
        assert not decision(operation.id, PAYLOAD.number + 1, NOW)
        assert decision(operation.id, PAYLOAD.number, NOW)
        assert decision(operation.id, PAYLOAD.number, NOW) is (expected is OpState.DECLINED)
        stored = store.get(operation.id)
        assert stored is not None
        assert stored.state is expected
        row = memory.connection.execute(
            "SELECT approved_at FROM ops WHERE id = ?", (operation.id,)
        ).fetchone()
        assert (row["approved_at"] is not None) is (expected is OpState.APPROVED)
        pending_ids = {pending.id for pending in store.non_final()}
        assert (operation.id in pending_ids) is (expected is OpState.APPROVED)
