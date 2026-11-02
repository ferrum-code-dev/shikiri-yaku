"""The notice handler: a finished family is announced once, and a health message ends on recovery.

Design §6.5: the last task of a family to finish sends one message for the family, and a replay
sends nothing more. A health signal is sent once and edited to "Recovered" when it closes.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final, cast

import pytest

from shikiri_yaku.adapters.stderr import StderrReporter
from shikiri_yaku.adapters.telegram_topics import TOPIC_OPEN, TelegramDestinations
from shikiri_yaku.app.copy_editor import CopyEditor
from shikiri_yaku.app.health_source import LINK_SCOPE, RECOVERED_LINE
from shikiri_yaku.app.notifier import Notifier
from shikiri_yaku.app.router import Router
from shikiri_yaku.bootstrap import ENDING_NOTICE_PREFIXES, schedule_log
from shikiri_yaku.domain.outbound import STOP_REMINDERS_LABEL, Purpose
from shikiri_yaku.domain.outcome import Informed, Notified, Resolved
from shikiri_yaku.domain.rule_classifier import RuleClassifier
from shikiri_yaku.domain.signal import Closing, Kind
from shikiri_yaku.extensions.notice.extension import notice_extension
from shikiri_yaku.extensions.notice.handler import HEALTH_KEY, SENT, WAITING, NoticeHandler
from shikiri_yaku.extensions.product.extension import product_extension
from shikiri_yaku.journal import (
    Database,
    JournalFinalEdits,
    JournalRouting,
    NewSignal,
    OpStore,
    SignalState,
    SignalStore,
)
from shikiri_yaku.journal.replies import JournalReplies
from shikiri_yaku.journal.topics import TopicStore
from shikiri_yaku.ports import Button, Message, ShikiruLinkDownError, ShikiruTask, TaskStatus
from tests.support.destinations import FixedDestinations
from tests.support.notices import WHERE, journal_notifier, journal_outbox

if TYPE_CHECKING:
    from collections.abc import Generator, Sequence

    from shikiri_yaku.domain.signal import Signal
    from shikiri_yaku.ports import Channel, ShikiruClient

PROJECT: Final = "api"
NOW: Final = datetime(2026, 10, 29, 9, 0, tzinfo=UTC)
HEALTH_TEXT: Final = "The link to api has been unreachable since 09:00."
FAMILY_TEXT: Final = "api · Title of T-1 integrated (3 tasks)"


class _Clock:
    def now(self) -> datetime:
        return NOW

    async def sleep(self, seconds: float) -> None:
        del seconds


class _Tasks:
    """Shikiru's task list of the project; `down` makes it fail the way a down link does."""

    def __init__(self, tasks: Sequence[ShikiruTask] = (), *, down: bool = False) -> None:
        self._tasks = list(tasks)
        self._down = down

    async def list_tasks(self, statuses: Sequence[str] | None = None) -> list[ShikiruTask]:
        del statuses
        if self._down:
            raise ShikiruLinkDownError
        return self._tasks


class _Channel:
    def __init__(self) -> None:
        self.edits: list[tuple[str, Message]] = []

    async def edit(self, message_ref: str, message: Message) -> None:
        self.edits.append((message_ref, message))


class _Fallback:
    def __init__(self) -> None:
        self.forwarded: list[tuple[str, str]] = []

    async def forward(
        self, number: int, signal: Signal, note: str, suggestion: str, *, standing: bool
    ) -> bool:
        del number, suggestion, standing
        self.forwarded.append((signal.id, note))
        return False


@pytest.fixture
def memory() -> Generator[Database]:
    database = Database.open(":memory:")
    yield database
    database.close()


def _task(task_id: str, status: str, parent: str | None = None) -> ShikiruTask:
    return ShikiruTask.model_validate(
        {"id": task_id, "parent_id": parent, "status": status, "title": f"Title of {task_id}"}
    )


def _done(database: Database, task_id: str) -> tuple[int, Signal]:
    signals = SignalStore(database)
    number = signals.insert_if_absent(
        NewSignal(
            f"{PROJECT}:task_done:{task_id}", PROJECT, "task_done", "task_done", "done", task_id
        ),
        NOW,
    ).row_id
    stored = signals.get(number)
    assert stored is not None
    return number, stored.to_signal()


def _handler(database: Database, client: _Tasks) -> NoticeHandler:
    outbox = journal_outbox(database)
    return NoticeHandler(
        {PROJECT: cast("ShikiruClient", client)},
        SignalStore(database),
        outbox,
        journal_notifier(outbox, _Clock()),
        _Clock(),
        destinations=FixedDestinations([WHERE]),
    )


def _texts(database: Database) -> list[str]:
    return [pending.message.text for pending in journal_outbox(database).pending()]


