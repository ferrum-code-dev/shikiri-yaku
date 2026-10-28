"""Messages that carried a secret are deleted from the PO's chat, attempt journaled first."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final

import pytest

from shikiri_yaku.app.secret_cleanup import SecretCleanup
from shikiri_yaku.domain.inbound import Inbound, InboundKind
from shikiri_yaku.journal import Database, Delivery, JournalInbound
from shikiri_yaku.ports import ChannelError, ChannelRefusedError, Message
from tests.support.secrets import SECRETS

if TYPE_CHECKING:
    from collections.abc import Generator
    from pathlib import Path

NOW: Final = datetime(2026, 10, 28, 9, 0, tzinfo=UTC)
CHANNEL: Final = "telegram"
OWNER: Final = 1001
TEXT: Final = 800
TAP: Final = 802
TEXT_REF: Final = "1001/90"
QUESTION_REF: Final = "1001/42"


class _Clock:
    def now(self) -> datetime:
        return NOW

    async def sleep(self, seconds: float) -> None:
        raise NotImplementedError


class _Channel:
    """Records each deletion with the journal's state of that message when the platform is asked.

    Each failure is raised by one call, in order; the calls after them succeed.
    """

    def __init__(self, database: Database, *, failures: tuple[Exception, ...] = ()) -> None:
        self._database = database
        self._failures = list(failures)
        self.deleted: list[str] = []
        self.states: list[str] = []

    async def send(self, destination: str, message: Message) -> str:
        raise NotImplementedError

    async def edit(self, message_ref: str, message: Message) -> None:
        raise NotImplementedError

    async def delete(self, message_ref: str) -> None:
        self.deleted.append(message_ref)
        row = self._database.connection.execute(
            "SELECT delivery FROM messages WHERE message_ref = ?", (message_ref,)
        ).fetchone()
        self.states.append(row["delivery"])
        if self._failures:
            raise self._failures.pop(0)


@pytest.fixture
def database() -> Generator[Database]:
    database = Database.open(":memory:")
    yield database
    database.close()


def _message(kind: InboundKind, text: str) -> Inbound:
    origin = QUESTION_REF if kind == InboundKind.REPLY else None
    return Inbound(TEXT, kind, OWNER, TEXT_REF, reply_to_ref=origin, text=text)


def _store(database: Database, item: Inbound) -> None:
    JournalInbound(database).record_batch(CHANNEL, [item], item.update_id + 1, NOW)


def _cleanup(database: Database, channel: _Channel) -> SecretCleanup:
    return SecretCleanup(JournalInbound(database), channel, _Clock(), interval_s=1)


def _tick(cleanup: SecretCleanup) -> None:
    asyncio.run(cleanup.tick())


def _delivery(database: Database, update_id: int) -> str:
    row = database.connection.execute(
        "SELECT delivery FROM messages WHERE update_id = ?", (update_id,)
    ).fetchone()
    return str(row["delivery"])


@pytest.mark.parametrize(("form", "secret"), SECRETS.items())
@pytest.mark.parametrize("kind", [InboundKind.TEXT, InboundKind.REPLY])
def test_a_secret_message_is_deleted_once_its_attempt_is_journaled(
    database: Database, form: str, secret: str, kind: InboundKind
) -> None:
    _store(database, _message(kind, secret))
    channel = _Channel(database)

    _tick(_cleanup(database, channel))

    assert channel.deleted == [TEXT_REF]
    assert channel.states == [Delivery.DELETE_ATTEMPTED]
    row = database.connection.execute(
        "SELECT * FROM messages WHERE update_id = ?", (TEXT,)
    ).fetchone()
    assert (row["delivery"], row["refusal"], row["body"]) == (Delivery.DELETED, form, None)


def test_a_message_without_a_secret_is_left_in_the_chat(database: Database) -> None:
    _store(database, _message(InboundKind.TEXT, "Use the second way"))
    channel = _Channel(database)

    _tick(_cleanup(database, channel))

    assert channel.deleted == []


def test_a_tap_with_a_secret_in_its_data_is_refused_but_the_bots_message_is_kept(
    database: Database,
) -> None:
    _store(database, Inbound(TAP, InboundKind.TAP, OWNER, QUESTION_REF, data=SECRETS["sk_key"]))
    channel = _Channel(database)

    _tick(_cleanup(database, channel))

    assert channel.deleted == []
    assert _delivery(database, TAP) == Delivery.REFUSED


def test_a_transient_failure_is_attempted_on_each_later_tick_until_the_platform_answers(
    database: Database,
) -> None:
    _store(database, _message(InboundKind.TEXT, SECRETS["sk_key"]))
    channel = _Channel(database, failures=(ChannelError("offline"), ChannelError("offline")))
    cleanup = _cleanup(database, channel)

    for _ in range(3):
        _tick(cleanup)

    assert channel.deleted == [TEXT_REF] * 3
    assert channel.states == [Delivery.DELETE_ATTEMPTED] * 3
    assert _delivery(database, TEXT) == Delivery.DELETED


def test_a_refusal_by_the_platform_is_not_repeated(database: Database) -> None:
    _store(database, _message(InboundKind.TEXT, SECRETS["sk_key"]))
    channel = _Channel(database, failures=(ChannelRefusedError("platform_refused"),))
    cleanup = _cleanup(database, channel)

    _tick(cleanup)
    _tick(cleanup)

    assert channel.deleted == [TEXT_REF]
    assert _delivery(database, TEXT) == Delivery.DELETE_ATTEMPTED


def test_a_transient_failure_is_tried_again_after_a_restart(tmp_path: Path) -> None:
    path = tmp_path / "journal.db"
    first = Database.open(path)
    _store(first, _message(InboundKind.TEXT, SECRETS["sk_key"]))
    _tick(_cleanup(first, _Channel(first, failures=(ChannelError("offline"),))))
    state = _delivery(first, TEXT)
    first.close()

    restarted = Database.open(path)
    channel = _Channel(restarted)
    _tick(_cleanup(restarted, channel))
    final = _delivery(restarted, TEXT)
    restarted.close()

    assert state == Delivery.REFUSED
    assert channel.deleted == [TEXT_REF]
    assert final == Delivery.DELETED


def test_a_refused_message_not_yet_deleted_is_deleted_after_a_restart(tmp_path: Path) -> None:
    path = tmp_path / "journal.db"
    first = Database.open(path)
    _store(first, _message(InboundKind.TEXT, SECRETS["sk_key"]))
    first.close()

    restarted = Database.open(path)
    channel = _Channel(restarted)
    _tick(_cleanup(restarted, channel))
    restarted.close()

    assert channel.deleted == [TEXT_REF]


def test_an_attempt_cut_short_by_a_crash_is_not_repeated_after_a_restart(tmp_path: Path) -> None:
    path = tmp_path / "journal.db"
    first = Database.open(path)
    _store(first, _message(InboundKind.TEXT, SECRETS["sk_key"]))
    crashing = _Channel(first, failures=(RuntimeError("crash before the platform answered"),))
    with pytest.raises(RuntimeError, match="crash before"):
        _tick(_cleanup(first, crashing))
    first.close()

    restarted = Database.open(path)
    channel = _Channel(restarted)
    _tick(_cleanup(restarted, channel))
    state = _delivery(restarted, TEXT)
    restarted.close()

    assert channel.deleted == []
    assert state == Delivery.DELETE_ATTEMPTED
