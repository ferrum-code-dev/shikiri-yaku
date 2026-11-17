"""Submit all and Reject all for the tasks an Expert answer proposes.

Each task is a submit op of its proposal, journaled before Shikiru is asked. The proposal moves
only from the state its tap expects, so a second tap, or a tap after a restart, submits nothing.
A tap counts only when it comes from the action message sent after the task list.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Final, Protocol

from pydantic import ValidationError

from shikiri_yaku.app.command_reply import queue_command_reply
from shikiri_yaku.domain.callback import (
    ADMIN_DECLINE_VERB,
    ADMIN_RUN_VERB,
    REJECT_ALL_VERB,
    SUBMIT_ALL_VERB,
    decode_callback,
    reject_all_data,
    submit_all_data,
)
from shikiri_yaku.domain.expert import AdminAnswer, ExpertAnswer, admin_message_text, stored_answer
from shikiri_yaku.domain.outbound import Purpose, fit
from shikiri_yaku.domain.schedule import Destination
from shikiri_yaku.domain.secrets import secret_form
from shikiri_yaku.ports import (
    BLOCK_SEPARATOR,
    MESSAGE_TEXT_LIMIT,
    Button,
    Message,
    ProposalStatus,
    SubmitMode,
    SubmitPayload,
    SubmitStatus,
    join_blocks,
)
from shikiri_yaku.settings import ADMIN_APPROVAL_EXPIRY_MIN

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from datetime import datetime

    from shikiri_yaku.app.expert import ConversationLog
    from shikiri_yaku.app.expert_admin import AdminAccess
    from shikiri_yaku.app.submit import Submitter
    from shikiri_yaku.domain.expert import ExpertTask
    from shikiri_yaku.domain.inbound import Inbound
    from shikiri_yaku.ports import Clock, FinalEdits, Outbox, SubmitOp

_ADMIN_INTENDED: Final = "intended"
_ADMIN_DECLINED: Final = "declined"
_TURN_PENDING: Final = "pending"
_TURN_DONE: Final = "done"
_CONVERSATION_OPEN: Final = "open"
_SUBMIT_ALL: Final = "Submit all"
_REJECT_ALL: Final = "Reject all"
_REJECTED: Final = "Rejected. Nothing submitted."
_SUPERSEDED: Final = "Superseded by a newer proposal. Nothing submitted."
_INTERRUPTED: Final = "Interrupted by a restart. Check the tasks marked unknown first."
_RESULTS: Final = "Task results:"
_PLANNING_MARKER: Final = " (planning)"
_PLAN_AND_EXECUTE: Final = (
    "Plan this work, then execute the plan by creating the follow-up tasks with their dependencies."
)
_ACTION_PROMPT: Final = "All task details are above. Choose an action."
_NOT_OPEN: Final[dict[str | None, str]] = {
    ProposalStatus.VOIDED: "Nothing submitted: a newer proposal in this conversation replaced it.",
    ProposalStatus.REJECTED: "Nothing submitted: this proposal was rejected.",
    ProposalStatus.SUBMITTING: "Nothing submitted again: this proposal is being submitted.",
    ProposalStatus.SUBMITTED: "Nothing submitted again: this proposal was already submitted.",
    ProposalStatus.INTERRUPTED: (
        "Nothing submitted again: this proposal's submit was interrupted. Check its tasks."
    ),
    ProposalStatus.UNOFFERED: "Nothing submitted: this proposal was not offered.",
    ProposalStatus.OFFERED: "Nothing submitted: use the buttons after the task details.",
    None: "Nothing submitted: this proposal is no longer open.",
}
_STATUSES: Final = (_REJECTED, _SUPERSEDED, _INTERRUPTED)
_STATUS_RESERVE: Final = max(len(status) for status in _STATUSES) + len(BLOCK_SEPARATOR)
_FINAL: Final = (
    ProposalStatus.SUBMITTED,
    ProposalStatus.INTERRUPTED,
    ProposalStatus.REJECTED,
    ProposalStatus.VOIDED,
)


def answer_key(update_id: int) -> str:
    """Return the notice key of the message that shows an Expert answer."""
    return f"expert:answer:{update_id}"


def action_key(update_id: int) -> str:
    """Return the notice key of the action message sent after every task detail."""
    return f"{answer_key(update_id)}:actions"


class ProposalLog(Protocol):
    """Expert task proposals in the journal; each move is guarded on the state it expects."""

    def intend_admin(
        self, conversation_id: int, update_id: int, command: str, reason: str, now: datetime
    ) -> tuple[int, list[int]]: ...
    def admin_op(self, op_id: int) -> tuple[int, str, datetime] | None: ...
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
    ) -> bool: ...
    def expire_admin(self, cutoff: datetime, now: datetime) -> list[int]: ...

    def offer(self, conversation_id: int, update_id: int, *, offered: bool) -> list[int]:
        """Record an answer's proposal and void the offered ones of its conversation.

        Return the proposals voided; an answer already recorded voids nothing.
        """
        ...

    def state(self, update_id: int) -> str | None: ...

    def begin(self, update_id: int, payloads: list[SubmitPayload], now: datetime) -> bool:
        """Journal the tasks and start submitting; False unless the proposal was offered."""
        ...

    def reject(self, update_id: int) -> bool:
        """Reject an offered proposal; False unless it was offered."""
        ...

    def finish(self, update_id: int) -> None: ...

    def ops(self, update_id: int) -> list[SubmitOp]: ...

    def unfinished(self) -> list[int]:
        """Return the proposals a restart found submitting."""
        ...

    def interrupt(self, update_id: int, now: datetime) -> None:
        """Interrupt a submitting proposal: its issued tasks become unknown."""
        ...

    def decided(self) -> list[tuple[int, str]]:
        """Return the proposals in a final state, with that state."""
        ...

    def offered(self) -> list[int]:
        """Return offered proposals to resume delivering after a restart."""
        ...

    def bound(self, destination: Destination, message_ref: str) -> str | None:
        """Return the notice key of the sent message a tap came from."""
        ...


@dataclass(frozen=True, slots=True)
class _Proposal:
    destination: Destination
    conversation_id: int
    answer: ExpertAnswer


class ExpertProposals:
    """Offer an answer's tasks, submit them on Submit all, reject them on Reject all."""

    def __init__(  # noqa: PLR0917 -- each collaborator is a distinct port
        self,
        log: ProposalLog,
        conversations: ConversationLog,
        submitter: Submitter,
        final: FinalEdits,
        outbox: Outbox,
        clock: Clock,
        *,
        channel: str,
        group_id: int,
        access: AdminAccess,
        approval_expiry_min: int = ADMIN_APPROVAL_EXPIRY_MIN,
    ) -> None:
        self._log = log
        self._conversations = conversations
        self._submitter = submitter
        self._final = final
        self._outbox = outbox
        self._clock = clock
        self._channel = channel
        self._group_id = group_id
        self._access = access
        self._admin_expiry = timedelta(minutes=approval_expiry_min)

    def answer_messages(
        self, conversation_id: int, update_id: int, answer: ExpertAnswer
    ) -> tuple[Message, ...]:
        """Return answer and task-list messages, followed by its action message.

        A newer proposal voids the offered proposals of its conversation: their buttons are
        removed. An answer delivered again shows its proposal's current state, without buttons
        unless it is still offered.
        """
        if not answer.tasks:
            return (Message(fit(answer.reply, MESSAGE_TEXT_LIMIT)),)
        for voided in self._log.offer(conversation_id, update_id, offered=True):
            self._settle(voided)
        state = self._log.state(update_id)
        if state == ProposalStatus.OFFERED:
            return (
                *_initial_messages(answer),
                Message(
                    _ACTION_PROMPT,
                    (
                        Button(_SUBMIT_ALL, submit_all_data(update_id)),
                        Button(_REJECT_ALL, reject_all_data(update_id)),
                    ),
                ),
            )
        return _initial_messages(answer)

    def queue_answer(self, update_id: int) -> None:
        """Queue every answer part before its action message, using stable outbox keys."""
        opened = self._open(update_id)
        if opened is None:
            return
        part = 0
        for message in self.answer_messages(opened.conversation_id, update_id, opened.answer):
            if message.buttons:
                key = action_key(update_id)
            else:
                key = answer_key(update_id) if part == 0 else f"{answer_key(update_id)}:part:{part}"
                part += 1
            self._outbox.queue(
                None,
                Purpose.EXPERT,
                [opened.destination],
                message,
                key=key,
                now=self._clock.now(),
                conversation_id=opened.conversation_id,
            )

    def queue_admin(self, update_id: int) -> None:
        """Journal an admin proposal and queue its exact text."""
        opened = self._open(update_id)
        if opened is None or not isinstance(opened.answer, AdminAnswer):
            return
        answer = opened.answer
        conversation = self._conversations.conversation(opened.conversation_id)
        if (
            answer.command is None
            or conversation is None
            or conversation.state != _CONVERSATION_OPEN
        ):
            return
        op_id, _ = self._log.intend_admin(
            opened.conversation_id, update_id, answer.command, answer.reason, self._clock.now()
        )
        op = self._log.admin_op(op_id)
        if op is None or op[1] != _ADMIN_INTENDED:
            return
        self._outbox.queue(
            None,
            Purpose.EXPERT,
            [opened.destination],
            Message(
                admin_message_text(answer.command, answer.reason),
                (
                    Button("Run", f"{ADMIN_RUN_VERB}:{op_id}"),
                    Button("Decline", f"{ADMIN_DECLINE_VERB}:{op_id}"),
                ),
            ),
            key=f"admin:proposal:{op_id}",
            now=self._clock.now(),
            conversation_id=opened.conversation_id,
        )

    def expire_admin(self) -> None:
        now = self._clock.now()
        self._log.expire_admin(now - self._admin_expiry, now)

    async def admin_tap(  # noqa: C901, PLR0911, PLR0912 -- each refusal is a distinct decision
        self,
        item: Inbound,
        run: Callable[[int, int], Awaitable[None]],
        run_admin: Callable[[int, int], Awaitable[None]] | None = None,
    ) -> bool:
        try:
            tap = decode_callback(item.data or "")
        except ValidationError:
            return False
        if tap[0] not in (ADMIN_RUN_VERB, ADMIN_DECLINE_VERB):
            return False
        op = self._log.admin_op(tap[1])
        if op is None:
            self._admin_reply(item, "Proposal is no longer available.")
            return True
        conversation_id, state, created = op
        conversation = self._conversations.conversation(conversation_id)
        if conversation is None or conversation.thread_id is None:
            self._admin_reply(item, "Conversation is closed.")
            return True
        destination = Destination(self._channel, str(self._group_id), conversation.thread_id)
        if self._log.bound(self._destination(item), item.message_ref) != f"admin:proposal:{tap[1]}":
            return True
        now = self._clock.now()
        admin = self._access.is_admin(item.sender)
        if not admin:
            reason = "Only a listed admin may decide."
        elif state != _ADMIN_INTENDED:
            # keep: a done decline turn may be undelivered after a stop, and run only delivers it
            turn = self._conversations.turn(item.update_id)
            if (
                state == _ADMIN_DECLINED
                and turn is not None
                and turn.state in (_TURN_PENDING, _TURN_DONE)
                and turn.conversation_id == conversation_id
            ):
                await run(conversation_id, item.update_id)
            reason = f"Proposal is {state}."
        elif conversation.state != _CONVERSATION_OPEN:
            reason = "Conversation is closed."
        elif created <= now - self._admin_expiry:
            reason = "Proposal has expired."
        else:
            approved = tap[0] == ADMIN_RUN_VERB
            if self._log.decide_admin(
                tap[1],
                item.update_id,
                item.sender,
                admin=admin,
                approve=approved,
                now=now,
                cutoff=now - self._admin_expiry,
            ):
                self._access.admit(conversation_id, item.sender)
                if approved and run_admin is not None:
                    await run_admin(tap[1], conversation_id)
                elif not approved:
                    await run(conversation_id, item.update_id)
                return True
            reason = "Proposal is no longer pending."
        queue_command_reply(
            self._outbox,
            [destination],
            reason,
            key=f"admin:decision:{item.update_id}",
            now=now,
            conversation_id=conversation_id,
        )
        return True

    def _admin_reply(self, item: Inbound, reason: str) -> None:
        queue_command_reply(
            self._outbox,
            [self._destination(item)],
            reason,
            key=f"admin:decision:{item.update_id}",
            now=self._clock.now(),
        )

    async def tap(self, item: Inbound) -> bool:
        """Handle a Submit all or Reject all tap; False when the tap is not one of these.

        A tap from any message other than the proposal's action message is ignored.
        """
        try:
            tap = decode_callback(item.data or "")
        except ValidationError:
            return False
        if tap[0] not in (SUBMIT_ALL_VERB, REJECT_ALL_VERB):
            return False
        proposal = self._open(tap[1])
        if proposal is not None and not self._access.admit(proposal.conversation_id, item.sender):
            return True
        message_key = self._log.bound(self._destination(item), item.message_ref)
        if message_key != action_key(tap[1]):
            if message_key == answer_key(tap[1]):
                opened = self._open(tap[1])
                if opened is not None:
                    self._say(item, opened, self._log.state(tap[1]))
            return True
        if tap[0] == SUBMIT_ALL_VERB:
            await self._submit_all(item, tap[1])
        else:
            self._reject(item, tap[1])
        return True

    async def _submit_all(self, item: Inbound, proposal_id: int) -> None:
        opened = self._open(proposal_id)
        if opened is None:
            return
        payloads = [_payload(opened.destination, proposal_id, task) for task in opened.answer.tasks]
        if not self._log.begin(proposal_id, payloads, self._clock.now()):
            self._say(item, opened, self._log.state(proposal_id))
            return
        for op in self._log.ops(proposal_id):
            sent = await self._submitter.advance(op)
            if sent.status not in (SubmitStatus.CONFIRMED, SubmitStatus.FAILED):
                break
        self._log.finish(proposal_id)
        self._settle(proposal_id)

    def _reject(self, item: Inbound, proposal_id: int) -> None:
        opened = self._open(proposal_id)
        if opened is None:
            return
        if self._log.reject(proposal_id):
            self._settle(proposal_id)
        else:
            self._say(item, opened, self._log.state(proposal_id))

    def recover(self) -> None:
        """Interrupt proposals a restart found submitting; then settle every final proposal again.

        A state is final before its message is edited, so a crash between the two is made up
        here: settling writes the same text again, and an unchanged text is not edited again.
        """
        now = self._clock.now()
        for proposal_id in self._log.unfinished():
            self._log.interrupt(proposal_id, now)
        for proposal_id in self._log.offered():
            opened = self._open(proposal_id)
            if opened is None:
                continue
            self.queue_answer(proposal_id)
        for proposal_id, state in self._log.decided():
            self._settle(proposal_id)
            if state == ProposalStatus.INTERRUPTED:
                self._tell_interrupted(proposal_id)

    def _tell_interrupted(self, proposal_id: int) -> None:
        opened = self._open(proposal_id)
        if opened is None:
            return
        for index, message in enumerate(_interrupted_notices(self._log.ops(proposal_id))):
            suffix = "" if index == 0 else f":part:{index}"
            self._outbox.queue(
                None,
                Purpose.EXPERT,
                [opened.destination],
                message,
                key=f"expert:proposal:{proposal_id}:interrupted{suffix}",
                now=self._clock.now(),
                conversation_id=opened.conversation_id,
            )

    def _destination(self, item: Inbound) -> Destination:
        return Destination(self._channel, str(item.chat), item.thread)

    def _open(self, proposal_id: int) -> _Proposal | None:
        turn = self._conversations.turn(proposal_id)
        if turn is None or turn.answer is None:
            return None
        conversation = self._conversations.conversation(turn.conversation_id)
        if conversation is None or conversation.thread_id is None:
            return None
        return _Proposal(
            Destination(self._channel, str(self._group_id), conversation.thread_id),
            conversation.id,
            stored_answer(turn.answer),
        )

    def _say(self, item: Inbound, opened: _Proposal, state: str | None) -> None:
        queue_command_reply(
            self._outbox,
            [opened.destination],
            _NOT_OPEN[state],
            key=f"expert:proposal:{item.update_id}",
            now=self._clock.now(),
            conversation_id=opened.conversation_id,
        )

    def _settle(self, proposal_id: int) -> None:
        """Edit the action message to its final state and queue any result messages."""
        state = self._log.state(proposal_id)
        opened = self._open(proposal_id)
        if opened is None or state not in _FINAL:
            return
        messages = _settled_messages(state, opened.answer, self._log.ops(proposal_id))
        # keep: the channel refuses an edit to unchanged text, which the PO reads as a refusal
        self._final.settle_notice(action_key(proposal_id), messages[0])
        for index, message in enumerate(messages[1:], 1):
            self._outbox.queue(
                None,
                Purpose.EXPERT,
                [opened.destination],
                message,
                key=f"expert:proposal:{proposal_id}:result:{index}",
                now=self._clock.now(),
                conversation_id=opened.conversation_id,
            )


