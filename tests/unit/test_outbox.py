from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from shikiri_yaku.app.outbox_sender import OutboxSender
from shikiri_yaku.app.scheduler import Scheduler
from shikiri_yaku.bootstrap import schedule_log
from shikiri_yaku.domain.outbound import Purpose
from shikiri_yaku.domain.schedule import Destination
from shikiri_yaku.domain.signal import Kind, Signal
from shikiri_yaku.extensions.product.forwarder import ProductForwarder
from shikiri_yaku.journal import (
    Database,
    Delivery,
    JournalOutbox,
    MessageStore,
    NewSignal,
    SignalState,
    SignalStore,
)
from shikiri_yaku.ports import Button, ChannelError, ChannelRefusedError, Message

if TYPE_CHECKING:
    from collections.abc import Generator

    from shikiri_yaku.domain.schedule import Parked

START = datetime(2026, 10, 28, 9, 0, tzinfo=UTC)
OWNER = Destination("telegram", "1001")
QUESTION = Message("S1 · Which way?", (Button("1", "a:1:1"),))
TOKEN = "123456789:" + "A" * 35


class _Clock:
    def __init__(self) -> None:
        self.moment = START

    def now(self) -> datetime:
        return self.moment

    async def sleep(self, seconds: float) -> None:
        raise NotImplementedError


class _Channel:
    def __init__(self) -> None:
        self.sent: list[tuple[str, Message]] = []
        self.failing = False
        self.refusal: str | None = None
        self.attempts = 0

    async def send(self, destination: str, message: Message) -> str:
        self.attempts += 1
        if self.refusal is not None:
            raise ChannelRefusedError(self.refusal)
        if self.failing:
            raise ChannelError
        self.sent.append((destination, message))
        return str(len(self.sent))

    async def edit(self, message_ref: str, message: Message) -> None:
        raise NotImplementedError


class _Reporter:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def report(self, line: str) -> None:
        self.lines.append(line)


class _Target:
    async def wake(self, parked: Parked) -> None:
        raise NotImplementedError


@pytest.fixture
def memory() -> Generator[Database]:
    database = Database.open(":memory:")
    yield database
    database.close()


def _signal(database: Database, kind: str = "escalation") -> int:
    signals = SignalStore(database)
    number = signals.insert_if_absent(
        NewSignal(f"api:{kind}:e1", "api", kind, "decision_required", "x", "T-1"), START
    ).row_id
    signals.classify(
        number, category="product", classified_by="rule", verdict=None, handler="p", now=START
    )
    signals.transition(number, SignalState.NEW, SignalState.HANDLING, START)
    signals.transition(number, SignalState.HANDLING, SignalState.FORWARDED, START)
    return number


def _outbox(database: Database, reporter: _Reporter | None = None) -> JournalOutbox:
    return JournalOutbox(database, MessageStore(database, reporter or _Reporter()))


def _sender(
    database: Database, channel: _Channel, reporter: _Reporter | None = None
) -> OutboxSender:
    """A new sender over the same journal: what an Operator restart gives."""
    return OutboxSender(_outbox(database, reporter), channel, _Clock(), interval_s=1)


def test_a_recorded_message_is_sent_after_a_restart_once_and_never_again_once_confirmed(
    memory: Database,
) -> None:
    number = _signal(memory)
    _outbox(memory).queue(number, Purpose.QUESTION, [OWNER], QUESTION, key="forward:1", now=START)
    channel = _Channel()

    asyncio.run(_sender(memory, channel).tick())
    asyncio.run(_sender(memory, channel).tick())

    assert channel.sent == [("1001", QUESTION)]
    assert _outbox(memory).pending() == []


