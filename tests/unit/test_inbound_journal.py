"""What the PO sent, kept in the journal: ids, dedupe, owners, secrets, cursor and restart."""

from __future__ import annotations

import asyncio
from dataclasses import fields
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final

import pytest

from shikiri_yaku.adapters.telegram_updates import TelegramUpdates
from shikiri_yaku.app.inbound_poller import InboundPoller
from shikiri_yaku.domain.inbound import Batch, FileRef, Inbound, InboundKind, Received
from shikiri_yaku.domain.schedule import Destination
from shikiri_yaku.journal import Database, JournalInbound
from shikiri_yaku.journal.conversations import Conversations
from tests.support.secrets import SECRETS
from tests.support.telegram_bot import FakeBotApi

if TYPE_CHECKING:
    from collections.abc import Generator
    from pathlib import Path

    from shikiri_yaku.ports import Inbox

NOW: Final = datetime(2026, 10, 28, 9, 0, tzinfo=UTC)
CHANNEL: Final = "telegram"
OWNER: Final = 1001
STRANGER: Final = 2002
GROUP: Final = -1005
OTHER_GROUP: Final = -1006
WAIT_S: Final = 20
TEXT, TAP, OTHER = 700, 701, 702
ITEMS: Final = (
    Received(
        key=str(TEXT),
        kind=InboundKind.REPLY,
        sender=str(OWNER),
        chat=OWNER,
        message_ref="1001/90",
        reply_to_ref="1001/42",
        text="Use the second way",
    ),
    Received(
        key=str(TAP),
        kind=InboundKind.TAP,
        sender=str(OWNER),
        chat=OWNER,
        message_ref="1001/42",
        data="a:1:1",
    ),
    Received(
        key=str(OTHER),
        kind=InboundKind.TEXT,
        sender=str(STRANGER),
        chat=STRANGER,
        message_ref="2002/92",
        text="hello",
    ),
)


class _Clock:
    def now(self) -> datetime:
        return NOW

    async def sleep(self, seconds: float) -> None:
        raise NotImplementedError


def _stored(number: int, event: Received) -> Inbound:
    values = {field.name: getattr(event, field.name) for field in fields(Received)}
    return Inbound(id=number, **values)


class _Updates:
    """Gives the same batch every time, as a channel may until its cursor is confirmed."""

    def __init__(self, batch: Batch) -> None:
        self.batch = batch
        self.cursors: list[str | None] = []

    async def fetch(self, cursor: str | None, wait_s: float) -> Batch:  # noqa: ARG002 -- the port's shape
        self.cursors.append(cursor)
        return self.batch


@pytest.fixture
def database() -> Generator[Database]:
    database = Database.open(":memory:")
    yield database
    database.close()


@pytest.fixture
def journal(database: Database) -> JournalInbound:
    return JournalInbound(database)


def _poll(updates: Inbox, journal: JournalInbound) -> None:
    poller = InboundPoller(
        updates, journal, _Clock(), channel=CHANNEL, owners=[str(OWNER)], wait_s=WAIT_S
    )
    asyncio.run(poller.poll_once())


def _telegram_update(sender: int, chat: int) -> dict[str, Any]:
    return {
        "update_id": TEXT,
        "message": {
            "message_id": 90,
            "from": {"id": sender},
            "chat": {"id": chat},
            "text": "hello",
        },
    }


def test_the_same_update_delivered_twice_is_stored_once(journal: JournalInbound) -> None:
    updates = _Updates(Batch("after", ITEMS))

    _poll(updates, journal)
    first = journal.pending()
    _poll(updates, journal)

    assert journal.pending() == first
    assert first == [_stored(1, ITEMS[0]), _stored(2, ITEMS[1])]
    assert updates.cursors == [None, "after"]


def test_an_erased_event_is_not_stored_again_and_its_id_is_never_used_again(
    database: Database, journal: JournalInbound
) -> None:
    journal.record_batch(CHANNEL, ITEMS[:1], "first", NOW)
    [asked] = journal.pending()
    conversations = Conversations(database)
    conversations.create(asked.id, chat=OWNER, thread=None, text="q", scope=[], now=NOW)
    conversation = conversations.get(asked.id)
    assert conversation is not None
    conversations.erase(conversation.id, room_place=Destination(CHANNEL, str(GROUP), 7))
    assert journal.pending() == []

    journal.record_batch(CHANNEL, ITEMS[:1], "again", NOW)
    assert journal.pending() == []
    journal.record_batch(CHANNEL, ITEMS[1:2], "second", NOW)

    [later] = journal.pending()
    assert later.id > asked.id


def test_channels_never_share_an_id_or_a_key(journal: JournalInbound) -> None:
    journal.record_batch(CHANNEL, ITEMS[:2], "a", NOW)
    journal.record_batch("later", ITEMS[:1], "b", NOW)
    journal.record_batch(CHANNEL, ITEMS[2:], "c", NOW)

    assert journal.pending() == [
        _stored(number, ITEMS[at]) for number, at in enumerate((0, 1, 0, 2), 1)
    ]


