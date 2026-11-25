"""Sketches: the picture an Expert answer asks for, drawn once and sent to the answer's topic."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from shikiri_yaku.app.expert_files import move_sketch
from shikiri_yaku.app.expert_runs import conversation_directory
from shikiri_yaku.app.expert_turns import queue_expert_notice
from shikiri_yaku.domain.outbound import Purpose
from shikiri_yaku.domain.schedule import Destination
from shikiri_yaku.domain.secrets import secret_form
from shikiri_yaku.ports import Message, SketchError

if TYPE_CHECKING:
    from pathlib import Path

    from shikiri_yaku.domain.expert import ConversationRecord
    from shikiri_yaku.ports import Clock, Outbox, Sketcher

NO_CODEX: Final = "No Codex rung is in expert.ladder, so the sketch was not drawn."
WITHHELD: Final = "The sketch description was withheld by secret protection."
NOT_DRAWN: Final = "The sketch was not drawn: {reason}"
NOT_SAVED: Final = "The sketch was drawn, but it could not be saved in this conversation's files."


def sketch_key(update_id: int) -> str:
    """Return the notice key of an answer's sketch; a message under it means it was tried."""
    return f"expert:sketch:{update_id}"


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
        channel: str,
        group_id: int,
        known_secrets: tuple[str, ...] = (),
    ) -> None:
        self._sketcher = sketcher
        self._outbox = outbox
        self._clock = clock
        self._directory = directory
        self._channel = channel
        self._group_id = group_id
        self._known_secrets = tuple(secret for secret in known_secrets if secret)

    async def draw(
        self, conversation: ConversationRecord, update_id: int, description: str
    ) -> None:
        key = sketch_key(update_id)
        if conversation.thread_id is None or self._outbox.notice_queued(key):
            return
        destination = Destination(self._channel, str(self._group_id), conversation.thread_id)
        if self._sketcher is None:
            self._notice(destination, NO_CODEX, key, conversation.id)
            return
        if self._withheld(description):
            self._notice(destination, WITHHELD, key, conversation.id)
            return
        try:
            drawn = await self._sketcher.draw(description)
        except SketchError as error:
            self._notice(destination, NOT_DRAWN.format(reason=error), key, conversation.id)
            return
        try:
            picture = move_sketch(
                conversation_directory(self._directory, conversation.id), update_id, drawn
            )
        except OSError:
            self._notice(destination, NOT_SAVED, key, conversation.id)
            return
        self._outbox.queue(
            None,
            Purpose.EXPERT,
            [destination],
            Message("", photo=str(picture)),
            key=key,
            now=self._clock.now(),
            conversation_id=conversation.id,
        )

    def _withheld(self, description: str) -> bool:
        return secret_form([description]) is not None or any(
            secret in description for secret in self._known_secrets
        )

    def _notice(self, destination: Destination, text: str, key: str, conversation_id: int) -> None:
        queue_expert_notice(
            self._outbox,
            self._clock,
            destination,
            text,
            key=key,
            conversation_id=conversation_id,
        )
