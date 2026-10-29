from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final, cast

import pytest

from shikiri_yaku.adapters.telegram_topics import TelegramDestinations
from shikiri_yaku.app.goal import GoalCommand
from shikiri_yaku.app.submit import Submitter
from shikiri_yaku.domain.inbound import Inbound, InboundKind
from shikiri_yaku.domain.schedule import Destination
from shikiri_yaku.journal import Database, JournalOutbox, MessageStore, OpStore, SignalStore
from shikiri_yaku.journal.replies import JournalReplies
from shikiri_yaku.journal.submits import JournalSubmits
from shikiri_yaku.journal.topics import TopicStore
from shikiri_yaku.ports import (
    Ack,
    AckStatus,
    ShikiruLinkDownError,
    ShikiruRejectedError,
    SubmitStatus,
)
from tests.support.telegram_bot import FakeBotApi

if TYPE_CHECKING:
    from collections.abc import Generator

    from shikiri_yaku.ports import Clock, Message, ShikiruClient

NOW: Final = datetime(2026, 10, 28, 9, 0, tzinfo=UTC)
OWNER: Final = Destination("telegram", "1001")
GOAL: Final = "/goal api Add the export"
DONE: Final = Ack(id=7, status=AckStatus.DONE, result={"task_id": "T-9"}, message=None)
GROUP: Final = -1005
TOPIC: Final = 77
GENERAL_THREAD: Final = 5


class _Clock:
    def now(self) -> datetime:
        return NOW


class _Reporter:
    def report(self, line: str) -> None:
        pytest.fail(line)


class _Client:
    def __init__(self) -> None:
        self.answer: Ack | Exception = DONE
        self.requests: list[str] = []

    async def submit_task(self, request: str) -> Ack:
        self.requests.append(request)
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


class _Goal:
    """The /goal path over an in-memory journal and a fake Shikiru client."""

    def __init__(self, database: Database) -> None:
        self.client = _Client()
        self.submits = JournalSubmits(database, OpStore(database))
        self.outbox = JournalOutbox(database, MessageStore(database, _Reporter()))
        clients = {"api": cast("ShikiruClient", self.client)}
        clock = cast("Clock", _Clock())
        replies = JournalReplies(database, SignalStore(database), OpStore(database))
        submitter = Submitter(self.submits, clients, clock)
        self.topics = TopicStore(database)
        self.command = GoalCommand(
            submitter,
            replies,
            clients,
            self.outbox,
            clock,
            destinations=TelegramDestinations(
                self.topics, FakeBotApi(), owners=(1001,), group_id=GROUP, components=("api",)
            ),
            channel="telegram",
        )

    def send(self, text: str, update: int = 1, chat: int = 1001, thread: int | None = None) -> None:
        item = Inbound(update, InboundKind.TEXT, 1001, chat, f"{chat}/90", text=text, thread=thread)
        asyncio.run(self.command.handle(item))

    def tap(self, data: str, ref: str, update: int = 2, chat: int = 1001) -> None:
        asyncio.run(self.command.tap(Inbound(update, InboundKind.TAP, 1001, chat, ref, data=data)))

    def flush(self) -> dict[str, Message]:
        """Confirm every pending message as sent, so the journal can match a tap to it."""
        sent: dict[str, Message] = {}
        for pending in self.outbox.pending():
            ref = f"M-{pending.message_id}"
            self.outbox.confirm(pending.message_id, ref)
            sent[ref] = pending.message
        return sent


@pytest.fixture
def goal() -> Generator[_Goal]:
    database = Database.open(":memory:")
    yield _Goal(database)
    database.close()


def _unknown_goal(goal: _Goal, chat: int = 1001) -> tuple[int, str]:
    """Send a goal whose outcome is unknown and flush its question; return the op id and ref."""
    goal.client.answer = ShikiruLinkDownError("link down")
    goal.send(GOAL, chat=chat)
    [op] = goal.submits.unfinished()
    [ref] = goal.flush()
    goal.client.answer = DONE
    return op.id, ref


def test_a_goal_is_submitted_once_and_its_task_id_is_reported(goal: _Goal) -> None:
    goal.send(GOAL)
    [pending] = goal.outbox.pending()
    assert pending.destination == OWNER
    assert "T-9" in pending.message.text
    assert goal.client.requests == ["Add the export"]


