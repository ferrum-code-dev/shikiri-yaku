from __future__ import annotations

import multiprocessing
import os
import threading
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final

import pytest

from shikiri_yaku.adapters.stderr import StderrReporter
from shikiri_yaku.domain.loop_guard import Cause
from shikiri_yaku.journal import (
    CursorStore,
    Database,
    Inserted,
    JournalTooNewError,
    Lifecycle,
    MessageStore,
    NewOp,
    NewSignal,
    OpState,
    OpStore,
    Purpose,
    SignalState,
    SignalStore,
)
from shikiri_yaku.journal.database import LATEST_VERSION
from shikiri_yaku.journal.migrations import MIGRATIONS
from shikiri_yaku.ports import Message

if TYPE_CHECKING:
    from collections.abc import Generator
    from pathlib import Path

    from shikiri_yaku.journal import StoredSignal

NOW = datetime(2026, 10, 28, 9, 0, tzinfo=UTC)
PROJECT = "api"
BATCH_SIZE = 3
RACERS = 2
ROUNDS = 25


def _stored(signals: SignalStore, number: int) -> StoredSignal:
    stored = signals.get(number)
    assert stored is not None
    return stored


def _signal(source_id: str) -> NewSignal:
    return NewSignal(
        id=f"{PROJECT}:escalation:{source_id}",
        project=PROJECT,
        kind="escalation",
        reason="decision_required",
        text="which database?",
    )


class _CrashingSignals(SignalStore):
    """Fails right after the last signal of a batch is inserted, before the cursor moves."""

    def __init__(self, database: Database, *, hard_kill: bool) -> None:
        super().__init__(database)
        self._hard_kill = hard_kill

    def insert_if_absent(self, signal: NewSignal, now: datetime) -> Inserted:
        inserted = super().insert_if_absent(signal, now)
        if signal.id.endswith(f"E{BATCH_SIZE}"):
            if self._hard_kill:
                os._exit(1)
            message = "crash after the signal insert"
            raise RuntimeError(message)
        return inserted


def _run_crashing_batch(path: str, *, hard_kill: bool) -> None:
    database = Database.open(path)
    cursors = CursorStore(database, _CrashingSignals(database, hard_kill=hard_kill))
    signals = [_signal(f"E{number}") for number in range(1, BATCH_SIZE + 1)]
    cursors.record_batch(PROJECT, signals, 20, None, NOW)


def _assert_batch_left_nothing(path: Path) -> None:
    database = Database.open(path)
    cursor = CursorStore(database, SignalStore(database)).get(PROJECT)
    count = database.connection.execute("SELECT count(*) FROM signals").fetchone()[0]
    database.close()
    assert cursor is not None
    assert cursor.last_event_id == 10
    assert count == 0


@pytest.fixture
def memory() -> Generator[Database]:
    database = Database.open(":memory:")
    yield database
    database.close()


@pytest.fixture
def journal_path(tmp_path: Path) -> Path:
    path = tmp_path / "journal.db"
    database = Database.open(path)
    CursorStore(database, SignalStore(database)).record_batch(PROJECT, [], 10, None, NOW)
    database.close()
    return path


