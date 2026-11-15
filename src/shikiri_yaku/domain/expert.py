"""The Expert's validated answer and conversation context."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from datetime import datetime
    from pathlib import Path

    from shikiri_yaku.domain.classification import CallRecord


class ExpertTask(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    component: str
    title: str
    text: str


class ExpertAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    reply: str
    title: str
    tasks: list[ExpertTask]
    findings: str


class AdminAnswer(ExpertAnswer):
    """An admin answer adds the optional command text and its reason, both model-written."""

    command: str | None
    reason: str


COMMAND_FIELD: Final = "command"


def stored_answer(text: str) -> ExpertAnswer:
    """Read a journaled answer: an admin answer has a command key, an ordinary one has none."""
    model = AdminAnswer if COMMAND_FIELD in json.loads(text) else ExpertAnswer
    return model.model_validate_json(text)


MODE_EXPERT: Final = "expert"
MODE_ADMIN: Final = "admin"


@dataclass(frozen=True, slots=True)
class ExpertSession:
    rung: int
    provider: str
    session_id: str
    findings: str


@dataclass(frozen=True, slots=True)
class ConversationRecord:
    id: int
    update_id: int
    chat: int
    origin_thread: int | None
    thread_id: int | None
    scope: list[str]
    state: str
    effort: str | None
    session: ExpertSession | None
    ending_at: datetime | None
    mode: str = MODE_EXPERT


@dataclass(frozen=True, slots=True)
class TurnRecord:
    update_id: int
    conversation_id: int
    thread_id: int | None
    text: str
    state: str
    answer: str | None


@dataclass(frozen=True, slots=True)
class ExpertTurn:
    message: str
    reply: str


@dataclass(frozen=True, slots=True)
class ExpertRequest:
    directory: Path
    scope: dict[str, Path]
    message: str
    context: str = ""
    effort: str | None = None
    session: ExpertSession | None = None
    turns: tuple[ExpertTurn, ...] = ()
    update_id: int = 0
    admin: bool = False


@dataclass(frozen=True, slots=True)
class ExpertResult:
    answer: ExpertAnswer
    session: ExpertSession
    call: CallRecord | None
