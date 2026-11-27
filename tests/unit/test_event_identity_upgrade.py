"""The journal numbers inbound events itself, and a journal written before that keeps working.

The old journal is made by the migrations it had then, and holds the rows the code of that time
wrote. The upgrade, the poll after it and the taps run through the real inbound path.
"""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Final, cast

import pytest

from shikiri_yaku.adapters.telegram import MESSAGE_TEXT_LIMIT
from shikiri_yaku.adapters.telegram_files import DOWNLOAD_LIMIT_BYTES
from shikiri_yaku.adapters.telegram_updates import TelegramUpdates
from shikiri_yaku.app.expert_admin import AdminAccess
from shikiri_yaku.app.expert_ending import ExpertEnding
from shikiri_yaku.app.expert_proposals import action_key
from shikiri_yaku.app.expert_runs import ConversationRuns
from shikiri_yaku.app.expert_sketches import ExpertSketches
from shikiri_yaku.app.expert_turns import ExpertTurns
from shikiri_yaku.app.inbound_poller import InboundPoller
from shikiri_yaku.app.inbound_service import InboundService
from shikiri_yaku.app.replies import Replies
from shikiri_yaku.domain.callback import submit_all_data
from shikiri_yaku.domain.expert import ExpertAnswer, ExpertTask
from shikiri_yaku.domain.outbound import Purpose
from shikiri_yaku.journal import (
    Database,
    JournalInbound,
    JournalOutbox,
    MessageStore,
    NewOp,
    NewSignal,
    OpStore,
    SignalStore,
)
from shikiri_yaku.journal import database as journal_database
from shikiri_yaku.journal.conversations import Conversations
from shikiri_yaku.journal.final_edits import JournalFinalEdits
from shikiri_yaku.journal.migrations import MIGRATIONS
from shikiri_yaku.journal.replies import JournalReplies
from shikiri_yaku.ports import Ack, AckStatus, Button, Message
from tests.support.attachments import FakeDownloads
from tests.support.destinations import FixedDestinations
from tests.support.expert_proposals import expert_proposals
from tests.support.rooms import GROUP, FakeRooms
from tests.unit import test_ask, test_replies

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from shikiri_yaku.app.commands import Commands
    from shikiri_yaku.app.goal import GoalCommand
    from shikiri_yaku.app.health_source import HealthSource
    from shikiri_yaku.ports import ShikiruClient

CHANNEL: Final = "telegram"
OWNER: Final = 5
NOW: Final = test_ask.NOW
THEN: Final = NOW.isoformat()
ROOM: Final = 22
CREATED, PROPOSED, UNANSWERED = 700, 701, 702
ADMIN_UPDATE: Final = 703
OFFSET: Final = 706
QUESTION_TAP, SUBMIT_TAP, RUN_TAP = 706, 707, 708
QUESTION_REF: Final = f"{OWNER}/42"
PROPOSAL_REF: Final = f"{GROUP}/10"
ADMIN_REF: Final = f"{GROUP}/11"


def _at_schema(monkeypatch: pytest.MonkeyPatch, path: Path, number: int) -> Database:
    monkeypatch.setattr(journal_database, "MIGRATIONS", MIGRATIONS[:number])
    monkeypatch.setattr(journal_database, "LATEST_VERSION", number)
    try:
        return Database.open(path)
    finally:
        monkeypatch.undo()


def _sent(messages: MessageStore, ref: str, chat: str, key: str, body: Message) -> None:
    row = messages.record_outbound(
        signal_number=None,
        channel=CHANNEL,
        chat=chat,
        purpose=Purpose.EXPERT,
        body=body,
        notice_key=key,
        now=NOW,
    )
    messages.confirm_delivery(row.row_id, ref)


