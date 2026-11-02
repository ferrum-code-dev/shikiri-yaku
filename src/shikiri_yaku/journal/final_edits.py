from __future__ import annotations

import json
from datetime import UTC
from typing import TYPE_CHECKING

from pydantic import TypeAdapter

from shikiri_yaku.domain.outbound import TIME_FORMAT, fit
from shikiri_yaku.journal.messages import Delivery, Direction
from shikiri_yaku.ports import (
    BLOCK_SEPARATOR,
    FinalEdit,
    Message,
    join_blocks,
    text_room,
)

if TYPE_CHECKING:
    from shikiri_yaku.journal.database import Database
    from shikiri_yaku.journal.replies import JournalReplies
    from shikiri_yaku.ports import RecordedResolution

_BODY = TypeAdapter(Message)
# keep: any copy with buttons ends too, so a review message is finished like a question
# keep: a copy without buttons ends when its notice key starts with a prefix the caller names
_SHOWS_BUTTONS = """
    direction = :out AND delivery = :sent
    AND refusal IS NULL AND final_body IS NULL AND body IS NOT NULL
    AND (json_array_length(json_extract(body, '$.buttons')) > 0 OR EXISTS (
        SELECT 1 FROM json_each(:endings) AS ending
        WHERE substr(messages.notice_key, 1, length(ending.value)) = ending.value))
"""


class JournalFinalEdits:
    """The final state of each sent copy lives on its messages row until the edit is confirmed.

    The body keeps the buttons the copy was sent with, also after the edit: a tap made before it
    may be processed after it, and is answered from the journal as "Already resolved".
    """

    def __init__(
        self,
        database: Database,
        replies: JournalReplies,
        *,
        ending_prefixes: tuple[str, ...] = (),
    ) -> None:
        self._database = database
        self._replies = replies
        self._endings = ending_prefixes

    def _arguments(self) -> dict[str, str]:
        return {
            "out": Direction.OUT,
            "sent": Delivery.SENT,
            "endings": json.dumps(self._endings),
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
                final = final_copy(_BODY.validate_json(row["body"]), line)
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


def final_copy(original: Message, line: str) -> Message:
    """Return a sent copy's final state: its heading, as much text as fits, then the line."""
    room = text_room(original.heading)
    shown = fit(line, room)
    left = room - len(shown) - len(BLOCK_SEPARATOR)
    kept = fit(original.text, left) if left > len(BLOCK_SEPARATOR) else ""
    return Message(join_blocks(kept, shown), heading=original.heading)


def _line(resolution: RecordedResolution) -> str:
    return f"Resolved: {resolution.label} at {resolution.at.astimezone(UTC).strftime(TIME_FORMAT)}"