def test_migrations_create_fresh_database(memory: Database) -> None:
    database = memory
    tables = {
        row["name"]
        for row in database.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {"cursors", "signals", "messages", "ops", "topics"} <= tables


def test_database_newer_than_code_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "journal.db"
    database = Database.open(path)
    database.connection.execute(f"PRAGMA user_version = {LATEST_VERSION + 1}")
    database.close()
    with pytest.raises(JournalTooNewError):
        Database.open(path)


def test_same_signal_id_twice_is_one_row(memory: Database) -> None:
    database = memory
    signals = SignalStore(database)
    first = signals.insert_if_absent(_signal("E1"), NOW)
    second = signals.insert_if_absent(_signal("E1"), NOW)
    assert (first.is_new, second.is_new) == (True, False)
    assert first.row_id == second.row_id
    assert database.connection.execute("SELECT count(*) FROM signals").fetchone()[0] == 1


def test_signal_transition_is_conditional_and_parked_signals_wake(memory: Database) -> None:
    signals = SignalStore(memory)
    number = signals.insert_if_absent(_signal("E1"), NOW).row_id
    wake = NOW + timedelta(hours=1)
    assert signals.transition(number, SignalState.NEW, SignalState.PARKED, NOW, wake_at=wake)
    assert not signals.transition(number, SignalState.NEW, SignalState.HANDLING, NOW)
    assert signals.parked_due(NOW) == []
    assert [s.number for s in signals.parked_due(wake)] == [number]


def test_a_removed_projects_open_signals_close_with_the_note_and_the_rest_stay(
    memory: Database,
) -> None:
    signals = SignalStore(memory)
    gone = NewSignal("old:escalation:E1", "old", "escalation", "decision_required", "which?")
    parked = NewSignal("old:escalation:E2", "old", "escalation", "decision_required", "which?")
    resolved = NewSignal("old:escalation:E3", "old", "escalation", "decision_required", "which?")
    gone_number = signals.insert_if_absent(gone, NOW).row_id
    parked_number = signals.insert_if_absent(parked, NOW).row_id
    resolved_number = signals.insert_if_absent(resolved, NOW).row_id
    assert signals.transition(parked_number, SignalState.NEW, SignalState.PARKED, NOW, note="wait")
    assert signals.transition(resolved_number, SignalState.NEW, SignalState.RESOLVED, NOW)
    kept = signals.insert_if_absent(_signal("E1"), NOW).row_id

    assert signals.close_removed([PROJECT], "removed", NOW) == 2

    gone_after = _stored(signals, gone_number)
    assert (gone_after.state, gone_after.notes) == (SignalState.CLOSED, "removed")
    assert _stored(signals, parked_number).notes == "wait\nremoved"
    assert _stored(signals, resolved_number).state == SignalState.RESOLVED
    assert _stored(signals, kept).state == SignalState.NEW


def test_non_final_ops_exclude_confirmed_and_failed(memory: Database) -> None:
    ops = OpStore(memory)
    ids = [ops.intend(NewOp(f"op{n}", "resolve", PROJECT), NOW).row_id for n in range(4)]
    assert ops.intend(NewOp("op0", "resolve", PROJECT), NOW).is_new is False
    assert ops.transition(ids[1], OpState.INTENDED, OpState.CONFIRMED, NOW)
    assert ops.transition(ids[2], OpState.INTENDED, OpState.FAILED, NOW)
    assert [op.id for op in ops.non_final()] == [ids[0], ids[3]]


def test_op_is_approved_by_exactly_one_of_two_concurrent_callers(tmp_path: Path) -> None:
    path = tmp_path / "journal.db"
    setup = Database.open(path)
    setup_ops = OpStore(setup)
    op_ids = [
        setup_ops.intend(NewOp(f"install{n}", "install", PROJECT), NOW).row_id
        for n in range(ROUNDS)
    ]
    setup.close()
    barrier = threading.Barrier(RACERS)
    wins: list[int] = []

    def approve() -> None:
        database = Database.open(path)
        ops = OpStore(database)
        for op_id in op_ids:
            barrier.wait()
            if ops.transition(op_id, OpState.INTENDED, OpState.APPROVED, NOW):
                wins.append(op_id)
        database.close()

    threads = [threading.Thread(target=approve) for _ in range(RACERS)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(wins) == op_ids


def test_batch_commits_signals_and_cursor_together(journal_path: Path) -> None:
    database = Database.open(journal_path)
    cursors = CursorStore(database, SignalStore(database))
    stop = Lifecycle(event_id=18, kind="engine_stopped", has_reason=False, at=NOW)
    cursors.record_batch(PROJECT, [_signal("E1")], 20, stop, NOW)
    cursors.record_batch(PROJECT, [], 25, None, NOW)
    cursor = cursors.get(PROJECT)
    assert cursor is not None
    assert (cursor.last_event_id, cursor.lifecycle) == (25, stop)
    database.close()


def test_failure_after_signal_insert_leaves_cursor_and_signals_unchanged(
    journal_path: Path,
) -> None:
    database = Database.open(journal_path)
    cursors = CursorStore(database, _CrashingSignals(database, hard_kill=False))
    signals = [_signal(f"E{number}") for number in range(1, BATCH_SIZE + 1)]
    with pytest.raises(RuntimeError, match="crash after"):
        cursors.record_batch(PROJECT, signals, 20, None, NOW)
    database.close()
    _assert_batch_left_nothing(journal_path)


def test_process_killed_after_signal_insert_leaves_cursor_and_signals_unchanged(
    journal_path: Path,
) -> None:
    context = multiprocessing.get_context("spawn")
    process = context.Process(
        target=_run_crashing_batch, args=(str(journal_path),), kwargs={"hard_kill": True}
    )
    process.start()
    process.join()
    assert process.exitcode == 1
    _assert_batch_left_nothing(journal_path)


def test_a_notice_is_queued_once_per_owner_chat(memory: Database) -> None:
    signal = SignalStore(memory).insert_if_absent(_signal("E1"), NOW).row_id
    messages = MessageStore(memory, StderrReporter())

    def queue(chat: str) -> Inserted:
        return messages.record_outbound(
            signal_number=signal,
            channel="telegram",
            chat=chat,
            purpose=Purpose.NOTICE,
            body=Message("done"),
            notice_key="done:api:T-1",
            now=NOW,
        )

    first, second = queue("owner-a"), queue("owner-b")
    assert (first.is_new, second.is_new) == (True, True)
    assert first.row_id != second.row_id
    retry = queue("owner-b")
    assert (retry.row_id, retry.is_new) == (second.row_id, False)
    assert len(messages.unconfirmed()) == 2


def test_upgrade_from_schema_one_keeps_existing_messages(memory: Database) -> None:
    connection = memory.connection
    for table in (
        "command_replies",
        "topics",
        "messages",
        "ops",
        "signals",
        "cursors",
        "polling",
    ):
        connection.execute(f"DROP TABLE {table}")
    for statement in MIGRATIONS[0]:
        connection.execute(statement)
    connection.execute("PRAGMA user_version = 1")
    connection.execute(
        "INSERT INTO messages (direction, channel, chat, delivery, created_at, notice_key)"
        " VALUES ('out', 'telegram', 'owner-a', 'sent', 'then', 'done:api:T-1')"
    )
    for number, statements in enumerate(MIGRATIONS[1:], start=2):
        for statement in statements:
            connection.execute(statement)
        connection.execute(f"PRAGMA user_version = {number}")
    rows = connection.execute("SELECT notice_key FROM messages").fetchall()
    assert [row["notice_key"] for row in rows] == ["done:api:T-1"]


def test_created_times_filter_by_kind_project_cause_and_window(memory: Database) -> None:
    ops = OpStore(memory)
    old = NOW - timedelta(days=2)
    ops.intend(NewOp("a", "retry", PROJECT, "t1", "agent_error"), old)
    ops.intend(NewOp("b", "retry", PROJECT, "t1", "agent_error"), NOW)
    ops.intend(NewOp("c", "retry", PROJECT, "t2", "agent_error"), NOW)
    ops.intend(NewOp("d", "retry", "web", "t1", "agent_error"), NOW)
    cause = Cause("t1", "agent_error")
    assert ops.created_times("retry", PROJECT, cause, None) == [old, NOW]
    assert ops.created_times("retry", PROJECT, cause, NOW - timedelta(days=1)) == [NOW]
    assert ops.created_times("retry", PROJECT, None, None) == [old, NOW, NOW]
    assert ops.created_times("restart", PROJECT, None, None) == []


_SCHEMA_BEFORE_SLOTS: Final = 19


@pytest.mark.parametrize(
    ("purpose", "notice_key", "slot"),
    [
        (Purpose.QUESTION, "forward:3", "question"),
        (Purpose.QUESTION, "reshow:9", "question"),
        (Purpose.QUESTION, "open:700:3", "question"),
        (Purpose.QUESTION, "input:700", None),
        (Purpose.QUESTION, "goal:7", None),
        (Purpose.APPROVAL, "install:5", "install"),
        (Purpose.APPROVAL, "install:5:open:700", "install"),
        (Purpose.APPROVAL, "confirm:7", "confirm:7"),
        (Purpose.APPROVAL, "confirm:7:700", "confirm:7"),
        (Purpose.APPROVAL, "confirm:123:700", "confirm:123"),
        (Purpose.NOTICE, "install-refused:5", None),
        (Purpose.NOTICE, "forward:3", None),
        (Purpose.NOTICE, "open:700:done:0", None),
    ],
)
def test_a_copy_sent_before_slots_is_given_the_slot_its_kind_has_now(
    memory: Database, purpose: Purpose, notice_key: str, slot: str | None
) -> None:
    """An upgrade gives each old copy the slot its kind gets now, read from its notice key.

    Input prompts, goal replies and notices keep no slot, so they never replace another copy
    (design §7).
    """
    connection = memory.connection
    connection.execute("ALTER TABLE messages DROP COLUMN slot")
    connection.execute(f"PRAGMA user_version = {_SCHEMA_BEFORE_SLOTS}")
    connection.execute(
        "INSERT INTO messages (direction, channel, chat, delivery, purpose, notice_key,"
        " created_at) VALUES ('out', 'telegram', 'owner', 'sent', ?, ?, 'then')",
        (purpose, notice_key),
    )
    for statements in MIGRATIONS[_SCHEMA_BEFORE_SLOTS:]:
        for statement in statements:
            connection.execute(statement)
    row = connection.execute(
        "SELECT slot FROM messages WHERE notice_key = ?", (notice_key,)
    ).fetchone()
    assert row["slot"] == slot
