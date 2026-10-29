"""The PO message of a pull request ready for review (design §6.4, §7).

Pure: it builds the text and the buttons and does no I/O.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from shikiri_yaku.domain.outbound import fit
from shikiri_yaku.ports import MESSAGE_TEXT_LIMIT, Button, Message

if TYPE_CHECKING:
    from shikiri_yaku.domain.signal import PullRequest, Signal

TITLE: Final = "Pull request ready for review"
OPEN_PR_LABEL: Final = "Open PR"
ACKNOWLEDGE_LABEL: Final = "Acknowledge"
# keep: the verb the replies read as Acknowledge (design §7); its notice key makes it silence
ACKNOWLEDGE_TAP: Final = "k"


def review_message(
    number: int, signal: Signal, pull: PullRequest, title: str, *, product: str
) -> Message:
    """Return the review message: its header, the title and commit count, and two buttons.

    The Open PR button is a link; Acknowledge carries the signal number as its data.
    """
    text = "\n".join(
        block for block in (_header(number, signal, product), _body(pull, title)) if block
    )
    buttons = (
        Button(OPEN_PR_LABEL, url=pull.url),
        Button(ACKNOWLEDGE_LABEL, f"{ACKNOWLEDGE_TAP}:{number}"),
    )
    return Message(fit(text, MESSAGE_TEXT_LIMIT), buttons)


def _header(number: int, signal: Signal, product: str) -> str:
    parts = (f"S{number}", product, signal.project, signal.task_id, TITLE)
    return " · ".join(part for part in parts if part)


def _body(pull: PullRequest, title: str) -> str:
    return " · ".join(part for part in (title, _commits(pull.commits)) if part)


def _commits(count: int) -> str:
    return f"{count} commit" if count == 1 else f"{count} commits"
