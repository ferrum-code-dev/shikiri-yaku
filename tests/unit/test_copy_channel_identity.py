"""Sent-copy operations keep equal channel references on separate journal rows."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final

import pytest
from pydantic import TypeAdapter

from shikiri_yaku.app.copy_editor import CopyEditor
from shikiri_yaku.app.copy_replacer import CopyReplacer
from shikiri_yaku.domain.outbound import Purpose
from shikiri_yaku.domain.place import Place
from shikiri_yaku.journal import Database, JournalFinalEdits, JournalReplacements, MessageStore
from shikiri_yaku.journal.ops import OpStore
from shikiri_yaku.journal.replies import JournalReplies
from shikiri_yaku.journal.signals import SignalStore
from shikiri_yaku.ports import ChannelError, ChannelRefusedError, Message
from tests.support.secrets import SECRETS

if TYPE_CHECKING:
    from collections.abc import Generator

NOW: Final = datetime(2026, 10, 28, tzinfo=UTC)
REF: Final = "same-reference"
_BODY: Final = TypeAdapter(Message)


class _Clock:
    def now(self) -> datetime:
        return NOW

    async def sleep(self, seconds: float) -> None:
        raise NotImplementedError


class _Reporter:
    def report(self, line: str) -> None:
        pytest.fail(line)


class _Channel:
    def __init__(self, failure: Exception | None = None) -> None:
        self.failure = failure
        self.calls: list[str] = []

    async def edit(self, message_ref: str, message: Message) -> None:
        del message
        self.calls.append(message_ref)
        if self.failure is not None:
            raise self.failure

    async def delete(self, message_ref: str) -> None:
        self.calls.append(message_ref)
        if self.failure is not None:
            raise self.failure


@pytest.fixture
def journal() -> Generator[Database]:
    database = Database.open(":memory:")
    yield database
    database.close()


def _sent(store: MessageStore, channel: str, ref: str, *, slot: str | None = None) -> int:
    created = store.record_outbound(
        signal_number=None,
        place=Place(channel, "place"),
        purpose=Purpose.NOTICE,
        body=Message("original"),
        notice_key=None,
        now=NOW,
        slot=slot,
    )
    store.confirm_delivery(created.row_id, ref)
    return created.row_id


def _row(database: Database, message_id: int) -> tuple[object, ...]:
    row = database.connection.execute(
        "SELECT delivery, body, refusal, final_edited, final_body FROM messages WHERE id = ?",
        (message_id,),
    ).fetchone()
    return tuple(row)


@pytest.mark.parametrize("outcome", ["success", "refused", "secret"])
def test_final_edit_changes_only_the_selected_channel_copy(journal: Database, outcome: str) -> None:
    store = MessageStore(journal, _Reporter())
    other = _sent(store, "second", REF)
    first = _sent(store, "first", REF)
    body = Message(f"secret {SECRETS['bot_token']}") if outcome == "secret" else Message("final")
    journal.connection.execute(
        "UPDATE messages SET final_body = ? WHERE id IN (?, ?)",
        (_BODY.dump_json(body).decode(), first, other),
    )
    journal.connection.execute("UPDATE messages SET final_edited = 1 WHERE id = ?", (other,))
    untouched = _row(journal, other)
    replies = JournalReplies(journal, SignalStore(journal), OpStore(journal), channel_name="first")
    final = JournalFinalEdits(journal, replies, max_text=1000)
    failure = ChannelRefusedError("refused") if outcome == "refused" else None
    channel = _Channel(failure)
    asyncio.run(CopyEditor(final, store, channel, channel, _Clock(), interval_s=1).tick())

    assert _row(journal, other) == untouched
    first_row = _row(journal, first)
    assert first_row[3] == 1
    if outcome == "secret":
        assert first_row[0] == "deleted"
        assert first_row[1] is None
        assert first_row[2] is not None
    elif outcome == "refused":
        assert first_row[2] == "refused"
    else:
        assert first_row[2] is None


def test_confirming_an_edit_leaves_an_equal_pending_edit_on_another_channel(
    journal: Database,
) -> None:
    store = MessageStore(journal, _Reporter())
    first = _sent(store, "first", REF)
    other = _sent(store, "second", REF)
    journal.connection.execute(
        "UPDATE messages SET final_body = ? WHERE id IN (?, ?)",
        (_BODY.dump_json(Message("final")).decode(), first, other),
    )
    replies = JournalReplies(journal, SignalStore(journal), OpStore(journal), channel_name="first")
    final = JournalFinalEdits(journal, replies, max_text=1000)
    edit, _ = final.to_edit()
    final.confirm_edit(edit.message_id, edit.message)
    assert _row(journal, first)[3] == 1
    assert _row(journal, other)[3] == 0


def test_confirming_a_deletion_leaves_another_channels_attempt_unconfirmed(
    journal: Database,
) -> None:
    store = MessageStore(journal, _Reporter())
    first = _sent(store, "first", REF)
    other = _sent(store, "second", REF)
    assert store.record_deletion(first, "secret", now=NOW)
    assert store.record_deletion(other, "secret", now=NOW)

    store.confirm_deletion(first)

    assert _row(journal, first)[0] == "deleted"
    assert _row(journal, other)[0] == "delete_attempted"


@pytest.mark.parametrize("outcome", ["success", "refused", "transient"])
def test_replacement_changes_only_the_selected_channel_copy(
    journal: Database, outcome: str
) -> None:
    store = MessageStore(journal, _Reporter())
    first = _sent(store, "first", REF, slot="question")
    other = _sent(store, "second", REF, slot="question")
    _sent(store, "first", "newer", slot="question")
    untouched = _row(journal, other)
    error = {
        "success": None,
        "refused": ChannelRefusedError("refused"),
        "transient": ChannelError("offline"),
    }[outcome]
    channel = _Channel(error)
    replacements = JournalReplacements(journal, store, max_text=1000)
    asyncio.run(CopyReplacer(replacements, channel, _Clock(), interval_s=1).tick())

    assert channel.calls == [REF]
    assert _row(journal, other) == untouched
    first_row = _row(journal, first)
    assert first_row[0] == ("deleted" if outcome == "success" else "sent")
    assert (first_row[4] is not None) == (outcome == "refused")
