# Shikiri-yaku (仕切り役)

[![Release](https://img.shields.io/github/v/release/ferrum-code-dev/shikiri-yaku?include_prereleases)](https://github.com/ferrum-code-dev/shikiri-yaku/releases)
[![License](https://img.shields.io/github/license/ferrum-code-dev/shikiri-yaku)](https://github.com/ferrum-code-dev/shikiri-yaku/blob/main/LICENSE)
[![CI](https://github.com/ferrum-code-dev/shikiri-yaku/actions/workflows/ci.yml/badge.svg)](https://github.com/ferrum-code-dev/shikiri-yaku/actions/workflows/ci.yml)

Autonomous Operator for product development with Shikiru.

Shikiri-yaku is a small background service between a Product Owner (PO) and
[Shikiru](https://github.com/ferrum-code-dev/shikiru), which runs coding agents. When Shikiru raises
an escalation, it waits for a person. Shikiri-yaku takes that seat:

1. It watches the escalations of every Shikiru project in a product.
2. It sorts each escalation into a category and hands it to a handler.
3. The handler resolves it, or forwards it to the PO in chat (Telegram first).

Resolving can mean a retry, a wait for a quota reset, an engine restart, or an install of a missing
tool after the owner approves the exact command. Forwarding means one tap applies the answer in Shikiru.

- **Every route ends at the owner.** Anything unknown, unsure or failed is forwarded.
- **Agents propose, code acts.** The Reasoner cannot change anything. Code builds each command and
  runs it only after owner approval.
- **Shikiru is the source of truth.** Shikiri-yaku reads and commands it through MCP.
- **One instance per product,** with its own chat bot.

## Contents

- [Requirements](#requirements)
- [Install](#install)
- [Quick start](#quick-start)
- [Telegram](#telegram)
- [Chat commands](#chat-commands)
- [Approvals and automatic actions](#approvals-and-automatic-actions)
- [Service](#service)
- [Applying changes](#applying-changes)
- [Recovery](#recovery)
- [Safety and limits](#safety-and-limits)
- [Reference](#reference)
- [Contributing](#contributing)

## Requirements

- Python 3.14 and [uv](https://docs.astral.sh/uv/).
- [Shikiru](https://github.com/ferrum-code-dev/shikiru) installed as `shikiru` on `PATH`, for example
  with `uv tool install shikiru-cli`. Each component must be an existing local Shikiru project that
  `shikiru init` has set up.
- A Telegram bot token from [BotFather](https://t.me/BotFather). Keep it private. The bot uses long
  polling, so do not configure a webhook.
- Optional: the local `claude` and/or `codex` CLI, for a Reasoner ladder.

`doctor` fails on a Shikiru older than this release's minimum, and names that minimum.

## Install

```sh
uv tool install shikiri-yaku
```

This repository holds the Python implementation and CLI. For development, see
[CONTRIBUTING.md](CONTRIBUTING.md). A service must run a release, not a checkout. See
[Applying changes](#applying-changes).

## Quick start

An instance lives at `~/.shikiri-yaku/<product>/` by default. This example uses the product `shop`.

1. Create the instance and its secrets directory:

   ```sh
   mkdir -p "$HOME/.shikiri-yaku/shop/secrets"
   ```

2. Save `$HOME/.shikiri-yaku/shop/config.toml`. Set `path` to an existing Shikiru project. Set
   `owners` to your numeric Telegram account ID.

   ```toml
   product = "shop"

   [[components]]
   name = "api"
   path = "~/src/shop-api"

   [channels.telegram]
   token_file = "secrets/telegram.token"
   owners = [123456789]
   ```

3. Put only the bot token in `secrets/telegram.token`, and keep it out of source control. It must be
   a regular file with mode `0600`:

   ```sh
   chmod 600 "$HOME/.shikiri-yaku/shop/secrets/telegram.token"
   ```

4. Check the instance, then start it in the foreground:

   ```sh
   shikiri-yaku --product shop doctor
   shikiri-yaku --product shop run
   ```

`doctor` checks the config, the token-file mode, Telegram, each Shikiru link, and any Reasoner CLI.
It contacts Telegram and Shikiru. `run` stays attached until you stop it. For a background service,
see [Service](#service).

To enable the Reasoner, add a `[reasoner]` table. See [Reasoner settings](docs/reference.md#reasoner).

The optional `[expert]` table configures read-only Expert agents and their ladder. See
[Expert settings](docs/reference.md#expert). A forum group is required for `/ask`.

Every key and default is in the [reference](docs/reference.md#configuration).

## Telegram

Only the user IDs in `channels.telegram.owners` can issue commands or press buttons.

- **Private chat:** each owner must open the bot and press **Start** first.
- **Forum group (optional):** set `group_id` to a forum supergroup. The bot must be an administrator
  with permission to manage topics, and every owner must be a member. With `[expert]`, the bot also
  needs permission to delete messages (`can_delete_messages`), because an Expert conversation ends
  by deleting its topic. By default, the group should hold only the bot and the owners, plus the
  other instances' bots in a shared group, because other members can read its messages.
  Non-owners' messages and button taps are ignored.

Several instances can share one group, each with its own bot. See
[Several instances in one group](docs/reference.md#several-instances-in-one-group). If Telegram
topics go wrong, see [Recovery](#recovery).

## Chat commands

Commands work in private chats and forum topics. Replies go to the chat and topic where the command
was sent.

| Command | What it does |
| --- | --- |
| `/status` | Shows each component's engine state, task counts, quota blocks and Reasoner health. |
| `/open` | Re-shows open questions and pending approvals. |
| `/help` | Lists the commands. Free text also returns this list. |
| `/goal <component> <request>` | Submits a request to that component's Shikiru project. |
| `/ask <question>` | Opens an Expert topic and answers the question. |

In a component topic, the topic names the component, so write `/goal <request>` there:

```text
/goal api Add CSV export to the reports page
```

The request goes to Shikiru as written. If the outcome is uncertain, the bot offers **Submit again**
and never retries on its own. Check the Shikiru project before you tap it, because the first request
may have been accepted. Details are in [Goal outcomes](docs/reference.md#goal-outcomes).

`/ask` in a component topic limits the Expert to that component; in General it can read all
components. In a shared group, address General requests to the bot as `/ask@bot <question>`.
If topic creation is uncertain, ask again; an interrupted turn is not repeated automatically.

When an Expert answer proposes tasks, it lists each task, then offers **Submit all** and **Reject
all** after every task-list message has been sent. If a task-list message is refused, the actions
are not offered. Submit all sends each
task to its component, one at a time, then shows the complete task ids; long results continue in
following messages too. Reject all submits nothing. A tap submits at most once: a second tap, a
tap after a restart, or a tap on a proposal that a newer answer voided says so and submits nothing.
If the bot restarts during Submit all, it says which tasks were submitted, which are unknown and
not sent again, and which were never sent. Check Shikiru before you resubmit any of them. Details are in
[Task proposals](docs/reference.md#task-proposals).

Each message in an Expert topic is a follow-up turn of that conversation, answered after the
turns sent before it. In an Expert topic, `/effort [level]` shows or sets the effort of the next
turn, `/summarize` asks for a short summary, and `/help` lists them. The other commands do not
work in an Expert topic, and the Expert commands do not work elsewhere. `/close` ends the
conversation. So does closing its topic in Telegram, or going unused for `retention_days`. An ending
deletes the topic and the conversation's record and files. If Telegram refuses the deletion, the
conversation stays, the PO is told once, and the deletion is tried again.

## Approvals and automatic actions

An escalation arrives with its question and the choices Shikiru offers. Tap a choice to apply it.
Open questions are reminded after 4 hours, then every 24 hours.

**Install approvals** show the tool, the exact command and its effect. The model cannot write a
command. Code builds it from a fixed recipe, such as `uv tool install` or `brew install`. **Install**
runs that exact command on the Operator host, and only after you tap it. **Decline** runs nothing.
Approvals expire after 24 hours.

The Operator acts on its own only in these cases. Everything else reaches you as a question:

- **Agent and integration errors:** retried a limited number of times per day, then forwarded.
- **Quota exhausted:** retried after the reset, if the reset is within 24 hours. Otherwise forwarded.
- **Engine down:** the bot asks Shikiru to start the engine, up to one restart per component per hour.
- **Missing tool:** installed only after you tap **Install**.
- **Pull requests, finished work and health:** the bot posts notices. The Operator never merges,
  comments on or reviews a pull request.

Full rules are in [Automatic actions](docs/reference.md#automatic-actions).

## Service

After you install the command and create a valid instance, run:

```sh
shikiri-yaku --product shop service install
```

This writes a per-product service unit and starts it, replacing the running service on repeat installs.
Stop any other process holding the instance lock, such as a foreground `run`, first. The unit uses
the `PATH` of the shell that ran the command, so `shikiru` and the Reasoner CLIs are found.

On macOS the unit is a LaunchAgent; on Linux it is a user service. Start and stop it with:

```sh
shikiri-yaku --product shop service start
shikiri-yaku --product shop service stop
```

`service stop` returns once the service has stopped. A stopped service stays down until you run
`service start` or `service restart`, or log in again.

To stop the service and remove its unit, run `service uninstall`. It keeps `config.toml`, the token file, the journal and its backups:

```sh
shikiri-yaku --product shop service uninstall
```

Logs, status and rewritten units are covered in [Service](docs/reference.md#service).

## Applying changes

A running service reads its code and `config.toml` only when it starts.

- **After a config change:** run `shikiri-yaku service restart`.
- **After a code change:** the owner runs the Release workflow, as
  [CONTRIBUTING.md](CONTRIBUTING.md#releasing) describes. Then, on each host:

  ```sh
  uv tool upgrade shikiri-yaku
  shikiri-yaku service restart
  ```

- **Never use `--editable`.** The service would follow unreviewed checkout changes.
- **Rollback** installs an older version with `uv tool install shikiri-yaku==<version>`, then restarts.
  It restores code, not data. See [Rolling back](docs/reference.md#rolling-back).

## Recovery

- **Restart:** the journal survives restarts. The Operator checks Shikiru and the host before it
  resumes interrupted work. An install that was already issued never runs twice.
- **Uncertain Telegram topic:** check the forum yourself. Then adopt the topic's thread ID, or forget
  the uncertain row if no topic was created:

  ```sh
  shikiri-yaku --product shop telegram adopt api 123
  shikiri-yaku --product shop telegram forget api
  ```

  Details are in [Topic recovery](docs/reference.md#topic-recovery).
- **Diagnostics:** `doctor` checks the setup, and `stats` reads the local journal. A foreground `run`
  needs the service stopped first, because both use the instance lock.

More cases, including engine-down signals and clean stops, are in
[Recovery](docs/reference.md#recovery).

## Safety and limits

- The bot sends Shikiru's question, choices, task and component names, and handler notes to Telegram.
  Group members can read posts, including non-owners when `allow_other_members = true`. Only owners
  can act.
- Components must be local Shikiru project directories. Telegram is the only chat channel.
- The Reasoner is optional. Without it, or when every rung fails, escalations go to the PO.
- A Codex rung's read-only sandbox stops writes, not reads. Its model can read any file your user
  account can read.

Local state, data flow and all limits are in the [reference](docs/reference.md#local-state).

## Reference

The [reference](docs/reference.md) covers:

- [Configuration](docs/reference.md#configuration) and every setting and default
- [Command line](docs/reference.md#command-line), including [JSON output](docs/reference.md#json-output)
- [Telegram](docs/reference.md#telegram), [several instances](docs/reference.md#several-instances-in-one-group) and [topic recovery](docs/reference.md#topic-recovery)
- [Questions, approvals and reminders](docs/reference.md#questions-approvals-and-reminders)
- [Service](docs/reference.md#service), [applying changes](docs/reference.md#applying-changes) and [recovery](docs/reference.md#recovery)
- [Local state](docs/reference.md#local-state) and [limits](docs/reference.md#limits)

## Contributing

A change needs the offline quality gate to pass:

```sh
uv run poe check
```

Run `uv run poe fix` first to format the code. The gate uses fakes and local fixtures, not a live
Telegram bot or agent CLI. [CONTRIBUTING.md](CONTRIBUTING.md) covers how to propose a change, the
architecture and safety rules, and the release process.
