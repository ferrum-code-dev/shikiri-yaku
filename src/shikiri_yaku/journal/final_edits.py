from __future__ import annotations

from datetime import UTC
from typing import TYPE_CHECKING

from pydantic import TypeAdapter

from shikiri_yaku.domain.outbound import TIME_FORMAT, Purpose, fit
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

    The body keeps the buttons the copy was sent with, also after the edit: a tap made before it
    may be processed after it, and is answered from the journal as "Already resolved".
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

    def unsettled(self) -> list[tuple[int, str]]:
        rows = self._database.connection.execute(
            f"SELECT DISTINCT signal_number, final_line FROM messages "  # noqa: S608
            f"LEFT JOIN signals ON signals.number = signal_number WHERE {_SHOWS_BUTTONS} "
            "AND signal_number IS NOT NULL ORDER BY signal_number",
            self._arguments(),
        ).fetchall()
        found: list[tuple[int, str]] = []
        for row in rows:
            if row["final_line"] is not None:
                found.append((row["signal_number"], row["final_line"]))
                continue
            recorded = self._replies.recorded_resolution(row["signal_number"])
            if recorded is not None:
                found.append((row["signal_number"], _line(recorded)))
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
                shown = fit(line, MESSAGE_TEXT_LIMIT)
                room = MESSAGE_TEXT_LIMIT - len(_SEPARATOR) - len(shown)
                head = f"{fit(text, room)}{_SEPARATOR}" if room > len(_SEPARATOR) else ""
                final = Message(f"{head}{shown}")
                connection.execute(
                    "UPDATE messages SET final_body = ? WHERE id = ?",
                    (_BODY.dump_json(final).decode(), row["id"]),
                )

    def to_edit(self) -> list[FinalEdit]:
        rows = self._database.connection.execute(
            """
            SELECT platform_message_id, final_body FROM messages
            WHERE direction = ? AND delivery = ? AND final_body IS NOT NULL
              AND final_edited = 0 ORDER BY id
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
            UPDATE messages SET final_edited = 1
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


def _line(resolution: RecordedResolution) -> str:
    return f"Resolved: {resolution.label} at {resolution.at.astimezone(UTC).strftime(TIME_FORMAT)}"