def test_an_update_of_someone_but_an_owner_is_not_stored_and_the_offset_moves_on(
    database: Database, journal: JournalInbound
) -> None:
    _poll(_Updates(Batch("after", ITEMS[2:])), journal)

    assert journal.pending() == []
    assert journal.cursor(CHANNEL) == "after"
    count = database.connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    assert count == 0


@pytest.mark.parametrize(
    ("sender", "chat", "served"),
    [
        (OWNER, OWNER, True),
        (OWNER, GROUP, True),
        (OWNER, OTHER_GROUP, False),
        (STRANGER, GROUP, False),
    ],
)
def test_an_owner_item_is_kept_from_the_private_chat_or_the_configured_group_only(
    database: Database,
    journal: JournalInbound,
    *,
    sender: int,
    chat: int,
    served: bool,
) -> None:
    api = FakeBotApi([_telegram_update(sender, chat)], after_send=False)
    item = Received(
        key=str(TEXT),
        kind=InboundKind.TEXT,
        sender=str(sender),
        chat=chat,
        message_ref=f"{chat}/90",
        text="hello",
    )

    _poll(TelegramUpdates(api, group_id=GROUP), journal)

    assert journal.pending() == ([_stored(1, item)] if served else [])
    assert journal.cursor(CHANNEL) == str(TEXT + 1)
    count = database.connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    assert count == (1 if served else 0)


def test_a_rollback_preserves_both_items_and_cursor(
    database: Database, journal: JournalInbound
) -> None:
    journal.record_batch(CHANNEL, ITEMS[:1], str(TEXT + 1), NOW)
    with pytest.raises(RuntimeError, match="crash after recording"), database.transaction():  # noqa: PT012 -- simulate interruption before commit
        journal.record_batch(CHANNEL, ITEMS[1:2], str(TAP + 1), NOW)
        message = "crash after recording"
        raise RuntimeError(message)

    assert journal.pending() == [_stored(1, ITEMS[0])]
    assert journal.cursor(CHANNEL) == str(TEXT + 1)


def test_an_item_stays_pending_until_handled_also_after_a_restart(tmp_path: Path) -> None:
    path = tmp_path / "journal.db"
    first = Database.open(path)
    JournalInbound(first).record_batch(CHANNEL, ITEMS[:2], str(TAP + 1), NOW)
    first.close()

    restarted = Database.open(path)
    journal = JournalInbound(restarted)
    assert [item.key for item in journal.pending()] == [str(TEXT), str(TAP)]
    assert journal.cursor(CHANNEL) == str(TAP + 1)

    journal.mark_handled(journal.pending()[0].id)
    restarted.close()
    reopened = Database.open(path)

    assert [item.key for item in JournalInbound(reopened).pending()] == [str(TAP)]
    reopened.close()


@pytest.mark.parametrize(
    ("form", "secret"),
    SECRETS.items(),
)
@pytest.mark.parametrize("kind", [InboundKind.TEXT, InboundKind.TAP])
def test_text_with_a_secret_is_kept_without_its_text_and_is_not_pending(
    database: Database, journal: JournalInbound, form: str, secret: str, kind: InboundKind
) -> None:
    item = Received(
        key=str(TEXT),
        kind=kind,
        sender=str(OWNER),
        chat=OWNER,
        message_ref="1001/90",
        text=secret if kind == InboundKind.TEXT else None,
        data=secret if kind == InboundKind.TAP else None,
    )
    journal.record_batch(CHANNEL, [item], str(TEXT + 1), NOW)

    assert journal.pending() == []
    row = database.connection.execute("SELECT * FROM messages WHERE direction = 'in'").fetchone()
    assert (row["body"], row["data"], row["refusal"]) == (None, None, form)


def test_a_file_item_is_kept_with_its_caption_and_its_file_reference(
    journal: JournalInbound,
) -> None:
    item = Received(
        key=str(740),
        kind=InboundKind.FILE,
        sender=str(OWNER),
        chat=GROUP,
        message_ref=f"{GROUP}/95",
        text="look",
        thread=None,
        file=FileRef(id="file-1", name="spec.pdf", size=3),
    )

    _poll(_Updates(Batch("741", (item,))), journal)

    assert journal.pending() == [_stored(1, item)]


def test_a_file_from_someone_but_an_owner_is_not_kept(journal: JournalInbound) -> None:
    stranger = Received(
        key=str(741),
        kind=InboundKind.FILE,
        sender=str(STRANGER),
        chat=GROUP,
        message_ref=f"{GROUP}/96",
        file=FileRef(id="file-2", name="x.bin"),
    )

    _poll(_Updates(Batch("742", (stranger,))), journal)

    assert journal.pending() == []


def test_a_file_whose_caption_carries_a_secret_is_refused_and_kept_only_to_delete(
    journal: JournalInbound,
) -> None:
    item = Received(
        key=str(742),
        kind=InboundKind.FILE,
        sender=str(OWNER),
        chat=GROUP,
        message_ref=f"{GROUP}/97",
        text=f"key {SECRETS['sk_key']}",
        file=FileRef(id="file-3", name="key.txt"),
    )

    _poll(_Updates(Batch("743", (item,))), journal)

    assert journal.pending() == []
    assert [stored.key for stored in journal.to_delete()] == ["742"]
