"""The quota wait: park until the reset plus a minute, then retry once, whatever happens between."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, cast

import pytest

from shikiri_yaku.app.loop_guard import LoopGuard
from shikiri_yaku.domain.outcome import Forward, Resolved, WaitUntil
from shikiri_yaku.domain.signal import Kind, Signal
from shikiri_yaku.extensions.infrastructure.extension import infrastructure_extension
from shikiri_yaku.extensions.infrastructure.rules import LADDER_EXHAUSTED, QUOTA_EXHAUSTED
from shikiri_yaku.journal import (
    Database,
    JournalActions,
    JournalRetries,
    JournalWaits,
    NewSignal,
    OpStore,
    SignalStore,
)
from shikiri_yaku.ports import Ack, AckStatus, EngineStatus, QuotaWindow
from tests.support.notices import journal_notifier, journal_outbox

if TYPE_CHECKING:
    from collections.abc import Generator

    from shikiri_yaku.domain.extension import Handler
    from shikiri_yaku.domain.outcome import Outcome
    from shikiri_yaku.journal import JournalOutbox
    from shikiri_yaku.ports import ShikiruClient

START = datetime(2026, 10, 28, 9, 0, tzinfo=UTC)
RESET = START + timedelta(hours=3)
WAKE = RESET + timedelta(minutes=1)
TASK = "T-1"


class FakeClock:
    def __init__(self) -> None:
        self.at = START

    def now(self) -> datetime:
        return self.at

    async def sleep(self, seconds: float) -> None:
        raise NotImplementedError


class QuotaClient:
    """Shikiru as the wait sees it: the quota windows it reports, and each resolution it gets."""

    def __init__(self, quotas: dict[str, list[QuotaWindow]]) -> None:
        self.quotas = quotas
        self.resolved: list[tuple[str, str]] = []

    async def status(self) -> EngineStatus:
        return EngineStatus(engine_up=True, quotas=self.quotas)

    async def resolve_escalation(
        self, escalation_id: str, kind: str, text: str | None = None
    ) -> Ack:
        del text
        self.resolved.append((escalation_id, kind))
        return Ack(id=1, status=AckStatus.DONE, result=None, message=None)


class Shop:
    """One instance over one journal; `handler()` builds the objects a process start builds."""

    def __init__(self, database: Database, client: QuotaClient, clock: FakeClock) -> None:
        self.database = database
        self.client = client
        self.clock = clock
        self.outbox: JournalOutbox = journal_outbox(database)

    def handler(self) -> Handler:
        ops = OpStore(self.database)
        guard = LoopGuard(JournalActions(ops), self.clock, 1, 1)
        extension = infrastructure_extension(
            {"api": cast("ShikiruClient", self.client)},
            guard,
            JournalRetries(self.database, ops),
            self.clock,
            waits=JournalWaits(self.database, ops),
            notifier=journal_notifier(self.outbox, self.clock),
            quota_wait_max=timedelta(hours=24),
            quota_wait_margin=timedelta(minutes=1),
        )
        return extension.handler

    def notices(self) -> int:
        return len(self.outbox.pending())


def exhausted(resets_at: datetime | None) -> dict[str, list[QuotaWindow]]:
    return {"5h": [QuotaWindow(name="5h", used_pct=100.0, resets_at=resets_at)]}


def journal_escalation(database: Database, reason: str, source: str = "E1") -> Signal:
    """Journal the escalation as the Router would, so a retry can find its number."""
    signal = Signal("api", Kind.ESCALATION, source, reason, "blocked", TASK, source)
    new = NewSignal(signal.id, "api", "escalation", reason, "blocked", TASK, source)
    SignalStore(database).insert_if_absent(new, START)
    return signal


def handle(handler: Handler, signal: Signal) -> Outcome:
    return asyncio.run(handler.handle(signal))


@pytest.fixture
def memory() -> Generator[Database]:
    database = Database.open(":memory:")
    yield database
    database.close()


@pytest.mark.parametrize("reason", [QUOTA_EXHAUSTED, LADDER_EXHAUSTED])
@pytest.mark.parametrize(
    "quotas",
    [
        pytest.param(
            {"5h": [QuotaWindow(name="5h", used_pct=40.0, resets_at=RESET)]},
            id="no-exhausted-window",
        ),
        pytest.param(exhausted(None), id="exhausted-without-reset"),
        pytest.param(exhausted(START + timedelta(hours=25)), id="reset-beyond-the-limit"),
    ],
)
def test_a_quota_escalation_forwards_when_no_wait_fits(
    memory: Database, reason: str, quotas: dict[str, list[QuotaWindow]]
) -> None:
    shop = Shop(memory, QuotaClient(quotas), FakeClock())
    signal = journal_escalation(memory, reason)

    outcome = handle(shop.handler(), signal)

    assert isinstance(outcome, Forward)
    assert shop.client.resolved == []
    assert shop.notices() == 0


@pytest.mark.parametrize("reason", [QUOTA_EXHAUSTED, LADDER_EXHAUSTED])
def test_a_reset_in_three_hours_parks_until_it_plus_a_minute_with_one_notice(
    memory: Database, reason: str
) -> None:
    shop = Shop(memory, QuotaClient(exhausted(RESET)), FakeClock())
    signal = journal_escalation(memory, reason)

    outcome = handle(shop.handler(), signal)

    assert isinstance(outcome, WaitUntil)
    assert outcome.wake_at == WAKE
    assert shop.notices() == 1
    assert shop.client.resolved == []


def test_the_call_after_the_wake_time_retries_once_and_tells_nothing_more(
    memory: Database,
) -> None:
    clock = FakeClock()
    shop = Shop(memory, QuotaClient(exhausted(RESET)), clock)
    signal = journal_escalation(memory, QUOTA_EXHAUSTED)
    handle(shop.handler(), signal)

    clock.at = WAKE
    outcome = handle(shop.handler(), signal)

    assert isinstance(outcome, Resolved)
    assert shop.client.resolved == [("E1", "retry")]
    assert shop.notices() == 1


def test_a_restart_between_the_two_calls_resolves_the_same_way(memory: Database) -> None:
    clock = FakeClock()
    shop = Shop(memory, QuotaClient(exhausted(RESET)), clock)
    signal = journal_escalation(memory, QUOTA_EXHAUSTED)
    handle(shop.handler(), signal)

    clock.at = WAKE
    outcome = handle(Shop(memory, shop.client, clock).handler(), signal)

    assert isinstance(outcome, Resolved)
    assert shop.client.resolved == [("E1", "retry")]
    assert shop.notices() == 1


def test_a_restart_before_the_wake_time_parks_until_the_same_time(memory: Database) -> None:
    clock = FakeClock()
    shop = Shop(memory, QuotaClient(exhausted(RESET)), clock)
    signal = journal_escalation(memory, QUOTA_EXHAUSTED)
    handle(shop.handler(), signal)

    clock.at = START + timedelta(hours=1)
    outcome = handle(Shop(memory, shop.client, clock).handler(), signal)

    assert isinstance(outcome, WaitUntil)
    assert outcome.wake_at == WAKE
    assert shop.notices() == 1
    assert shop.client.resolved == []


def test_a_wake_time_long_past_after_an_outage_resolves_at_once(memory: Database) -> None:
    clock = FakeClock()
    shop = Shop(memory, QuotaClient(exhausted(RESET)), clock)
    signal = journal_escalation(memory, QUOTA_EXHAUSTED)
    handle(shop.handler(), signal)

    clock.at = START + timedelta(hours=10)
    outcome = handle(shop.handler(), signal)

    assert isinstance(outcome, Resolved)
    assert shop.client.resolved == [("E1", "retry")]


def test_a_second_quota_wait_for_the_same_task_forwards_naming_the_first_retry(
    memory: Database,
) -> None:
    clock = FakeClock()
    client = QuotaClient(exhausted(RESET))
    shop = Shop(memory, client, clock)
    first = journal_escalation(memory, QUOTA_EXHAUSTED, "E1")
    handle(shop.handler(), first)
    clock.at = WAKE
    handle(shop.handler(), first)

    later_reset = WAKE + timedelta(hours=2)
    client.quotas = exhausted(later_reset)
    second = journal_escalation(memory, QUOTA_EXHAUSTED, "E2")
    handle(shop.handler(), second)
    clock.at = later_reset + timedelta(minutes=1)
    outcome = handle(shop.handler(), second)

    assert isinstance(outcome, Forward)
    assert WAKE.isoformat() in outcome.note
    assert client.resolved == [("E1", "retry")]
    assert shop.notices() == 2


def test_a_quota_signal_without_an_escalation_forwards_and_parks_nothing(
    memory: Database,
) -> None:
    shop = Shop(memory, QuotaClient(exhausted(RESET)), FakeClock())
    signal = Signal("api", Kind.ESCALATION, "E1", QUOTA_EXHAUSTED, "blocked", TASK)

    outcome = handle(shop.handler(), signal)

    assert isinstance(outcome, Forward)
    assert shop.notices() == 0
    assert JournalWaits(memory, OpStore(memory)).existing(signal) is None
