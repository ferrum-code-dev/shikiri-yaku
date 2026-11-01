"""Replaces the bot token and the ids of recorded Bot API replies with placeholders (design §12).

The recording script saves real replies as fixtures. Nothing it writes may hold the token, or an id
that names a chat or a user. getUpdates also returns messages from people other than the PO, so the
ids are not only the ones the script knows: every id a reply names under an id key is replaced, and
so is every occurrence of it in strings and numbers. A payload without them comes back unchanged.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, cast

from shikiri_yaku.domain.secrets import secret_form

if TYPE_CHECKING:
    from collections.abc import Mapping

TOKEN_PLACEHOLDER: Final = "<token>"  # noqa: S105 -- a placeholder, not the token
# keep: an integer keeps the Bot API's type, so a recorded reply still parses as before
ID_PLACEHOLDER: Final = 0
ID_KEYS: Final = frozenset({"id", "user_id", "chat_id"})


class RecordingLeakError(Exception):
    """A reply would still hold the token or an id after redaction, so nothing may be written."""


@dataclass(frozen=True)
class Secrets:
    """The bot token, which must not be empty, and the ids that name the PO's chats and the bot."""

    token: str
    ids: frozenset[int]


def redact(value: object, secrets: Secrets) -> object:
    """Return the value with the token and the ids replaced, wherever a payload can hold them."""
    if isinstance(value, dict):
        pairs = cast("dict[str, object]", value).items()
        return {_text(key, secrets): redact(item, secrets) for key, item in pairs}
    if isinstance(value, list):
        return [redact(item, secrets) for item in cast("list[object]", value)]
    if isinstance(value, str):
        return _text(value, secrets)
    if isinstance(value, int) and not isinstance(value, bool) and value in secrets.ids:
        return ID_PLACEHOLDER
    return value


def discover(value: object) -> frozenset[int]:
    """Return every user or chat id that the payload names under an id key, however deep."""
    if isinstance(value, dict):
        found: set[int] = set()
        for key, item in cast("dict[str, object]", value).items():
            if key in ID_KEYS and isinstance(item, int) and not isinstance(item, bool):
                found.add(item)
            found |= discover(item)
        return frozenset(found)
    if isinstance(value, list):
        items = cast("list[object]", value)
        return frozenset(identity for item in items for identity in discover(item))
    return frozenset()


def redact_recording(replies: Mapping[str, object], secrets: Secrets) -> dict[str, object]:
    """Return the replies with the token and their ids replaced, or refuse if one remains."""
    named = frozenset(identity for reply in replies.values() for identity in discover(reply))
    everything = Secrets(secrets.token, secrets.ids | named)
    redacted = {name: redact(reply, everything) for name, reply in replies.items()}
    if any(holds_secret(value, secrets) for value in redacted.values()):
        raise RecordingLeakError
    return redacted


def holds_secret(value: object, secrets: Secrets) -> bool:
    """Return whether a payload still holds the token, another bot token, or an unreplaced id."""
    text = json.dumps(value)
    return (
        secret_form([text]) is not None
        or leaks(text, secrets)
        or bool(discover(value) - {ID_PLACEHOLDER})
    )


def leaks(text: str, secrets: Secrets) -> bool:
    """Return whether the token or one of the ids still appears in the text."""
    return secrets.token in text or any(str(number) in text for number in secrets.ids)


def _text(text: str, secrets: Secrets) -> str:
    # keep: the token goes first, since it carries the bot's id as its prefix
    replaced = text.replace(secrets.token, TOKEN_PLACEHOLDER)
    # keep: longer ids go first, so an id that is a part of another one does not split it
    for number in sorted(secrets.ids, key=lambda identity: len(str(identity)), reverse=True):
        replaced = replaced.replace(str(number), str(ID_PLACEHOLDER))
    return replaced
