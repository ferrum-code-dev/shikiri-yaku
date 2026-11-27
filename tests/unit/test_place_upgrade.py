"""A journal that stored a chat and a thread keeps working once it stores places.

The old journal is made by the migrations it had then, and holds the rows the code of that time
wrote. Everything after the upgrade runs through the journal, the app and the Telegram adapter.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import sqlite3  # noqa: TID251 -- the journal's uniqueness is what the test asks about
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Final, cast

import pytest
from pydantic import TypeAdapter

from shikiri_yaku.adapters.telegram import (
    MESSAGE_TEXT_LIMIT,
    TelegramChannel,
    TelegramForumTopics,
    telegram_place,
)
from shikiri_yaku.adapters.telegram_files import DOWNLOAD_LIMIT_BYTES
from shikiri_yaku.adapters.telegram_topics import TOPIC_OPEN, TelegramPlaces
from shikiri_yaku.adapters.telegram_updates import TelegramUpdates
from shikiri_yaku.app.copy_editor import CopyEditor
from shikiri_yaku.app.copy_replacer import CopyReplacer
from shikiri_yaku.app.expert_admin import AdminAccess
from shikiri_yaku.app.expert_ending import ExpertEnding
from shikiri_yaku.app.expert_proposals import action_key
from shikiri_yaku.app.expert_runs import ConversationRuns
from shikiri_yaku.app.expert_sketches import ExpertSketches
from shikiri_yaku.app.expert_turns import ExpertTurns
from shikiri_yaku.app.goal import GoalCommand
from shikiri_yaku.app.outbox_sender import OutboxSender
from shikiri_yaku.app.replies import Replies
from shikiri_yaku.app.scheduler import Scheduler
from shikiri_yaku.app.submit import Submitter
from shikiri_yaku.domain.callback import submit_all_data, submit_data
from shikiri_yaku.domain.expert import ExpertAnswer, ExpertTask
from shikiri_yaku.domain.inbound import InboundKind
from shikiri_yaku.domain.outbound import Purpose
from shikiri_yaku.journal import (
    Database,
    JournalInbound,
    JournalOutbox,
    MessageStore,
    NewSignal,
    OpStore,
    SignalStore,
)
from shikiri_yaku.journal import database as journal_database
from shikiri_yaku.journal.conversations import Conversations
from shikiri_yaku.journal.final_edits import JournalFinalEdits
from shikiri_yaku.journal.migrations import MIGRATIONS
from shikiri_yaku.journal.replacements import JournalReplacements
from shikiri_yaku.journal.replies import JournalReplies
from shikiri_yaku.journal.schedule import JournalSchedule
from shikiri_yaku.journal.submits import JournalSubmits
from shikiri_yaku.journal.topics import TopicStore
from shikiri_yaku.ports import Ack, AckStatus, Button, Message
from tests.support.attachments import FakeDownloads
from tests.support.expert_proposals import expert_proposals
from tests.unit import test_ask, test_replies

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import datetime
    from pathlib import Path

    from shikiri_yaku.app.commands import Commands
    from shikiri_yaku.app.scheduler import WakeTarget
    from shikiri_yaku.ports import ShikiruClient

# keep: the schema the releases before places wrote
SCHEMA: Final = 31
CHANNEL: Final = "telegram"
OWNER: Final = 1001
GROUP: Final = -100123
TOPIC: Final = 7
ROOM: Final = 22
THEN: Final = test_ask.NOW.isoformat()
CREATED, PROPOSED, CLOSED = 700, 701, 702
OFFSET: Final = 706
# keep: the message id the fake gives its first sent message
FIRST_SENT: Final = 100
PRIVATE_REF: Final = f"{OWNER}/10"
REPLACED_REF: Final = f"{GROUP}/11"
EDITED_REF: Final = f"{GROUP}/12"
PROPOSAL_REF: Final = f"{GROUP}/13"
# keep: each place as sendMessage names it: the chat, and the thread inside a topic
PRIVATE: Final = (str(OWNER), None)
GENERAL: Final = (str(GROUP), None)
COMPONENT: Final = (str(GROUP), TOPIC)
IN_ROOM: Final = (str(GROUP), ROOM)
_BODY: Final = TypeAdapter(Message)


def _row(journal: Database, place: tuple[str, int | None], **columns: object) -> None:
    """Write a message row as the old code stored it: by chat and thread."""
    values = {
        "channel": CHANNEL,
        "chat": place[0],
        "thread": place[1],
        "created_at": THEN,
        **columns,
    }
    names = ", ".join(values)
    marks = ", ".join("?" * len(values))
    journal.connection.execute(
        f"INSERT INTO messages ({names}) VALUES ({marks})",  # noqa: S608
        tuple(values.values()),
    )


def _out(
    journal: Database,
    place: tuple[str, int | None],
    key: str,
    body: Message,
    *,
    ref: str | None = None,
    columns: Mapping[str, object] | None = None,
) -> None:
    _row(
        journal,
        place,
        direction="out",
        notice_key=key,
        body=_BODY.dump_json(body).decode(),
        delivery="pending" if ref is None else "sent",
        platform_message_id=ref,
        **{"purpose": Purpose.NOTICE, **(columns or {})},
    )


def _old_records(journal: Database) -> int:
    """Write what an old journal holds with work under way; return the submit op's number."""
    connection = journal.connection
    TopicStore(journal).save("api", TOPIC, TOPIC_OPEN)
    signals = SignalStore(journal)
    number = signals.insert_if_absent(
        NewSignal(
            "api:escalation:E-1",
            "api",
            "escalation",
            "decision_required",
            "question",
            "T-1",
            "E-1",
        ),
        test_ask.NOW,
    ).row_id
    connection.execute("UPDATE signals SET state = 'forwarded' WHERE number = ?", (number,))
    question = Message("question", (Button("Choice", f"a:{number}:1"),))
    asked = {"signal_number": number, "purpose": Purpose.QUESTION, "slot": "question"}
    _out(journal, PRIVATE, f"forward:{number}", question, ref=PRIVATE_REF, columns=asked)
    _out(journal, COMPONENT, f"forward:{number}", question, ref=REPLACED_REF, columns=asked)
    _out(journal, COMPONENT, f"reshow:{number}", question, columns=asked)
    for name, place in (("private", PRIVATE), ("general", GENERAL), ("component", COMPONENT)):
        _out(journal, place, f"waiting:{name}", Message(name))
    final = _BODY.dump_json(Message("final state")).decode()
    _out(
        journal, GENERAL, "edited", Message("before"), ref=EDITED_REF, columns={"final_body": final}
    )

    connection.execute(
        "INSERT INTO conversations (id, update_id, chat, origin_thread, scope, state, thread_id,"
        " last_activity) VALUES (1, ?, ?, ?, '[\"api\"]', 'open', ?, ?)",
        (CREATED, GROUP, TOPIC, ROOM, THEN),
    )
    task = ExpertTask(component="api", title="Export", text="Add it", planning=False, files=[])
    for update, tasks in ((CREATED, []), (PROPOSED, [task])):
        answer = ExpertAnswer(reply="Plan", title="Idea", tasks=tasks, findings="")
        connection.execute(
            "INSERT INTO turns (conversation_id, update_id, text, state, answer)"
            " VALUES (1, ?, 'ask', 'done', ?)",
            (update, answer.model_dump_json()),
        )
    connection.execute("INSERT INTO proposals VALUES (?, 1, 'offered')", (PROPOSED,))
    connection.execute("INSERT INTO command_replies VALUES (?)", (f"expert:open:{CREATED}",))
    for update, kind, place, delivery in (
        (CREATED, "text", COMPONENT, "handled"),
        (CLOSED, "topic_closed", IN_ROOM, "received"),
    ):
        _row(
            journal,
            place,
            direction="in",
            update_id=update,
            event_key=str(update),
            kind=kind,
            sender=str(OWNER),
            message_ref=f"{GROUP}/{update}",
            delivery=delivery,
        )
    connection.execute(
        "INSERT INTO polling (channel, cursor, last_id) VALUES (?, ?, ?)",
        (CHANNEL, str(OFFSET), OFFSET - 1),
    )
    _out(
        journal,
        COMPONENT,
        f"expert:open:{CREATED}:0",
        Message("Open", (Button("Open topic", url="https://t.me/c/123/22"),)),
        ref=f"{GROUP}/14",
        columns={"conversation_id": 1},
    )
    _out(
        journal,
        IN_ROOM,
        action_key(PROPOSED),
        Message("Plan", (Button("Submit all", submit_all_data(PROPOSED)),)),
        ref=PROPOSAL_REF,
        columns={"conversation_id": 1, "purpose": Purpose.EXPERT},
    )
    _out(journal, IN_ROOM, "room:waiting", Message("room"), columns={"conversation_id": 1})
    # keep: an untagged copy, which only its place makes the conversation's
    _out(journal, IN_ROOM, "room:untagged", Message("untagged"), ref=f"{GROUP}/15")

    destination = {"channel": CHANNEL, "chat": str(GROUP), "thread": TOPIC}
    payload = {"component": "api", "request": "Add the import", "destination": destination}
    inserted = connection.execute(
        "INSERT INTO ops (identity, kind, project, payload, state, created_at, updated_at)"
        " VALUES (?, 'submit', 'api', ?, 'issued', ?, ?) RETURNING id",
        (f"{CHANNEL}:submit:650", json.dumps(payload), THEN, THEN),
    ).fetchone()
    return inserted["id"]


