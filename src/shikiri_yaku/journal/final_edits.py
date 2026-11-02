from __future__ import annotations

from datetime import UTC
from typing import TYPE_CHECKING

from pydantic import TypeAdapter

from shikiri_yaku.domain.outbound import REMINDERS_STOPPED_LINE, TIME_FORMAT, fit
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
# keep: a copy with buttons is edited to its final state, as a question is; one without is not
_SHOWS_BUTTONS = """
    direction = :out AND delivery = :sent
    AND refusal IS NULL AND body IS NOT NULL
    AND json_array_length(json_extract(body, '$.buttons')) > 0
"""


class JournalFinalEdits:
    """The final state of each sent copy lives on its messages row until the edit is confirmed.

    The body keeps the buttons the copy was sent with, also after the edit: a tap made before it
    may be processed after it, and is answered from the journal as "Already resolved". A standing
    copy reads "Reminders stopped" once its signal is silenced, and again when the signal ends.
    """

    def __init__(self, database: Database, replies: JournalReplies) -> None:
        self._database = database
        self._replies = replies

    def _arguments(self) -> dict[str, str]:
        return {"out": Direction.OUT, "sent": Delivery.SENT}

    def unsettled(self) -> list[tuple[int, str]]:
        # keep: a copy shown as "Reminders stopped" is unsettled again until its signal ends
        rows = self._database.connection.execute(
            "SELECT DISTINCT signal_number, signals.final_line AS signal_line FROM messages "  # noqa: S608
            f"LEFT JOIN signals ON signals.number = signal_number WHERE {_SHOWS_BUTTONS} "
            "AND signal_number IS NOT NULL "
            "AND (final_body IS NULL OR messages.final_line = :stopped) ORDER BY signal_number",
            {**self._arguments(), "stopped": REMINDERS_STOPPED_LINE},
        ).fetchall()
        found: list[tuple[int, str]] = []
        for row in rows:
            line = self._ending(row["signal_number"], row["signal_line"])
            if line is not None:
                found.append((row["signal_number"], line))
        return found

    def _ending(self, number: int, signal_line: str | None) -> str | None:
        """Return the line the signal's copies end with now, or None while nothing ends them."""
        if signal_line is not None:
            return signal_line
        recorded = self._replies.recorded_resolution(number)
        if recorded is not None:
            return _line(recorded)
        return REMINDERS_STOPPED_LINE if self._replies.silenced(number) else None

    def settle(self, number: int, line: str) -> None:
        connection = self._database.connection
        with self._database.transaction():
            rows = connection.execute(
                f"SELECT id, body FROM messages WHERE signal_number = :number AND {_SHOWS_BUTTONS}"  # noqa: S608
                " AND (final_body IS NULL OR (final_line IS NOT NULL AND final_line IS NOT :line))",
                {**self._arguments(), "number": number, "line": line},
            ).fetchall()
            for row in rows:
                final = final_copy(_BODY.validate_json(row["body"]), line)
                connection.execute(
                    "UPDATE messages SET final_body = ?, final_line = ?, final_edited = 0"
                    " WHERE id = ?",
                    (_BODY.dump_json(final).decode(), line, row["id"]),
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
