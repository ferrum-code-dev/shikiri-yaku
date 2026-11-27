"""Edits every copy of a resolved question to its final state, without buttons."""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

from shikiri_yaku.domain.secrets import secret_form
from shikiri_yaku.ports import ChannelError, ChannelRefusedError

if TYPE_CHECKING:
    from shikiri_yaku.ports import Clock, Copies, Deleter, Editor, FinalEdits


class CopyEditor:
    """Records the final state of each copy, then asks the platform to show it.

    The final state is journaled before any edit, so a restart finishes what was started. A failure
    the platform may repeat leaves the edit recorded for the next tick; one it refuses for good is
    given up, and the PO is notified of it. An edit that carries a secret is never made: the sent
    copy is deleted instead, its attempt journaled first and not repeated. The journal supplies the
    line, whether the PO's own resolve was confirmed or the PO resolved elsewhere; this class only
    applies it.
    """

    def __init__(
        self,
        log: FinalEdits,
        copies: Copies,
        editor: Editor,
        deleter: Deleter,
        clock: Clock,
        *,
        interval_s: float,
    ) -> None:
        self._log = log
        self._copies = copies
        self._editor = editor
        self._deleter = deleter
        self._clock = clock
        self._interval_s = interval_s

    async def run(self) -> None:
        while True:
            await self.tick()
            await self._clock.sleep(self._interval_s)

    async def tick(self) -> None:
        for number, line in self._log.unsettled():
            self._log.settle(number, line)
        for edit in self._log.to_edit():
            if (form := secret_form(edit.message.texts())) is not None:
                await self._delete_copy(edit.message_id, edit.message_ref, form)
                self._log.give_up(edit.message_id, edit.message, form)
                continue
            try:
                await self._editor.edit(edit.message_ref, edit.message)
            except ChannelRefusedError as error:
                self._copies.record_refused_edit(edit.message_id, str(error), now=self._clock.now())
                self._log.give_up(edit.message_id, edit.message, str(error))
            except ChannelError:
                continue
            else:
                self._log.confirm_edit(edit.message_id, edit.message)

    async def _delete_copy(self, message_id: int, message_ref: str, reason: str) -> None:
        """Delete the copy a secret was to be edited into, when the journal knows it.

        The attempt is journaled, and the PO's notice queued, before the platform is asked. A failed
        deletion stays journaled and is not retried; the give-up stands either way.
        """
        if not self._copies.record_deletion(message_id, reason, now=self._clock.now()):
            return
        with contextlib.suppress(ChannelError, ChannelRefusedError):
            await self._deleter.delete(message_ref)
            self._copies.confirm_deletion(message_id)
