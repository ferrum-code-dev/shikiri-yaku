# Shikiri-yaku (仕切り役)

[![Release](https://img.shields.io/github/v/release/ferrum-code-dev/shikiri-yaku?include_prereleases)](https://github.com/ferrum-code-dev/shikiri-yaku/releases)
[![License](https://img.shields.io/github/license/ferrum-code-dev/shikiri-yaku)](https://github.com/ferrum-code-dev/shikiri-yaku/blob/main/LICENSE)
[![CI](https://github.com/ferrum-code-dev/shikiri-yaku/actions/workflows/ci.yml/badge.svg)](https://github.com/ferrum-code-dev/shikiri-yaku/actions/workflows/ci.yml)

An autonomous Operator for product development with Shikiru.

Shikiri-yaku is a small background service that sits between a Product Owner and
[Shikiru](https://github.com/ferrum-code-dev/shikiru), the coordinator that runs coding agents.
Shikiru stops and waits for a person when it raises an escalation. Shikiri-yaku takes that seat:
it watches the escalations of every Shikiru project of a product, sorts each into a category and
hands it to a handler. A handler either resolves it (retry, wait for a quota reset, restart an
engine, install a missing tool after the owner approves the exact command) or forwards it to the
Product Owner in a chat (Telegram first), where one tap applies the answer in Shikiru.

- **Small fixed core, growth by extension.** Categories, handlers, actions, reasoning jobs and
  channels are registered extensions. The current chat commands are documented below.
- **Every route ends at the owner.** Anything unknown, unsure or failed is forwarded, so nothing
  is dropped and nothing works worse than plain forwarding.
- **Agents propose, code acts.** The Operator's reasoning agent may look things up but cannot change
  anything, and only names what is missing; plain code builds the command, and runs it only after
  the owner approved it.
- **Shikiru stays the source of truth,** read and commanded through MCP. Nothing of the Operator
  is stored in the managed repositories.
- **One instance per product,** with its own chat bot. Each component is a Shikiru project.
- **Small by design:** one Python process, SQLite, one TOML file and the OS service manager.

## Install and first run

This repository contains the Python implementation and command-line interface. Install a release
using the steps below.

### Prerequisites

- Python 3.14 and [uv](https://docs.astral.sh/uv/).
- Shikiru installed and available as `shikiru` on `PATH`, version `0.1.0` up to but not including
  `0.2.0`. Install a release of [Shikiru](https://github.com/ferrum-code-dev/shikiru) either from
  its release wheel on its GitHub Releases page, or from its tag with
  `uv tool install git+https://github.com/ferrum-code-dev/shikiru@v0.1.3`. `doctor` fails outside
  that range. Each configured component must be an existing local Shikiru project directory, one
  that `shikiru init` has set up; Shikiri-yaku connects with `shikiru --project <path> mcp`.
- A Telegram bot token. Create a bot with [BotFather](https://t.me/BotFather) and keep its token
  private. The bot uses long polling, so do not configure a webhook for it.
- Optionally, the local `claude` and/or `codex` CLI for a configured Reasoner ladder.

### Install a release

Install the console command `shikiri-yaku` from a release of this repository, either from its
wheel:

```sh
uv tool install https://github.com/ferrum-code-dev/shikiri-yaku/releases/download/v0.1.4/shikiri_yaku-0.1.4-py3-none-any.whl
```

or from its tag:

```sh
uv tool install git+https://github.com/ferrum-code-dev/shikiri-yaku@v0.1.4
```

The version in these addresses is `0.1.4`. When a newer release exists, change it to that version.

**From a checkout:** for development, see [CONTRIBUTING.md](CONTRIBUTING.md). A service must run a
release, not a checkout (see [Applying changes](#applying-changes)).

### Create an instance

By default, one product's instance lives at `~/.shikiri-yaku/<product>/`. For a product named
`shop`, create the directory and its config file:

```sh
mkdir -p "$HOME/.shikiri-yaku/shop/secrets"
```

Save the TOML below as `$HOME/.shikiri-yaku/shop/config.toml`. Replace the component path with
the absolute path or `~` path of an existing Shikiru project, and replace the example owner ID
with your numeric Telegram account ID.

```toml
product = "shop"

[[components]]
name = "api"
path = "~/src/shop-api"

[channels.telegram]
token_file = "secrets/telegram.token"
owners = [123456789]
```

Create the token file at `$HOME/.shikiri-yaku/shop/secrets/telegram.token` and put only the bot
token in it. Keep it out of source control. The token file must be a regular file with mode
`0600`; set that exact mode with:

```sh
chmod 600 "$HOME/.shikiri-yaku/shop/secrets/telegram.token"
```

The `owners` list contains numeric Telegram user IDs that may act on messages. For private-chat
delivery, each owner must open the bot and press **Start** before it can message them. You can
include several owner IDs.

To use a forum group instead, add `group_id` under `[channels.telegram]` with the numeric ID of a
forum supergroup. The bot must be an administrator with permission to manage topics, and every
configured owner must belong to the group. By default, the group must contain only the bot and
configured owners because other members can read its messages. Set `allow_other_members = true`
to allow additional readers; only configured owners can act on messages. To share the group with
other instances, see [Several instances in one group](#several-instances-in-one-group).

To enable the optional Reasoner, add a `[reasoner]` table. Its ordered ladder accepts
`adapter:model` or `adapter:model:effort` entries; the supported adapters are `claude` and
`codex`. Replace the model placeholders with model names accepted by the installed CLI. The
optional effort is passed to Claude; Codex ignores it.

Once the config and protected token file are in place, check the instance and start it in the
foreground:

```sh
shikiri-yaku --product shop doctor
shikiri-yaku --product shop run
```

`doctor` checks the local config and token-file mode, Telegram readiness, each Shikiru project
link, and any configured Reasoner CLIs. It contacts Telegram and Shikiru; `run` stays attached
until stopped.

### Configuration reference

Only the sections and keys listed here are accepted. Misspelled or unknown keys are errors.
Product and component names must start with an ASCII letter or digit and may then contain letters,
digits, `_` or `-`. Component names must be unique. At least one component is required. Components
run on this machine: `path` must be an absolute path or begin with `~`; remote `host` components
are not supported. A component removed from the file has its open signals closed with a note at the
next start.

| Section | Keys and defaults |
| --- | --- |
| Root | `product` (required); `components` (required, one or more `[[components]]` entries) |
| `[[components]]` | `name` (required), `path` (required); `host` is unsupported |
| `[channels.telegram]` | `owners` (required, one or more integer IDs); `token_file = "secrets/telegram.token"`; `group_id` (optional); `allow_other_members = false`; `shared_group = false` (requires `group_id`) |
| `[operator]` | All keys below are optional; every value is a positive integer |
| `[handlers.infrastructure]` | All keys below are optional; every value is a positive integer |
| `[handlers.product]` | All keys below are optional; every value is a positive integer |
| `[reasoner]` | Optional; `ladder` is required when this section is present |

`token_file` defaults to a path relative to the instance directory. It may be set to another
relative path there or an absolute path. The Telegram table is required even when using defaults,
and `owners` has no default. If `group_id` is set, it must identify a Telegram forum supergroup.

#### `[operator]` defaults

`telegram_call_timeout_s` must be greater than `telegram_poll_wait_s`.

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

#### `[handlers.infrastructure]` defaults

| Setting | Default | Setting | Default |
| --- | ---: | --- | ---: |
| `command_timeout_min` | 10 | `quota_wait_max_h` | 24 |
| `quota_wait_margin_min` | 1 | `retry_budget_per_day` | 1 |
| `restart_budget_per_hour` | 1 | `restart_confirm_s` | 60 |
| `restart_poll_s` | 5 | `approval_expiry_h` | 24 |
| `host_lock_poll_s` | 1 | `install_output_tail_lines` | 20 |

#### `[handlers.product]` defaults

| Setting | Default | Setting | Default |
| --- | ---: | --- | ---: |
| `reminder_first_h` | 4 | `reminder_repeat_h` | 24 |
| `message_title_max_chars` | 80 | `button_label_max_chars` | 30 |

#### Optional `[reasoner]`

Omitting this section disables the Reasoner. When present, `ladder` must contain one or more
rungs in fallback order. Each adapter must be `claude` or `codex`; model and effort must be
non-empty strings. The local CLI for every rung must be installed and runnable. The optional
numeric settings below are positive integers.

| Setting | Default |
| --- | ---: |
| `timeout_s` | 120 |
| `cooldown_min` | 5 |
| `max_parallel` | 2 |
| `input_max_chars` | 4000 |

For example, add this section to `config.toml` and replace its placeholders with CLI-supported
values:

```toml
[reasoner]
ladder = ["claude:<model>:<effort>", "codex:<model>"]
```

The Reasoner is optional; without it, escalations that need a model assessment are forwarded to
the Product Owner.

## Command line

Global options come before the command. `--product` names the product. When `--home` (default
`~/.shikiri-yaku`) holds exactly one product directory, `--product` may be left out; with none or
several, the command refuses and names what it found.

```
shikiri-yaku [--product NAME] [--home PATH] [--json] [--version] COMMAND
```

- `run` serves one product until SIGTERM.
- `doctor` checks one product's instance.
- `stats` prints the figures of one product's journal.
- `install-service` writes a launchd or systemd unit for running `run` at login.
- `restart` restarts the service of one product, or of every product under `--home` when
  `--product` is left out, in name order. Unlike the other verbs, it does not refuse several
  products. It prints one line per product and exits 1 if any was not restarted. See
  [Applying changes](#applying-changes).
- `telegram adopt <component> <thread-id>` and `telegram forget <component>` repair an uncertain
  Telegram topic, or reuse one made by hand, as described below.
- `--json` makes `doctor` and `stats` print one JSON object on standard output instead of text, and
  `restart` print a JSON list. The other commands ignore it. Exit codes are the same. `doctor`
  reports its failed checks inside the object. When `stats` cannot run, it prints one `✘` line on
  standard error and nothing on standard output.

The older forms, with the product after the verb (`run shop`) or `topics …`, are usage errors.

`shikiri-yaku --json doctor` has the keys `passed` (true when every check passed), `failed` (the
number of failed checks), `total` and `checks`. Each entry of `checks` has `name`, `passed` and
`detail`.

`shikiri-yaku --json stats` has the keys `completed`, `queued`, `lifetime_seconds`,
`waiting_seconds`, `share`, `handled_without_po`, `reasons`, `verdicts`, `installs` and
`install_outcomes`. The two durations are in seconds, and `share` is the waiting fraction from 0 to
1, or `null` when it is unavailable. The last five keys each map a label to its count.

`shikiri-yaku --json restart` prints a list with one entry per product, in name order. Each entry
has `product`, `passed` (true when it restarted) and `result`: `restarted`, or the reason it did
not, such as `not installed`.

## Chat commands

Only Telegram user IDs listed in `channels.telegram.owners` can issue commands or act on buttons.
In a private chat, each configured owner must first open the bot and press **Start**. In a forum
group, non-owners' messages and button taps are ignored; if `allow_other_members = true`, those
members can still read everything the bot posts.

Commands work in private chats and in forum topics. Replies go back to the chat and topic where
the command was sent.

| Command | What it does |
| --- | --- |
| `/status` | Shows each component's engine state, running/queued/escalated task counts, quota blocks, and configured Reasoner rung health. In a component topic, it shows only that component, without the Reasoner rungs. |
| `/open` | Re-shows open questions and pending approvals that Shikiru still reports as open. Each item goes to its component destination; if none are waiting, the bot says so in the command's chat. In a component topic, it re-shows only that component's items, and says so by its name when none are waiting. |
| `/help` | Lists the supported commands. Free text also returns this list. An unknown slash command reports the name and lists commands. |
| `/goal <component> <request>` | Submits a task request to that component's Shikiru project. Use this form in a private chat or General topic. |

In a component topic, the topic supplies the component, so write `/goal <request>` there. The
request is sent to Shikiru as written; it is not split across components or rewritten into a plan.
For example:

```text
/goal api Add CSV export to the reports page
```

If Shikiru confirms the submission, the bot replies with its task ID. If Shikiru rejects it, the
bot says it was not submitted. If the connection fails after submission may have reached Shikiru,
the bot says the result is uncertain and offers **Submit again**. It does not retry automatically.
The same prompt appears after a restart if the process crashed after issuing the submission but
before recording Shikiru's acknowledgement. Recovery marks that goal uncertain and does not send
it again automatically. Inspect the Shikiru project before tapping **Submit again**: the first
request may have been accepted, so a retry can create a duplicate task.

### Several instances in one group

Several instances can share one forum group. Each instance needs its own bot, token file, and
components; two instances never share a bot. Give every instance the same `group_id` and set
`shared_group = true` in each `[channels.telegram]` table. `shared_group` requires `group_id`.

With `shared_group = true`:

- A command (text that starts with `/`) in a topic that is neither General nor one of this
  instance's component topics gets no reply from this instance.
- `/goal <component> <request>` in General is submitted only when `<component>` is one of this
  instance's components. For another instance's component it gets no reply.
- `/status` and `/open` in General cover this instance's components only.
- `/help`, free text, and a `/goal` without a component are answered by every instance's bot, so
  the group receives one list per instance.

Every instance's bot must be an administrator of the group with the manage-topics right, as this
instance's own bot must be. Telegram lists only administrators, so the start-up check reads them
with the other bots included. It admits the group's owners, this bot, and the other administrator
bots, and refuses any other member: a person who joins as an ordinary member or as an
administrator, or a bot that is not an administrator. Add an instance's bot as an administrator to
admit it. `allow_other_members = true` is not a way to admit another instance's bot: it skips the
membership check and lets any member read the group.

## Questions, approvals, and reminders

An open escalation is sent with its question and available choices. Tap a choice to apply it. A
choice that asks for text prompts for a reply to the bot's message; reply with the answer in that
chat and topic. **Accept** and **Cancel task** require a second **Confirm** tap before the choice is
sent to Shikiru. `/open` can show the current question again.

If a question is shortened to fit Telegram's message limit, it includes **Details**. That button
requests the escalation packet from Shikiru and sends it in additional messages. Details can
contain project data; if the packet matches a secret pattern, it is refused and the packet is not
sent.

Standing notices (for example, an engine that remains down) have **Stop reminders**. Tapping it
ends reminders and edits the notice to say **Reminders stopped**; it does not close the underlying
signal. If the issue is later fixed, the bot edits the notice again with the final state. Open
questions are reminded after 4 hours and then every 24 hours by default. Those intervals can be
changed in `[handlers.product]`. After downtime, the scheduler sends only the latest due reminder,
not a backlog of missed reminders.

### Install approval

An install approval shows the tool, a model-written reason, the complete command, its package
manager, and the possible effect. Supported command recipes are `brew install`, `uv tool install`,
`npm install -g`, `pipx install`, and `cargo install`, each followed by the validated package name.
The model cannot provide a command. The code builds the argument list from one of those recipes,
stores it before asking, and checks that the stored list still exactly matches the displayed
recipe before running it.

Tap **Install** only when you approve that exact displayed command and its effect. The command
runs on the Operator host, outside the component repositories, and only after the approval is
recorded. It may update dependencies of the installed tool. Tap **Decline** to run nothing and
forward the original escalation to you. An approval expires after 24 hours by default; `/open`
shows only approvals that remain valid and open.

## Automatic actions and notices

The Operator acts on its own only in the cases below. Anything else reaches you as a question,
with what it tried.

- **Agent and integration errors** (`agent_error`, `agent_stuck`, `agent_protocol`,
  `integration_conflict`, `integration_blocked`): the escalation is given back to Shikiru's agents
  at once. Each cause gets `retry_budget_per_day` retries a day (one by default); after that the
  escalation is forwarded to you.
- **Quota exhausted** (`quota_exhausted`, `ladder_exhausted`): the bot tells you when the quota
  resets and retries the escalation after that time plus `quota_wait_margin_min`. A reset that is
  unknown, or further away than `quota_wait_max_h`, is forwarded at once.
- **Engine down or faulted** (`engine_down`, `engine_fault`): the bot asks Shikiru to start the
  engine, waits until it is up and tells you. If the start is refused, fails or is not confirmed, it
  forwards the failed checks from `shikiru doctor` and asks you to run `shikiru start` in that
  project. An `engine_error` escalation gets the same restart if the engine is down, and is then
  retried. Restarts are limited to `restart_budget_per_hour` per component (one an hour by default).
- **Missing tool**: an install runs only after you tap **Install** (see
  [Install approval](#install-approval)).
- **Pull request ready for review**: when Shikiru reports an open pull request for a task, the bot
  sends its title, its commit count and an **Open PR** link, with **Stop reminders**. When the pull
  request is merged, closed unmerged or withdrawn, the message is edited to a final line that says
  how it ended. The Operator never merges, comments on or reviews a pull request.
- **Finished work**: when a task and its child tasks have all settled, the bot sends one notice per
  task family, such as "api · Export invoices integrated (3 tasks)". It has no buttons.
- **Health**: when every configured Reasoner rung fails, decisions are forwarded without a
  suggestion; when a component's Shikiru link stays unreachable for `link_unreachable_min` (10
  minutes by default), the bot says so. Either notice has **Stop reminders** and is edited to
  "Recovered" when the problem clears.

Standing messages (the pull-request message and health notices) keep their **Stop reminders**
button, which silences their reminders without closing the underlying signal.

## Service operation

Run `shikiri-yaku --product shop install-service` after installing the command and creating a
valid instance. This writes a per-product service unit under your home directory and prints its
path and activation command. **Writing the unit does not activate it.** The generated unit carries
the `PATH` from the shell where `install-service` ran, so `shikiru` and configured Reasoner CLIs
can be found when the service starts.

On macOS, the unit is a LaunchAgent at
`~/Library/LaunchAgents/shikiri-yaku-shop.plist`. Activate it with the printed command:

```sh
launchctl bootstrap gui/$(id -u) "$HOME/Library/LaunchAgents/shikiri-yaku-shop.plist"
launchctl print gui/$(id -u)/shikiri-yaku-shop
```

Stop and unload it with:

```sh
launchctl bootout gui/$(id -u) "$HOME/Library/LaunchAgents/shikiri-yaku-shop.plist"
```

To keep it from loading at a later login, disable the label with
`launchctl disable gui/$(id -u)/shikiri-yaku-shop`; use `launchctl enable` with that same target
before bootstrapping it again. The generated plist does not set stdout/stderr log files. Use
`launchctl print` to inspect job state; to see application output while troubleshooting, stop the
service and run `shikiri-yaku --product shop run` in a terminal.

On Linux, the unit is a user service at
`~/.config/systemd/user/shikiri-yaku-shop.service`. The printed command enables it at login and
starts it now:

```sh
systemctl --user enable --now shikiri-yaku-shop.service
systemctl --user status shikiri-yaku-shop.service
journalctl --user -u shikiri-yaku-shop.service -f
```

Stop it for now with `systemctl --user stop shikiri-yaku-shop.service`. To stop and disable it at
login, use `systemctl --user disable --now shikiri-yaku-shop.service`.

`run` handles SIGINT and SIGTERM as clean stops and exits successfully. The generated macOS and
Linux units restart after an unsuccessful exit; a stop requested through the service manager is
not restarted. Only one process can hold an instance's lock. After changing `config.toml`, restart
that product's service so it reads the new settings. If you rerun `install-service` to rewrite a
Linux unit, run
`systemctl --user daemon-reload` and restart the service; on macOS, boot out and bootstrap the
LaunchAgent again.

### Applying changes

A running service reads its code and `config.toml` when it starts, so a change takes effect after a
restart. `shikiri-yaku restart` restarts the unit of each product under `--home`, or of the one
named by `--product`. On macOS it runs `launchctl kickstart -k`; on Linux it runs
`systemctl --user restart`. A product whose unit was never installed is reported as
`not installed`, and nothing runs for it. A restart the service manager refuses, such as a unit that
was written but not loaded, is reported with the service manager's message.

A service runs the installed copy of `shikiri-yaku` that `install-service` found on the login
`PATH`, never a checkout. That copy is a wheel built from a tagged commit. Apply a change in one of
these ways:

- After a code change, release it. Commit the change, bump `version` in `pyproject.toml` (SemVer)
  and commit that too. Then tag the release commit `v<version>` and deploy from that checkout:

  ```sh
  git tag v0.2.0
  uv run poe deploy
  ```

  The tag must equal the version at HEAD. `poe deploy` refuses a work tree that is not clean and a
  HEAD that is not tagged `v<version>`. Then it runs `uv run poe check`, builds the wheel into
  `$XDG_DATA_HOME/shikiri-yaku/releases` (default `~/.local/share/shikiri-yaku/releases`, the last
  five kept), copies each product's journal to `<home>/<product>/backups/journal-schema<N>.db` with
  SQLite's online backup (the last three kept per product), installs the wheel, runs
  `shikiri-yaku --json restart`, and checks that `shikiri-yaku --version` shows the version and
  `doctor` passes for every product. It stops at the first failing step. A failure before the
  install changes nothing installed and restarts nothing. The verification waits up to a minute for
  each product: a failing `doctor` runs again, up to six runs in all with ten seconds between them,
  before it counts as failed. Success records the wheel as `current` in the release store.
- After a `config.toml` change, run `shikiri-yaku restart` alone.

A failure after the install is rolled back: the script reinstalls the wheel that `current` names,
restarts the products, prints what failed and exits 1. `uv run poe rollback` does that rollback on
its own, without deploying anything. The first deploy has no `current`; the script says so and the
failed release stays installed, so fix the problem and deploy again. A product that `restart` does
not report as restarted, such as one with `not installed`, fails the verification and so rolls the
deploy back; install its service with `install-service` first.

A rollback restores code, not data. A release that migrated a journal leaves the old version unable
to open it: it stops with `JournalTooNewError`. Then, for that product, stop its service
(`systemctl --user stop shikiri-yaku-<product>.service` on Linux,
`launchctl bootout gui/$(id -u)/shikiri-yaku-<product>` on macOS), copy the newest
`journal-schema<N>.db` in `<home>/<product>/backups/` over `<home>/<product>/journal.db`, delete
`journal.db-wal` and `journal.db-shm` if they exist, and start the service again. Changes made after
that backup are lost. When a rollback fails, the script prints the backup paths to restore.

Run the steps on each host where a service runs. Do not install with `--editable`: the running
service would follow unreviewed changes in the checkout, including after a crash restart.

## Diagnostics and recovery

Run `shikiri-yaku --product shop doctor` after setup or a configuration change. It checks the
configuration and component paths, instance lock, journal, Telegram readiness, each Shikiru MCP
link, and each configured Reasoner CLI. It may contact Telegram and Shikiru. A failed check gives
exit code 1; use `--json` before `doctor` for machine-readable details. `stats` reads the local
journal and summarizes completed/queued work, waits, reasons, verdicts, and install outcomes; it
does not contact Shikiru.

For a foreground diagnostic session, stop the service first, then run:

```sh
shikiri-yaku --product shop run
```

The service and foreground command use the same instance lock, so a second `run` is refused. The
Operator keeps its journal across restarts and reconciles interrupted work from what Shikiru and
the host report. An issued resolve may have reached Shikiru; after restart, it checks whether the
escalation is still open and either confirms the result or shows the question again.

An install that was approved but not yet issued resumes automatically after restart if its
escalation is still open; the already-approved stored command is run. An issued install is never
run a second time: recovery checks whether the tool exists. If found, Shikiru is answered; if
missing, the bot asks for a fresh approval. Telegram may show one duplicate message if the process
stopped after Telegram accepted a send but before the journal recorded its acknowledgement.

Unexpected engine-down signals can trigger one automatic restart per component per hour by
default. If a start fails or the budget is exhausted, the bot forwards the reason and the failed checks
from `shikiru doctor`, and tells you to start it with `shikiru start` in that component's project. A clean Shikiru engine stop observed
while the Operator is running is treated as intentional after `stop_grace_min` (2 minutes by
default). If the Operator restarts before recording that grace period, an unrespected stop may be
treated as an engine-down signal on the next run. A stop already marked respected remains so
across Operator restarts; during a first sync, a clean stop found in Shikiru's history is also
treated as respected. Use `/status` to see the current engine state.

### Telegram topic recovery

With a forum configured, start-up creates a topic for each configured component. It closes topics
for components removed from the configuration. The built-in General topic is used for Operator
messages and is neither created nor adopted. Topic changes are recorded in the instance journal;
the Bot API does not let the Operator list existing forum topics.

If start-up reports an uncertain topic creation, inspect the forum yourself. If the topic exists,
adopt its positive numeric thread ID (the number at the end of the topic link):

```sh
shikiri-yaku --product shop telegram adopt api 123
```

Adoption records the thread; it does not verify that the ID belongs to a real topic. If no topic
was created, forget the uncertain journal row:

```sh
shikiri-yaku --product shop telegram forget api
```

`forget` only applies to an uncertain creation row, removes that row, and does not delete a
possibly created forum topic. Start the service again to create a topic. Telegram permits duplicate
topic names, so forgetting an uncertain row can leave an orphan topic and create another one. A
fresh `--home` or deleted journal also loses the topic mapping; do not point a new instance at the
same group without adopting its existing topics.

## Local state and practical limits

Each product's state is under `<home>/<product>` (`~/.shikiri-yaku/<product>` by default):

- `config.toml` and the configured token file;
- `journal.db`, which records signals, outbound message bodies, goal text, pending operations, and
  restart/recovery state;
- `instance.lock` while that product is running; the shared `<home>/host.lock` is held while any
  instance runs a host command;
- `install-scratch/` for approved install commands, outside all component repositories;
- `output/install-<op-id>.log` for install output. The output directory is mode `0700` and each
  output file is mode `0600`; package-manager output can contain sensitive paths or tokens.

Keep the instance directory private and back it up only as carefully as other operational data.
The bot sends Shikiru's question or headline, offered choices and consequences, relevant task and
component names, and handler notes to Telegram. **Details** sends more of the Shikiru packet when
requested. Telegram group members can read posted messages, including non-owners when
`allow_other_members = true`; only configured owners can act.

Components must be local Shikiru project directories. Telegram is the only chat channel. The
Reasoner is optional and uses configured local Claude or Codex CLIs. When enabled, it receives the
escalation details and host facts needed for its assessment; the CLI/provider handles those inputs.
A Claude rung gets no file-reading tool, only the web and package lookups its adapter lists. A Codex
rung's read-only sandbox stops writes but not reads, so its model can read any file your user
account can read. Without a Reasoner, escalations that need a model assessment go to the Product Owner. If all
configured Reasoner rungs fail, the escalation still goes to the Product Owner with a note and a
Reasoner health signal. Automatic quota waits are limited to 24 hours by default, retries to one
per day, and engine restarts to one per component per hour. These operational values can be
changed in configuration.

## Contributor validation

To check a change, propose one, or run the quality gate, see [CONTRIBUTING.md](CONTRIBUTING.md).
