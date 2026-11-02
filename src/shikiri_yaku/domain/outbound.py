"""What an outbound message is for, which decides how replies to it are read.

The notice of a refusal is an outbound message too, and its wording is here (design §7, §9).
"""

from __future__ import annotations

from datetime import UTC
from enum import StrEnum
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from datetime import datetime


class Purpose(StrEnum):
    QUESTION = "question"
    APPROVAL = "approval"
    NOTICE = "notice"


def message_label(number: int | None) -> str:
    """Name a message in the log: its signal, or a command reply when no signal is involved."""
    return "command reply" if number is None else f"S{number}"


def refusal_notice(number: int | None) -> str:
    """Return a refusal's notice: its signal number if it has one, and nothing it contained."""
    prefix = "" if number is None else f"{message_label(number)} · "
    return f"{prefix}A message was refused and not sent. Its text was not kept."


# keep: the last line of a message whose signal ended, so the PO sees why its buttons went
ACKNOWLEDGED_LINE: Final = "Acknowledged"
ENGINE_UP_LINE: Final = "Engine is up again"
# keep: the last line of a copy that a newer copy replaced, when the platform will not delete it
REPLACED_LINE: Final = "Replaced by a newer message"
# keep: a slot names the one live copy of a kind of message for a signal (design §7)
# keep: a newer copy in the same slot, chat and thread replaces the older one once accepted
QUESTION_SLOT: Final = "question"
INSTALL_SLOT: Final = "install"
TIME_FORMAT: Final = "%H:%M UTC"  # keep: the Operator has no time zone setting; its clock is UTC
_CUT_MARK: Final = "…"


def moment_text(moment: datetime, now: datetime) -> str:
    """Say a moment as the PO reads it: its time, with its date only when it is not today."""
    utc = moment.astimezone(UTC)
    if utc.date() == now.astimezone(UTC).date():
        return utc.strftime(TIME_FORMAT)
    return f"{utc.strftime(TIME_FORMAT)} on {utc.date().isoformat()}"


def fit(text: str, room: int) -> str:
    """Cut the text to `room` characters, ending in a mark when something was cut."""
    if len(text) <= room:
        return text
    return text[: room - len(_CUT_MARK)] + _CUT_MARK


_BREAKS: Final = ("\n\n", "\n", " ")


def split_reply(text: str, limit: int) -> list[str]:
    """Cut the text into parts of at most `limit` characters, each at the best break that fits.

    The breaks are a blank line, a line break and a space, in that order of preference; a word
    longer than the limit is cut at the limit. Nothing is dropped except the break itself.
    """
    parts: list[str] = []
    rest = text
    while len(rest) > limit:
        cut = _cut(rest, limit)
        parts.append(rest[:cut])
        rest = rest[cut:].lstrip()
    if rest:
        parts.append(rest)
    return parts


def _cut(text: str, limit: int) -> int:
    window = text[: limit + 1]
    for separator in _BREAKS:
        at = window.rfind(separator)
        if at > 0:
            return at
    return limit
