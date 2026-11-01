Replies of the Telegram Bot API for the Channel's tests. These fixtures are documentation-based, not
recorded: nothing here came from Telegram. Each file is written by hand from the documented shape of
the reply it stands for. A recording needs the bot token and the network, and the gate has neither.

| File | Stands for | Source (documentation-based, not recorded) |
| --- | --- | --- |
| `send_ok.json` | the reply to `sendMessage`: the sent Message with its id and chat | Bot API, "Available methods" (sendMessage) and "Message" |
| `edit_ok.json` | the reply to `editMessageText`: the edited Message | Bot API, "Available methods" (editMessageText) and "Message" |
| `delete_ok.json` | the reply to `deleteMessage`: `true` | Bot API, "Available methods" (deleteMessage) |
| `message_too_long.json` | a failed call with `error_code` 400; the description is hand-written | Bot API, "Making requests" (the error object) |
| `rate_limited.json` | a failed call with `error_code` 429 and `parameters.retry_after` | Bot API, "Making requests" (ResponseParameters) |
| `get_updates_ok.json` | the reply to `getUpdates`: a text, a reply and a button tap | Bot API, "Getting updates" (getUpdates, Update), "Available types" (Message, CallbackQuery) |
| `get_updates_conflict.json` | a refused `getUpdates` with `error_code` 409 while a webhook is set; the description is hand-written | Bot API, "Making requests" (the error object), "Getting updates" (a webhook and getUpdates cannot be used together) |
| `get_me_ok.json` | the reply to `getMe`: the User object of the bot | Bot API, "Available methods" (getMe) and "User" |
| `forum_topic_created.json` | the reply to `createForumTopic`: a ForumTopic | Bot API, "Available methods" (createForumTopic) and "ForumTopic" |
| `forum_topic_closed.json` | the reply to `closeForumTopic`: `true` | Bot API, "Available methods" (closeForumTopic) |
| `forum_group.json` | the reply to `getChat`: a forum supergroup ChatFullInfo, including its required `accent_color_id`, `max_reaction_count`, and `accepted_gift_types` fields | Bot API, "Available methods" (getChat), "ChatFullInfo", and "AcceptedGiftTypes" |
| `forum_bot_member.json`, `forum_owner_member.json` | replies to `getChatMember`: administrator and member ChatMember objects | Bot API, "Available methods" (getChatMember), "ChatMemberAdministrator", and "ChatMemberMember" |
| `forum_member_count.json` | the reply to `getChatMemberCount`: an integer | Bot API, "Available methods" (getChatMemberCount) |
| `get_webhook_info.json` | the reply to `getWebhookInfo` with no webhook: `url` is empty; a test sets a URL to stand for a webhook | Bot API, "Available methods" (getWebhookInfo) and "WebhookInfo" |

In A10d the official page was read in part: the getMe entry, the required User fields, the response
shape (`ok`, `result`, `description`, `error_code`, `parameters`) and ResponseParameters with
`retry_after`. The part read states no HTTP status code for error replies, so the status codes above
come from third-party copies, not from the page. No `description` is checked against Telegram. The
adapter reads a `description` in two places only: a refused `getUpdates` shows it, with the code, in
the health message (C8); a `getWebhookInfo` reply with a webhook URL names only that URL's host.
Elsewhere a refusal carries the Channel's own reason, and platform text is never kept.

`get_updates_ok.json` was written from the documented Update, Message and CallbackQuery shapes
without reading the page in this change; the reply-to chat and message fields follow the same Message type.

To replace these with real replies, a human runs `scripts/record_telegram.py` with the PO's test bot.
It saves the replies under `recorded/`, with the token and the ids replaced by placeholders, and
prints how each one differs from the fixture here. Tests never read the token file or run the script.