def _initial_messages(answer: ExpertAnswer) -> tuple[Message, ...]:
    lines = _task_lines(answer.tasks)
    if secret_form([answer.reply, *lines]) is not None:
        return (Message(join_blocks(answer.reply, "\n".join(lines))),)
    usable = MESSAGE_TEXT_LIMIT - _STATUS_RESERVE
    reply = fit(answer.reply, usable)
    first_room = usable - len(reply) - (len(BLOCK_SEPARATOR) if reply else 0)
    if first_room <= 0 and reply:
        chunks = _pack_lines(lines, usable, usable)
        return (Message(reply), *(Message(chunk) for chunk in chunks))
    chunks = _pack_lines(lines, first_room, usable)
    texts = [join_blocks(reply, chunks[0])] if chunks else [reply]
    texts.extend(chunks[1:])
    return tuple(Message(text) for text in texts)


def _settled_messages(
    state: str | None, answer: ExpertAnswer, ops: list[SubmitOp]
) -> tuple[Message, ...]:
    """Return the edited action message and results that show every complete task id."""
    if state in (ProposalStatus.SUBMITTED, ProposalStatus.INTERRUPTED):
        outcomes = _outcome_lines(answer.tasks, ops)
        if secret_form(outcomes) is not None:
            return (Message("Task results were withheld by secret protection."),)
        if state == ProposalStatus.INTERRUPTED:
            status = anchor_status = _INTERRUPTED
        else:
            status = _submitted_line(ops, ".")
            anchor_status = _submitted_line(ops, "; results follow.")
        complete = join_blocks("\n".join(outcomes), status)
        if len(complete) <= MESSAGE_TEXT_LIMIT:
            return (Message(complete),)
        return (Message(anchor_status), *_result_messages(outcomes))
    if state in (ProposalStatus.REJECTED, ProposalStatus.VOIDED):
        if secret_form(_task_lines(answer.tasks)) is not None:
            return (Message("Proposal task details were withheld by secret protection."),)
        status = _REJECTED if state == ProposalStatus.REJECTED else _SUPERSEDED
        return (Message(status),)
    return _initial_messages(answer)


