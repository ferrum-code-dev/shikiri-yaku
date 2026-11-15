"""Web grants are enforced before transport and survive conversation boundaries."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import httpx

from shikiri_yaku.adapters.expert_web import ExpertWeb
from shikiri_yaku.app.expert_admin import AdminAccess
from shikiri_yaku.app.expert_web import ExpertWebRequests
from shikiri_yaku.domain.inbound import Inbound, InboundKind
from shikiri_yaku.journal.conversations import Conversations
from shikiri_yaku.journal.database import Database, stamp
from shikiri_yaku.journal.web import JournalWeb

if TYPE_CHECKING:
    from typing import Any

NOW = datetime(2026, 1, 1, tzinfo=UTC)
EARLIER = datetime(2025, 12, 31, tzinfo=UTC)


class Clock:
    def now(self) -> datetime:
        return NOW

    async def sleep(self, seconds: float) -> None:
        raise NotImplementedError


class Outbox:
    def __init__(self) -> None:
        self.messages: dict[str, object] = {}

    def queue(self, *args: Any, key: str, **_kwargs: Any) -> None:
        self.messages.setdefault(key, args[3])


def _presenter(
    log: JournalWeb, outbox: Outbox, conversations: Conversations, admins: frozenset[int]
) -> ExpertWebRequests:
    return ExpertWebRequests(
        log,
        outbox,  # type: ignore[arg-type]
        Clock(),
        channel="telegram",
        group_id=1,
        access=AdminAccess(conversations, Clock(), admins=admins),
    )


def test_fetch_checks_every_host_and_scheme() -> None:
    database = Database.open(":memory:")
    conversations = Conversations(database)
    conversations.create(1, chat=1, thread=None, text="question", scope=[], now=NOW)
    log = JournalWeb(database)
    contacted: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        contacted.append(request.url.host)
        return httpx.Response(302, headers={"location": "https://other.example/page"})

    web = ExpertWeb(
        log, 1, timeout_s=2, max_bytes=100, max_redirects=3, transport=httpx.MockTransport(handle)
    )
    asyncio.run(web.fetch("http://bad.example"))
    asyncio.run(web.fetch("https://example.com:bad"))
    asyncio.run(web.fetch("https://first.example"))
    assert contacted == []
    database.connection.execute(
        "INSERT INTO web_grants VALUES ('first.example', ?)", (NOW.isoformat(),)
    )
    asyncio.run(web.fetch("https://first.example"))
    assert contacted == ["first.example"]
    assert {
        row["host"] for row in database.connection.execute("SELECT host FROM web_requests")
    } == {"first.example", "other.example"}
    database.close()


def test_request_is_unique_and_taps_grant_once_for_later_conversations() -> None:
    database = Database.open(":memory:")
    conversations = Conversations(database)
    conversations.create(1, chat=1, thread=None, text="question", scope=[], now=NOW)
    row = conversations.get(1)
    assert row is not None
    conversations.opened(1, 7)
    log = JournalWeb(database)
    log.request(1, "docs.example")
    log.request(1, "docs.example")
    conversations.start(1)
    conversations.finish(1, "{}", None, None, NOW)
    outbox = Outbox()
    presenter = _presenter(log, outbox, conversations, frozenset())
    presenter.queue(1)
    presenter.queue(1)
    assert list(outbox.messages) == ["expert:web:1"]
    database.connection.execute(
        "INSERT INTO messages (direction, channel, chat, platform_message_id, "
        "notice_key, delivery, created_at) "
        "VALUES ('out', 'telegram', '1', '9', 'expert:web:1', 'sent', ?)",
        (NOW.isoformat(),),
    )
    runs: list[tuple[int, int]] = []

    async def run(conversation_id: int, update_id: int) -> None:
        runs.append((conversation_id, update_id))

    for update in (2, 3):
        item = Inbound(update, InboundKind.TAP, 1, 1, "9", data="w:1", thread=7)
        asyncio.run(presenter.tap(item, run))
    assert runs == [(row.id, 2)]
    assert database.connection.execute("SELECT count(*) FROM web_grants").fetchone()[0] == 1
    conversations.create(4, chat=1, thread=None, text="next", scope=[], now=NOW)
    contacted: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        contacted.append(request.url.host)
        return httpx.Response(200, text="ok")

    web = ExpertWeb(
        log, 4, timeout_s=2, max_bytes=100, max_redirects=3, transport=httpx.MockTransport(handle)
    )
    assert asyncio.run(web.fetch("https://docs.example/")) == "ok"
    assert contacted == ["docs.example"]
    assert database.connection.execute("SELECT count(*) FROM web_requests").fetchone()[0] == 1
    database.close()


def _activity(database: Database) -> str:
    row = database.connection.execute("SELECT last_activity FROM conversations").fetchone()
    return row["last_activity"]


def _admin_web_request(
    admins: frozenset[int], clock: Clock
) -> tuple[Database, ExpertWebRequests, list[int]]:
    database = Database.open(":memory:")
    conversations = Conversations(database)
    conversations.create(1, chat=1, thread=None, text="q", scope=[], now=EARLIER, mode="admin")
    conversations.opened(1, 7)
    conversations.start(1)
    conversations.finish(1, "{}", None, None, EARLIER)
    log = JournalWeb(database)
    log.request(1, "docs.example")
    database.connection.execute(
        "INSERT INTO messages (direction, channel, chat, platform_message_id, "
        "notice_key, delivery, created_at) "
        "VALUES ('out', 'telegram', '1', '9', 'expert:web:1', 'sent', ?)",
        (NOW.isoformat(),),
    )
    presenter = ExpertWebRequests(
        log,
        Outbox(),  # type: ignore[arg-type]
        clock,
        channel="telegram",
        group_id=1,
        access=AdminAccess(conversations, clock, admins=admins),
    )
    return database, presenter, []


def test_web_tap_in_admin_conversation_needs_a_listed_admin_and_counts_as_activity() -> None:
    clock = Clock()
    database, presenter, runs = _admin_web_request(frozenset({5}), clock)

    async def run(_conversation_id: int, update_id: int) -> None:
        runs.append(update_id)

    other = Inbound(2, InboundKind.TAP, 6, 1, "9", data="w:1", thread=7)
    assert asyncio.run(presenter.tap(other, run))
    assert runs == []
    assert database.connection.execute("SELECT count(*) FROM web_grants").fetchone()[0] == 0
    assert _activity(database) == stamp(EARLIER)
    assert (
        database.connection.execute("SELECT state FROM web_requests").fetchone()["state"]
        == "pending"
    )
    listed = Inbound(3, InboundKind.TAP, 5, 1, "9", data="w:1", thread=7)
    assert asyncio.run(presenter.tap(listed, run))
    assert runs == [3]
    assert _activity(database) == stamp(NOW)
    database.close()


def test_web_tap_after_the_admin_is_removed_from_the_list_is_ignored() -> None:
    clock = Clock()
    database, presenter, runs = _admin_web_request(frozenset(), clock)

    async def run(_conversation_id: int, update_id: int) -> None:
        runs.append(update_id)

    item = Inbound(2, InboundKind.TAP, 5, 1, "9", data="w:1", thread=7)
    assert asyncio.run(presenter.tap(item, run))
    assert runs == []
    database.close()
