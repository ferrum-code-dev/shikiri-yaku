"""What an outbound message is for, which decides how replies to it are read (design §7, §9)."""

from __future__ import annotations

from enum import StrEnum


class Purpose(StrEnum):
    QUESTION = "question"
    APPROVAL = "approval"
    NOTICE = "notice"
