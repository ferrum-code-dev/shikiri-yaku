"""What an outbound message is for, which decides how replies to it are read.

The notice of a refusal is an outbound message too, and its wording is here (design §7, §9).
"""

from __future__ import annotations

from enum import StrEnum


class Purpose(StrEnum):
    QUESTION = "question"
    APPROVAL = "approval"
    NOTICE = "notice"


def refusal_notice(number: int) -> str:
    """Return the notice of a refusal: its signal number and nothing it contained."""
    return f"S{number} · A message was refused and not sent. Its text was not kept."
