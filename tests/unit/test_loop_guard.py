from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from shikiri_yaku.app.loop_guard import LoopGuard
from shikiri_yaku.domain.loop_guard import Granted, Refused
from shikiri_yaku.journal import Database, JournalActions, OpStore

if TYPE_CHECKING:
    from collections.abc import Generator

START = datetime(2026, 10, 28, 9, 0, tzinfo=UTC)
PROJECT = "api"
REASON = "agent_error"


class FakeClock:
    def __init__(self) -> None:
        self.moment = START

    def now(self) -> datetime:
        return self.moment

    async def sleep(self, seconds: float) -> None:
        raise NotImplementedError


@pytest.fixture
def memory() -> Generator[Database]:
    database = Database.open(":memory:")
    yield database
    database.close()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def guard(memory: Database, clock: FakeClock) -> LoopGuard:
    return LoopGuard(JournalActions(OpStore(memory)), clock, 1, 1)


def _refusals(memory: Database) -> int:
    row = memory.connection.execute("SELECT count(*) FROM ops WHERE kind LIKE '%_refused'")
    return int(row.fetchone()[0])


def test_spent_retry_budget_is_refused_and_another_task_is_allowed(guard: LoopGuard) -> None:
    assert isinstance(guard.retry(PROJECT, "t1", REASON), Granted)
    assert isinstance(guard.retry(PROJECT, "t1", REASON), Refused)
    assert isinstance(guard.retry(PROJECT, "t2", REASON), Granted)
    assert isinstance(guard.retry(PROJECT, "t1", "agent_stuck"), Granted)


def test_restart_budget_counts_across_tasks_and_reasons(guard: LoopGuard) -> None:
    assert isinstance(guard.restart(PROJECT), Granted)
    assert isinstance(guard.retry(PROJECT, "t1", REASON), Granted)
    assert isinstance(guard.restart(PROJECT), Refused)
    assert isinstance(guard.restart("web"), Granted)


def test_identities_stay_unique_after_the_window_has_passed(
    guard: LoopGuard, clock: FakeClock
) -> None:
    granted = [guard.retry(PROJECT, "t1", REASON)]
    for _ in range(3):
        clock.moment += timedelta(days=1, minutes=1)
        granted.append(guard.retry(PROJECT, "t1", REASON))
    assert all(isinstance(result, Granted) for result in granted)
    assert len({result.op_id for result in granted if isinstance(result, Granted)}) == len(granted)


def test_a_refusal_is_journaled_with_its_history_and_not_counted(
    guard: LoopGuard, memory: Database, clock: FakeClock
) -> None:
    guard.retry(PROJECT, "t1", REASON)
    first = guard.retry(PROJECT, "t1", REASON)
    second = guard.retry(PROJECT, "t1", REASON)
    assert isinstance(first, Refused)
    assert isinstance(second, Refused)
    assert "was at 09:00 UTC." in first.note
    assert _refusals(memory) == 2
    guard.restart(PROJECT)
    assert isinstance(guard.restart(PROJECT), Refused)
    assert _refusals(memory) == 3
    assert {op.kind for op in OpStore(memory).non_final()} == {"retry", "restart"}
    clock.moment += timedelta(days=1, minutes=1)
    assert isinstance(guard.retry(PROJECT, "t1", REASON), Granted)
