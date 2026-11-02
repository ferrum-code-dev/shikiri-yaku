"""The review handler: one review message per pull request, with its link and number (§6.4)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final, cast

import pytest

from shikiri_yaku.adapters.stderr import StderrReporter
from shikiri_yaku.domain.outbound import Purpose
from shikiri_yaku.domain.outcome import Forward, Notified
from shikiri_yaku.domain.schedule import Destination
from shikiri_yaku.domain.signal import PullRequest
from shikiri_yaku.extensions.review.handler import NOTHING_TRIED, SENT, ReviewHandler
from shikiri_yaku.extensions.review.message import ACKNOWLEDGE_LABEL, OPEN_PR_LABEL
from shikiri_yaku.journal import Database, JournalOutbox, MessageStore, NewSignal, SignalStore
from shikiri_yaku.ports import Button, ShikiruLinkDownError, TaskDetail
from tests.support.destinations import FixedDestinations

if TYPE_CHECKING:
    from collections.abc import Generator, Sequence

    from shikiri_yaku.domain.signal import Signal
    from shikiri_yaku.ports import Message, Outbox, ShikiruClient

PROJECT: Final = "api"
TASK_ID: Final = "T-1"
TASK_TITLE: Final = "Add retries to the sync"
PRODUCT: Final = "Shop"
NOW: Final = datetime(2026, 10, 28, 9, 0, tzinfo=UTC)
OWNER: Final = Destination("telegram", "1001")
PULL: Final = PullRequest("https://github.com/acme/api/pull/7", 3)


class _Clock:
    def now(self) -> datetime:
        return NOW

    async def sleep(self, seconds: float) -> None:
        raise NotImplementedError


class _Client:
    """Answers get_task with the task's title, or fails the way a down link does."""

    def __init__(self, *, down: bool = False) -> None:
        self.down = down

    async def get_task(self, task_id: str) -> TaskDetail | None:
        if self.down:
            raise ShikiruLinkDownError
        return TaskDetail.model_validate(
            {
                "task": {
                    "id": task_id,
                    "parent_id": None,
                    "status": "done",
                    "title": TASK_TITLE,
                }
            }
        )


class _Outbox:
    def __init__(self) -> None:
        self.queued: list[tuple[int | None, Purpose, Sequence[Destination], Message, str]] = []

    def queue(
        self,
        number: int | None,
        purpose: Purpose,
        destinations: Sequence[Destination],
        message: Message,
        *,
        key: str,
        now: datetime,
        slot: str | None = None,
    ) -> None:
        del now, slot
        self.queued.append((number, purpose, destinations, message, key))


@pytest.fixture
def memory() -> Generator[Database]:
    database = Database.open(":memory:")
    yield database
    database.close()


def _stored(database: Database, pull: PullRequest | None) -> tuple[int, Signal]:
    signals = SignalStore(database)
    number = signals.insert_if_absent(
        NewSignal(
            "api:pr_review:1",
            PROJECT,
            "pr_review",
            "pr_opened",
            "x",
            TASK_ID,
            pull_request=pull,
        ),
        NOW,
    ).row_id
    stored = signals.get(number)
    assert stored is not None
    return number, stored.to_signal()


def _handler(database: Database, client: _Client, outbox: Outbox) -> ReviewHandler:
    return ReviewHandler(
        {PROJECT: cast("ShikiruClient", client)},
        SignalStore(database),
        outbox,
        _Clock(),
        product=PRODUCT,
        destinations=FixedDestinations([OWNER]),
    )


def test_a_pull_request_gets_one_review_message_with_its_link_and_signal_number(
    memory: Database,
) -> None:
    number, signal = _stored(memory, PULL)
    outbox = _Outbox()

    outcome = asyncio.run(_handler(memory, _Client(), cast("Outbox", outbox)).handle(signal))

    assert outcome == Notified(SENT)
    [(queued, purpose, destinations, message, key)] = outbox.queued
    assert (queued, purpose, tuple(destinations), key) == (
        number,
        Purpose.NOTICE,
        (OWNER,),
        f"review:{number}",
    )
    assert message.buttons == (
        Button(OPEN_PR_LABEL, url=PULL.url),
        Button(ACKNOWLEDGE_LABEL, f"k:{number}"),
    )


@pytest.mark.parametrize("down", [False, True], ids=["title", "link down"])
def test_the_message_names_the_task_title_and_survives_a_down_link(
    memory: Database, *, down: bool
) -> None:
    _, signal = _stored(memory, PULL)
    outbox = _Outbox()

    outcome = asyncio.run(
        _handler(memory, _Client(down=down), cast("Outbox", outbox)).handle(signal)
    )

    [(_, _, _, message, _)] = outbox.queued
    assert outcome == Notified(SENT)
    assert (TASK_TITLE in message.text) == (not down)


def test_a_signal_without_its_pull_request_is_forwarded_and_nothing_is_queued(
    memory: Database,
) -> None:
    _, signal = _stored(memory, None)
    outbox = _Outbox()

    outcome = asyncio.run(_handler(memory, _Client(), cast("Outbox", outbox)).handle(signal))

    assert outcome == Forward(NOTHING_TRIED)
    assert outbox.queued == []


def _journal_outbox(database: Database) -> JournalOutbox:
    """A new outbox over the same journal: what an Operator restart gives."""
    return JournalOutbox(database, MessageStore(database, StderrReporter()))


def test_a_handler_run_again_after_a_restart_queues_the_review_once(memory: Database) -> None:
    _, signal = _stored(memory, PULL)

    for _ in range(2):
        outbox = cast("Outbox", _journal_outbox(memory))
        asyncio.run(_handler(memory, _Client(), outbox).handle(signal))

    assert len(_journal_outbox(memory).pending()) == 1
