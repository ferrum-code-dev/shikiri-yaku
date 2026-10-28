from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

import pytest

from shikiri_yaku.app.event_source import EventSource
from shikiri_yaku.journal import (
    CursorStore,
    Database,
    Inserted,
    JournalEvents,
    NewOp,
    NewSignal,
    OpState,
    OpStore,
    SignalState,
    SignalStore,
)
from shikiri_yaku.ports import (
    EscalationRecord,
    EscalationView,
    ShikiruEvent,
    ShikiruProtocolError,
)

if TYPE_CHECKING:
    from collections.abc import Generator

    from shikiri_yaku.ports import ShikiruClient

NOW = datetime(2026, 10, 28, 9, 0, tzinfo=UTC)
PROJECT = "api"
WAIT_S = 1
HISTORY_END = 6
FAR_EVENT = 50
HISTORY_STOP = 3


def _event(event_id: int, kind: str, task_id: str | None = "T1", **data: Any) -> ShikiruEvent:
    return ShikiruEvent(
        id=event_id, ts=NOW, task_id=task_id, kind=kind, message=f"{kind} message", data=data
    )


def _raised(event_id: int, escalation_id: str) -> ShikiruEvent:
    return _event(event_id, "escalation", escalation_id=escalation_id, reason="decision_required")


def _view(escalation_id: str, status: str = "open") -> EscalationView:
    record = EscalationRecord(
        id=escalation_id,
        task_id="T1",
        reason="decision_required",
        headline=f"headline {escalation_id}",
        status=status,
        resolution=None,
    )
    return EscalationView(
        escalation=record, packet=None, question="which?", agent_options=[], resolutions=[]
    )


class _FakeClient:
    def __init__(self) -> None:
        self.events: list[ShikiruEvent] = []
        self.views: dict[str, EscalationView] = {}

    async def wait_for_events(self, since_id: int, timeout_s: float) -> list[ShikiruEvent]:
        del timeout_s
        return [event for event in self.events if event.id > since_id]

    async def list_escalations(self) -> list[EscalationView]:
        return list(self.views.values())

    async def get_escalation(self, escalation_id: str) -> EscalationView | None:
        return self.views.get(escalation_id)


class _FixedClock:
    def now(self) -> datetime:
        return NOW

    async def sleep(self, seconds: float) -> None:
        del seconds


class _CrashingSignals(SignalStore):
    """Dies right after an insert, so the batch's cursor move never commits."""

    def insert_if_absent(self, signal: NewSignal, now: datetime) -> Inserted:
        super().insert_if_absent(signal, now)
        message = "crash after the signal insert"
        raise RuntimeError(message)


@pytest.fixture
def database() -> Generator[Database]:
    memory = Database.open(":memory:")
    yield memory
    memory.close()


def _source(
    database: Database, client: _FakeClient, signals: SignalStore | None = None
) -> tuple[EventSource, JournalEvents]:
    journal = JournalEvents(
        database, CursorStore(database, signals or SignalStore(database)), OpStore(database)
    )
    return EventSource(
        PROJECT, cast("ShikiruClient", client), journal, _FixedClock(), WAIT_S
    ), journal


def _states(database: Database) -> dict[str, str]:
    rows = database.connection.execute("SELECT id, state FROM signals")
    return {row["id"]: row["state"] for row in rows}


def test_first_sync_signals_only_open_escalations_and_respects_a_clean_stop(
    database: Database,
) -> None:
    client = _FakeClient()
    client.events = [
        _event(1, "engine_started"),
        _raised(2, "E1"),
        _event(3, "escalation_resolved", escalation_id="E1"),
        _event(4, "task_done"),
        _raised(5, "E2"),
        _event(HISTORY_END, "engine_stopped"),
    ]
    client.views = {"E1": _view("E1", "resolved"), "E2": _view("E2")}
    source, journal = _source(database, client)
    asyncio.run(source.poll())
    assert _states(database) == {"api:escalation:E2": SignalState.NEW}
    assert journal.last_event_id(PROJECT) == HISTORY_END
    cursor = CursorStore(database, SignalStore(database)).get(PROJECT)
    assert cursor is not None
    assert cursor.respected_stop_id == HISTORY_END


def test_crash_between_signal_insert_and_cursor_commit_leaves_one_signal_per_event(
    database: Database,
) -> None:
    client = _FakeClient()
    source, journal = _source(database, client)
    asyncio.run(source.poll())
    client.events = [_raised(1, "E1"), _event(2, "task_done")]
    client.views = {"E1": _view("E1")}
    crashed, _ = _source(database, client, _CrashingSignals(database))
    with pytest.raises(RuntimeError, match="crash after"):
        asyncio.run(crashed.poll())
    assert journal.last_event_id(PROJECT) == 0
    asyncio.run(source.poll())
    asyncio.run(source.poll())
    assert sorted(_states(database)) == ["api:escalation:E1", "api:task_done:2"]


def test_engine_fault_of_a_running_engine_is_ignored_and_a_fault_stop_is_one_signal(
    database: Database,
) -> None:
    client = _FakeClient()
    source, _ = _source(database, client)
    asyncio.run(source.poll())
    client.events = [
        _event(1, "engine_fault", task_id=None, reason="crash"),
        _event(2, "engine_stopped", task_id=None, reason="repeated faults"),
    ]
    asyncio.run(source.poll())
    rows = database.connection.execute("SELECT id, kind, reason FROM signals").fetchall()
    assert [tuple(row) for row in rows] == [
        ("api:engine_fault:2", "engine_fault", "repeated faults")
    ]


