"""Build the Expert proposals over a journal, as composition does, for tests."""

from __future__ import annotations

from typing import TYPE_CHECKING

from shikiri_yaku.app.expert_proposals import ExpertProposals
from shikiri_yaku.app.submit import Submitter
from shikiri_yaku.journal.final_edits import JournalFinalEdits
from shikiri_yaku.journal.ops import OpStore
from shikiri_yaku.journal.proposals import JournalProposals
from shikiri_yaku.journal.replies import JournalReplies
from shikiri_yaku.journal.signals import SignalStore
from shikiri_yaku.journal.submits import JournalSubmits

if TYPE_CHECKING:
    from collections.abc import Mapping

    from shikiri_yaku.app.expert import ConversationLog
    from shikiri_yaku.journal.database import Database
    from shikiri_yaku.ports import Clock, FinalEdits, Outbox, ShikiruClient


def expert_proposals(
    database: Database,
    conversations: ConversationLog,
    outbox: Outbox,
    clock: Clock,
    clients: Mapping[str, ShikiruClient],
    *,
    channel: str,
    group_id: int,
    final: FinalEdits | None = None,
) -> ExpertProposals:
    replies = JournalReplies(
        database, SignalStore(database), OpStore(database), channel_name=channel
    )
    return ExpertProposals(
        JournalProposals(database, OpStore(database), channel_name=channel),
        conversations,
        Submitter(
            JournalSubmits(database, OpStore(database), channel_name=channel), clients, clock
        ),
        final or JournalFinalEdits(database, replies),
        outbox,
        clock,
        channel=channel,
        group_id=group_id,
    )