def _old_records(journal: Database) -> int:
    """Write what an old journal holds with work under way; return the admin op's number."""
    connection = journal.connection
    messages = MessageStore(journal, test_ask.Reporter())
    number = (
        SignalStore(journal)
        .insert_if_absent(
            NewSignal(
                "api:escalation:E-1",
                "api",
                "escalation",
                "decision_required",
                "question",
                "T-1",
                "E-1",
            ),
            NOW,
        )
        .row_id
    )
    question = messages.record_outbound(
        signal_number=number,
        channel=CHANNEL,
        chat=str(OWNER),
        purpose=Purpose.QUESTION,
        body=Message("question", (Button("Choice", f"a:{number}:1"),)),
        notice_key=f"forward:{number}",
        now=NOW,
    )
    messages.confirm_delivery(question.row_id, QUESTION_REF)

    connection.execute(
        "INSERT INTO conversations (id, update_id, chat, scope, state, thread_id, last_activity)"
        " VALUES (1, ?, ?, '[\"api\"]', 'open', ?, ?)",
        (CREATED, GROUP, ROOM, THEN),
    )
    task = ExpertTask(component="api", title="Export", text="Add it", planning=False, files=[])
    proposed = ExpertAnswer(reply="Plan", title="Idea", tasks=[task], findings="")
    for update, state, answer in (
        (CREATED, "done", ExpertAnswer(reply="Hi", title="Idea", tasks=[], findings="")),
        (PROPOSED, "done", proposed),
        (UNANSWERED, "pending", None),
    ):
        connection.execute(
            "INSERT INTO turns (conversation_id, update_id, text, state, answer)"
            " VALUES (1, ?, 'ask', ?, ?)",
            (update, state, None if answer is None else answer.model_dump_json()),
        )
    connection.execute("INSERT INTO proposals VALUES (?, 1, 'offered')", (PROPOSED,))
    _sent(
        messages,
        PROPOSAL_REF,
        str(GROUP),
        action_key(PROPOSED),
        Message("Plan", (Button("Submit all", submit_all_data(PROPOSED)),)),
    )
    op = (
        OpStore(journal)
        .intend(
            NewOp(
                identity=f"admin:{ADMIN_UPDATE}",
                kind="admin",
                project="",
                reason="test",
                payload=json.dumps({"conversation_id": 1, "command": "echo safe"}),
            ),
            NOW,
        )
        .row_id
    )
    _sent(
        messages,
        ADMIN_REF,
        str(GROUP),
        f"admin:proposal:{op}",
        Message("echo safe", (Button("Run", f"u:{op}"),)),
    )

    connection.execute(
        "INSERT INTO messages (direction, channel, chat, update_id, kind, sender, message_ref,"
        " body, delivery, created_at, thread)"
        " VALUES ('in', ?, ?, ?, 'text', ?, ?, 'more', 'received', ?, ?)",
        (CHANNEL, str(GROUP), UNANSWERED, OWNER, f"{GROUP}/12", THEN, ROOM),
    )
    connection.execute("INSERT INTO polling VALUES (?, ?)", (CHANNEL, OFFSET))
    return op


def _tap(update: int, chat: int, ref: str, data: str) -> dict[str, Any]:
    message = {"message_id": int(ref.rpartition("/")[2]), "chat": {"id": chat}}
    return {
        "update_id": update,
        "callback_query": {"from": {"id": OWNER}, "message": message, "data": data},
    }


class _Api:
    """Gives the same updates on every poll, the one stored before the upgrade among them."""

    def __init__(self, updates: list[dict[str, Any]]) -> None:
        self._updates = updates
        self.offsets: list[object] = []

    async def call(self, method: str, payload: Mapping[str, object]) -> object:  # noqa: ARG002
        self.offsets.append(payload["offset"])
        return {"ok": True, "result": self._updates}


class _Client(test_replies.Client):
    def __init__(self) -> None:
        super().__init__()
        self.submitted: list[str] = []

    async def submit_task(self, request: str, mode: str | None = None) -> Ack:  # noqa: ARG002
        self.submitted.append(request)
        return Ack(id=13, status=AckStatus.DONE, result={"task_id": "T-2"}, message=None)

    async def list_tasks(self, statuses: object = None) -> list[object]:  # noqa: ARG002
        return []


