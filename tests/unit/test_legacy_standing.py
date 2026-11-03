"""An upgraded journal marks the copies its categories recognize as standing, once (design §7).

A copy sent before the standing flag existed has its reminders and its Stop reminders button, but
no flag, so its tap would close the signal. The repair marks the copies whose category, signal
kind, notice key and button all match. A copy without the button stays information, and a copy
queued after the repair stands only when its handler says so.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final, cast

import pytest

from shikiri_yaku.app.copy_editor import CopyEditor
from shikiri_yaku.app.event_source import MERGED_LINE
from shikiri_yaku.bootstrap import ENDING_NOTICE_PREFIXES, schedule_log
from shikiri_yaku.domain.callback import stop_reminders_data
from shikiri_yaku.domain.outbound import ENGINE_UP_LINE, REMINDERS_STOPPED_LINE, Purpose
from shikiri_yaku.domain.signal import Closing, Kind, PullRequest
from shikiri_yaku.extensions.infrastructure.rules import LEGACY_COPIES as INFRASTRUCTURE_COPIES
from shikiri_yaku.extensions.notice.rules import LEGACY_COPIES as NOTICE_COPIES
from shikiri_yaku.extensions.review.rules import LEGACY_COPIES as REVIEW_COPIES
from shikiri_yaku.journal import (
    JournalFinalEdits,
    NewOp,
    NewSignal,
    OpState,
    Repairs,
    SignalState,
    SignalStore,
)
from shikiri_yaku.journal.migrations import MIGRATIONS
from shikiri_yaku.journal.replies import SILENCE
from shikiri_yaku.ports import Button, Message
from tests.unit.test_replies import OWNER, Harness, Reporter

if TYPE_CHECKING:
    from collections.abc import Generator

    from shikiri_yaku.ports import Channel

PROJECT: Final = "api"
REF: Final = "1001/77"
LATER_REF: Final = "1001/78"
NOW: Final = datetime(2026, 10, 28, tzinfo=UTC)
LATER: Final = NOW + timedelta(days=1)
BEFORE_STANDING: Final = 22
STOP_LABEL: Final = "Stop reminders"


class _Clock:
    def now(self) -> datetime:
        return LATER

    async def sleep(self, seconds: float) -> None:
        del seconds


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


def _forwarded(
    h: Harness, identity: str, kind: Kind, *, pull_request: PullRequest | None = None
) -> int:
    number = h.signals.insert_if_absent(
        NewSignal(identity, PROJECT, kind, kind.value, "text", pull_request=pull_request), NOW
    ).row_id
    assert h.signals.transition(number, SignalState.NEW, SignalState.FORWARDED, NOW)
    return number


def _state(h: Harness, number: int) -> SignalState:
    stored = h.signals.get(number)
    assert stored is not None
    return stored.state


def _record(h: Harness, number: int, ref: str, key: str, buttons: tuple[str, ...]) -> None:
    """Record a sent notice with the buttons it was sent with, as the outbox did before standing."""
    row = h.messages.record_outbound(
        signal_number=number,
        channel="telegram",
        chat="1001",
        purpose=Purpose.NOTICE,
        body=Message("notice", tuple(Button(STOP_LABEL, data) for data in buttons)),
        notice_key=key,
        now=NOW,
    )
    h.messages.confirm_delivery(row.row_id, ref)


def _stop_data(h: Harness, ref: str) -> str:
    """Return the callback data the copy was sent with, read back from the journal."""
    message = h.log().sent_message(OWNER, ref)
    assert message is not None
    (button,) = message.buttons
    return button.data


def _open_from_22(h: Harness) -> None:
    """Reopen the journal as one written at schema 22: no standing flag and no repair record."""
    with h.database.transaction() as connection:
        connection.execute("ALTER TABLE messages DROP COLUMN standing")
        connection.execute("DROP TABLE repairs")
        connection.execute(f"PRAGMA user_version = {BEFORE_STANDING}")
        for number, statements in enumerate(
            MIGRATIONS[BEFORE_STANDING:], start=BEFORE_STANDING + 1
        ):
            for statement in statements:
                connection.execute(statement)
            connection.execute(f"PRAGMA user_version = {number}")


def _repair(h: Harness) -> int:
    copies = [*INFRASTRUCTURE_COPIES, *NOTICE_COPIES, *REVIEW_COPIES]
    return Repairs(h.database).adopt_legacy_standing(copies)


def _awaiting(h: Harness) -> list[int]:
    schedule = schedule_log(h.database, Reporter())
    return [awaiting.number for awaiting in schedule.awaiting_answer()]


def _edit(h: Harness, channel: _Channel) -> None:
    editor = CopyEditor(
        JournalFinalEdits(h.database, h.log(), ending_prefixes=ENDING_NOTICE_PREFIXES),
        cast("Channel", channel),
        _Clock(),
        interval_s=1,
    )
    asyncio.run(editor.tick())


@pytest.mark.parametrize(
    ("kind", "key"),
    [
        (Kind.PR_REVIEW, "review:{number}"),
        (Kind.ENGINE_DOWN, "forward:{number}"),
        (Kind.ENGINE_FAULT, "forward:{number}"),
    ],
)
def test_a_legacy_copy_with_its_stop_button_stands_and_its_tap_silences_it(
    h: Harness, kind: Kind, key: str
) -> None:
    number = _forwarded(h, f"{PROJECT}:{kind}:1", kind)
    _record(h, number, REF, key.format(number=number), (stop_reminders_data(number),))
    _open_from_22(h)

    assert _repair(h) == 1
    assert _awaiting(h) == [number]

    h.handle(ref=REF, data=_stop_data(h, REF))

    assert _awaiting(h) == []
    assert h.log().silenced(number)
    assert _state(h, number) is SignalState.FORWARDED
    channel = _Channel()
    _edit(h, channel)
    assert [ref for ref, _ in channel.edits] == [REF]
    assert channel.edits[0][1].text.endswith(REMINDERS_STOPPED_LINE)


def test_a_buttonless_health_copy_stays_information_and_still_gets_its_recovery_edit(
    h: Harness,
) -> None:
    number = _forwarded(h, f"{PROJECT}:health:1", Kind.HEALTH)
    _record(h, number, REF, f"health:{number}", ())
    _open_from_22(h)

    assert _repair(h) == 0
    assert _awaiting(h) == []

    SignalStore(h.database).close(
        Closing(PROJECT, Kind.HEALTH, forwarded_only=True, final_line=ENGINE_UP_LINE), LATER
    )
    channel = _Channel()
    _edit(h, channel)
    assert [ref for ref, _ in channel.edits] == [REF]
    assert channel.edits[0][1].text.endswith(ENGINE_UP_LINE)
    assert channel.edits[0][1].buttons == ()


@pytest.mark.parametrize(
    ("kind", "key"),
    [
        (Kind.ENGINE_DOWN, "restart:{identity}"),
        (Kind.TASK_DONE, "forward:{number}"),
    ],
)
def test_a_restart_or_task_done_copy_with_a_stop_button_is_not_promoted(
    h: Harness, kind: Kind, key: str
) -> None:
    identity = f"{PROJECT}:{kind}:1"
    number = _forwarded(h, identity, kind)
    _record(
        h,
        number,
        REF,
        key.format(number=number, identity=identity),
        (stop_reminders_data(number),),
    )
    _open_from_22(h)

    assert _repair(h) == 0
    assert _awaiting(h) == []


def test_an_engine_forward_without_a_stop_button_stays_information(h: Harness) -> None:
    number = _forwarded(h, f"{PROJECT}:engine_down:1", Kind.ENGINE_DOWN)
    _record(h, number, REF, f"forward:{number}", ())
    _open_from_22(h)

    assert _repair(h) == 0
    assert _awaiting(h) == []


def test_a_legacy_pr_fallback_is_adopted_silenced_and_edited_when_the_pr_merges(
    h: Harness,
) -> None:
    pull = PullRequest("https://github.com/acme/api/pull/7", 3)
    number = _forwarded(h, f"{PROJECT}:pr_review:1", Kind.PR_REVIEW, pull_request=pull)
    _record(h, number, REF, f"forward:{number}", (stop_reminders_data(number),))
    _open_from_22(h)

    assert _repair(h) == 1
    assert _awaiting(h) == [number]
    h.handle(ref=REF, data=_stop_data(h, REF))
    assert _awaiting(h) == []
    assert h.log().silenced(number)
    assert _state(h, number) is SignalState.FORWARDED

    SignalStore(h.database).close(Closing(PROJECT, Kind.PR_REVIEW, final_line=MERGED_LINE), LATER)
    channel = _Channel()
    _edit(h, channel)

    assert [ref for ref, _ in channel.edits] == [REF]
    assert channel.edits[0][1].text.endswith(MERGED_LINE)
    assert channel.edits[0][1].buttons == ()


def test_a_legacy_pr_fallback_without_recorded_pr_metadata_is_not_adopted(h: Harness) -> None:
    number = _forwarded(h, f"{PROJECT}:pr_review:1", Kind.PR_REVIEW)
    _record(h, number, REF, f"forward:{number}", (stop_reminders_data(number),))
    _open_from_22(h)

    assert _repair(h) == 0
    assert _awaiting(h) == []


def test_a_silence_stored_before_the_repair_keeps_its_copy_hidden(h: Harness) -> None:
    number = _forwarded(h, f"{PROJECT}:pr_review:1", Kind.PR_REVIEW)
    _record(h, number, REF, f"review:{number}", (stop_reminders_data(number),))
    h.ops.intend(NewOp(f"{SILENCE}:{number}", SILENCE, PROJECT, state=OpState.CONFIRMED), NOW)
    _open_from_22(h)

    assert _repair(h) == 1
    assert h.log().silenced(number)
    assert _awaiting(h) == []


def test_the_repair_runs_once_and_a_copy_queued_after_it_is_not_promoted(h: Harness) -> None:
    old = _forwarded(h, f"{PROJECT}:pr_review:1", Kind.PR_REVIEW)
    _record(h, old, REF, f"review:{old}", (stop_reminders_data(old),))
    _open_from_22(h)
    assert _repair(h) == 1

    new = _forwarded(h, f"{PROJECT}:pr_review:2", Kind.PR_REVIEW)
    _record(h, new, LATER_REF, f"review:{new}", (stop_reminders_data(new),))

    assert _repair(h) == 0
    assert _awaiting(h) == [old]
