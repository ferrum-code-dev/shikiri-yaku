"""Sends a goal to Shikiru once per Telegram update and records the answer (§7, §10.1)."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from shikiri_yaku.ports import ShikiruError, ShikiruRejectedError, SubmitStatus

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import datetime

    from shikiri_yaku.ports import Clock, ShikiruClient, SubmitLog, SubmitOp, SubmitPayload


class Submitter:
    """Submits a goal once per update id and returns the op as the journal now holds it.

    The op is journaled before the call and claimed before it is made. An op that an earlier
    attempt issued may have reached Shikiru, so it becomes unknown and is never sent again. A
    retry replaces an unknown goal only when the PO taps *Submit again*. The caller submits one
    update at a time, so no two calls race for the same claim.
    """

    def __init__(
        self,
        log: SubmitLog,
        clients: Mapping[str, ShikiruClient],
        clock: Clock,
    ) -> None:
        self._log = log
        self._clients = clients
        self._clock = clock

    async def submit(self, update_id: int, payload: SubmitPayload) -> SubmitOp:
        """Return the op for the update id; `payload.component` must name a client."""
        client = self._clients[payload.component]
        now = self._clock.now()
        return await self._advance(client, self._log.intend(update_id, payload, now), now)

    async def retry(self, prompt_id: int, update_id: int) -> SubmitOp | None:
        """Return the replacement of an unknown goal, or None when the tap may not retry it."""
        now = self._clock.now()
        op = self._log.retry(prompt_id, update_id, now)
        if op is None:
            return None
        # keep: a component dropped from the settings leaves its replacement intended, unsent
        client = self._clients.get(op.payload.component)
        return op if client is None else await self._advance(client, op, now)

    def recover(self) -> list[SubmitOp]:
        """Settle the submits an earlier process left unfinished, and return the unknown ones.

        An issued op may have reached Shikiru, so it becomes unknown and is never sent again. An
        intended op was never issued: the replay of its stored inbound item sends it.
        """
        now = self._clock.now()
        unknown: list[SubmitOp] = []
        for op in self._log.unfinished():
            settled = self._unknown(op, now) if op.status == SubmitStatus.ISSUED else op
            if settled.status == SubmitStatus.UNKNOWN:
                unknown.append(settled)
        return unknown

    async def _advance(self, client: ShikiruClient, op: SubmitOp, now: datetime) -> SubmitOp:
        if op.status == SubmitStatus.ISSUED:
            return self._unknown(op, now)
        if op.status != SubmitStatus.INTENDED or not self._log.claim(op.id, now):
            return op
        return await self._send(client, op, now)

    async def _send(self, client: ShikiruClient, op: SubmitOp, now: datetime) -> SubmitOp:
        try:
            ack = await client.submit_task(op.payload.request)
        except ShikiruRejectedError:
            self._log.fail(op.id, now)
            return replace(op, status=SubmitStatus.FAILED)
        except ShikiruError:
            return self._unknown(op, now)
        if ack.task_id is None:
            return self._unknown(op, now)
        self._log.confirm(op.id, ack, now)
        return replace(op, status=SubmitStatus.CONFIRMED, task_id=ack.task_id)

    def _unknown(self, op: SubmitOp, now: datetime) -> SubmitOp:
        self._log.uncertain(op.id, now)
        return replace(op, status=SubmitStatus.UNKNOWN)