class _Health:
    def source_recovered(self, scope: str) -> None:
        pass


def _service(
    journal: Database, directory: Path, api: _Api, expert: test_ask.Expert, client: _Client
) -> InboundService:
    clock = test_ask.Clock()
    conversations = Conversations(journal)
    outbox = JournalOutbox(journal, MessageStore(journal, test_ask.Reporter()))
    clients = {"api": cast("ShikiruClient", client)}
    rooms = FakeRooms()
    runs = ConversationRuns()
    replies_log = JournalReplies(
        journal, SignalStore(journal), OpStore(journal), channel_name=CHANNEL
    )
    final = JournalFinalEdits(journal, replies_log, max_text=MESSAGE_TEXT_LIMIT)
    admins = frozenset({str(OWNER)})
    proposals = expert_proposals(
        journal,
        conversations,
        outbox,
        clock,
        clients,
        channel=CHANNEL,
        group_id=GROUP,
        access=AdminAccess(conversations, clock, admins=admins),
    )
    turns = ExpertTurns(
        conversations,
        outbox,
        clock,
        expert=expert,
        rooms=rooms,
        clients=clients,
        scope={"api": directory / "api"},
        directory=directory,
        channel=CHANNEL,
        max_text=MESSAGE_TEXT_LIMIT,
        max_file_bytes=DOWNLOAD_LIMIT_BYTES,
        runs=runs,
        ending=ExpertEnding(
            conversations,
            runs,
            rooms,
            outbox,
            clock,
            places=FixedDestinations(),
            directory=directory,
            retention=timedelta(days=14),
            admin_idle=timedelta(minutes=15),
            final=final,
        ),
        proposals=proposals,
        downloads=FakeDownloads(),
        sketches=ExpertSketches(
            None, outbox, clock, directory=directory, rooms=rooms, outbox_interval_s=1
        ),
        admins=admins,
    )
    replies = Replies(
        replies_log,
        outbox,
        clock,
        clients,
        channel=CHANNEL,
        commands=cast("Commands", None),
        goal=cast("GoalCommand", None),
        proposals=proposals,
        expert_turns=turns,
        max_text=MESSAGE_TEXT_LIMIT,
    )
    log = JournalInbound(journal)
    poller = InboundPoller(
        TelegramUpdates(api, group_id=GROUP),
        log,
        clock,
        channel=CHANNEL,
        owners=admins,
        wait_s=1,
    )
    return InboundService(
        poller,
        log,
        replies,
        cast("HealthSource", _Health()),
        clock,
        interval_s=1,
        expert=turns,
    )


