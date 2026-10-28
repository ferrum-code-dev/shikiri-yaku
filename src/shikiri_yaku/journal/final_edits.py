from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import TypeAdapter

from shikiri_yaku.domain.outbound import Purpose, fit
from shikiri_yaku.journal.messages import Delivery, Direction
from shikiri_yaku.ports import MESSAGE_TEXT_LIMIT, FinalEdit, Message

if TYPE_CHECKING:
    from shikiri_yaku.journal.database import Database
    from shikiri_yaku.journal.replies import JournalReplies
    from shikiri_yaku.ports import RecordedResolution

_BODY = TypeAdapter(Message)
_SEPARATOR = "\n\n"
# keep: a copy shows buttons when its stored body has any; a refusal or a pending edit ends the need
_SHOWS_BUTTONS = """
    direction = :out AND delivery = :sent AND purpose IN (:question, :approval)
    AND refusal IS NULL AND final_body IS NULL AND body IS NOT NULL
    AND json_array_length(json_extract(body, '$.buttons')) > 0
"""


class JournalFinalEdits:
    """The final state of each sent copy lives on its messages row until the edit is confirmed.

    The old body stays until then, so a tap on a copy that still shows its buttons is still
    answered from the journal.
    """

    def __init__(self, database: Database, replies: JournalReplies) -> None:
        self._database = database
        self._replies = replies

    def _arguments(self) -> dict[str, str]:
        return {
            "out": Direction.OUT,
            "sent": Delivery.SENT,
            "question": Purpose.QUESTION,
            "approval": Purpose.APPROVAL,
        }

    def unsettled(self) -> list[tuple[int, RecordedResolution]]:
        rows = self._database.connection.execute(
            f"SELECT DISTINCT signal_number FROM messages WHERE {_SHOWS_BUTTONS} "  # noqa: S608
            "AND signal_number IS NOT NULL ORDER BY signal_number",
            self._arguments(),
        ).fetchall()
        found: list[tuple[int, RecordedResolution]] = []
        for row in rows:
            recorded = self._replies.recorded_resolution(row["signal_number"])
            if recorded is not None:
                found.append((row["signal_number"], recorded))
        return found

    def settle(self, number: int, line: str) -> None:
        connection = self._database.connection
        with self._database.transaction():
            rows = connection.execute(
                f"SELECT id, body FROM messages WHERE signal_number = :number AND {_SHOWS_BUTTONS}",  # noqa: S608
                {**self._arguments(), "number": number},
            ).fetchall()
            for row in rows:
                text = _BODY.validate_json(row["body"]).text
                room = MESSAGE_TEXT_LIMIT - len(_SEPARATOR) - len(line)
                final = Message(f"{fit(text, room)}{_SEPARATOR}{line}")
                connection.execute(
                    "UPDATE messages SET final_body = ? WHERE id = ?",
                    (_BODY.dump_json(final).decode(), row["id"]),
                )

    def to_edit(self) -> list[FinalEdit]:
        rows = self._database.connection.execute(
            """
            SELECT platform_message_id, final_body FROM messages
            WHERE direction = ? AND delivery = ? AND final_body IS NOT NULL ORDER BY id
            """,
            (Direction.OUT, Delivery.SENT),
        )
        return [
            FinalEdit(row["platform_message_id"], _BODY.validate_json(row["final_body"]))
            for row in rows
        ]

    def confirm_edit(self, message_ref: str) -> None:
        self._database.connection.execute(
            """
            UPDATE messages SET body = final_body, final_body = NULL
            WHERE platform_message_id = ? AND direction = ? AND final_body IS NOT NULL
            """,
            (message_ref, Direction.OUT),
        )

    def give_up(self, message_ref: str, reason: str) -> None:
        self._database.connection.execute(
            """
            UPDATE messages SET final_body = NULL, refusal = COALESCE(refusal, ?)
            WHERE platform_message_id = ? AND direction = ? AND final_body IS NOT NULL
            """,
            (reason, message_ref, Direction.OUT),
        )