def test_a_duplicate_update_submits_once_and_replies_once(goal: _Goal) -> None:
    goal.send(GOAL)
    goal.send(GOAL)
    assert goal.client.requests == ["Add the export"]
    assert len(goal.outbox.pending()) == 1


@pytest.mark.parametrize("text", ["/goal", "/goal api", "/goal nope Add it", "/goal api   "])
def test_a_goal_without_a_known_component_and_request_gets_usage_and_submits_nothing(
    goal: _Goal, text: str
) -> None:
    goal.send(text)
    [pending] = goal.outbox.pending()
    assert "/goal" in pending.message.text
    assert pending.message.buttons == ()
    assert goal.client.requests == []
    assert goal.submits.unfinished() == []


def test_a_rejected_goal_is_reported_as_not_submitted_and_never_asked_about(goal: _Goal) -> None:
    goal.client.answer = ShikiruRejectedError("no such component")
    goal.send(GOAL)
    [pending] = goal.outbox.pending()
    assert pending.message.buttons == ()
    assert goal.submits.unfinished() == []


def test_an_unknown_outcome_is_asked_about_with_a_submit_again_button(goal: _Goal) -> None:
    goal.client.answer = ShikiruLinkDownError("link down")
    goal.send(GOAL)
    [op] = goal.submits.unfinished()
    [message] = goal.flush().values()
    assert [button.data for button in message.buttons] == [f"s:{op.id}"]
    assert goal.client.requests == ["Add the export"]


def test_submit_again_retries_the_unknown_goal_and_reports_its_task(goal: _Goal) -> None:
    prompt, ref = _unknown_goal(goal)
    goal.tap(f"s:{prompt}", ref)
    [pending] = goal.outbox.pending()
    assert "T-9" in pending.message.text
    assert goal.client.requests == ["Add the export"] * 2
    original = goal.submits.get(prompt)
    assert original is not None
    assert original.status == SubmitStatus.UNKNOWN
    assert goal.submits.unfinished() == []


def test_a_duplicate_delivery_of_one_tap_retries_once_and_replies_once(goal: _Goal) -> None:
    prompt, ref = _unknown_goal(goal)
    goal.tap(f"s:{prompt}", ref)
    goal.tap(f"s:{prompt}", ref)
    assert goal.client.requests == ["Add the export"] * 2
    assert len(goal.outbox.pending()) == 1


def test_a_tap_that_was_never_offered_retries_nothing(goal: _Goal) -> None:
    prompt, ref = _unknown_goal(goal)
    goal.tap(f"s:{prompt}", "M-missing", update=2)
    goal.tap(f"s:{prompt}", ref, update=3, chat=2002)
    goal.tap("s:999", ref, update=4)
    assert goal.client.requests == ["Add the export"]


def test_submit_again_tapped_in_the_group_retries_and_reports_in_the_group(goal: _Goal) -> None:
    prompt, ref = _unknown_goal(goal, chat=GROUP)
    goal.tap(f"s:{prompt}", ref, chat=GROUP)
    [pending] = goal.outbox.pending()
    assert "T-9" in pending.message.text
    assert pending.destination == Destination("telegram", str(GROUP))
    assert goal.client.requests == ["Add the export"] * 2


def test_a_goal_in_a_component_topic_is_submitted_to_that_component_in_that_topic(
    goal: _Goal,
) -> None:
    goal.topics.save("api", TOPIC)
    goal.send("/goal Add the export", chat=GROUP, thread=TOPIC)
    [pending] = goal.outbox.pending()
    assert pending.destination == Destination("telegram", str(GROUP), TOPIC)
    assert goal.client.requests == ["Add the export"]


def test_a_goal_in_the_general_topic_without_a_component_gets_usage_and_submits_nothing(
    goal: _Goal,
) -> None:
    goal.topics.save(None, GENERAL_THREAD)
    goal.send("/goal fix the export", chat=GROUP, thread=GENERAL_THREAD)
    [pending] = goal.outbox.pending()
    assert "/goal" in pending.message.text
    assert pending.destination == Destination("telegram", str(GROUP), GENERAL_THREAD)
    assert goal.client.requests == []
