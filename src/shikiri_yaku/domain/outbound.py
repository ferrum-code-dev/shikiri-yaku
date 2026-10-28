"""What an outbound message is for, which decides how replies to it are read.

The notice of a refusal is an outbound message too, and its wording is here (design §7, §9).
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final


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


TIME_FORMAT: Final = "%H:%M UTC"  # keep: the Operator has no time zone setting; its clock is UTC
_CUT_MARK: Final = "…"


def fit(text: str, room: int) -> str:
    """Cut the text to `room` characters, ending in a mark when something was cut."""
    if len(text) <= room:
        return text
    return text[: room - len(_CUT_MARK)] + _CUT_MARK