def _submitted_line(ops: list[SubmitOp], ending: str) -> str:
    confirmed = sum(op.status == SubmitStatus.CONFIRMED for op in ops)
    return f"Submitted: {confirmed} of {len(ops)} tasks{ending}"


def _result_messages(lines: list[str]) -> tuple[Message, ...]:
    header = _RESULTS
    chunks = _pack_lines(
        lines,
        MESSAGE_TEXT_LIMIT - len(header) - len(BLOCK_SEPARATOR),
        MESSAGE_TEXT_LIMIT,
    )
    if not chunks:
        return (Message(header),)
    return (Message(join_blocks(header, chunks[0])), *(Message(chunk) for chunk in chunks[1:]))


def _pack_lines(lines: list[str], first_room: int, other_room: int) -> list[str]:
    """Pack complete lines into messages, splitting only a line longer than a whole message."""
    chunks: list[str] = []
    current = ""
    room = max(0, first_room)
    for line in lines:
        if current:
            if len(current) + 1 + len(line) <= room:
                current = f"{current}\n{line}"
                continue
            chunks.append(current)
            current = ""
            room = other_room
        if len(line) <= room:
            current = line
            continue
        if room:
            chunks.append(line[:room])
            remaining = line[room:]
        else:
            remaining = line
        room = other_room
        while len(remaining) > room:
            chunks.append(remaining[:room])
            remaining = remaining[room:]
        current = remaining
    if current:
        chunks.append(current)
    return chunks


