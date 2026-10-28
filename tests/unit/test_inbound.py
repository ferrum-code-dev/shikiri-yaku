"""What the PO sends: getUpdates read as the Bot API documents it, and the journal behind it.

No socket is opened and no token is read; the Bot API is a fake that returns a fixture.
"""

from __future__ import annotations

import asyncio
import copy
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final

import pytest

from shikiri_yaku.adapters.telegram_updates import TelegramUpdates
from shikiri_yaku.app.inbound_poller import InboundPoller
from shikiri_yaku.domain.inbound import Batch, Inbound, InboundKind
from shikiri_yaku.journal import Database, JournalInbound
from shikiri_yaku.ports import ChannelError
from tests.support.secrets import SECRETS
from tests.support.telegram_bot import FIXTURES

if TYPE_CHECKING:
    from collections.abc import Generator, Mapping

NOW: Final = datetime(2026, 10, 28, 9, 0, tzinfo=UTC)
CHANNEL: Final = "telegram"
OWNER: Final = 1001
WAIT_S: Final = 20
UPDATES: Final = json.loads((FIXTURES / "get_updates_ok.json").read_text(encoding="utf-8"))
TEXT, REPLY, TAP, STRANGER, EDITED = 700, 701, 702, 703, 704


class _Api:
    def __init__(self, body: object) -> None:
        self.body = body
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call(self, method: str, payload: Mapping[str, object]) -> object:
        self.calls.append((method, dict(payload)))
        return self.body


class _Clock:
    def now(self) -> datetime:
        return NOW

    async def sleep(self, seconds: float) -> None:
        raise NotImplementedError


class _Updates:
    """Gives the same batch every time, as Telegram does until the offset moves past it."""

    def __init__(self, batch: Batch) -> None:
        self.batch = batch
        self.offsets: list[int] = []
        self.failing = False

    async def fetch(self, offset: int, wait_s: float) -> Batch:  # noqa: ARG002 -- the port's shape
        self.offsets.append(offset)
        if self.failing:
            raise ChannelError
        return self.batch


@pytest.fixture
def database() -> Generator[Database]:
    database = Database.open(":memory:")
    yield database
    database.close()


@pytest.fixture
def journal(database: Database) -> JournalInbound:
    return JournalInbound(database)


def _read(body: object) -> Batch:
    return asyncio.run(TelegramUpdates(_Api(body)).fetch(0, WAIT_S))


def _poller(updates: _Updates, journal: JournalInbound) -> InboundPoller:
    return InboundPoller(updates, journal, _Clock(), channel=CHANNEL, owners=[OWNER], wait_s=WAIT_S)


def test_each_documented_update_becomes_the_item_it_stands_for() -> None:
    batch = _read(UPDATES)

    by_id = {item.update_id: item for item in batch.items}
    assert batch.last_update_id == EDITED
    assert set(by_id) == {TEXT, REPLY, TAP, STRANGER}
    assert by_id[TEXT] == Inbound(
        TEXT, InboundKind.TEXT, OWNER, "1001/90", text="Use the second way"
    )
    assert by_id[REPLY].kind == InboundKind.REPLY
    assert (by_id[REPLY].message_ref, by_id[REPLY].reply_to_ref) == ("1001/91", "1001/42")
    assert by_id[TAP] == Inbound(TAP, InboundKind.TAP, OWNER, "1001/42", data="a:1:1")
    assert by_id[STRANGER].sender == 2002


def test_the_poll_asks_from_the_offset_and_for_the_kinds_it_reads() -> None:
    api = _Api(UPDATES)

    asyncio.run(TelegramUpdates(api).fetch(705, WAIT_S))

    assert api.calls == [
        (
            "getUpdates",
            {"offset": 705, "timeout": WAIT_S, "allowed_updates": ["message", "callback_query"]},
        )
    ]


def test_an_update_of_an_odd_shape_is_skipped_and_the_ones_after_it_are_read() -> None:
    body = copy.deepcopy(UPDATES)
    del body["result"][0]["message"]["from"]

    batch = _read(body)

    assert TEXT not in {item.update_id for item in batch.items}
    assert {item.update_id for item in batch.items} >= {REPLY, TAP}
    assert batch.last_update_id == EDITED


_UNREADABLE: Final[list[object]] = [
    {"ok": False, "error_code": 409, "description": "Conflict"},
    {"ok": True, "result": [{"message": {}}]},
    {"ok": True},
    [],
]


@pytest.mark.parametrize("body", _UNREADABLE)
def test_a_reply_that_is_not_a_list_of_updates_fails_the_poll(body: object) -> None:
    with pytest.raises(ChannelError):
        _read(body)


def test_the_same_update_delivered_twice_is_stored_once(journal: JournalInbound) -> None:
    updates = _Updates(_read(UPDATES))
    poller = _poller(updates, journal)

    asyncio.run(poller.poll_once())
    first = journal.pending()
    asyncio.run(poller.poll_once())

    assert journal.pending() == first
    assert [item.update_id for item in first] == [TEXT, REPLY, TAP]


def test_an_update_of_someone_but_an_owner_is_not_stored_and_the_offset_moves_on(
    journal: JournalInbound,
) -> None:
    only_stranger = Batch(
        STRANGER, tuple(item for item in _read(UPDATES).items if item.update_id == STRANGER)
    )
    poller = _poller(_Updates(only_stranger), journal)

    asyncio.run(poller.poll_once())

    assert journal.pending() == []
    assert journal.next_offset(CHANNEL) == STRANGER + 1


def test_a_failed_poll_leaves_the_offset_where_it_was(journal: JournalInbound) -> None:
    updates = _Updates(_read(UPDATES))
    poller = _poller(updates, journal)
    asyncio.run(poller.poll_once())
    updates.failing = True

    with pytest.raises(ChannelError):
        asyncio.run(poller.poll_once())

    assert journal.next_offset(CHANNEL) == EDITED + 1
    assert updates.offsets == [0, EDITED + 1]


def test_a_batch_that_cannot_be_journaled_moves_neither_items_nor_offset(
    journal: JournalInbound,
) -> None:
    good = _read(UPDATES).items[0]
    broken = Inbound(TAP, InboundKind.TAP, None, "1001/42")  # type: ignore[arg-type]

    with pytest.raises(Exception):  # noqa: B017, PT011 -- the journal's own storage error
        journal.record_batch(CHANNEL, [good, broken], EDITED + 1, NOW)

    assert journal.pending() == []
    assert journal.next_offset(CHANNEL) == 0


def test_an_item_stays_pending_until_it_is_marked_handled(journal: JournalInbound) -> None:
    asyncio.run(_poller(_Updates(_read(UPDATES)), journal).poll_once())

    journal.mark_handled(TEXT)

    assert [item.update_id for item in journal.pending()] == [REPLY, TAP]


def test_a_reply_with_a_secret_is_kept_without_its_text_and_is_not_pending(
    database: Database, journal: JournalInbound
) -> None:
    secret = Inbound(TEXT, InboundKind.TEXT, OWNER, "1001/90", text=f"key {SECRETS['sk_key']}")

    journal.record_batch(CHANNEL, [secret], TEXT + 1, NOW)

    assert journal.pending() == []
    row = database.connection.execute("SELECT * FROM inbound").fetchone()
    assert (row["text"], row["refusal"]) == (None, "sk_key")
