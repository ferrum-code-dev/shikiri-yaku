"""Web grants are enforced before transport and survive conversation boundaries."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import httpx

from shikiri_yaku.adapters.expert_web import ExpertWeb
from shikiri_yaku.app.expert_web import ExpertWebRequests
from shikiri_yaku.domain.inbound import Inbound, InboundKind
from shikiri_yaku.journal.conversations import Conversations
from shikiri_yaku.journal.database import Database
from shikiri_yaku.journal.web import JournalWeb

if TYPE_CHECKING:
    from typing import Any

NOW = datetime(2026, 1, 1, tzinfo=UTC)


class Clock:
    def now(self) -> datetime:
        return NOW


class Outbox:
    def __init__(self) -> None:
        self.messages: dict[str, object] = {}

    def queue(self, *args: Any, key: str, **_kwargs: Any) -> None:
        self.messages.setdefault(key, args[3])


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
    presenter = ExpertWebRequests(log, outbox, Clock(), channel="telegram", group_id=1)  # type: ignore[arg-type]
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