class _Api:
    """Answers sends and the other calls with success, and records them."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.updates: list[dict[str, Any]] = []
        self._ids = itertools.count(FIRST_SENT)

    async def call(self, method: str, payload: Mapping[str, object]) -> object:
        if method == "getUpdates":
            return {"ok": True, "result": self.updates}
        self.calls.append((method, dict(payload)))
        if method != "sendMessage":
            return {"ok": True, "result": True}
        chat = {"id": int(str(payload["chat_id"]))}
        return {"ok": True, "result": {"message_id": next(self._ids), "chat": chat}}

    async def upload(
        self, method: str, payload: Mapping[str, object], field: str, name: str, content: bytes
    ) -> object:
        raise NotImplementedError

    async def download(self, file_path: str) -> bytes:
        raise NotImplementedError

    def take(self, method: str) -> list[dict[str, Any]]:
        """Return the payloads of the method's calls made since the last take."""
        taken = [payload for name, payload in self.calls if name == method]
        self.calls = [call for call in self.calls if call[0] != method]
        return taken


class _Client(test_replies.Client):
    def __init__(self) -> None:
        super().__init__()
        self.submitted: list[str] = []

    async def submit_task(self, request: str, mode: str | None = None) -> Ack:  # noqa: ARG002
        self.submitted.append(request)
        return Ack(id=13, status=AckStatus.DONE, result={"task_id": "T-2"}, message=None)

    async def list_tasks(self, statuses: object = None) -> list[object]:  # noqa: ARG002
        return []


