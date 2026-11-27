"""Web grants are enforced before transport and survive conversation boundaries."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any, cast

import httpx
import pytest

from shikiri_yaku.adapters.expert_web import ExpertWeb
from shikiri_yaku.adapters.telegram import MESSAGE_TEXT_LIMIT
from shikiri_yaku.app.expert_admin import AdminAccess
from shikiri_yaku.app.expert_web import ExpertWebRequests
from shikiri_yaku.domain.inbound import Inbound, InboundKind
from shikiri_yaku.journal.conversations import Conversations
from shikiri_yaku.journal.database import Database, stamp
from shikiri_yaku.journal.final_edits import JournalFinalEdits
from shikiri_yaku.journal.web import JournalWeb

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


def _web_request(host: str = "docs.example") -> tuple[Database, JournalWeb, ExpertWebRequests]:
    database = Database.open(":memory:")
    conversations = Conversations(database)
    conversations.create(1, chat=1, thread=None, text="q", scope=[], now=NOW)
    conversations.opened(1, 7)
    conversations.start(1)
    conversations.finish(1, "{}", None, None, NOW)
    log = JournalWeb(database)
    log.request(1, host)
    database.connection.execute(
        "INSERT INTO messages (direction, channel, chat, platform_message_id, "
        "notice_key, delivery, created_at, conversation_id) "
        "VALUES ('out', 'telegram', '1', '9', 'expert:web:1', 'sent', ?, 1)",
        (NOW.isoformat(),),
    )
    presenter = ExpertWebRequests(
        log,
        Outbox(),  # type: ignore[arg-type]
        Clock(),
        channel="telegram",
        group_id=1,
        access=AdminAccess(conversations, Clock(), admins=frozenset()),
    )
    return database, log, presenter


def _final(database: Database) -> dict[str, Any]:
    row = database.connection.execute(
        "SELECT final_body, final_edited FROM messages WHERE notice_key = 'expert:web:1'"
    ).fetchone()
    assert row is not None
    assert row["final_body"] is not None
    return json.loads(row["final_body"])


@pytest.mark.parametrize(
    ("data", "word", "state"),
    [("w:1", "allowed", "allowed"), ("n:1", "denied", "denied")],
)
def test_web_tap_settles_request_message_after_decision(data: str, word: str, state: str) -> None:
    database, log, presenter = _web_request()
    runs: list[tuple[int, int]] = []

    async def run(conversation_id: int, update_id: int) -> None:
        runs.append((conversation_id, update_id))

    item = Inbound(2, InboundKind.TAP, 1, 1, "9", data=data, thread=7)
    assert asyncio.run(presenter.tap(item, run))
    assert len(runs) == 1
    assert log.granted("docs.example") is (state == "allowed")
    final = _final(database)
    assert final["buttons"] == []
    assert "docs.example" in final["text"]
    assert word in final["text"]
    edits = JournalFinalEdits(database, cast("object", None), max_text=MESSAGE_TEXT_LIMIT).to_edit()  # type: ignore[arg-type]
    assert [edit.message_ref for edit in edits] == ["9"]
    assert edits[0].message.text == final["text"]
    assert edits[0].message.buttons == ()
    database.close()


def test_web_tap_settles_once_across_duplicate_taps() -> None:
    database, _log, presenter = _web_request()
    runs: list[int] = []

    async def run(_conversation_id: int, update_id: int) -> None:
        runs.append(update_id)

    for update in (2, 3):
        item = Inbound(update, InboundKind.TAP, 1, 1, "9", data="w:1", thread=7)
        assert asyncio.run(presenter.tap(item, run))
    assert runs == [2]
    assert database.connection.execute("SELECT count(*) FROM web_grants").fetchone()[0] == 1
    assert (
        database.connection.execute(
            "SELECT count(*) FROM turns WHERE conversation_id = 1 AND update_id IN (2, 3)"
        ).fetchone()[0]
        == 1
    )
    assert _final(database)["buttons"] == []
    database.close()


def test_web_tap_from_another_message_does_not_settle() -> None:
    database, _log, presenter = _web_request()

    async def run(_conversation_id: int, update_id: int) -> None:  # pragma: no cover
        raise AssertionError(update_id)

    item = Inbound(2, InboundKind.TAP, 1, 1, "other", data="w:1", thread=7)
    assert asyncio.run(presenter.tap(item, run))
    assert database.connection.execute("SELECT final_body FROM messages").fetchone()[0] is None
    assert (
        database.connection.execute("SELECT state FROM web_requests").fetchone()["state"]
        == "pending"
    )
    database.close()


def test_web_tap_in_unauthorized_conversation_does_not_settle() -> None:
    clock = Clock()
    database, presenter, runs = _admin_web_request(frozenset({5}), clock)

    async def run(_conversation_id: int, update_id: int) -> None:
        runs.append(update_id)

    other = Inbound(2, InboundKind.TAP, 6, 1, "9", data="w:1", thread=7)
    assert asyncio.run(presenter.tap(other, run))
    assert database.connection.execute("SELECT final_body FROM messages").fetchone()[0] is None
    database.close()


def test_web_decision_and_settlement_commit_together() -> None:
    database, log, _ = _web_request()
    database.connection.execute(
        "CREATE TRIGGER crash_edit BEFORE UPDATE OF final_body ON messages "
        "BEGIN SELECT RAISE(ABORT, 'interrupted'); END"
    )
    with pytest.raises(Exception, match="interrupted"):
        log.decide(1, 2, allow=True, now=NOW)
    assert (
        database.connection.execute("SELECT state FROM web_requests").fetchone()["state"]
        == "pending"
    )
    assert database.connection.execute("SELECT count(*) FROM web_grants").fetchone()[0] == 0
    assert (
        database.connection.execute("SELECT count(*) FROM turns WHERE update_id = 2").fetchone()[0]
        == 0
    )
    database.connection.execute("DROP TRIGGER crash_edit")
    assert log.decide(1, 2, allow=True, now=NOW) == 1
    assert log.granted("docs.example")
    assert _final(database)["buttons"] == []
    database.close()


def test_web_recovery_settles_a_decision_left_without_its_edit() -> None:
    database, _log, presenter = _web_request()
    database.connection.execute(
        "UPDATE web_requests SET state = 'denied', decision_update_id = 2 WHERE id = 1"
    )
    presenter.recover()
    final = _final(database)
    assert final["buttons"] == []
    assert "denied" in final["text"]
    assert "docs.example" in final["text"]
    final_edits = JournalFinalEdits(database, cast("object", None), max_text=MESSAGE_TEXT_LIMIT)  # type: ignore[arg-type]
    (edit,) = final_edits.to_edit()
    final_edits.confirm_edit(edit.message_ref, edit.message)
    # keep: settling the same text again does not reopen an edit already confirmed
    presenter.recover()
    assert (
        database.connection.execute("SELECT final_edited FROM messages").fetchone()["final_edited"]
        == 1
    )
    database.close()
