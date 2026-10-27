"""What a handler returns for one signal (design §6)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import datetime


@dataclass(frozen=True, slots=True)
class Resolved:
    note: str


@dataclass(frozen=True, slots=True)
class Forward:
    note: str


@dataclass(frozen=True, slots=True)
class WaitUntil:
    wake_at: datetime
    note: str


type Outcome = Resolved | Forward | WaitUntil
