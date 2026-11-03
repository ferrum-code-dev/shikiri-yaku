"""A failed handler of an unresolved condition stands until the condition ends (design §6.1, §6.2).

A restart that Shikiru refuses, whose checks cannot be read, or that never returns keeps its
engine-down message on screen with Stop reminders. The tap silences the reminders, and the engine's
return still edits the message. A failed notice is information: its message closes its signal.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final, cast

import pytest

from shikiri_yaku.app.copy_editor import CopyEditor
from shikiri_yaku.app.loop_guard import LoopGuard
from shikiri_yaku.app.notifier import Notifier
from shikiri_yaku.app.router import Router
from shikiri_yaku.bootstrap import failure_policy, schedule_log
from shikiri_yaku.domain.outbound import ENGINE_UP_LINE, REMINDERS_STOPPED_LINE
from shikiri_yaku.domain.rule_classifier import RuleClassifier
from shikiri_yaku.domain.signal import Closing, Kind, Signal
from shikiri_yaku.extensions.infrastructure.extension import infrastructure_extension
from shikiri_yaku.extensions.infrastructure.rules import stands_on_failure as infrastructure_stands
from shikiri_yaku.extensions.notice.extension import notice_extension
from shikiri_yaku.extensions.notice.rules import stands_on_failure as notice_stands
from shikiri_yaku.extensions.product.extension import product_extension
from shikiri_yaku.extensions.product.forwarder import ProductForwarder
from shikiri_yaku.extensions.review.rules import stands_on_failure as review_stands
from shikiri_yaku.journal import (
    Database,
    JournalActions,
    JournalFinalEdits,
    JournalRetries,
    JournalRouting,
    JournalStarts,
    JournalWaits,
    NewSignal,
    OpStore,
    SignalState,
    SignalStore,
)
from shikiri_yaku.ports import DoctorCheck, ShikiruLinkDownError, ShikiruRejectedError
from shikiri_yaku.settings import (
    BUTTON_LABEL_MAX_CHARS,
    MESSAGE_TITLE_MAX_CHARS,
    InfrastructureSettings,
)
from tests.support.destinations import FixedDestinations
from tests.unit.test_replies import OWNER, Harness

if TYPE_CHECKING:
    from collections.abc import Callable, Generator, Mapping

    from shikiri_yaku.domain.outcome import Outcome
    from shikiri_yaku.ports import Channel, Message, ShikiruClient

PROJECT: Final = "api"
REF: Final = "1001/77"
NOW: Final = datetime(2026, 10, 28, tzinfo=UTC)
LATER: Final = NOW + timedelta(days=1)
TIMEOUT_S: Final = 0.05
INFRA: Final = InfrastructureSettings()


class _Clock:
    def __init__(self) -> None:
        self.moment = NOW

    def now(self) -> datetime:
        return self.moment

    async def sleep(self, seconds: float) -> None:
        del seconds


class _Shikiru:
    """An engine that is down; `failure` says how the start of it or the checks fail."""

    def __init__(self, failure: str) -> None:
        self.failure = failure

    async def start_engine(self) -> None:
        if self.failure == "start hangs":
            await asyncio.Event().wait()
        message = "the engine is locked by another start"
        raise ShikiruRejectedError(message)

    async def doctor(self) -> list[DoctorCheck]:
        if self.failure == "checks fail":
            message = "link lost before the checks"
            raise ShikiruLinkDownError(message)
        return [DoctorCheck(name="daemon", ok=False, detail="not running")]

    async def get_task(self, task_id: str) -> None:
        del task_id


class _TaskListDown:
    async def list_tasks(self) -> list[object]:
        message = "link lost before the task list"
        raise ShikiruLinkDownError(message)

    async def get_task(self, task_id: str) -> None:
        del task_id


class _NoInstall:
    reasons: frozenset[str] = frozenset()

    async def run(self, signal: Signal) -> Outcome:
        del signal
        raise NotImplementedError


class _Reporter:
    def report(self, line: str) -> None:
        pytest.fail(line)


class _Channel:
    def __init__(self) -> None:
        self.edits: list[tuple[str, Message]] = []

    async def edit(self, message_ref: str, message: Message) -> None:
        self.edits.append((message_ref, message))


@pytest.fixture
def h() -> Generator[Harness]:
    harness = Harness()
    yield harness
    harness.database.close()


def _signal(database: Database, number: int) -> Signal:
    stored = SignalStore(database).get(number)
    assert stored is not None
    return stored.to_signal()


def _state(database: Database, number: int) -> SignalState:
    stored = SignalStore(database).get(number)
    assert stored is not None
    return stored.state


def _engine_down(database: Database) -> int:
    return (
        SignalStore(database)
        .insert_if_absent(
            NewSignal(
                f"{PROJECT}:engine_down:1", PROJECT, Kind.ENGINE_DOWN, "agent_error", "x", "T1"
            ),
            NOW,
        )
        .row_id
    )


def _forwarder(h: Harness, clock: _Clock, clients: Mapping[str, ShikiruClient]) -> ProductForwarder:
    return ProductForwarder(
        MESSAGE_TITLE_MAX_CHARS,
        clients,
        h.outbox,
        clock,
        FixedDestinations([OWNER]),
        button_label_max_chars=BUTTON_LABEL_MAX_CHARS,
    )


def _restarting(h: Harness, clock: _Clock, failure: str) -> Router:
    journal = h.database
    clients = {PROJECT: cast("ShikiruClient", _Shikiru(failure))}
    guard = LoopGuard(
        JournalActions(OpStore(journal)),
        clock,
        retry_budget_per_day=INFRA.retry_budget_per_day,
        restart_budget_per_hour=INFRA.restart_budget_per_hour,
    )
    extension = infrastructure_extension(
        clients,
        guard,
        JournalRetries(journal, OpStore(journal)),
        clock,
        waits=JournalWaits(journal, OpStore(journal)),
        starts=JournalStarts(journal, OpStore(journal), SignalStore(journal)),
        notifier=Notifier(h.outbox, FixedDestinations([OWNER]), clock),
        install=_NoInstall(),
        quota_wait_max=timedelta(hours=INFRA.quota_wait_max_h),
        quota_wait_margin=timedelta(minutes=INFRA.quota_wait_margin_min),
        restart_confirm=timedelta(seconds=INFRA.restart_confirm_s),
        restart_poll=timedelta(seconds=INFRA.restart_poll_s),
    )
    return Router(
        RuleClassifier([extension, product_extension()]),
        {extension.category: extension.handler},
        _forwarder(h, clock, clients),
        JournalRouting(journal, SignalStore(journal)),
        clock,
        handler_timeout_s=TIMEOUT_S,
        forward_retry_s=INFRA.restart_poll_s,
        standing_on_failure=failure_policy([extension]),
    )


def _edit_copies(h: Harness, clock: _Clock, channel: _Channel) -> None:
    editor = CopyEditor(
        JournalFinalEdits(h.database, h.log()),
        cast("Channel", channel),
        clock,
        interval_s=1,
    )
    asyncio.run(editor.tick())


@pytest.mark.parametrize(
    ("failure", "cause"),
    [
        ("start refused", "Shikiru refused the start"),
        ("checks fail", "handler failed: ShikiruLinkDownError"),
        ("start hangs", "handler timed out"),
    ],
)
def test_a_failed_restart_stands_and_the_tap_only_silences_it(
    h: Harness, failure: str, cause: str
) -> None:
    number = _engine_down(h.database)
    clock = _Clock()
    router = _restarting(h, clock, failure)

    asyncio.run(router.route(number, _signal(h.database, number)))

    assert _state(h.database, number) is SignalState.FORWARDED
    stored = SignalStore(h.database).get(number)
    assert stored is not None
    assert cause in (stored.notes or "")
    (pending,) = h.outbox.pending()
    assert [button.data for button in pending.message.buttons] == [f"k:{number}"]
    h.outbox.confirm(pending.message_id, REF)
    schedule = schedule_log(h.database, _Reporter())
    assert [awaiting.number for awaiting in schedule.awaiting_answer()] == [number]

    h.handle(ref=REF, data=f"k:{number}")

    assert schedule.awaiting_answer() == []
    assert _state(h.database, number) is SignalState.FORWARDED
    channel = _Channel()
    _edit_copies(h, clock, channel)
    assert [ref for ref, _ in channel.edits] == [REF]
    assert channel.edits[0][1].text.endswith(REMINDERS_STOPPED_LINE)

    clock.moment = LATER
    SignalStore(h.database).close(
        Closing(PROJECT, Kind.ENGINE_DOWN, forwarded_only=True, final_line=ENGINE_UP_LINE),
        LATER,
    )
    _edit_copies(h, clock, channel)
    assert [ref for ref, _ in channel.edits] == [REF, REF]
    assert channel.edits[1][1].text.endswith(ENGINE_UP_LINE)
    assert channel.edits[1][1].buttons == ()
    assert _state(h.database, number) is SignalState.CLOSED


def test_a_failed_notice_is_information_and_closes_its_signal(h: Harness) -> None:
    number = (
        SignalStore(h.database)
        .insert_if_absent(
            NewSignal(
                f"{PROJECT}:task_done:T-1", PROJECT, Kind.TASK_DONE, "task_done", "done", "T-1"
            ),
            NOW,
        )
        .row_id
    )
    clock = _Clock()
    clients = {PROJECT: cast("ShikiruClient", _TaskListDown())}
    destinations = FixedDestinations([OWNER])
    extension = notice_extension(
        clients,
        SignalStore(h.database),
        h.outbox,
        Notifier(h.outbox, destinations, clock),
        clock,
        destinations=destinations,
    )
    router = Router(
        RuleClassifier([extension, product_extension()]),
        {extension.category: extension.handler},
        _forwarder(h, clock, clients),
        JournalRouting(h.database, SignalStore(h.database)),
        clock,
        handler_timeout_s=TIMEOUT_S,
        forward_retry_s=INFRA.restart_poll_s,
        standing_on_failure=failure_policy([extension]),
    )

    asyncio.run(router.route(number, _signal(h.database, number)))

    assert _state(h.database, number) is SignalState.CLOSED
    (pending,) = h.outbox.pending()
    assert pending.message.buttons == ()


@pytest.mark.parametrize(
    ("predicate", "kind", "stands"),
    [
        (infrastructure_stands, Kind.ENGINE_DOWN, True),
        (infrastructure_stands, Kind.ENGINE_FAULT, True),
        (infrastructure_stands, Kind.TASK_DONE, False),
        (notice_stands, Kind.HEALTH, True),
        (notice_stands, Kind.TASK_DONE, False),
        (review_stands, Kind.PR_REVIEW, True),
    ],
)
def test_only_an_unresolved_condition_stands_when_its_handler_fails(
    predicate: Callable[[Signal], bool], kind: Kind, *, stands: bool
) -> None:
    assert predicate(Signal(PROJECT, kind, "1", "", "x")) is stands