def _payload(destination: Destination, proposal_id: int, task: ExpertTask) -> SubmitPayload:
    if task.planning:
        return SubmitPayload(
            component=task.component,
            request=join_blocks(_PLAN_AND_EXECUTE, task.text),
            destination=destination,
            proposal=proposal_id,
            mode=SubmitMode.PLAN,
        )
    return SubmitPayload(
        component=task.component,
        request=task.text,
        destination=destination,
        proposal=proposal_id,
    )


def _task_lines(tasks: list[ExpertTask]) -> list[str]:
    lines: list[str] = []
    for index, task in enumerate(tasks, 1):
        marker = _PLANNING_MARKER if task.planning else ""
        lines.append(f"{index}. {task.component} · {task.title}{marker}")
    return lines


def _outcome_lines(tasks: list[ExpertTask], ops: list[SubmitOp]) -> list[str]:
    return [
        f"{index}. {task.component} · {task.title}: {_outcome(op)}"
        for index, (task, op) in enumerate(zip(tasks, ops, strict=True), 1)
    ]


def _outcome(op: SubmitOp) -> str:
    if op.status == SubmitStatus.CONFIRMED:
        return f"submitted as task {op.task_id or ''}"
    if op.status == SubmitStatus.FAILED:
        return "not submitted, Shikiru refused it"
    if op.status == SubmitStatus.UNKNOWN:
        return "unknown, it may have reached Shikiru; not sent again, please check"
    return "not submitted"


