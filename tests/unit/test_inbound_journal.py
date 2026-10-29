"""What the PO sent, kept in the journal: dedupe, owners, secrets, offset and restart."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final

import pytest

from shikiri_yaku.app.inbound_poller import InboundPoller
from shikiri_yaku.domain.inbound import Batch, Inbound, InboundKind
from shikiri_yaku.journal import Database, JournalInbound
from tests.support.secrets import SECRETS

if TYPE_CHECKING:
    from collections.abc import Generator
    from pathlib import Path

NOW: Final = datetime(2026, 10, 28, 9, 0, tzinfo=UTC)
CHANNEL: Final = "telegram"
OWNER: Final = 1001
STRANGER: Final = 2002
WAIT_S: Final = 20
TEXT, TAP, OTHER = 700, 701, 702
ITEMS: Final = (
    Inbound(TEXT, InboundKind.REPLY, OWNER, OWNER, "1001/90", "1001/42", text="Use the second way"),
    Inbound(TAP, InboundKind.TAP, OWNER, OWNER, "1001/42", data="a:1:1"),
    Inbound(OTHER, InboundKind.TEXT, STRANGER, STRANGER, "2002/92", text="hello"),
)


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

    async def fetch(self, offset: int, wait_s: float) -> Batch:  # noqa: ARG002 -- the port's shape
        self.offsets.append(offset)
        return self.batch


@pytest.fixture
def database() -> Generator[Database]:
    database = Database.open(":memory:")
    yield database
    database.close()


@pytest.fixture
def journal(database: Database) -> JournalInbound:
    return JournalInbound(database)


def _poll(updates: _Updates, journal: JournalInbound) -> None:
    poller = InboundPoller(
        updates, journal, _Clock(), channel=CHANNEL, owners=[OWNER], wait_s=WAIT_S
    )
    asyncio.run(poller.poll_once())


def test_the_same_update_delivered_twice_is_stored_once(journal: JournalInbound) -> None:
    updates = _Updates(Batch(OTHER, ITEMS))

    _poll(updates, journal)
    first = journal.pending()
    _poll(updates, journal)

    assert journal.pending() == first
    assert first == list(ITEMS[:2])
    assert updates.offsets == [0, OTHER + 1]


def test_an_update_of_someone_but_an_owner_is_not_stored_and_the_offset_moves_on(
    database: Database, journal: JournalInbound
) -> None:
    _poll(_Updates(Batch(OTHER, ITEMS[2:])), journal)

    assert journal.pending() == []
    assert journal.next_offset(CHANNEL) == OTHER + 1
    count = database.connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    assert count == 0


@pytest.mark.parametrize(("sender", "served"), [(OWNER, True), (STRANGER, False)])
def test_only_an_owner_item_is_kept(
    database: Database,
    journal: JournalInbound,
    *,
    sender: int,
    served: bool,
) -> None:
    item = Inbound(TEXT, InboundKind.TEXT, sender, sender, f"{sender}/90", text="hello")

    _poll(_Updates(Batch(TEXT, (item,))), journal)

    assert journal.pending() == ([item] if served else [])
    assert journal.next_offset(CHANNEL) == TEXT + 1
    count = database.connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    assert count == (1 if served else 0)


def test_a_rollback_preserves_both_items_and_offset(
    database: Database, journal: JournalInbound
) -> None:
    journal.record_batch(CHANNEL, ITEMS[:1], TEXT + 1, NOW)
    with pytest.raises(RuntimeError, match="crash after recording"), database.transaction():  # noqa: PT012 -- simulate interruption before commit
        journal.record_batch(CHANNEL, ITEMS[1:2], TAP + 1, NOW)
        message = "crash after recording"
        raise RuntimeError(message)

    assert journal.pending() == list(ITEMS[:1])
    assert journal.next_offset(CHANNEL) == TEXT + 1


def test_an_item_stays_pending_until_handled_also_after_a_restart(tmp_path: Path) -> None:
    path = tmp_path / "journal.db"
    first = Database.open(path)
    JournalInbound(first).record_batch(CHANNEL, ITEMS[:2], TAP + 1, NOW)
    first.close()

    restarted = Database.open(path)
    journal = JournalInbound(restarted)
    assert [item.update_id for item in journal.pending()] == [TEXT, TAP]
    assert journal.next_offset(CHANNEL) == TAP + 1

    journal.mark_handled(TEXT)
    restarted.close()
    reopened = Database.open(path)

    assert [item.update_id for item in JournalInbound(reopened).pending()] == [TAP]
    reopened.close()


@pytest.mark.parametrize(
    ("form", "secret"),
    SECRETS.items(),
)
@pytest.mark.parametrize("kind", [InboundKind.TEXT, InboundKind.TAP])
def test_text_with_a_secret_is_kept_without_its_text_and_is_not_pending(
    database: Database, journal: JournalInbound, form: str, secret: str, kind: InboundKind
) -> None:
    item = Inbound(
        TEXT,
        kind,
        OWNER,
        OWNER,
        "1001/90",
        text=secret if kind == InboundKind.TEXT else None,
        data=secret if kind == InboundKind.TAP else None,
    )
    journal.record_batch(CHANNEL, [item], TEXT + 1, NOW)

    assert journal.pending() == []
    row = database.connection.execute("SELECT * FROM messages WHERE direction = 'in'").fetchone()
    assert (row["body"], row["data"], row["refusal"]) == (None, None, form)