def test_resolved_escalation_and_pr_lifecycle_close_the_matching_signals(
    database: Database,
) -> None:
    client = _FakeClient()
    source, _ = _source(database, client)
    asyncio.run(source.poll())
    client.views = {"E1": _view("E1")}
    client.events = [
        _raised(1, "E1"),
        _event(2, "pr_opened", task_id="T1", url="u", branch="b", commits=1),
        _event(3, "escalation_resolved", escalation_id="E1", resolution="answer"),
        _event(4, "pr_opened", task_id="T2"),
        _event(5, "pr_withdrawn", task_id="T2"),
        _event(6, "pr_opened", task_id="T3"),
        _event(7, "task_done", task_id="T3"),
    ]
    asyncio.run(source.poll())
    assert _states(database) == {
        "api:escalation:E1": SignalState.CLOSED,
        "api:pr_review:2": SignalState.NEW,
        "api:pr_review:4": SignalState.CLOSED,
        "api:pr_review:6": SignalState.CLOSED,
        "api:task_done:7": SignalState.NEW,
    }


def test_resolved_event_confirms_only_the_issued_op_with_its_command_id(
    database: Database,
) -> None:
    client = _FakeClient()
    source, _ = _source(database, client)
    asyncio.run(source.poll())
    ops = OpStore(database)
    ids: dict[str, int] = {}
    for command_id in ("12", "13"):
        op = NewOp(f"op-{command_id}", "resolve", PROJECT, state=OpState.INTENDED)
        ids[command_id] = ops.intend(op, NOW).row_id
        assert ops.transition(
            ids[command_id], OpState.INTENDED, OpState.ISSUED, NOW, command_id=command_id
        )
    client.views = {"E1": _view("E1")}
    client.events = [
        _raised(1, "E1"),
        _event(2, "escalation_resolved", escalation_id="E1", command_id=12, resolution="answer"),
    ]
    asyncio.run(source.poll())
    states = {op.command_id: op.state for op in ops.non_final()}
    assert states == {"13": OpState.ISSUED}
    confirmed = ops.get(ids["12"])
    assert confirmed is not None
    assert confirmed.state == OpState.CONFIRMED


def test_cursor_behind_retention_repeats_the_first_sync(database: Database) -> None:
    client = _FakeClient()
    source, journal = _source(database, client)
    asyncio.run(source.poll())
    client.events = [_raised(FAR_EVENT, "E9")]
    client.views = {"E9": _view("E9")}
    asyncio.run(source.poll())
    assert journal.last_event_id(PROJECT) == FAR_EVENT
    assert list(_states(database)) == ["api:escalation:E9"]


def test_escalation_event_without_its_data_is_a_protocol_error(database: Database) -> None:
    client = _FakeClient()
    source, _ = _source(database, client)
    asyncio.run(source.poll())
    client.events = [_event(1, "escalation")]
    with pytest.raises(ShikiruProtocolError):
        asyncio.run(source.poll())


def test_crash_during_first_sync_leaves_no_cursor_so_the_clean_stop_is_still_respected(
    database: Database,
) -> None:
    client = _FakeClient()
    client.events = [_event(1, "engine_started"), _raised(2, "E1"), _event(3, "engine_stopped")]
    client.views = {"E1": _view("E1")}
    crashed, journal = _source(database, client, _CrashingSignals(database))
    with pytest.raises(RuntimeError, match="crash after"):
        asyncio.run(crashed.poll())
    assert journal.last_event_id(PROJECT) is None
    restarted, _ = _source(database, client)
    asyncio.run(restarted.poll())
    cursor = CursorStore(database, SignalStore(database)).get(PROJECT)
    assert cursor is not None
    assert cursor.respected_stop_id == HISTORY_STOP


def test_first_sync_keeps_only_the_pull_request_that_is_still_waiting(database: Database) -> None:
    client = _FakeClient()
    client.events = [
        _event(1, "pr_opened", task_id="T1", url="u1", branch="b1", commits=1),
        _event(2, "pr_opened", task_id="T2", url="u2", branch="b2", commits=1),
        _event(3, "pr_withdrawn", task_id="T2"),
        _event(4, "pr_opened", task_id="T3", url="u3", branch="b3", commits=1),
        _event(5, "task_done", task_id="T3"),
        _event(6, "pr_opened", task_id="T4", url="u4", branch="b4", commits=1),
        _event(7, "escalation", task_id="T4", escalation_id="E4", reason="pr_closed"),
        _raised(8, "E1"),
    ]
    client.views = {"E1": _view("E1")}
    source, _ = _source(database, client)
    asyncio.run(source.poll())
    assert _states(database) == {
        "api:pr_review:1": SignalState.NEW,
        "api:escalation:E1": SignalState.NEW,
    }


def test_restart_and_repeated_first_sync_add_no_second_pr_review_signal(
    database: Database,
) -> None:
    client = _FakeClient()
    client.events = [_event(1, "pr_opened", task_id="T1", url="u", branch="b", commits=1)]
    source, _ = _source(database, client)
    asyncio.run(source.poll())
    restarted, journal = _source(database, client)
    asyncio.run(restarted.poll())
    client.events.append(_event(FAR_EVENT, "engine_started"))
    asyncio.run(restarted.poll())
    assert journal.last_event_id(PROJECT) == FAR_EVENT
    assert _states(database) == {"api:pr_review:1": SignalState.NEW}