def _interrupted_notices(ops: list[SubmitOp]) -> tuple[Message, ...]:
    numbered = list(enumerate(ops, 1))
    submitted = [number for number, op in numbered if op.status == SubmitStatus.CONFIRMED]
    unknown = [number for number, op in numbered if op.status == SubmitStatus.UNKNOWN]
    unsent = [
        number
        for number, op in numbered
        if op.status not in (SubmitStatus.CONFIRMED, SubmitStatus.UNKNOWN)
    ]
    lines = ["Submit all was interrupted by a restart."]
    lines.extend(_number_lines("Submitted", submitted))
    lines.extend(_number_lines("Unknown, not sent again", unknown))
    lines.extend(_number_lines("Not submitted", unsent))
    return tuple(
        Message(text) for text in _pack_lines(lines, MESSAGE_TEXT_LIMIT, MESSAGE_TEXT_LIMIT)
    )


def _number_lines(label: str, numbers: list[int]) -> list[str]:
    if not numbers:
        return [f"{label}: none"]
    prefix = f"{label}: "
    continued = f"{label} (continued): "
    lines: list[str] = []
    current = prefix
    for number in numbers:
        value = str(number)
        separator = ", " if current not in (prefix, continued) else ""
        if len(current) + len(separator) + len(value) > MESSAGE_TEXT_LIMIT:
            lines.append(current)
            current = continued + value
        else:
            current = f"{current}{separator}{value}"
    lines.append(current)
    return lines
