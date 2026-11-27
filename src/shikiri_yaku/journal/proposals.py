"""Expert task proposals: the state of each proposal, and its tasks as submit ops."""

from __future__ import annotations

import json
from datetime import datetime
from typing import TYPE_CHECKING, Final

from shikiri_yaku.domain.expert import MODE_ADMIN
from shikiri_yaku.journal.database import stamp
from shikiri_yaku.journal.final_edits import settle_notice
from shikiri_yaku.journal.messages import Delivery, Direction
from shikiri_yaku.journal.ops import NewOp
from shikiri_yaku.journal.submits import JournalSubmits
from shikiri_yaku.ports import Message, ProposalStatus, SubmitStatus

if TYPE_CHECKING:
    from collections.abc import Callable

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
        self._ops = ops
        self._submits = JournalSubmits(database, ops, channel_name=channel_name)

    def intend_admin(
        self, conversation_id: int, update_id: int, command: str, reason: str, now: datetime
    ) -> tuple[int, list[int]]:
        """Record one proposal and void earlier pending proposals atomically."""
        with self._database.transaction() as connection:
            inserted = self._ops.intend(
                NewOp(
                    identity=f"admin:{update_id}",
                    kind=MODE_ADMIN,
                    project="",
                    reason=reason,
                    payload=json.dumps({"conversation_id": conversation_id, "command": command}),
                ),
                now,
            )
            if not inserted.is_new:
                return inserted.row_id, []
            voided = [
                row["id"]
                for row in connection.execute(
                    "SELECT id FROM ops WHERE kind = 'admin' AND state = 'intended' "
                    "AND json_extract(payload, '$.conversation_id') = ? AND id <> ?",
                    (conversation_id, inserted.row_id),
                )
            ]
            for op_id in voided:
                connection.execute(
                    "UPDATE ops SET state = 'void', updated_at = ?, "
                    "payload = json_set(payload, '$.update_id', ?, '$.actor', "
                    "(SELECT sender FROM messages WHERE update_id = ?), '$.approver', NULL) "
                    "WHERE id = ?",
                    (stamp(now), update_id, update_id, op_id),
                )
                settle_notice(connection, f"admin:proposal:{op_id}", Message("Proposal voided."))
            return inserted.row_id, voided

    def decide_admin(
        self,
        op_id: int,
        update_id: int,
        sender: int,
        *,
        admin: bool,
        approve: bool,
        now: datetime,
        cutoff: datetime,
    ) -> bool:
        """Decide once only while the conversation and the approval are open."""
        with self._database.transaction() as connection:
            changed = connection.execute(
                "UPDATE ops SET state = ?, updated_at = ?, "
                "payload = json_set(payload, '$.approver', ?, '$.actor', ?, '$.update_id', ?) "
                "WHERE id = ? AND kind = 'admin' AND state = 'intended' "
                "AND ? AND created_at > ? AND EXISTS "
                "(SELECT 1 FROM conversations WHERE id = "
                "json_extract(ops.payload, '$.conversation_id') AND state = 'open')",
                (
                    "approved" if approve else "declined",
                    stamp(now),
                    sender,
                    sender,
                    update_id,
                    op_id,
                    admin,
                    stamp(cutoff),
                ),
            ).rowcount
            if changed:
                settle_notice(
                    connection,
                    f"admin:proposal:{op_id}",
                    Message("Approved." if approve else "Declined."),
                )
            if changed and not approve:
                connection.execute(
                    "INSERT INTO turns (conversation_id, update_id, text, state) "
                    "SELECT json_extract(payload, '$.conversation_id'), ?, "
                    "'The admin declined the proposed command.', 'pending' FROM ops WHERE id = ? "
                    "ON CONFLICT(update_id) DO NOTHING",
                    (update_id, op_id),
                )
            return changed == 1

    def issue_admin(self, op_id: int, now: datetime) -> tuple[int, str] | None:
        """Claim an approved command before it starts."""
        with self._database.transaction() as connection:
            row = connection.execute(
                "UPDATE ops SET state = 'issued', updated_at = ? "
                "WHERE id = ? AND kind = 'admin' AND state = 'approved' RETURNING payload",
                (stamp(now), op_id),
            ).fetchone()
            if row is None:
                return None
            payload = json.loads(row["payload"])
            return payload["conversation_id"], payload["command"]

    def finish_admin(
        self,
        op_id: int,
        now: datetime,
        output: str,
        code: int | None,
        duration: float,
        *,
        cut: bool,
        notice: Callable[[], None] | None = None,
    ) -> int | None:
        """Record the result, output notice, and Expert turn atomically."""
        with self._database.transaction() as connection:
            row = connection.execute(
                "UPDATE ops SET state = ?, updated_at = ?, "
                "payload = json_set(payload, '$.exit_code', ?, '$.duration_s', ?, '$.cut', ?) "
                "WHERE id = ? AND kind = 'admin' AND state = 'issued' RETURNING payload",
                ("failed" if code is None else "confirmed", stamp(now), code, duration, cut, op_id),
            ).fetchone()
            if row is None:
                return None
            conversation_id = json.loads(row["payload"])["conversation_id"]
            row = connection.execute("SELECT MIN(update_id) FROM turns").fetchone()
            turn_id = min(-1, (row[0] or 0) - 1)
            inserted = connection.execute(
                "INSERT INTO turns (conversation_id, update_id, text, state) "
                "SELECT id, ?, ?, 'pending' FROM conversations WHERE id = ? AND state = 'open'",
                (turn_id, output, conversation_id),
            ).rowcount
            if not inserted:
                return None
            if notice is not None:
                notice()
            return turn_id

    def recover_admin(
        self,
        now: datetime,
        notice: Callable[[int, int, str], None] | None = None,
    ) -> list[tuple[int, int, str]]:
        """Settle uncertain commands once, without replaying them.

        `notice` is called for each one in the same transaction, so its notice is journaled with
        the settlement, or not at all.
        """
        with self._database.transaction() as connection:
            rows = connection.execute(
                "UPDATE ops SET state = CASE state WHEN 'approved' THEN 'void' ELSE 'unknown' END, "
                "updated_at = ? WHERE kind = 'admin' AND state IN ('approved', 'issued') "
                "RETURNING id, payload, state",
                (stamp(now),),
            ).fetchall()
            recovered = [
                (row["id"], json.loads(row["payload"])["conversation_id"], row["state"])
                for row in rows
            ]
            if notice is not None:
                for op_id, conversation_id, state in recovered:
                    notice(op_id, conversation_id, state)
            return recovered

    def admin_op(self, op_id: int) -> tuple[int, str, datetime] | None:
        """Return the conversation, state and proposal time of an admin operation."""
        row = self._database.connection.execute(
            "SELECT payload, state, created_at FROM ops WHERE id = ? AND kind = 'admin'",
            (op_id,),
        ).fetchone()
        if row is None:
            return None
        created = datetime.fromisoformat(row["created_at"])
        return json.loads(row["payload"])["conversation_id"], row["state"], created

    def expire_admin(self, cutoff: datetime, now: datetime) -> list[int]:
        """Expire pending admin proposals whose approval period has elapsed."""
        with self._database.transaction() as connection:
            rows = connection.execute(
                "UPDATE ops SET state = 'expired', updated_at = ?, "
                "payload = json_set(payload, '$.approver', NULL, '$.actor', NULL, "
                "'$.update_id', NULL) "
                "WHERE kind = 'admin' AND state = 'intended' AND created_at <= ? "
                "RETURNING id",
                (stamp(now), stamp(cutoff)),
            ).fetchall()
            for row in rows:
                settle_notice(
                    connection, f"admin:proposal:{row['id']}", Message("Proposal expired.")
                )
            return [row["id"] for row in rows]

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