def test_a_crash_between_sending_and_confirming_resends_once_with_the_signal_reference(
    memory: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    number = _signal(memory)
    _outbox(memory).queue(number, Purpose.QUESTION, [OWNER], QUESTION, key="forward:1", now=START)
    channel = _Channel()

    with monkeypatch.context() as patch:
        patch.setattr(JournalOutbox, "confirm", _crash)
        with pytest.raises(KeyboardInterrupt):
            asyncio.run(_sender(memory, channel).tick())
    asyncio.run(_sender(memory, channel).tick())
    asyncio.run(_sender(memory, channel).tick())

    assert len(channel.sent) == 2
    assert all("S1" in message.text for _, message in channel.sent)


def _crash(outbox: JournalOutbox, message_id: int, message_ref: str) -> None:
    del outbox, message_id, message_ref
    raise KeyboardInterrupt


def test_a_failed_send_stays_pending_and_is_sent_when_the_channel_recovers(
    memory: Database,
) -> None:
    number = _signal(memory)
    _outbox(memory).queue(number, Purpose.QUESTION, [OWNER], QUESTION, key="forward:1", now=START)
    channel = _Channel()
    channel.failing = True
    asyncio.run(_sender(memory, channel).tick())
    assert len(_outbox(memory).pending()) == 1

    channel.failing = False
    asyncio.run(_sender(memory, channel).tick())
    assert len(channel.sent) == 1


def test_a_message_the_platform_refuses_for_good_is_never_offered_again(memory: Database) -> None:
    number = _signal(memory)
    _outbox(memory).queue(number, Purpose.QUESTION, [OWNER], QUESTION, key="forward:1", now=START)
    channel = _Channel()
    channel.refusal = "too_long"

    asyncio.run(_sender(memory, channel).tick())
    channel.refusal = None
    asyncio.run(_sender(memory, channel).tick())

    assert channel.attempts == 1
    assert channel.sent == []
    assert _outbox(memory).pending() == []
    [stored] = MessageStore(memory, _Reporter()).for_signal(number)
    assert (stored.delivery, stored.body, stored.refusal) == (Delivery.REFUSED, None, "too_long")


def test_a_question_and_a_reminder_are_sent_by_the_same_sender(memory: Database) -> None:
    number = _signal(memory, Kind.PR_REVIEW)
    clock = _Clock()
    forwarder = ProductForwarder("Shop", {}, _outbox(memory), clock, [OWNER])
    signal = Signal("api", Kind.PR_REVIEW, "7", "review_ready", "A review is ready.")
    asyncio.run(forwarder.forward(number, signal, "note", ""))
    channel = _Channel()
    asyncio.run(_sender(memory, channel).tick())
    assert len(channel.sent) == 1

    clock.moment = START + timedelta(hours=4)
    scheduler = Scheduler(
        schedule_log(memory, _Reporter()),
        _Target(),
        clock,
        interval_s=1,
        reminder_first_h=4,
        reminder_repeat_h=24,
    )
    asyncio.run(scheduler.tick())
    assert channel.sent[1:] == []
    asyncio.run(_sender(memory, channel).tick())
    assert "Still waiting" in channel.sent[1][1].text


def test_a_secret_is_never_kept_or_sent_and_the_po_gets_a_notice_without_it(
    memory: Database,
) -> None:
    number = _signal(memory)
    secret = Message(f"S1 · token {TOKEN}", (Button("1", "a:1:1"),))
    _outbox(memory).queue(number, Purpose.QUESTION, [OWNER], secret, key="forward:1", now=START)
    channel = _Channel()

    asyncio.run(_sender(memory, channel).tick())

    [(_, notice)] = channel.sent
    assert f"S{number}" in notice.text
    journal = repr(memory.connection.execute("SELECT * FROM messages").fetchall())
    assert TOKEN not in journal
    [refused, _] = MessageStore(memory, _Reporter()).for_signal(number)
    assert (refused.delivery, refused.body, refused.refusal) == (
        Delivery.REFUSED,
        None,
        "telegram_bot_token",
    )


def test_a_notice_the_platform_refuses_is_reported_and_gets_no_notice_of_its_own(
    memory: Database,
) -> None:
    number = _signal(memory)
    secret = Message(f"S1 {TOKEN}")
    _outbox(memory).queue(number, Purpose.QUESTION, [OWNER], secret, key="forward:1", now=START)
    reporter = _Reporter()
    channel = _Channel()
    channel.refusal = "platform_refused"

    asyncio.run(_sender(memory, channel, reporter).tick())
    asyncio.run(_sender(memory, channel, reporter).tick())

    assert channel.attempts == 1
    assert _outbox(memory, reporter).pending() == []
    assert len(reporter.lines) == 1
    assert TOKEN not in reporter.lines[0]


def test_a_notice_that_cannot_be_journaled_is_reported_as_metadata_and_the_refusal_is_kept(
    memory: Database,
) -> None:
    memory.connection.execute(
        "CREATE TRIGGER no_notice BEFORE INSERT ON messages WHEN NEW.notice_key LIKE 'refused:%' "
        "BEGIN SELECT RAISE(ABORT, 'no notice'); END"
    )
    number = _signal(memory)
    reporter = _Reporter()
    secret = Message(f"S1 {TOKEN}")
    _outbox(memory, reporter).queue(
        number, Purpose.QUESTION, [OWNER], secret, key="forward:1", now=START
    )

    [stored] = MessageStore(memory, reporter).for_signal(number)
    assert (stored.delivery, stored.refusal) == (Delivery.REFUSED, "telegram_bot_token")
    [line] = reporter.lines
    assert f"S{number}" in line
    assert TOKEN not in line
