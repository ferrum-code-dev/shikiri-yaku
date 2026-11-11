"""The Expert's validated answer and conversation context."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
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


@dataclass(frozen=True, slots=True)
class ExpertTurn:
    message: str
    reply: str


@dataclass(frozen=True, slots=True)
class ExpertSession:
    rung: int
    provider: str
    session_id: str
    findings: str


@dataclass(frozen=True, slots=True)
class ExpertRequest:
    directory: Path
    scope: dict[str, Path]
    message: str
    effort: str | None = None
    session: ExpertSession | None = None
    turns: tuple[ExpertTurn, ...] = ()


@dataclass(frozen=True, slots=True)
class ExpertResult:
    answer: ExpertAnswer
    session: ExpertSession
    call: CallRecord | None
