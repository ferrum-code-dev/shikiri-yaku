Replies of the Telegram Bot API for the Channel's tests, written by hand from the documented shape of
each reply (Bot API "Making requests", "Message", "Available methods"). Nothing here was recorded
from Telegram: a recording needs the bot token and the network, and the gate has neither.

The wording of each `description` is not checked against Telegram. The adapter does not match on
it: a 400 is a refusal, and its description is the reason kept in the journal.

To replace these with real replies, a human runs the Bot API against a test bot and copies the
bodies here. Tests never read the token file.
