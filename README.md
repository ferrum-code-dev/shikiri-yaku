# Shikiri-yaku (仕切り役)

[![Release](https://img.shields.io/github/v/release/ferrum-code-dev/shikiri-yaku?include_prereleases)](https://github.com/ferrum-code-dev/shikiri-yaku/releases)
[![License](https://img.shields.io/github/license/ferrum-code-dev/shikiri-yaku)](https://github.com/ferrum-code-dev/shikiri-yaku/blob/main/LICENSE)
[![CI](https://github.com/ferrum-code-dev/shikiri-yaku/actions/workflows/ci.yml/badge.svg)](https://github.com/ferrum-code-dev/shikiri-yaku/actions/workflows/ci.yml)

An autonomous Operator for product development with Shikiru.

Shikiri-yaku is a small background service between a Product Owner and
[Shikiru](https://github.com/ferrum-code-dev/shikiru), the coordinator that runs coding agents.
Shikiru stops and waits for a person when it raises an escalation. Shikiri-yaku takes that seat:

1. It watches the escalations of every Shikiru project of a product.
2. It sorts each escalation into a category and hands it to a handler.
3. The handler either resolves it or forwards it to the Product Owner in a chat (Telegram first).

Resolving means a retry, a wait for a quota reset, an engine restart, or an install of a missing
tool after the owner approves the exact command. When forwarded, one tap applies the answer in
Shikiru.

- **Small fixed core, growth by extension.** Categories, handlers, actions, reasoning jobs and
  channels are registered extensions. The current chat commands are listed in
  [Chat commands](#chat-commands).
- **Every route ends at the owner.** Anything unknown, unsure or failed is forwarded, so nothing is
  dropped and nothing works worse than plain forwarding.
- **Agents propose, code acts.** The reasoning agent may look things up but cannot change anything,
  and only names what is missing. Plain code builds each command and runs it only after the owner
  approved it.
- **Shikiru stays the source of truth,** read and commanded through MCP. Nothing of the Operator is
  stored in the managed repositories.
- **One instance per product,** with its own chat bot. Each component is a Shikiru project.
- **Small by design:** one Python process, SQLite, one TOML file and the OS service manager.

## Install and first run

This repository contains the Python implementation and command-line interface. For development,
see [CONTRIBUTING.md](CONTRIBUTING.md).

### Prerequisites

- Python 3.14 and [uv](https://docs.astral.sh/uv/).
- [Shikiru](https://github.com/ferrum-code-dev/shikiru) installed and available as `shikiru` on
  `PATH`, for example with `uv tool install shikiru-cli`.
  - Each configured component must be an existing local Shikiru project directory, one that
    `shikiru init` has set up. Shikiri-yaku connects with `shikiru --project <path> mcp`.
  - `doctor` fails on a Shikiru older than this release's minimum, and names that minimum.
- A Telegram bot token from [BotFather](https://t.me/BotFather). Keep it private. The bot uses long
  polling, so do not configure a webhook for it.
- Optional: the local `claude` and/or `codex` CLI, for a configured Reasoner ladder.

### Install

Install the console command `shikiri-yaku` from PyPI:

```sh
uv tool install shikiri-yaku
```

A service must run a release, not a checkout (see [Applying changes](#applying-changes)).

### Create an instance

By default, a product's instance lives at `~/.shikiri-yaku/<product>/`. For a product named `shop`:

1. Create the instance and its secrets directory:

   ```sh
   mkdir -p "$HOME/.shikiri-yaku/shop/secrets"
   ```

2. Save the TOML below as `$HOME/.shikiri-yaku/shop/config.toml`. Replace the component path with
   the absolute or `~` path of an existing Shikiru project, and the owner ID with your numeric
   Telegram account ID.

   ```toml
   product = "shop"

   [[components]]
   name = "api"
   path = "~/src/shop-api"

   [channels.telegram]
   token_file = "secrets/telegram.token"
   owners = [123456789]
   ```

3. Put only the bot token in `$HOME/.shikiri-yaku/shop/secrets/telegram.token`, and keep the file out
   of source control. It must be a regular file with mode `0600`:

   ```sh
   chmod 600 "$HOME/.shikiri-yaku/shop/secrets/telegram.token"
   ```

### Telegram destinations

- **Owners.** `owners` lists the numeric Telegram user IDs that may act on messages; several are
  allowed. For private-chat delivery, each owner must open the bot and press **Start** first.
- **Forum group (optional).** Add `group_id` under `[channels.telegram]`, set to the numeric ID of a
  forum supergroup.
  - The bot must be an administrator with permission to manage topics.
  - Every configured owner must belong to the group.
  - By default, the group must contain only the bot and configured owners, because other members can
    read its messages.
  - `allow_other_members = true` allows additional readers. Only configured owners can still act on
    messages.
  - To share the group with other instances, see
    [Several instances in one group](#several-instances-in-one-group).

### First run

Once the config and protected token file are in place, check the instance, then start it in the
foreground:

```sh
shikiri-yaku --product shop doctor
shikiri-yaku --product shop run
```

- `doctor` checks the local config and token-file mode, Telegram readiness, each Shikiru project
  link, and any configured Reasoner CLIs. It contacts Telegram and Shikiru.
- `run` stays attached until stopped.

### Optional Reasoner

To enable the Reasoner, add a `[reasoner]` table with an ordered `ladder`:

```toml
[reasoner]
ladder = ["claude:<model>:<effort>", "codex:<model>"]
```

Replace the placeholders with model names, and effort values, that the installed CLI accepts. The
full rules are in [`[reasoner]`](#reasoner) below.

## Configuration reference

`config.toml` accepts only the sections and keys listed here. A misspelled or unknown key is an
error.

- **Names.** Product and component names start with an ASCII letter or digit, then may contain
  letters, digits, `_` or `-`. Component names must be unique. At least one component is required.
- **Components run on this machine.** `path` must be an absolute path or begin with `~`. Remote
  `host` components are not supported.
- **Removed components.** A component removed from the file has its open signals closed, with a
  note, at the next start.
- **`token_file`** defaults to `secrets/telegram.token`, relative to the instance directory. It may
  be another relative path there, or an absolute path.
- **`[channels.telegram]`** is required even when using defaults. `owners` has no default. If
  `group_id` is set, it must identify a Telegram forum supergroup.

| Section | Keys and defaults |
| --- | --- |
| Root | `product` (required); `components` (required, one or more `[[components]]` entries) |
| `[[components]]` | `name` (required), `path` (required); `host` is unsupported |
| `[channels.telegram]` | `owners` (required, one or more integer IDs); `token_file = "secrets/telegram.token"`; `group_id` (optional); `allow_other_members = false`; `shared_group = false` (requires `group_id`) |
| `[operator]` | All keys below are optional; every value is a positive integer |
| `[handlers.infrastructure]` | All keys below are optional; every value is a positive integer |
| `[handlers.product]` | All keys below are optional; every value is a positive integer |
| `[reasoner]` | Optional; `ladder` is required when this section is present |

### `[operator]`

All keys are optional. `telegram_call_timeout_s` must be greater than `telegram_poll_wait_s`.

| Setting | Default | Setting | Default |
| --- | ---: | --- | ---: |
| `event_wait_s` | 30 | `liveness_interval_s` | 60 |
| `scheduler_interval_s` | 30 | `outbox_interval_s` | 2 |
| `handler_timeout_s` | 900 | `dispatch_interval_s` | 2 |
| `forward_retry_s` | 60 | `stop_grace_min` | 2 |
| `lost_command_min` | 2 | `link_unreachable_min` | 10 |
| `link_backoff_initial_s` | 1 | `link_backoff_max_s` | 60 |
| `link_call_timeout_s` | 30 | `link_start_timeout_s` | 120 |
| `telegram_call_timeout_s` | 30 | `telegram_poll_wait_s` | 20 |
| `inbound_interval_s` | 1 | `command_name_max_chars` | 32 |

### `[handlers.infrastructure]`

All keys are optional.

| Setting | Default | Setting | Default |
| --- | ---: | --- | ---: |
| `command_timeout_min` | 10 | `quota_wait_max_h` | 24 |
| `quota_wait_margin_min` | 1 | `retry_budget_per_day` | 1 |
| `restart_budget_per_hour` | 1 | `restart_confirm_s` | 60 |
| `restart_poll_s` | 5 | `approval_expiry_h` | 24 |
| `host_lock_poll_s` | 1 | `install_output_tail_lines` | 20 |

### `[handlers.product]`

All keys are optional.

| Setting | Default | Setting | Default |
| --- | ---: | --- | ---: |
| `reminder_first_h` | 4 | `reminder_repeat_h` | 24 |
| `message_title_max_chars` | 80 | `button_label_max_chars` | 30 |

### `[reasoner]`

Optional. Omitting the section disables the Reasoner. Without it, escalations that need a model
assessment are forwarded to the Product Owner.

- `ladder` lists one or more rungs in fallback order. Each rung is `adapter:model` or
  `adapter:model:effort`.
- The adapter must be `claude` or `codex`. Model and effort must be non-empty strings. The effort is
  optional; it is passed to Claude, and Codex ignores it.
- The local CLI for every rung must be installed and runnable.
- The numeric settings below are optional positive integers.

| Setting | Default |
| --- | ---: |
| `timeout_s` | 120 |
| `cooldown_min` | 5 |
| `max_parallel` | 2 |
| `input_max_chars` | 4000 |

## Command line

```
shikiri-yaku [--product NAME] [--home PATH] [--json] [--version] COMMAND
```

Global options come before the command.

| Option | Meaning |
| --- | --- |
| `--product NAME` | The product to work on. It may be left out when `--home` holds exactly one product directory. With none or several, the command refuses and names what it found. |
| `--home PATH` | The directory that holds the product directories. Default: `~/.shikiri-yaku`. |
| `--json` | Prints `doctor`'s and `stats`' output as one JSON object on standard output instead of text, and `restart`'s as a JSON list. Other commands ignore it. Exit codes are the same. |
| `--version` | Shows the version and exits. |

| Command | Purpose |
| --- | --- |
| `run` | Serves one product until SIGTERM. |
| `doctor` | Checks one product's instance. |
| `stats` | Prints the figures of one product's journal. |
| `install-service` | Writes a launchd or systemd unit that runs `run` at login. |
| `restart` | Restarts the service of one product, or, when `--product` is left out, of every product under `--home` in name order. Unlike the other verbs, it does not refuse several products. It prints one line per product and exits 1 if any was not restarted. See [Applying changes](#applying-changes). |
| `telegram adopt <component> <thread-id>` | Repairs an uncertain Telegram topic, or reuses one made by hand. See [Telegram topic recovery](#telegram-topic-recovery). |
| `telegram forget <component>` | Deletes an uncertain topic's row, so start-up creates a topic. See [Telegram topic recovery](#telegram-topic-recovery). |

The older forms, with the product after the verb (`run shop`) or `topics …`, are usage errors.

`doctor` reports its failed checks inside the JSON object. When `stats` cannot run, it prints one `✘`
line on standard error and nothing on standard output.

### JSON output

| Command | Keys |
| --- | --- |
| `doctor` | `passed` (true when every check passed), `failed` (the number of failed checks), `total`, and `checks`. Each entry of `checks` has `name`, `passed` and `detail`. |
| `stats` | `completed`, `queued`, `lifetime_seconds`, `waiting_seconds`, `share`, `handled_without_po`, `reasons`, `verdicts`, `installs` and `install_outcomes`. The two durations are in seconds. `share` is the waiting fraction from 0 to 1, or `null` when unavailable. The last five keys each map a label to its count. |
| `restart` | A list with one entry per product, in name order. Each entry has `product`, `passed` (true when it restarted) and `result`: `restarted`, or the reason it did not, such as `not installed`. |

## Chat commands

Only Telegram user IDs listed in `channels.telegram.owners` can issue commands or act on buttons.

- **Private chat:** each configured owner must first open the bot and press **Start**.
- **Forum group:** non-owners' messages and button taps are ignored. If `allow_other_members = true`,
  those members can still read everything the bot posts.

Commands work in private chats and in forum topics. Replies go back to the chat and topic where the
command was sent.

| Command | What it does |
| --- | --- |
| `/status` | Shows each component's engine state, running/queued/escalated task counts, quota blocks, and configured Reasoner rung health. In a component topic, it shows only that component, without the Reasoner rungs. |
| `/open` | Re-shows open questions and pending approvals that Shikiru still reports as open. Each item goes to its component destination; if none are waiting, the bot says so in the command's chat. In a component topic, it re-shows only that component's items, and says so by its name when none are waiting. |
| `/help` | Lists the supported commands. Free text also returns this list. An unknown slash command reports the name and lists commands. |
| `/goal <component> <request>` | Submits a task request to that component's Shikiru project. Use this form in a private chat or the General topic. |

### `/goal`

The request is sent to Shikiru as written. It is not split across components or rewritten into a
plan. In a component topic, the topic supplies the component, so write `/goal <request>` there:

```text
/goal api Add CSV export to the reports page
```

The outcome depends on what Shikiru reports:

- **Confirmed:** the bot replies with its task ID.
- **Rejected:** the bot says it was not submitted.
- **Connection failed after submission may have reached Shikiru:** the bot says the result is
  uncertain and offers **Submit again**. It does not retry automatically.
- **Crash after submission, before the acknowledgement was recorded:** after a restart, the same
  uncertain prompt with **Submit again** appears. Recovery marks the goal uncertain and does not send
  it again automatically. Inspect the Shikiru project before tapping **Submit again**: the first
  request may have been accepted, so a retry can create a duplicate task.

### Several instances in one group

Several instances can share one forum group. Each instance needs its own bot, token file and
components; two instances never share a bot. Give every instance the same `group_id` and set
`shared_group = true` in each `[channels.telegram]` table. `shared_group` requires `group_id`.

With `shared_group = true`:

- A command (text that starts with `/`) in a topic that is neither General nor one of this
  instance's component topics gets no reply from this instance.
- `/goal <component> <request>` in General is submitted only when `<component>` is one of this
  instance's components. For another instance's component, it gets no reply.
- `/status` and `/open` in General cover this instance's components only.
- `/help`, free text, and a `/goal` without a component are answered by every instance's bot, so the
  group receives one list per instance.

Every instance's bot must be an administrator of the group with the manage-topics right, as this
instance's own bot must be. Telegram lists only administrators, so the start-up check reads them with
the other bots included. It admits the group's owners, this bot, and the other administrator bots,
and refuses any other member: a person who joins as an ordinary member or as an administrator, or a
bot that is not an administrator. Add an instance's bot as an administrator to admit it.
`allow_other_members = true` is not a way to admit another instance's bot: it skips the membership
check and lets any member read the group.

## Questions, approvals, and reminders

An open escalation is sent with its question and the choices Shikiru offers. Tap a choice to apply it.

- **Text answers.** A choice that asks for text prompts for a reply to the bot's message. Reply with
  the answer in that chat and topic.
- **Confirmation.** **Accept** and **Cancel task** need a second **Confirm** tap before the choice is
  sent to Shikiru.
- **Showing it again.** `/open` re-shows open questions.
- **Details.** If a question is shortened to fit Telegram's message limit, it includes **Details**.
  That button requests the escalation packet from Shikiru and sends it in additional messages.
  Details can contain project data. If the packet matches a secret pattern, it is refused and not
  sent.

### Reminders and standing notices

- Open questions are reminded after 4 hours, then every 24 hours, by default. Change these in
  `[handlers.product]`.
- After downtime, the scheduler sends only the latest due reminder, not a backlog of missed ones.
- Standing notices, such as an engine that remains down, have **Stop reminders**. Tapping it ends
  reminders and edits the notice to say **Reminders stopped**. It does not close the underlying
  signal.
- If the issue is later fixed, the bot edits the notice again with the final state.

### Install approval

An install approval shows the tool, a model-written reason, the complete command, its package
manager, and the possible effect.

The model cannot provide a command. The code builds the argument list from one of these recipes,
stores it before asking, and checks that the stored list still exactly matches the displayed recipe
before running it:

| Recipe, followed by the validated package name | Package manager |
| --- | --- |
| `brew install` | Homebrew |
| `uv tool install` | uv |
| `npm install -g` | npm |
| `pipx install` | pipx |
| `cargo install` | Cargo |

- **Install:** tap only when you approve that exact displayed command and its effect. The command
  runs on the Operator host, outside the component repositories, and only after the approval is
  recorded. It may update dependencies of the installed tool.
- **Decline:** runs nothing and forwards the original escalation to you.
- **Expiry:** an approval expires after 24 hours by default (`approval_expiry_h`). `/open` shows only
  approvals that remain valid and open.

## Automatic actions and notices

The Operator acts on its own only in the cases below. Anything else reaches you as a question, with
what it tried.

- **Agent and integration errors** (`agent_error`, `agent_stuck`, `agent_protocol`,
  `integration_conflict`, `integration_blocked`): the escalation goes back to Shikiru's agents at
  once. Each cause gets `retry_budget_per_day` retries a day (one by default). After that, the
  escalation is forwarded to you.
- **Quota exhausted** (`quota_exhausted`, `ladder_exhausted`): the bot tells you when the quota
  resets, then retries the escalation after that time plus `quota_wait_margin_min`. A reset that is
  unknown, or further away than `quota_wait_max_h` (24 hours by default), is forwarded at once.
- **Engine down or faulted** (`engine_down`, `engine_fault`): the bot asks Shikiru to start the
  engine, waits until it is up, and tells you. If the start is refused, fails or is not confirmed,
  the bot forwards the failed checks from `shikiru doctor` and asks you to run `shikiru start` in
  that project. An `engine_error` escalation gets the same restart if the engine is down, and is
  then retried. Restarts are limited to `restart_budget_per_hour` per component (one an hour by
  default).
- **Missing tool:** an install runs only after you tap **Install** (see
  [Install approval](#install-approval)).
- **Pull request ready for review:** when Shikiru reports an open pull request for a task, the bot
  sends its title, its commit count and an **Open PR** link, with **Stop reminders**. When the pull
  request is merged, closed unmerged or withdrawn, the message is edited to a final line that says
  how it ended. The Operator never merges, comments on or reviews a pull request.
- **Finished work:** when a task and its child tasks have all settled, the bot sends one notice per
  task family, such as "api · Export invoices integrated (3 tasks)". It has no buttons.
- **Health:** when every configured Reasoner rung fails, decisions are forwarded without a
  suggestion. When a component's Shikiru link stays unreachable for `link_unreachable_min` (10
  minutes by default), the bot says so. Either notice has **Stop reminders** and is edited to
  "Recovered" when the problem clears.

Standing messages (the pull-request message and health notices) keep their **Stop reminders**
button, which silences their reminders without closing the underlying signal.

## Service operation

Run `shikiri-yaku --product shop install-service` after installing the command and creating a valid
instance. It writes a per-product service unit under your home directory and prints the unit's path
and activation command.

**Writing the unit does not activate it.** The unit carries the `PATH` of the shell where
`install-service` ran, so `shikiru` and configured Reasoner CLIs can be found when the service
starts.

### macOS

The unit is a LaunchAgent at `~/Library/LaunchAgents/shikiri-yaku-shop.plist`. Activate it with the
printed command:

```sh
launchctl bootstrap gui/$(id -u) "$HOME/Library/LaunchAgents/shikiri-yaku-shop.plist"
launchctl print gui/$(id -u)/shikiri-yaku-shop
```

Stop and unload it with:

```sh
launchctl bootout gui/$(id -u) "$HOME/Library/LaunchAgents/shikiri-yaku-shop.plist"
```

- **Stop it at a later login:** disable the label with
  `launchctl disable gui/$(id -u)/shikiri-yaku-shop`. Use `launchctl enable` with that same target
  before bootstrapping it again.
- **Logs:** the generated plist sets no stdout or stderr log files. Use `launchctl print` to inspect
  job state. To see application output while troubleshooting, stop the service and run
  `shikiri-yaku --product shop run` in a terminal.

### Linux

The unit is a user service at `~/.config/systemd/user/shikiri-yaku-shop.service`. The printed command
enables it at login and starts it now:

```sh
systemctl --user enable --now shikiri-yaku-shop.service
systemctl --user status shikiri-yaku-shop.service
journalctl --user -u shikiri-yaku-shop.service -f
```

- Stop it for now: `systemctl --user stop shikiri-yaku-shop.service`.
- Stop it and disable it at login: `systemctl --user disable --now shikiri-yaku-shop.service`.

### Clean stops, restarts and rewritten units

- `run` handles SIGINT and SIGTERM as clean stops and exits successfully.
- The generated macOS and Linux units restart after an unsuccessful exit. A stop requested through the
  service manager is not restarted.
- Only one process can hold an instance's lock.
- After changing `config.toml`, restart that product's service so it reads the new settings.
- After rewriting a unit with `install-service`, reload it. On Linux, run
  `systemctl --user daemon-reload` and restart the service. On macOS, boot out and bootstrap the
  LaunchAgent again.

## Applying changes

A running service reads its code and `config.toml` when it starts, so a change takes effect after a
restart. `shikiri-yaku restart` restarts the unit of each product under `--home`, or of the one named
by `--product`. On macOS it runs `launchctl kickstart -k`; on Linux it runs `systemctl --user
restart`. A product whose unit was never installed is reported as `not installed`, and nothing runs
for it. A restart the service manager refuses, such as a unit that was written but not loaded, is
reported with the service manager's message.

A service runs the installed copy of `shikiri-yaku` that `install-service` found on the login `PATH`,
never a checkout. That copy is a release. Do not install with `--editable`: the running service would
follow unreviewed changes in the checkout, including after a crash restart.

### After a code change

1. The owner releases it: runs the Release workflow, as
   [CONTRIBUTING.md](CONTRIBUTING.md#releasing) describes.
2. On each host, upgrade the installed copy and restart its services:

   ```sh
   uv tool upgrade shikiri-yaku
   shikiri-yaku restart
   ```

Before a release migrates a journal, `shikiri-yaku` copies it to
`<home>/<product>/backups/journal-schema<N>.db`, where `<N>` is the schema version before the
migration. The newest three copies are kept per product.

### After a `config.toml` change

Run `shikiri-yaku restart` alone.

### Rolling back

To go back to an earlier release, install its version and restart:

```sh
uv tool install shikiri-yaku==<version>
shikiri-yaku restart
```

A rollback restores code, not data. A release that migrated a journal leaves the old version unable
to open it: it stops with `JournalTooNewError`. To restore the data, repeat these steps for that
product on each host where its service runs:

1. Stop its service: `systemctl --user stop shikiri-yaku-<product>.service` on Linux, or
   `launchctl bootout gui/$(id -u)/shikiri-yaku-<product>` on macOS.
2. Copy the newest `journal-schema<N>.db` in `<home>/<product>/backups/` over
   `<home>/<product>/journal.db`.
3. Delete `journal.db-wal` and `journal.db-shm` if they exist.
4. Start the service again, as in [Service operation](#service-operation).

Changes made after that backup are lost.

## Diagnostics and recovery

### Doctor and stats

- `shikiri-yaku --product shop doctor` checks the configuration and component paths, instance lock,
  journal, Telegram readiness, each Shikiru MCP link, and each configured Reasoner CLI. Run it after
  setup or a configuration change. It may contact Telegram and Shikiru. A failed check gives exit
  code 1.
- Put `--json` before the command (`shikiri-yaku --json --product shop doctor`) for machine-readable
  output; see [JSON output](#json-output).
- `stats` reads the local journal and summarizes completed and queued work, waits, reasons, verdicts
  and install outcomes. It does not contact Shikiru.

### Foreground diagnostics

For a foreground diagnostic session, stop the service first, then run
`shikiri-yaku --product shop run`. The service and the foreground command use the same instance
lock, so a second `run` is refused.

### Restart recovery

The Operator keeps its journal across restarts and reconciles interrupted work from what Shikiru and
the host report.

- **Resolve.** An issued resolve may have reached Shikiru. After restart, the Operator checks whether
  the escalation is still open, then either confirms the result or shows the question again.
- **Install approved, not issued.** It resumes automatically after restart if its escalation is still
  open; the already-approved stored command is run.
- **Install issued.** It is never run a second time. Recovery checks whether the tool exists: if it
  is found, Shikiru is answered; if it is missing, the bot asks for a fresh approval.
- **Duplicate messages.** Telegram may show one duplicate message if the process stopped after
  Telegram accepted a send but before the journal recorded its acknowledgement.

### Engine-down signals and clean stops

- An unexpected engine-down signal can trigger one automatic restart per component per hour by
  default.
- If a start fails or the budget is used up, the bot forwards the reason and the failed checks from
  `shikiru doctor`, and tells you to run `shikiru start` in that component's project.
- A clean Shikiru engine stop observed while the Operator is running is treated as intentional after
  `stop_grace_min` (2 minutes by default).
- If the Operator restarts before recording that grace period, a stop that is not yet recorded as
  respected may be treated as an engine-down signal on the next run.
- A stop already recorded as respected stays so across Operator restarts. During a first sync, a
  clean stop found in Shikiru's history is also treated as respected.
- Use `/status` to see the current engine state.

## Telegram topic recovery

With a forum configured, start-up creates a topic for each configured component and closes topics
for components removed from the configuration. The built-in General topic is used for Operator
messages and is neither created nor adopted. Topic changes are recorded in the instance journal; the
Bot API does not let the Operator list existing forum topics.

If start-up reports an uncertain topic creation, inspect the forum yourself, then choose one case:

1. **The topic exists:** adopt its positive numeric thread ID, the number at the end of the topic
   link.

   ```sh
   shikiri-yaku --product shop telegram adopt api 123
   ```

   Adoption records the thread; it does not verify that the ID belongs to a real topic.

2. **No topic was created:** forget the uncertain journal row, then start the service again to
   create a topic.

   ```sh
   shikiri-yaku --product shop telegram forget api
   ```

`forget` applies only to an uncertain creation row, removes that row, and does not delete a possibly
created forum topic. Telegram permits duplicate topic names, so forgetting an uncertain row can leave
an orphan topic and create another one. A fresh `--home` or a deleted journal also loses the topic
mapping, so do not point a new instance at the same group without adopting its existing topics.

## Local state and practical limits

### Local state

Each product's state is under `<home>/<product>` (`~/.shikiri-yaku/<product>` by default):

- `config.toml` and the configured token file;
- `journal.db`, which records signals, outbound message bodies, goal text, pending operations, and
  restart and recovery state;
- `instance.lock` while that product is running; the shared `<home>/host.lock` is held while any
  instance runs a host command;
- `install-scratch/` for approved install commands, outside all component repositories;
- `output/install-<op-id>.log` for install output. The output directory is mode `0700` and each file
  is mode `0600`, because package-manager output can contain sensitive paths or tokens;
- `reasoner-scratch/` for the temporary directories of Reasoner runs;
- `backups/journal-schema<N>.db` for the journal copies made before migrations (see
  [Applying changes](#applying-changes)).

Keep the instance directory private and back it up only as carefully as other operational data.

### Data that leaves the machine

- The bot sends Shikiru's question or headline, offered choices and consequences, relevant task and
  component names, and handler notes to Telegram. **Details** sends more of the Shikiru packet when
  requested.
- Telegram group members can read posted messages, including non-owners when
  `allow_other_members = true`. Only configured owners can act.
- When the Reasoner is enabled, it receives the escalation details and host facts it needs for its
  assessment; the CLI or provider handles those inputs.

### Limits

- Components must be local Shikiru project directories. Telegram is the only chat channel.
- The Reasoner is optional and uses configured local Claude or Codex CLIs. Without one, escalations
  that need a model assessment go to the Product Owner.
- If every configured Reasoner rung fails, the escalation still goes to the Product Owner, with a
  note and a Reasoner health signal.
- A Claude rung gets no file-reading tool, only the web and package lookups its adapter lists. A
  Codex rung's read-only sandbox stops writes but not reads, so its model can read any file your user
  account can read.
- Automatic quota waits are limited to 24 hours by default (`quota_wait_max_h`), retries to one per
  day (`retry_budget_per_day`), and engine restarts to one per component per hour
  (`restart_budget_per_hour`). These operational values can be changed in configuration.

## Contributor validation

A change needs the offline quality gate to pass:

```sh
uv run poe check
```

Run `uv run poe fix` first to format the code. The gate uses fakes and local fixtures, not a live
Telegram bot or agent CLI. [CONTRIBUTING.md](CONTRIBUTING.md) covers how to propose a change, the
architecture and safety rules, and the release process.
