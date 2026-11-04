"""The PO message of a pull request ready for review.

Pure: it builds the text and the buttons and does no I/O.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from shikiri_yaku.domain.callback import stop_reminders_data
from shikiri_yaku.domain.outbound import STOP_REMINDERS_LABEL, fit
from shikiri_yaku.ports import MESSAGE_TEXT_LIMIT, Button, Message

if TYPE_CHECKING:
    from shikiri_yaku.domain.signal import PullRequest, Signal

TITLE: Final = "Pull request ready for review"
OPEN_PR_LABEL: Final = "Open PR"


def review_message(
    number: int,
    signal: Signal,
    pull: PullRequest,
    title: str,
    *,
    product: str,
    in_topic: bool,
) -> Message:
    """Return the review message: its header, the title and commit count, and two buttons.

    The Open PR button is a link; Stop reminders carries the signal number as its data, and the
    review stays open until it is fixed. The component name is left out of the header
    where the message is already in that component's topic.
    """
    text = "\n".join(
        block
        for block in (_header(number, signal, product, in_topic=in_topic), _body(pull, title))
        if block
    )
    buttons = (
        Button(OPEN_PR_LABEL, url=pull.url),
        Button(STOP_REMINDERS_LABEL, stop_reminders_data(number)),
    )
    return Message(fit(text, MESSAGE_TEXT_LIMIT), buttons)


def _header(number: int, signal: Signal, product: str, *, in_topic: bool) -> str:
    # keep: a message in its component's topic already names the component, so it is left out
    project = "" if in_topic else signal.project
    parts = (f"S{number}", product, project, signal.task_id, TITLE)
    return " · ".join(part for part in parts if part)


def _body(pull: PullRequest, title: str) -> str:
    return " · ".join(part for part in (title, _commits(pull.commits)) if part)


def _commits(count: int) -> str:
    return f"{count} commit" if count == 1 else f"{count} commits"
