"""The inbound job: unreadable batches are visible once, transport failures are not (design §7)."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final

import pytest

from shikiri_yaku.adapters.telegram_updates import TelegramUpdates
from shikiri_yaku.app.health_source import HealthSource
from shikiri_yaku.app.inbound_poller import InboundPoller
from shikiri_yaku.app.inbound_service import InboundService
from shikiri_yaku.domain.inbound import Batch, Inbound, InboundKind
from shikiri_yaku.domain.signal import OPERATOR_SCOPE
from shikiri_yaku.journal import CursorStore, Database, JournalInbound, JournalSources, SignalStore
from shikiri_yaku.ports import ChannelError, ChannelProtocolError, ShikiruLinkDownError
from tests.support.telegram_bot import FIXTURES

if TYPE_CHECKING:
    from collections.abc import Generator, Mapping

    from shikiri_yaku.app.replies import Replies
    from shikiri_yaku.ports import Updates

NOW: Final = datetime(2026, 10, 28, 9, 0, tzinfo=UTC)
OWNER: Final = 1001
TAP: Final = Inbound(700, InboundKind.TAP, OWNER, OWNER, "1001/42", data="a:1:1")
CONFLICT: Final[dict[str, Any]] = json.loads(
    (FIXTURES / "get_updates_conflict.json").read_text(encoding="utf-8")
)


class _Clock:
    def now(self) -> datetime:
        return NOW

    async def sleep(self, seconds: float) -> None:
        raise NotImplementedError


class _Updates:
    """Gives each scripted outcome once: an exception is raised, a batch is returned."""

    def __init__(self, *script: Batch | Exception) -> None:
        self.script = list(script)

    async def fetch(self, offset: int, wait_s: float) -> Batch:  # noqa: ARG002 -- the port's shape
        outcome = self.script.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class _Replies:
    def __init__(self) -> None:
        self.down = False
        self.handled: list[int] = []

    async def handle(self, item: Inbound) -> None:
        if self.down:
            raise ShikiruLinkDownError
        self.handled.append(item.update_id)


class Harness:
    def __init__(self, database: Database, updates: Updates) -> None:
        self.database = database
        self.log = JournalInbound(database)
        self.replies = _Replies()
        signals = SignalStore(database)
        sources = JournalSources(CursorStore(database, signals), signals)
        health = HealthSource(OPERATOR_SCOPE, sources, _Clock(), 10)
        poller = InboundPoller(
            updates, self.log, _Clock(), channel="telegram", owners=[OWNER], wait_s=1
        )
        replies: Replies = self.replies  # pyright: ignore[reportAssignmentType]
        self.service = InboundService(
            poller,
            self.log,
            replies,
            health,
            _Clock(),
            interval_s=1,
            health_scope="telegram_inbound_batch",
        )

    def tick(self) -> None:
        asyncio.run(self.service.tick())

    def health_signals(self) -> list[tuple[str, str]]:
        rows = self.database.connection.execute(
            "SELECT state, reason FROM signals WHERE kind = 'health' AND project = ?",
            (OPERATOR_SCOPE,),
        )
        return [(row["state"], row["reason"]) for row in rows]

    def health_texts(self) -> list[str]:
        rows = self.database.connection.execute(
            "SELECT text FROM signals WHERE kind = 'health' AND project = ?", (OPERATOR_SCOPE,)
        )
        return [row["text"] for row in rows]


@pytest.fixture
def database() -> Generator[Database]:
    database = Database.open(":memory:")
    yield database
    database.close()


class _RefusingApi:
    """Answers every call with the Bot API's refusal of getUpdates while a webhook is set."""

    async def call(self, method: str, payload: Mapping[str, object]) -> object:  # noqa: ARG002
        return CONFLICT


def test_an_unreadable_batch_opens_one_signal_that_the_next_good_batch_closes(
    database: Database,
) -> None:
    harness = Harness(
        database,
        _Updates(
            ChannelProtocolError("unreadable"),
            ChannelProtocolError("unreadable"),
            Batch(None, ()),
        ),
    )
    harness.tick()
    harness.tick()
    [(state, reason)] = harness.health_signals()
    assert state != "closed"
    assert reason == "telegram_inbound_batch"

    harness.tick()
    assert [state for state, _ in harness.health_signals()] == ["closed"]


def test_a_refused_poll_puts_its_code_and_telegrams_description_in_the_health_message(
    database: Database,
) -> None:
    harness = Harness(database, TelegramUpdates(_RefusingApi(), group_id=None))
    harness.tick()

    [text] = harness.health_texts()
    assert "409" in text
    assert CONFLICT["description"] in text


def test_a_transport_failure_is_retried_without_a_signal(database: Database) -> None:
    harness = Harness(database, _Updates(ChannelError("offline"), Batch(701, (TAP,))))
    harness.tick()
    assert harness.health_signals() == []
    harness.tick()
    assert harness.replies.handled == [700]


def test_an_item_stays_pending_while_shikiru_is_down(database: Database) -> None:
    harness = Harness(database, _Updates(Batch(700, (TAP,)), Batch(None, ())))
    harness.replies.down = True
    harness.tick()
    assert [item.update_id for item in harness.log.pending()] == [700]

    harness.replies.down = False
    harness.tick()
    assert harness.replies.handled == [700]
    assert harness.log.pending() == []