# keep: 28 is the schema the plan names, 30 the one the releases before this change wrote
@pytest.mark.parametrize("schema", [28, 30])
def test_an_upgraded_journal_loses_nothing_and_handles_nothing_twice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, schema: int
) -> None:
    path = tmp_path / "journal.db"
    old = _at_schema(monkeypatch, path, schema)
    op = _old_records(old)
    old.close()

    journal = Database.open(path)
    redelivered = {
        "update_id": UNANSWERED,
        "message": {
            "message_id": 12,
            "from": {"id": OWNER},
            "chat": {"id": GROUP},
            "message_thread_id": ROOM,
            "text": "more",
        },
    }
    api = _Api(
        [
            redelivered,
            _tap(QUESTION_TAP, OWNER, QUESTION_REF, "a:1:1"),
            _tap(SUBMIT_TAP, GROUP, PROPOSAL_REF, submit_all_data(PROPOSED)),
            _tap(RUN_TAP, GROUP, ADMIN_REF, f"u:{op}"),
        ]
    )
    expert, client = test_ask.Expert(), _Client()
    inbound = JournalInbound(journal)
    assert inbound.cursor(CHANNEL) == str(OFFSET)
    assert [(item.id, item.key, item.sender) for item in inbound.pending()] == [
        (UNANSWERED, str(UNANSWERED), str(OWNER))
    ]
    service = _service(journal, tmp_path, api, expert, client)

    async def poll_repeatedly() -> None:
        # keep: more polls than the work needs, so a redelivery that ran again would show
        for _ in range(3):
            await service.tick()
            await asyncio.gather(*(asyncio.all_tasks() - {asyncio.current_task()}))

    asyncio.run(poll_repeatedly())

    assert api.offsets[0] == OFFSET
    stored = journal.connection.execute(
        "SELECT event_key, update_id FROM messages WHERE direction = 'in' ORDER BY update_id"
    ).fetchall()
    keys = [row["event_key"] for row in stored]
    assert keys == [str(key) for key in (UNANSWERED, QUESTION_TAP, SUBMIT_TAP, RUN_TAP)]
    assert all(row["update_id"] >= OFFSET for row in stored[1:])
    assert inbound.pending() == []
    assert [request.update_id for request in expert.requests] == [UNANSWERED]
    assert len(client.calls) == 1
    assert len(client.submitted) == 1
    states = journal.connection.execute(
        "SELECT (SELECT state FROM ops WHERE id = ?), (SELECT state FROM proposals),"
        " (SELECT COUNT(*) FROM turns)",
        (op,),
    ).fetchone()
    assert tuple(states) == ("approved", "submitted", 3)
    assert journal.connection.execute("PRAGMA foreign_key_check").fetchall() == []

    # keep: the erasure takes the old event's row, and the poll after it delivers the event again
    Conversations(journal).erase(1, room_place=FakeRooms().place(str(ROOM)))
    asyncio.run(poll_repeatedly())

    assert inbound.pending() == []
    assert (
        journal.connection.execute(
            "SELECT COUNT(*) FROM messages WHERE event_key = ?", (str(UNANSWERED),)
        ).fetchone()[0]
        == 0
    )
    assert len(expert.requests) == 1
    journal.close()


_PROPOSAL: Final = "INSERT INTO proposals VALUES (40, 1, 'offered')"
_TURN: Final = "INSERT INTO turns (conversation_id, update_id, text, state) VALUES (1, 40, '', '')"
_OP: Final = (
    "INSERT INTO ops (identity, kind, project, state, created_at, updated_at)"
    " VALUES ('{identity}', '{kind}', '', 'failed', '', '')"
)


@pytest.mark.parametrize(
    ("offset", "held", "first"),
    [
        (706, [], 706),
        (10, [_PROPOSAL], 41),
        (10, [_TURN], 41),
        (10, [_OP.format(identity="admin:refused:40", kind="admin")], 41),
        (10, [_OP.format(identity="telegram:resolve:40", kind="resolve")], 41),
    ],
    ids=["offset", "proposal", "turn", "erased admin op", "resolve op"],
)
def test_the_first_new_id_is_past_the_stored_offset_and_every_id_held(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    offset: int,
    held: list[str],
    first: int,
) -> None:
    path = tmp_path / "journal.db"
    old = _at_schema(monkeypatch, path, 30)
    old.connection.execute(
        "INSERT INTO conversations (id, update_id, chat, scope, state, last_activity)"
        " VALUES (1, 1, 1, '[]', 'open', '')"
    )
    old.connection.execute("INSERT INTO polling VALUES (?, ?)", (CHANNEL, offset))
    for statement in held:
        old.connection.execute(statement)
    old.close()

    journal = Database.open(path)
    inbound = JournalInbound(journal)
    api = _Api([_tap(900, OWNER, QUESTION_REF, "a:1:1")])
    batch = asyncio.run(TelegramUpdates(api, group_id=None).fetch(inbound.cursor(CHANNEL), 1))
    assert batch.cursor is not None
    inbound.record_batch("later", batch.events, batch.cursor, NOW)
    inbound.record_batch(CHANNEL, batch.events, batch.cursor, NOW)

    assert [item.id for item in inbound.pending()] == [first, first + 1]
    journal.close()
