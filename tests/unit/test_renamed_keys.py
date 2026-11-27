"""An upgraded journal renames the keys written under the old command names, once.

A tap, a settle and a replay look up the notice or reply they belong to by its key. A copy sent
before the rename carries the old key, so the repair writes the new key in its place; otherwise the
copy would keep its buttons but not its final edit, and a replayed update would be answered again.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

import pytest

from shikiri_yaku.adapters.telegram import telegram_place
from shikiri_yaku.domain.outbound import Purpose
from shikiri_yaku.journal import Repairs
from shikiri_yaku.ports import Message
from tests.unit.test_replies import NOW, Harness

if TYPE_CHECKING:
    from collections.abc import Generator


@pytest.fixture
def h() -> Generator[Harness]:
    harness = Harness()
    yield harness
    harness.database.close()


_RENAMES: Final = [
    ("ask:select:5", "expert:select:5"),
    ("ask:open:5:0", "expert:open:5:0"),
    ("ask:answer:5:actions", "expert:answer:5:actions"),
    ("ask:proposal:5:result:0", "expert:proposal:5:result:0"),
    ("ask:interrupted:5", "expert:interrupted:5"),
    ("goal:9", "submit:9"),
    ("goal:usage:5", "submit:usage:5"),
    ("open:5:done", "pending:5:done"),
    ("open:5:3", "pending:5:3"),
    ("install:7:open:5", "install:7:pending:5"),
    ("expert:select:6", "expert:select:6"),
    ("install:7:pending:5", "install:7:pending:5"),
    ("forward:3", "forward:3"),
]


def _notice_keys(h: Harness) -> list[str]:
    rows = h.database.connection.execute(
        "SELECT notice_key FROM messages WHERE notice_key IS NOT NULL ORDER BY id"
    ).fetchall()
    return [row["notice_key"] for row in rows]


def _reply_keys(h: Harness) -> list[str]:
    rows = h.database.connection.execute("SELECT reply_key FROM command_replies").fetchall()
    return [row["reply_key"] for row in rows]


@pytest.mark.parametrize(("old", "new"), _RENAMES)
def test_a_key_under_an_old_command_name_is_renamed_in_notices_and_replies(
    h: Harness, old: str, new: str
) -> None:
    h.messages.record_outbound(
        signal_number=h.number,
        place=telegram_place("1001"),
        purpose=Purpose.NOTICE,
        body=Message("notice"),
        notice_key=old,
        now=NOW,
    )
    h.database.connection.execute("INSERT INTO command_replies (reply_key) VALUES (?)", (old,))

    Repairs(h.database).rename_keys()

    assert _notice_keys(h) == [new]
    assert _reply_keys(h) == [new]


def test_the_rename_is_recorded_so_a_later_call_renames_nothing(h: Harness) -> None:
    Repairs(h.database).rename_keys()
    h.database.connection.execute(
        "INSERT INTO command_replies (reply_key) VALUES (?)", ("open:5:done",)
    )

    assert Repairs(h.database).rename_keys() == 0
    assert _reply_keys(h) == ["open:5:done"]