class _Clock:
    def __init__(self) -> None:
        self.moment: datetime = test_ask.NOW

    def now(self) -> datetime:
        return self.moment

    async def sleep(self, seconds: float) -> None:
        raise NotImplementedError


def _sent_to(payloads: list[dict[str, Any]]) -> list[tuple[str, int | None]]:
    return [(item["chat_id"], item.get("message_thread_id")) for item in payloads]


def _tap(update: int, ref: str, data: str, thread: int | None) -> dict[str, Any]:
    chat, _, message_id = ref.partition("/")
    message: dict[str, Any] = {"message_id": int(message_id), "chat": {"id": int(chat)}}
    if thread is not None:
        message |= {"message_thread_id": thread, "is_topic_message": True}
    callback = {"from": {"id": OWNER}, "message": message, "data": data}
    return {"update_id": update, "callback_query": callback}


class _Operator:
    """The parts that act on an upgraded journal, wired as composition wires them."""

    def __init__(self, journal: Database, directory: Path) -> None:
        self.api, self.clock, self.client = _Api(), _Clock(), _Client()
        clients = {"api": cast("ShikiruClient", self.client)}
        messages = MessageStore(journal, test_ask.Reporter())
        signals, ops = SignalStore(journal), OpStore(journal)
        self.outbox = JournalOutbox(journal, messages)
        replies_log = JournalReplies(journal, signals, ops, channel_name=CHANNEL)
        final = JournalFinalEdits(journal, replies_log, max_text=MESSAGE_TEXT_LIMIT)
        channel = TelegramChannel(self.api, self.clock)
        rooms = TelegramForumTopics(self.api, GROUP)
        places = TelegramPlaces(
            TopicStore(journal), owners=(OWNER,), group_id=GROUP, reconciler=None
        )
        conversations = Conversations(journal)
        runs = ConversationRuns()
        self.ending = ExpertEnding(
            conversations,
            runs,
            rooms,
            self.outbox,
            self.clock,
            places=places,
            directory=directory,
            retention=timedelta(days=14),
            admin_idle=timedelta(minutes=15),
            final=final,
        )
        proposals = expert_proposals(
            journal,
            conversations,
            self.outbox,
            self.clock,
            clients,
            channel=CHANNEL,
            group_id=GROUP,
            access=AdminAccess(conversations, self.clock, admins=frozenset()),
        )
        self.turns = ExpertTurns(
            conversations,
            self.outbox,
            self.clock,
            expert=test_ask.Expert(),
            rooms=rooms,
            clients=clients,
            scope={"api": directory / "api"},
            directory=directory,
            max_text=MESSAGE_TEXT_LIMIT,
            max_file_bytes=DOWNLOAD_LIMIT_BYTES,
            runs=runs,
            ending=self.ending,
            proposals=proposals,
            downloads=FakeDownloads(),
            sketches=ExpertSketches(
                None, self.outbox, self.clock, directory=directory, rooms=rooms, outbox_interval_s=1
            ),
        )
        self.goal = GoalCommand(
            Submitter(JournalSubmits(journal, ops, channel_name=CHANNEL), clients, self.clock),
            replies_log,
            clients,
            self.outbox,
            self.clock,
            places=places,
            max_text=MESSAGE_TEXT_LIMIT,
        )
        self.replies = Replies(
            replies_log,
            self.outbox,
            self.clock,
            clients,
            channel=CHANNEL,
            commands=cast("Commands", None),
            goal=self.goal,
            proposals=proposals,
            expert_turns=self.turns,
            max_text=MESSAGE_TEXT_LIMIT,
        )
        self.sender = OutboxSender(self.outbox, channel, self.clock, interval_s=1)
        self.editor = CopyEditor(final, messages, channel, channel, self.clock, interval_s=1)
        self.replacer = CopyReplacer(
            JournalReplacements(journal, messages, max_text=MESSAGE_TEXT_LIMIT),
            channel,
            self.clock,
            interval_s=1,
        )
        self.scheduler = Scheduler(
            JournalSchedule(journal, signals, messages, ops),
            cast("WakeTarget", None),
            self.clock,
            interval_s=1,
            reminder_first_h=4,
            reminder_repeat_h=24,
        )

    def send(self) -> list[dict[str, Any]]:
        asyncio.run(self.sender.tick())
        return self.api.take("sendMessage")


