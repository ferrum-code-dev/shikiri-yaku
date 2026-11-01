"""Replaces the bot token and the ids of a recorded Bot API reply with placeholders (design §12).

The recording script saves real replies as fixtures. Nothing it writes may hold the token, or an id
that names the PO's chats or the test bot, so each one is replaced everywhere a payload can hold it:
in strings, in keys and in numbers. A payload without them comes back unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, cast

TOKEN_PLACEHOLDER: Final = "<token>"  # noqa: S105 -- a placeholder, not the token
# keep: an integer keeps the Bot API's type, so a recorded reply still parses as before
ID_PLACEHOLDER: Final = 0


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
    if isinstance(value, int) and value in secrets.ids:
        return ID_PLACEHOLDER
    return value


def leaks(text: str, secrets: Secrets) -> bool:
    """Return whether the token or one of the ids still appears in the text."""
    return secrets.token in text or any(str(number) in text for number in secrets.ids)


def _text(text: str, secrets: Secrets) -> str:
    # keep: the token goes first, since it carries the bot's id as its prefix
    replaced = text.replace(secrets.token, TOKEN_PLACEHOLDER)
    for number in secrets.ids:
        replaced = replaced.replace(str(number), str(ID_PLACEHOLDER))
    return replaced
