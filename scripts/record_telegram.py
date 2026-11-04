r"""Records real Bot API replies from the PO's test bot as fixtures.

A human runs it; the quality gate never does. The script sends a harmless message to the PO's chat
with the bot, edits it, waits while the PO replies to it and taps its button, reads the updates,
deletes the message, and creates and closes a forum topic in the group. Each reply is saved as JSON
under tests/fixtures/telegram/recorded/ with the token and the ids replaced by placeholders. It then
prints how each one differs from the documentation-based fixture of the same name.

    uv run python scripts/record_telegram.py --token-file secrets/test-bot.token \
        --chat 123456789 --group -1001234567890

Use a bot made for this, not the production bot, and stop the Operator first: a bot's updates are
read by one consumer at a time. The token file is read here and by the Bot API transport this script
uses; the token is never printed or saved. Nothing is written when a reply, or a file already in the
output directory, would still hold the token or an id.
"""

from __future__ import annotations

import argparse
import asyncio
import difflib
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Final

from pydantic import BaseModel, ConfigDict, StrictBool, StrictInt, ValidationError

from shikiri_yaku.adapters.telegram_http import HttpBotApi
from shikiri_yaku.adapters.telegram_redact import (
    RecordingLeakError,
    Secrets,
    holds_secret,
    redact_recording,
)
from shikiri_yaku.domain.secrets import secret_form
from shikiri_yaku.ports import ChannelError

if TYPE_CHECKING:
    from collections.abc import Mapping

ROOT: Final = Path(__file__).resolve().parents[1]
FIXTURES: Final = ROOT / "tests" / "fixtures" / "telegram"
RECORDED: Final = FIXTURES / "recorded"
BOT_TOKEN_FORM: Final = "telegram_bot_token"  # noqa: S105 -- the name of a secret form, not a secret
TIMEOUT_S: Final = 30
ALLOWED_UPDATES: Final = ["message", "callback_query"]
BUTTONS: Final = {"inline_keyboard": [[{"text": "1", "callback_data": "recording:1"}]]}
SEND_TEXT: Final = "Recording check: a harmless message from the test bot. Reply to it and tap 1."
EDIT_TEXT: Final = "Recording check: edited. Reply to it and tap 1."
PROMPT: Final = "In the chat, reply to the edited message and tap its button 1, then press Enter: "
TOPIC_NAME: Final = "recording check"


class RecordingError(Exception):
    pass


class _Envelope(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    ok: StrictBool
    description: str = ""


class _MessageRef(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    message_id: StrictInt


class _Sent(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    result: _MessageRef


class _ThreadRef(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    message_thread_id: StrictInt


class _Topic(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    result: _ThreadRef


async def _call(
    api: HttpBotApi,
    replies: dict[str, object],
    name: str,
    method: str,
    payload: Mapping[str, object],
) -> object:
    reply = await api.call(method, payload)
    replies[name] = reply
    try:
        envelope = _Envelope.model_validate(reply)
    except ValidationError:
        message = f"{method} did not reply in the Bot API's shape"
        raise RecordingError(message) from None
    if not envelope.ok:
        message = f"{method} was refused: {envelope.description}"
        raise RecordingError(message)
    return reply


async def _record(api: HttpBotApi, chat: int, group: int) -> dict[str, object]:
    replies: dict[str, object] = {}
    await _call(api, replies, "get_me_ok", "getMe", {})
    sent = await _call(
        api,
        replies,
        "send_ok",
        "sendMessage",
        {"chat_id": chat, "text": SEND_TEXT, "reply_markup": BUTTONS},
    )
    message_id = _Sent.model_validate(sent).result.message_id
    await _call(
        api,
        replies,
        "edit_ok",
        "editMessageText",
        {"chat_id": chat, "message_id": message_id, "text": EDIT_TEXT, "reply_markup": BUTTONS},
    )
    await asyncio.to_thread(input, PROMPT)
    await _call(
        api,
        replies,
        "get_updates_ok",
        "getUpdates",
        {"timeout": 0, "allowed_updates": ALLOWED_UPDATES},
    )
    await _call(
        api, replies, "delete_ok", "deleteMessage", {"chat_id": chat, "message_id": message_id}
    )
    created = await _call(
        api,
        replies,
        "forum_topic_created",
        "createForumTopic",
        {"chat_id": group, "name": TOPIC_NAME},
    )
    thread = _Topic.model_validate(created).result.message_thread_id
    await _call(
        api,
        replies,
        "forum_topic_closed",
        "closeForumTopic",
        {"chat_id": group, "message_thread_id": thread},
    )
    return replies


def _read_token(path: Path) -> str:
    try:
        token = path.read_text(encoding="utf-8").strip()
    except OSError:
        message = "the token file cannot be read"
        raise RecordingError(message) from None
    if secret_form([token]) != BOT_TOKEN_FORM:
        message = "the token file does not hold a Telegram bot token"
        raise RecordingError(message)
    return token


def _recorded_values() -> list[object]:
    try:
        return [json.loads(path.read_text(encoding="utf-8")) for path in RECORDED.glob("*.json")]
    except json.JSONDecodeError:
        message = "a file in the output directory is not a recorded reply"
        raise RecordingError(message) from None


def _dump(value: object) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False) + "\n"


def _save(redacted: Mapping[str, object]) -> None:
    RECORDED.mkdir(parents=True, exist_ok=True)
    for name, value in redacted.items():
        (RECORDED / f"{name}.json").write_text(_dump(value), encoding="utf-8")


def _report(redacted: Mapping[str, object]) -> None:
    for name, value in redacted.items():
        documented = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
        before = json.dumps(documented, indent=2, sort_keys=True).splitlines()
        after = json.dumps(value, indent=2, sort_keys=True).splitlines()
        diff = list(
            difflib.unified_diff(
                before,
                after,
                f"fixtures/telegram/{name}.json",
                f"recorded/{name}.json",
                lineterm="",
            )
        )
        sys.stdout.write("\n".join(diff) + "\n" if diff else f"{name}: same as the fixture\n")


async def _main(arguments: argparse.Namespace) -> int:
    token = _read_token(arguments.token_file)
    bot_id = int(token.partition(":")[0])
    secrets = Secrets(token, frozenset({arguments.chat, arguments.group, bot_id}))
    if any(holds_secret(value, secrets) for value in _recorded_values()):
        message = "a file in the output directory would hold the token or an id"
        raise RecordingError(message)
    api = HttpBotApi(arguments.token_file, TIMEOUT_S)
    replies = await _record(api, arguments.chat, arguments.group)
    redacted = redact_recording(replies, secrets)
    _save(redacted)
    _report(redacted)
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--token-file",
        type=Path,
        required=True,
        help="file holding the test bot's token; only this script and the Bot API transport "
        "read it, and the token is never printed or saved",
    )
    parser.add_argument(
        "--chat",
        type=int,
        required=True,
        help="id of the PO's private chat with the test bot; in a private chat it is also "
        "the PO's user id",
    )
    parser.add_argument(
        "--group",
        type=int,
        required=True,
        help="id of a forum supergroup in which the test bot is an administrator with the "
        "manage-topics right",
    )
    return parser


def main() -> int:
    arguments = _parser().parse_args()
    try:
        return asyncio.run(_main(arguments))
    except (RecordingError, ChannelError) as error:
        sys.stderr.write(f"FAILED: {error}\n")
        return 1
    except ValidationError:
        sys.stderr.write("FAILED: a reply did not have the shape this script reads\n")
        return 1
    except RecordingLeakError:
        sys.stderr.write(
            "FAILED: a reply would still hold the token or an id; nothing was written\n"
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
