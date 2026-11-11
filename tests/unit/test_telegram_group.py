"""The start-up membership check of a group, against Bot API replies built from the fixtures."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import pytest

from shikiri_yaku.adapters.telegram import (
    BOT_DELETE_RIGHT,
    BOT_TOPIC_RIGHTS,
    GROUP_HAS_OTHER_MEMBERS,
    GROUP_MEMBER_COUNT_UNAVAILABLE,
    OWNER_NOT_MEMBER,
    UNEXPECTED_REPLY,
    probe_bot,
)
from shikiri_yaku.ports import ChannelError, ChannelUnavailableError

if TYPE_CHECKING:
    from collections.abc import Mapping

_FIXTURES: Final = Path(__file__).resolve().parents[1] / "fixtures" / "telegram"
GROUP_ID: Final = -1009
BOT_ID: Final = 4242
OWNER_ID: Final = 123456789
OWNERS: Final = (OWNER_ID,)
OTHER_BOT_ID: Final = 5001
SECOND_OTHER_BOT_ID: Final = 5002
HUMAN_ID: Final = 987654321
_FIXED_REPLIES: Final = {
    "getMe": "get_me_ok.json",
    "getWebhookInfo": "get_webhook_info.json",
    "getChat": "forum_group.json",
}


def _reply(name: str) -> dict[str, Any]:
    reply: dict[str, Any] = json.loads((_FIXTURES / name).read_text(encoding="utf-8"))
    return reply


def _administrator(user_id: int, *, is_bot: bool) -> dict[str, Any]:
    entry: dict[str, Any] = _reply("forum_bot_member.json")["result"]
    entry["user"] = {"id": user_id, "is_bot": is_bot, "first_name": "Administrator"}
    return entry


BOT_ADMIN: Final = _administrator(BOT_ID, is_bot=True)
OTHER_BOT_ADMIN: Final = _administrator(OTHER_BOT_ID, is_bot=True)
SECOND_OTHER_BOT_ADMIN: Final = _administrator(SECOND_OTHER_BOT_ID, is_bot=True)
OWNER_ADMIN: Final = _administrator(OWNER_ID, is_bot=False)
HUMAN_ADMIN: Final = _administrator(HUMAN_ID, is_bot=False)


class _Group:
    """The Bot API for one forum group: the bot and the owners answer from the fixtures.

    `administrators` is the list getChatAdministrators returns, and `count` the number that
    getChatMemberCount returns. `replies` overrides the reply to a method by name.
    """

    def __init__(
        self,
        *,
        administrators: list[dict[str, Any]],
        count: int,
        manage_topics: bool = True,
        delete_messages: bool = False,
        owner_member: bool = True,
        replies: Mapping[str, object] | None = None,
    ) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._administrators = administrators
        self._count = count
        self._manage_topics = manage_topics
        self._delete_messages = delete_messages
        self._owner_member = owner_member
        self._replies: Mapping[str, object] = replies or {}

    async def call(self, method: str, payload: Mapping[str, object]) -> object:
        self.calls.append((method, dict(payload)))
        if method in self._replies:
            return self._replies[method]
        if method == "getChatMember":
            return self._member(payload["user_id"])
        if method == "getChatAdministrators":
            return {"ok": True, "result": self._administrators}
        if method == "getChatMemberCount":
            return {"ok": True, "result": self._count}
        return _reply(_FIXED_REPLIES[method])

    def _member(self, user_id: object) -> dict[str, Any]:
        if user_id == BOT_ID:
            reply = _reply("forum_bot_member.json")
            reply["result"]["can_manage_topics"] = self._manage_topics
            reply["result"]["can_delete_messages"] = self._delete_messages
            return reply
        reply = _reply("forum_owner_member.json")
        if not self._owner_member:
            reply["result"]["status"] = "left"
        return reply


def _probe(
    api: _Group,
    *,
    shared_group: bool = True,
    allow_other_members: bool = False,
    delete_right: bool = False,
) -> None:
    asyncio.run(
        probe_bot(
            api,
            GROUP_ID,
            OWNERS,
            allow_other_members=allow_other_members,
            shared_group=shared_group,
            delete_right=delete_right,
        )
    )


def _called(api: _Group) -> list[str]:
    return [method for method, _ in api.calls]


@pytest.mark.parametrize(
    ("administrators", "count"),
    [
        pytest.param([BOT_ADMIN], 2, id="own-bot-only"),
        pytest.param([BOT_ADMIN, OTHER_BOT_ADMIN], 3, id="one-other-bot"),
        pytest.param([BOT_ADMIN, OTHER_BOT_ADMIN, SECOND_OTHER_BOT_ADMIN], 4, id="two-other-bots"),
        pytest.param([BOT_ADMIN, OWNER_ADMIN], 2, id="owner-administrator-counted-once"),
    ],
)
def test_shared_group_admits_the_administrator_bots_and_the_owners(
    administrators: list[dict[str, Any]], count: int
) -> None:
    api = _Group(administrators=administrators, count=count)

    _probe(api)

    assert ("getChatAdministrators", {"chat_id": GROUP_ID, "return_bots": True}) in api.calls


@pytest.mark.parametrize(
    ("administrators", "count"),
    [
        pytest.param([BOT_ADMIN], 3, id="unidentified-extra-member"),
        pytest.param([BOT_ADMIN, HUMAN_ADMIN], 3, id="person-as-administrator"),
        pytest.param([BOT_ADMIN, OTHER_BOT_ADMIN, HUMAN_ADMIN], 4, id="person-beside-other-bot"),
        pytest.param([BOT_ADMIN, OTHER_BOT_ADMIN], 4, id="member-beside-other-bot"),
        pytest.param([BOT_ADMIN, OTHER_BOT_ADMIN], 2, id="count-below-administrators"),
    ],
)
def test_shared_group_refuses_any_member_beyond_the_owners_and_administrator_bots(
    administrators: list[dict[str, Any]], count: int
) -> None:
    with pytest.raises(ChannelError) as raised:
        _probe(_Group(administrators=administrators, count=count))

    assert type(raised.value) is ChannelError
    assert str(raised.value) == GROUP_HAS_OTHER_MEMBERS


def test_without_a_shared_group_only_the_owners_and_own_bot_are_counted() -> None:
    api = _Group(administrators=[BOT_ADMIN, OTHER_BOT_ADMIN], count=2)

    _probe(api, shared_group=False)

    assert "getChatAdministrators" not in _called(api)


def test_without_a_shared_group_another_bot_administrator_is_refused() -> None:
    api = _Group(administrators=[BOT_ADMIN, OTHER_BOT_ADMIN], count=3)

    with pytest.raises(ChannelError) as raised:
        _probe(api, shared_group=False)

    assert type(raised.value) is ChannelError
    assert str(raised.value) == GROUP_HAS_OTHER_MEMBERS
    assert "getChatAdministrators" not in _called(api)


def test_allow_other_members_skips_the_membership_checks() -> None:
    api = _Group(administrators=[BOT_ADMIN, HUMAN_ADMIN], count=9)

    _probe(api, allow_other_members=True)

    assert "getChatAdministrators" not in _called(api)
    assert "getChatMemberCount" not in _called(api)


@pytest.mark.parametrize(
    ("manage_topics", "owner_member", "refusal"),
    [
        pytest.param(False, True, BOT_TOPIC_RIGHTS, id="bot-without-manage-topics"),
        pytest.param(True, False, OWNER_NOT_MEMBER, id="owner-not-a-member"),
    ],
)
def test_allow_other_members_still_checks_the_bot_and_the_owners(
    *, manage_topics: bool, owner_member: bool, refusal: str
) -> None:
    api = _Group(
        administrators=[BOT_ADMIN],
        count=2,
        manage_topics=manage_topics,
        owner_member=owner_member,
    )

    with pytest.raises(ChannelError) as raised:
        _probe(api, allow_other_members=True)

    assert type(raised.value) is ChannelError
    assert str(raised.value) == refusal


@pytest.mark.parametrize(
    ("reply", "message"),
    [
        pytest.param(
            {"ok": False, "error_code": 429, "parameters": None},
            GROUP_MEMBER_COUNT_UNAVAILABLE,
            id="rate-limited",
        ),
        pytest.param(
            {"ok": False, "error_code": 500, "parameters": None},
            GROUP_MEMBER_COUNT_UNAVAILABLE,
            id="temporary-server-error",
        ),
        pytest.param({"ok": True, "result": "nope"}, UNEXPECTED_REPLY, id="not-a-list"),
        pytest.param(
            {"ok": True, "result": [{"status": "administrator"}]},
            UNEXPECTED_REPLY,
            id="administrator-without-a-user",
        ),
    ],
)
def test_an_administrator_reply_that_is_not_usable_makes_the_check_unavailable(
    reply: object, message: str
) -> None:
    api = _Group(administrators=[], count=2, replies={"getChatAdministrators": reply})

    with pytest.raises(ChannelUnavailableError) as raised:
        _probe(api)

    assert type(raised.value) is ChannelUnavailableError
    assert str(raised.value) == message


@pytest.mark.parametrize("error_code", [400, 403], ids=["bad-request", "forbidden"])
def test_a_definite_administrator_refusal_fails_the_membership_check(error_code: int) -> None:
    api = _Group(
        administrators=[],
        count=2,
        replies={
            "getChatAdministrators": {
                "ok": False,
                "error_code": error_code,
                "parameters": None,
            }
        },
    )

    with pytest.raises(ChannelError) as raised:
        _probe(api)

    assert type(raised.value) is ChannelError
    assert str(raised.value) == GROUP_MEMBER_COUNT_UNAVAILABLE


@pytest.mark.parametrize(
    ("delete_right", "delete_messages", "refused"),
    [
        pytest.param(False, False, False, id="not-required-without-expert"),
        pytest.param(True, False, True, id="missing-with-expert"),
        pytest.param(True, True, False, id="granted-with-expert"),
    ],
)
def test_expert_topics_need_the_right_to_delete_messages(
    *, delete_right: bool, delete_messages: bool, refused: bool
) -> None:
    api = _Group(administrators=[BOT_ADMIN], count=2, delete_messages=delete_messages)

    if refused:
        with pytest.raises(ChannelError) as raised:
            _probe(api, shared_group=False, delete_right=delete_right)
        assert str(raised.value) == BOT_DELETE_RIGHT
        assert "can_delete_messages" in str(raised.value)
    else:
        _probe(api, shared_group=False, delete_right=delete_right)
