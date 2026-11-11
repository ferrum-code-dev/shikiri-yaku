"""Expert task proposals: the state of each proposal, and its tasks as submit ops."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from shikiri_yaku.journal.messages import Delivery, Direction
from shikiri_yaku.journal.submits import JournalSubmits
from shikiri_yaku.ports import ProposalStatus, SubmitStatus

if TYPE_CHECKING:
    from datetime import datetime

    from shikiri_yaku.domain.schedule import Destination
    from shikiri_yaku.journal.database import Database
    from shikiri_yaku.journal.ops import OpStore
    from shikiri_yaku.ports import SubmitOp, SubmitPayload

_DECIDED: tuple[str, ...] = (
    ProposalStatus.SUBMITTED,
    ProposalStatus.REJECTED,
    ProposalStatus.VOIDED,
    ProposalStatus.INTERRUPTED,
)
_DECIDED_SQL: Final = (
    "SELECT update_id, state FROM proposals WHERE state IN (?, ?, ?, ?) ORDER BY update_id"
)


class JournalProposals:
    """Each proposal moves only from the state the guard names, so its tasks are journaled once.

    A proposal is offered, then submitting (its tasks journaled with it, in one transaction), then
    submitted; or rejected or voided while offered; or interrupted when a restart finds it
    submitting. A proposal too long to offer is unoffered. Its tasks are the submit ops whose
    payload names the proposal.
    """

    def __init__(self, database: Database, ops: OpStore, *, channel_name: str) -> None:
        self._database = database
        self._submits = JournalSubmits(database, ops, channel_name=channel_name)

    def offer(self, conversation_id: int, update_id: int, *, offered: bool) -> list[int]:
        """Record an answer's proposal and void the offered ones of its conversation.

        An answer already recorded, such as a turn delivered again, changes nothing: a replay
        cannot void a newer proposal. Return the proposals voided.
        """
        state = ProposalStatus.OFFERED if offered else ProposalStatus.UNOFFERED
        with self._database.transaction() as connection:
            inserted = connection.execute(
                "INSERT INTO proposals (update_id, conversation_id, state) VALUES (?, ?, ?)"
                " ON CONFLICT (update_id) DO NOTHING",
                (update_id, conversation_id, state),
            ).rowcount
            if inserted != 1:
                return []
            voided = [
                row["update_id"]
                for row in connection.execute(
                    "SELECT update_id FROM proposals"
                    " WHERE conversation_id = ? AND state = ? AND update_id <> ?",
                    (conversation_id, ProposalStatus.OFFERED, update_id),
                ).fetchall()
            ]
            connection.executemany(
                "UPDATE proposals SET state = ? WHERE update_id = ?",
                [(ProposalStatus.VOIDED, voided_id) for voided_id in voided],
            )
        return voided

    def state(self, update_id: int) -> str | None:
        row = self._database.connection.execute(
            "SELECT state FROM proposals WHERE update_id = ?", (update_id,)
        ).fetchone()
        return None if row is None else row["state"]

    def begin(self, update_id: int, payloads: list[SubmitPayload], now: datetime) -> bool:
        """Journal the tasks and move the proposal to submitting; False unless it was offered."""
        with self._database.transaction():
            if not self._move(update_id, ProposalStatus.OFFERED, ProposalStatus.SUBMITTING):
                return False
            for index, payload in enumerate(payloads):
                self._submits.intend_task(update_id, index, payload, now)
        return True

    def reject(self, update_id: int) -> bool:
        return self._move(update_id, ProposalStatus.OFFERED, ProposalStatus.REJECTED)

    def finish(self, update_id: int) -> None:
        self._move(update_id, ProposalStatus.SUBMITTING, ProposalStatus.SUBMITTED)

    def ops(self, update_id: int) -> list[SubmitOp]:
        return self._submits.for_proposal(update_id)

    def unfinished(self) -> list[int]:
        """Return the proposals a restart found submitting."""
        rows = self._database.connection.execute(
            "SELECT update_id FROM proposals WHERE state = ? ORDER BY update_id",
            (ProposalStatus.SUBMITTING,),
        ).fetchall()
        return [row["update_id"] for row in rows]

    def decided(self) -> list[tuple[int, str]]:
        """Return the proposals whose state is final, with their state, in order.

        Recovery settles their messages again: the state is the record of what the message
        should show, so a settle lost to a crash is made up on the next start.
        """
        rows = self._database.connection.execute(_DECIDED_SQL, _DECIDED).fetchall()
        return [(row["update_id"], row["state"]) for row in rows]

    def offered(self) -> list[int]:
        """Return open proposals whose answer delivery can be resumed after a restart."""
        rows = self._database.connection.execute(
            "SELECT update_id FROM proposals WHERE state = ? ORDER BY update_id",
            (ProposalStatus.OFFERED,),
        ).fetchall()
        return [row["update_id"] for row in rows]

    def bound(self, destination: Destination, message_ref: str) -> str | None:
        """Return the notice key of the sent message a tap came from, if the journal has it.

        A message id is unique within its chat, so the thread is not needed to find it.
        """
        row = self._database.connection.execute(
            """
            SELECT notice_key FROM messages
            WHERE channel = ? AND chat = ? AND platform_message_id = ?
                AND direction = ? AND delivery = ?
            """,
            (
                destination.channel,
                destination.chat,
                message_ref,
                Direction.OUT,
                Delivery.SENT,
            ),
        ).fetchone()
        return None if row is None else row["notice_key"]

    def interrupt(self, update_id: int, now: datetime) -> None:
        """Mark a submitting proposal interrupted; its issued tasks become unknown, never resent.

        An intended task was never issued, so it stays unsent.
        """
        with self._database.transaction():
            if self._move(update_id, ProposalStatus.SUBMITTING, ProposalStatus.INTERRUPTED):
                for op in self._submits.for_proposal(update_id):
                    if op.status == SubmitStatus.ISSUED:
                        self._submits.uncertain(op.id, now)

    def _move(self, update_id: int, expected: str, new: str) -> bool:
        moved = self._database.connection.execute(
            "UPDATE proposals SET state = ? WHERE update_id = ? AND state = ?",
            (new, update_id, expected),
        ).rowcount
        return moved == 1