FAMILY_OF_THREE = [_task("T-1", TaskStatus.DONE), _task("T-2", TaskStatus.DONE, "T-1")]
ANNOUNCED = [
    pytest.param(
        [_task("T-1", TaskStatus.DONE)],
        "T-1",
        ["api · Title of T-1 integrated (1 task)"],
        id="a task with no children is announced at once",
    ),
    pytest.param(
        [*FAMILY_OF_THREE, _task("T-3", TaskStatus.DONE, "T-1")],
        "T-3",
        [FAMILY_TEXT],
        id="the last of three is announced for the family with the count 3",
    ),
    pytest.param(
        [*FAMILY_OF_THREE, _task("T-3", TaskStatus.DONE, "T-2")],
        "T-3",
        [FAMILY_TEXT],
        id="a grandchild walks up to the root",
    ),
    pytest.param(
        [*FAMILY_OF_THREE, _task("T-3", TaskStatus.CANCELLED, "T-1")],
        "T-2",
        [FAMILY_TEXT],
        id="a cancelled child neither holds the family back nor leaves the count",
    ),
]
SILENT = [
    pytest.param(
        [*FAMILY_OF_THREE, _task("T-3", TaskStatus.RUNNING, "T-1")],
        "T-2",
        id="a running child",
    ),
    pytest.param(
        [_task("T-1", TaskStatus.DONE), _task("T-2", TaskStatus.QUEUED, "T-1")],
        "T-1",
        id="a queued child",
    ),
    pytest.param(
        [_task("T-1", TaskStatus.INTEGRATING), _task("T-2", TaskStatus.DONE, "T-1")],
        "T-2",
        id="an integrating root",
    ),
    pytest.param(
        [*FAMILY_OF_THREE, _task("T-3", TaskStatus.ESCALATED, "T-1")],
        "T-2",
        id="an escalated child",
    ),
]


@pytest.mark.parametrize(("tasks", "trigger", "texts"), ANNOUNCED)
def test_the_last_task_of_a_family_announces_it_once(
    memory: Database, tasks: list[ShikiruTask], trigger: str, texts: list[str]
) -> None:
    _, signal = _done(memory, trigger)

    outcome = asyncio.run(_handler(memory, _Tasks(tasks)).handle(signal))

    assert outcome == Informed(SENT)
    assert _texts(memory) == texts


@pytest.mark.parametrize(("tasks", "trigger"), SILENT)
def test_a_family_with_an_open_task_stays_silent(
    memory: Database, tasks: list[ShikiruTask], trigger: str
) -> None:
    _, signal = _done(memory, trigger)

    outcome = asyncio.run(_handler(memory, _Tasks(tasks)).handle(signal))

    assert outcome == Resolved(WAITING)
    assert _texts(memory) == []


def test_replaying_the_events_of_a_finished_family_sends_nothing_more(memory: Database) -> None:
    family = [*FAMILY_OF_THREE, _task("T-3", TaskStatus.DONE, "T-1")]
    handler = _handler(memory, _Tasks(family))

    for trigger in ("T-3", "T-2", "T-3"):
        _, signal = _done(memory, trigger)
        asyncio.run(handler.handle(signal))

    assert _texts(memory) == [FAMILY_TEXT]


def test_a_health_message_is_sent_once_and_its_closing_edits_it_to_recovered(
    memory: Database,
) -> None:
    signals = SignalStore(memory)
    number = signals.insert_if_absent(
        NewSignal(
            f"{PROJECT}:health:{LINK_SCOPE}:2026-10-29T09:00Z",
            PROJECT,
            "health",
            LINK_SCOPE,
            HEALTH_TEXT,
        ),
        NOW,
    ).row_id
    stored = signals.get(number)
    assert stored is not None

    outcome = asyncio.run(_handler(memory, _Tasks()).handle(stored.to_signal()))

    assert outcome == Notified(SENT)
    outbox = journal_outbox(memory)
    [pending] = outbox.pending()
    assert pending.message.text == HEALTH_TEXT
    assert pending.message.buttons == (Button(STOP_REMINDERS_LABEL, f"k:{number}"),)
    outbox.confirm(pending.message_id, "ref-1")
    signals.close(
        Closing(PROJECT, kind=Kind.HEALTH, reason=LINK_SCOPE, final_line=RECOVERED_LINE), NOW
    )
    channel = _Channel()
    replies = JournalReplies(memory, signals, OpStore(memory))
    editor = CopyEditor(
        JournalFinalEdits(memory, replies),
        cast("Channel", channel),
        _Clock(),
        interval_s=1,
    )

    asyncio.run(editor.tick())

    [(ref, message)] = channel.edits
    assert ref == "ref-1"
    assert message.text == f"{HEALTH_TEXT}\n\n{RECOVERED_LINE}"
    assert message.buttons == ()


