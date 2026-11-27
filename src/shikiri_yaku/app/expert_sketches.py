"""Sketches: the picture an Expert answer asks for, drawn once and sent to the answer's topic."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from shikiri_yaku.app.expert_files import move_sketch
from shikiri_yaku.app.expert_runs import conversation_directory
from shikiri_yaku.app.expert_turns import queue_expert_notice
from shikiri_yaku.domain.outbound import Purpose
from shikiri_yaku.domain.secrets import secret_form
from shikiri_yaku.ports import Message, SketchError

if TYPE_CHECKING:
    from pathlib import Path

    from shikiri_yaku.domain.expert import ConversationRecord
    from shikiri_yaku.domain.place import Place
    from shikiri_yaku.ports import Clock, Outbox, Rooms, Sketcher

DRAWING: Final = "Drawing the sketch…"
NO_CODEX: Final = "No Codex rung is in expert.ladder, so the sketch was not drawn."
WITHHELD: Final = "The sketch description was withheld by secret protection."
NOT_DRAWN: Final = "The sketch was not drawn: {reason}"
NOT_SAVED: Final = "The sketch was drawn, but it could not be saved in this conversation's files."
INTERRUPTED: Final = "The sketch was not drawn: the bot restarted before it was finished."


def sketch_key(update_id: int) -> str:
    """Return the notice key of an answer's sketch; a message under it means it was tried."""
    return f"expert:sketch:{update_id}"


def progress_key(update_id: int) -> str:
    return f"{sketch_key(update_id)}:progress"


class ExpertSketches:
    """Draws the sketch an answer asks for and sends it to the answer's topic as a photo.

    A turn delivered again draws nothing, because the sketch's notice key is already journaled.
    A refusal or a failure is a notice under the same key. The picture is moved into the
    conversation's files, so the tasks submitted later name it too.
    """

    def __init__(
        self,
        sketcher: Sketcher | None,
        outbox: Outbox,
        clock: Clock,
        *,
        directory: Path,
        rooms: Rooms,
        outbox_interval_s: float,
        known_secrets: tuple[str, ...] = (),
    ) -> None:
        self._sketcher = sketcher
        self._outbox = outbox
        self._clock = clock
        self._directory = directory
        self._rooms = rooms
        self._outbox_interval_s = outbox_interval_s
        self._known_secrets = tuple(secret for secret in known_secrets if secret)

    async def draw(
        self, conversation: ConversationRecord, update_id: int, description: str
    ) -> None:
        key = sketch_key(update_id)
        if conversation.room is None or self._outbox.notice_queued(key):
            return
        place = self._rooms.place(conversation.room)
        if self._sketcher is None:
            self._notice(place, NO_CODEX, key, conversation.id)
            return
        if self._withheld(description):
            self._notice(place, WITHHELD, key, conversation.id)
            return
        queue_expert_notice(
            self._outbox,
            self._clock,
            place,
            DRAWING,
            key=progress_key(update_id),
            slot=key,
            conversation_id=conversation.id,
        )
        if not await self._await_sent(progress_key(update_id)):
            return
        try:
            drawn = await self._sketcher.draw(description)
        except SketchError as error:
            self._notice(place, NOT_DRAWN.format(reason=error), key, conversation.id)
            return
        try:
            picture = move_sketch(
                conversation_directory(self._directory, conversation.id), update_id, drawn
            )
        except OSError:
            self._notice(place, NOT_SAVED, key, conversation.id)
            return
        self._outbox.queue(
            None,
            Purpose.EXPERT,
            [place],
            Message("", photo=str(picture)),
            key=key,
            now=self._clock.now(),
            slot=key,
            conversation_id=conversation.id,
        )

    def recover(self) -> None:
        """Replace the progress message of each drawing a restart interrupted, and say so.

        Nothing draws again after a restart, so the interrupted sketch gets its notice, which
        takes the progress message's slot and so deletes it.
        """
        self._outbox.recover_interrupted_drawings(Message(INTERRUPTED), self._clock.now())

    async def _await_sent(self, key: str) -> bool:
        """Wait for delivery; a permanently refused progress message prevents the drawing."""
        while not self._outbox.sent(key):
            if self._outbox.settled(key):
                return False
            await self._clock.sleep(self._outbox_interval_s)
        return True

    def _withheld(self, description: str) -> bool:
        return secret_form([description]) is not None or any(
            secret in description for secret in self._known_secrets
        )

    def _notice(self, place: Place, text: str, key: str, conversation_id: int) -> None:
        queue_expert_notice(
            self._outbox,
            self._clock,
            place,
            text,
            key=key,
            slot=key,
            conversation_id=conversation_id,
        )