def _send_replace_edit_and_remind(operator: _Operator) -> str:
    """Do the outgoing work the old journal left; return the *Submit again* message's reference."""
    # keep: the interrupted submit is asked about in the place it was sent from
    operator.goal.recover()
    sent = operator.send()
    assert _sent_to(sent) == [COMPONENT, PRIVATE, GENERAL, COMPONENT, IN_ROOM, COMPONENT]
    [asked_again] = [number for number, item in enumerate(sent) if "Add the import" in item["text"]]
    assert _sent_to([sent[asked_again]]) == [COMPONENT]

    asyncio.run(operator.replacer.tick())
    assert operator.api.take("deleteMessage") == [{"chat_id": str(GROUP), "message_id": 11}]
    asyncio.run(operator.editor.tick())
    [edit] = operator.api.take("editMessageText")
    assert (edit["chat_id"], edit["message_id"]) == (str(GROUP), 12)

    operator.clock.moment += timedelta(hours=5)
    asyncio.run(operator.scheduler.tick())
    assert sorted(_sent_to(operator.send()), key=str) == [COMPONENT, PRIVATE]
    return f"{GROUP}/{FIRST_SENT + asked_again}"


def test_an_upgraded_journal_keeps_every_message_in_its_place(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "journal.db"
    with monkeypatch.context() as patch:
        patch.setattr(journal_database, "MIGRATIONS", MIGRATIONS[:SCHEMA])
        patch.setattr(journal_database, "LATEST_VERSION", SCHEMA)
        old = Database.open(path)
    submit_op = _old_records(old)
    old.close()

    journal = Database.open(path)
    assert journal.connection.execute("PRAGMA foreign_key_check").fetchall() == []
    operator = _Operator(journal, tmp_path)
    again_ref = _send_replace_edit_and_remind(operator)

    operator.api.updates = [
        _tap(OFFSET, PRIVATE_REF, "a:1:1", None),
        _tap(OFFSET + 1, again_ref, submit_data(submit_op), TOPIC),
        _tap(OFFSET + 2, PROPOSAL_REF, submit_all_data(PROPOSED), ROOM),
    ]
    inbound = JournalInbound(journal)
    updates = TelegramUpdates(operator.api, group_id=GROUP)
    batch = asyncio.run(updates.fetch(inbound.cursor(CHANNEL), 1))
    assert batch.cursor is not None
    inbound.record_batch(CHANNEL, batch.events, batch.cursor, operator.clock.now())
    closed, *taps = inbound.pending()
    for tap in taps:
        asyncio.run(operator.replies.handle(tap))
    assert [call[0] for call in operator.client.calls] == ["E-1"]
    assert operator.client.submitted == ["Add the import", "Add it"]
    [report] = [item for item in operator.send() if "Request submitted" in item["text"]]
    assert _sent_to([report]) == [COMPONENT]

    assert (closed.kind, closed.place) == (InboundKind.ROOM_CLOSED, telegram_place(GROUP, ROOM))
    assert operator.turns.owns(closed)
    asyncio.run(operator.turns.handle(closed))
    asyncio.run(operator.editor.tick())
    asyncio.run(operator.ending.sweep())
    assert operator.api.take("deleteForumTopic")[0] == {
        "chat_id": GROUP,
        "message_thread_id": ROOM,
    }
    left = journal.connection.execute(
        "SELECT (SELECT COUNT(*) FROM conversations) + (SELECT COUNT(*) FROM turns)"
        " + (SELECT COUNT(*) FROM proposals) + (SELECT COUNT(*) FROM command_replies"
        " WHERE reply_key LIKE 'expert:%') + (SELECT COUNT(*) FROM messages"
        " WHERE conversation_id = 1 OR place = ? OR update_id = ?)",
        (telegram_place(GROUP, ROOM).id, CREATED),
    ).fetchone()[0]
    assert left == 0

    # keep: a notice stays unique per key and place, so only the two new places get one
    count = "SELECT COUNT(*) FROM messages"
    before = journal.connection.execute(count).fetchone()[0]
    for place in (telegram_place(OWNER), telegram_place(GROUP, TOPIC), telegram_place(GROUP)):
        operator.outbox.queue(
            None,
            Purpose.NOTICE,
            [place],
            Message("again"),
            key="waiting:private",
            now=operator.clock.now(),
        )
    assert journal.connection.execute(count).fetchone()[0] - before == 2
    # keep: a sent reference is unique per channel, whichever place the second copy is in
    [first, _] = operator.outbox.pending()
    with pytest.raises(sqlite3.IntegrityError):
        operator.outbox.confirm(first.message_id, PRIVATE_REF)
    journal.close()