def test_a_health_copy_sent_before_the_upgrade_is_edited_to_recovered_and_never_reminded(
    memory: Database,
) -> None:
    signals = SignalStore(memory)
    number = signals.insert_if_absent(
        NewSignal(
            f"{PROJECT}:health:{LINK_SCOPE}:2026-10-29T09:00Z",
            PROJECT,
            "health",
            LINK_SCOPE,
            HEALTH_TEXT,
        ),
        NOW,
    ).row_id
    signals.transition(number, SignalState.NEW, SignalState.HANDLING, NOW)
    signals.transition(number, SignalState.HANDLING, SignalState.FORWARDED, NOW)
    # keep: the copy an Operator had before the upgrade: no button, and no standing flag
    outbox = journal_outbox(memory)
    outbox.queue(
        number,
        Purpose.NOTICE,
        [WHERE],
        Message(HEALTH_TEXT),
        key=f"{HEALTH_KEY}:{number}",
        now=NOW,
    )
    [pending] = outbox.pending()
    outbox.confirm(pending.message_id, "ref-1")
    assert schedule_log(memory, StderrReporter()).awaiting_answer() == []

    signals.close(
        Closing(PROJECT, kind=Kind.HEALTH, reason=LINK_SCOPE, final_line=RECOVERED_LINE), NOW
    )
    replies = JournalReplies(memory, signals, OpStore(memory))
    channel = _Channel()
    editor = CopyEditor(
        JournalFinalEdits(memory, replies, ending_prefixes=ENDING_NOTICE_PREFIXES),
        cast("Channel", channel),
        _Clock(),
        interval_s=1,
    )

    asyncio.run(editor.tick())

    [(ref, message)] = channel.edits
    assert ref == "ref-1"
    assert message.text == f"{HEALTH_TEXT}\n\n{RECOVERED_LINE}"


async def _route(router: Router, number: int, signal: Signal) -> None:
    router.submit(number, signal)
    await router.join()


def _router(database: Database, client: _Tasks, fallback: _Fallback) -> Router:
    outbox = journal_outbox(database)
    extension = notice_extension(
        {PROJECT: cast("ShikiruClient", client)},
        SignalStore(database),
        outbox,
        journal_notifier(outbox, _Clock()),
        _Clock(),
        destinations=FixedDestinations([WHERE]),
    )
    return Router(
        RuleClassifier([extension, product_extension()]),
        {extension.category: extension.handler},
        fallback,
        JournalRouting(database, SignalStore(database)),
        _Clock(),
        handler_timeout_s=1,
        forward_retry_s=1,
    )


def test_a_notice_that_fails_is_forwarded_with_the_original_signal(memory: Database) -> None:
    number, signal = _done(memory, "T-1")
    fallback = _Fallback()

    asyncio.run(_route(_router(memory, _Tasks(down=True), fallback), number, signal))

    [(sent_id, note)] = fallback.forwarded
    assert sent_id == signal.id
    assert "ShikiruLinkDownError" in note
    stored = SignalStore(memory).get(number)
    assert stored is not None
    assert stored.state is SignalState.FORWARDED


def test_a_finished_task_is_informed_and_its_signal_closes_without_forwarding(
    memory: Database,
) -> None:
    number, signal = _done(memory, "T-1")
    fallback = _Fallback()
    tasks = _Tasks([_task("T-1", TaskStatus.DONE)])

    asyncio.run(_route(_router(memory, tasks, fallback), number, signal))

    stored = SignalStore(memory).get(number)
    assert stored is not None
    assert stored.state is SignalState.CLOSED
    assert _texts(memory) == ["api · Title of T-1 integrated (1 task)"]
    assert fallback.forwarded == []


@pytest.mark.parametrize(
    ("group_id", "prefixed"),
    [(None, True), (-1009, False)],
    ids=["private chat", "component topic"],
)
def test_the_integrated_notice_names_the_component_only_outside_its_topic(
    memory: Database, *, group_id: int | None, prefixed: bool
) -> None:
    topics = TopicStore(memory)
    topics.save(PROJECT, 73, TOPIC_OPEN)
    destinations = TelegramDestinations(topics, owners=(1001,), group_id=group_id, reconciler=None)
    outbox = journal_outbox(memory)
    handler = NoticeHandler(
        {PROJECT: cast("ShikiruClient", _Tasks([_task("T-1", TaskStatus.DONE)]))},
        SignalStore(memory),
        outbox,
        Notifier(outbox, destinations, _Clock()),
        _Clock(),
        destinations=destinations,
    )
    _, signal = _done(memory, "T-1")

    asyncio.run(handler.handle(signal))

    [text] = _texts(memory)
    assert text.startswith(f"{PROJECT} · ") == prefixed
    assert "Title of T-1 integrated (1 task)" in text
